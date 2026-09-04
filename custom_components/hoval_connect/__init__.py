"""The Hoval Connect integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
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

# Minimum Home Assistant version this integration supports.
#
# `via_device_id` (DeviceInfo and DeviceRegistry.async_get_or_create) landed in
# HA 2026.8. Earlier releases have no **kwargs on async_get_or_create, so the
# keyword raises TypeError and every circuit entity silently fails to register.
# HACS enforces the floor declared in hacs.json, but a manual install bypasses
# that, so the check is repeated here to fail with an explanatory message rather
# than an opaque TypeError.
MIN_HA_VERSION = (2026, 8)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.WATER_HEATER,
]

type HovalConnectConfigEntry = ConfigEntry[HovalRuntimeData]


class HovalPlantDevices:
    """Resolve and cache the device-registry IDs of plant (hub) devices.

    Circuit devices link to their plant with ``via_device_id``, which requires the
    parent's real device-registry ID rather than an identifier tuple. The plant
    device must therefore already be registered before any circuit that points at
    it — an unresolvable ``via_device_id`` raises ``DeviceInfoError`` and the
    entity is dropped, whereas the removed ``via_device`` only logged a warning.

    Plants are registered on demand rather than only during ``async_setup_entry``
    because the platforms re-scan ``coordinator.data.plants`` on every
    ``SIGNAL_NEW_CIRCUITS`` dispatch, so a plant that appears after setup would
    otherwise have no registered parent device.
    """

    def __init__(self, hass: HomeAssistant, entry: HovalConnectConfigEntry) -> None:
        """Initialize the resolver for one config entry."""
        self._hass = hass
        self._entry = entry
        self._device_ids: dict[str, str] = {}

    @callback
    def async_get_device_id(self, plant_id: str, plant_data: HovalPlantData) -> str:
        """Return the device-registry ID of a plant, registering it if needed."""
        if (device_id := self._device_ids.get(plant_id)) is not None:
            return device_id

        device = dr.async_get(self._hass).async_get_or_create(
            config_entry_id=self._entry.entry_id,
            **plant_device_info(plant_data),
        )
        self._device_ids[plant_id] = device.id
        return device.id


@dataclass
class HovalRuntimeData:
    """Runtime data for the Hoval Connect integration."""

    coordinator: HovalDataCoordinator
    api: HovalConnectApi
    plant_devices: HovalPlantDevices


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
    plant_device_id: str,
    circuit_data: HovalCircuitData,
) -> DeviceInfo:
    """Build DeviceInfo for a circuit device parented to its plant.

    ``plant_id`` still builds the circuit's identifiers — it is part of the
    entity/device identity and must stay byte-for-byte stable across the upgrade
    so existing devices are matched rather than duplicated. ``plant_device_id``
    is only the parent link.
    """
    model = CIRCUIT_TYPE_NAMES.get(circuit_data.circuit_type, circuit_data.circuit_type)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{plant_id}_{circuit_data.path}")},
        name=f"Hoval {circuit_data.name}",
        manufacturer="Hoval",
        model=model,
        via_device_id=plant_device_id,
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


def _check_ha_version() -> None:
    """Raise a clear error when Home Assistant is too old for this release.

    Raises:
        ConfigEntryError: if the running HA version predates MIN_HA_VERSION.
    """
    if (MAJOR_VERSION, MINOR_VERSION) >= MIN_HA_VERSION:
        return
    required = f"{MIN_HA_VERSION[0]}.{MIN_HA_VERSION[1]}"
    running = f"{MAJOR_VERSION}.{MINOR_VERSION}"
    raise ConfigEntryError(
        f"Hoval Connect requires Home Assistant {required} or newer "
        f"(running {running}). Upgrade Home Assistant, or install Hoval Connect "
        "v0.21.1, which supports older releases."
    )


async def async_setup_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Set up Hoval Connect from a config entry."""
    _check_ha_version()

    session = async_get_clientsession(hass)
    api = HovalConnectApi(session, entry.data["email"], entry.data["password"])

    health_store = Store(hass, HEALTH_STORAGE_VERSION, HEALTH_STORAGE_KEY)

    coordinator = HovalDataCoordinator(hass, entry, api, health_store)
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

    plant_devices = HovalPlantDevices(hass, entry)
    entry.runtime_data = HovalRuntimeData(
        coordinator=coordinator,
        api=api,
        plant_devices=plant_devices,
    )

    # Register the parent device for each plant BEFORE forwarding to the
    # platforms: a circuit's via_device_id must already resolve when its entity
    # is added, otherwise the device registry rejects the entity outright.
    for plant_id, plant_data in coordinator.data.plants.items():
        plant_devices.async_get_device_id(plant_id, plant_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # NOTE: deliberately no entry.add_update_listener() here. The options flow is
    # an OptionsFlowWithReload, and Home Assistant raises ValueError when a config
    # entry carries update listeners while such a flow saves options. The reauth
    # flow's async_update_reload_and_abort() likewise warns about update listeners
    # and stops accepting them in HA 2026.12. Options changes now take effect via
    # a full config-entry reload, which re-runs this function.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HovalConnectConfigEntry) -> bool:
    """Unload a config entry, flushing health counters to storage first."""
    coordinator = entry.runtime_data.coordinator
    # Force an immediate save so counters are not lost on a clean shutdown even
    # if the debounced save (triggered after each successful poll) hasn't fired.
    await coordinator.async_save_health()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
