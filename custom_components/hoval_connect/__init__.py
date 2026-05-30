"""The Hoval Connect integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store

from .api import HovalConnectApi
from .const import (
    CIRCUIT_TYPE_NAMES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEALTH_STORAGE_KEY,
    HEALTH_STORAGE_VERSION,
)
from .coordinator import HovalCircuitData, HovalDataCoordinator, HovalPlantData

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.WATER_HEATER,
]

# Unique-ID suffixes that belong to the plant device (not to any specific circuit).
# Used by _async_cleanup_orphaned_entities to distinguish plant-level entities from
# circuit-level ones so plant entities are never accidentally removed.
_PLANT_LEVEL_ENTITY_SUFFIXES: frozenset[str] = frozenset({
    # binary_sensor
    "online",
    "error",
    # plant-level sensors
    "latest_event_type",
    "latest_event_message",
    "latest_event_time",
    "active_events",
    "weather_condition",
    "weather_temperature",
    # API connection health sensors
    "api_last_success",
    "api_last_error_time",
    "api_last_error",
    "api_last_error_type",
    "api_consecutive_failures",
    "api_total_failures",
    "api_auth_failures",
    "api_total_polls",
    "api_poll_latency",
    "api_ema_latency",
    "api_avg_latency",
    "api_p95_latency",
    "api_failure_rate_1h",
    "api_auth_failure_rate_1h",
    "api_availability_1h",
})

type HovalConnectConfigEntry = ConfigEntry[HovalRuntimeData]


@dataclass
class HovalRuntimeData:
    """Runtime data for the Hoval Connect integration."""

    coordinator: HovalDataCoordinator
    api: HovalConnectApi


def plant_device_info(plant_data: HovalPlantData) -> DeviceInfo:
    """Build DeviceInfo for a plant device."""
    return DeviceInfo(
        identifiers={(DOMAIN, plant_data.plant_id)},
        name=f"Hoval {plant_data.name}",
        manufacturer="Hoval",
        model="Plant",
    )


def circuit_device_info(
    plant_id: str,
    circuit_data: HovalCircuitData,
) -> DeviceInfo:
    """Build DeviceInfo for a circuit device."""
    model = CIRCUIT_TYPE_NAMES.get(circuit_data.circuit_type, circuit_data.circuit_type)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{plant_id}_{circuit_data.path}")},
        name=f"Hoval {circuit_data.name}",
        manufacturer="Hoval",
        model=model,
        via_device=(DOMAIN, plant_id),
    )


def _get_scan_interval(entry: HovalConnectConfigEntry) -> timedelta:
    """Get the scan interval from options or use default."""
    seconds = entry.options.get(CONF_SCAN_INTERVAL, int(DEFAULT_SCAN_INTERVAL.total_seconds()))
    return timedelta(seconds=seconds)


async def async_setup_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Set up Hoval Connect from a config entry."""
    session = async_get_clientsession(hass)
    api = HovalConnectApi(session, entry.data["email"], entry.data["password"])

    health_store = Store(hass, HEALTH_STORAGE_VERSION, HEALTH_STORAGE_KEY)

    coordinator = HovalDataCoordinator(hass, api, health_store)
    coordinator.update_interval = _get_scan_interval(entry)

    # Restore persisted health counters (total_polls, total_failures, EMA, etc.)
    # BEFORE the first refresh so sensors show historical context immediately.
    stored_health = await health_store.async_load()
    if stored_health and isinstance(stored_health, dict):
        coordinator.connection_health.restore_from_store(stored_health)
        _LOGGER.debug(
            "Restored health counters: total_polls=%d total_failures=%d ema=%.0f ms",
            coordinator.connection_health.total_polls,
            coordinator.connection_health.total_failures,
            coordinator.connection_health.ema_latency_ms or 0,
        )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HovalRuntimeData(coordinator=coordinator, api=api)

    # Register a parent device for each plant so circuit devices can use via_device
    device_reg = dr.async_get(hass)
    for plant_id, plant_data in coordinator.data.plants.items():
        device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, plant_id)},
            name=f"Hoval {plant_data.name}",
            manufacturer="Hoval",
            model="Plant",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Remove any entity that was registered by an older version of the integration
    # but is no longer produced by the current coordinator data.  This prevents the
    # "entity is no longer being provided" warning that appears after upgrades that
    # changed which circuit types are supported (e.g. BL → WEZ renaming in v3 API).
    await _async_cleanup_orphaned_entities(hass, entry, coordinator)

    # Listen for options changes to update polling interval dynamically
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_cleanup_orphaned_entities(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    coordinator: HovalDataCoordinator,
) -> None:
    """Remove entity registry entries that are no longer produced by the integration.

    Called once after the first successful coordinator refresh and after all
    platforms have registered their entities.  Any registered entity whose
    unique_id does not match a currently-known circuit path or a plant-level
    suffix is considered stale and removed.

    This handles the case where the Hoval v3 API changed a circuit type
    (e.g. a heat generator that was previously returned as BL now comes back
    as WEZ), leaving old BL entities orphaned in the registry.
    """
    registry = er.async_get(hass)

    # Build the set of valid {plant_id}_{circuit_path}_ prefixes
    circuit_prefixes: set[str] = set()
    valid_plant_ids: set[str] = set()
    for plant_id, plant_data in coordinator.data.plants.items():
        valid_plant_ids.add(plant_id)
        for path in plant_data.circuits:
            circuit_prefixes.add(f"{plant_id}_{path}_")

    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        uid = entity_entry.unique_id
        if uid is None:
            continue

        # Keep plant-level entities (e.g. online, error, api_*)
        is_plant_level = any(
            uid == f"{pid}_{suffix}"
            for pid in valid_plant_ids
            for suffix in _PLANT_LEVEL_ENTITY_SUFFIXES
        )
        if is_plant_level:
            continue

        # Keep circuit-level entities whose path is present in coordinator data
        if any(uid.startswith(prefix) for prefix in circuit_prefixes):
            continue

        _LOGGER.info(
            "Removing stale entity '%s' (unique_id=%s) — circuit no longer present in API data. "
            "This is expected after a Hoval API circuit-type change (e.g. BL → WEZ).",
            entity_entry.entity_id,
            uid,
        )
        registry.async_remove(entity_entry.entity_id)
        removed += 1

    if removed:
        _LOGGER.info(
            "Cleaned up %d orphaned entity/entities for Hoval config entry %s. "
            "If you believe this is an error, check your HA logs for the circuit "
            "types returned by the API.",
            removed,
            entry.entry_id,
        )

    return True


async def _async_options_updated(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
) -> None:
    """Handle options update — adjust polling interval without reload."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _get_scan_interval(entry)
    _LOGGER.debug("Polling interval updated to %s", coordinator.update_interval)


async def async_unload_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Unload a config entry, flushing health counters to storage first."""
    coordinator = entry.runtime_data.coordinator
    # Force an immediate save so counters are not lost on a clean shutdown even
    # if the debounced save (triggered after each successful poll) hasn't fired.
    await coordinator.async_save_health()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
