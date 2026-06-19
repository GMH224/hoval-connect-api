"""The Hoval Connect integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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
    """Get the scan interval from options or use default.

    The stored value is coerced to int defensively: earlier builds could persist
    the interval as a string (the frontend submits dropdown values as strings),
    which would otherwise raise TypeError in timedelta(). A non-numeric value
    falls back to the default so the integration always loads.
    """
    default_s = int(DEFAULT_SCAN_INTERVAL.total_seconds())
    try:
        seconds = int(entry.options.get(CONF_SCAN_INTERVAL, default_s))
    except (TypeError, ValueError):
        seconds = default_s
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

    # Listen for options changes to update polling interval dynamically
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

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
