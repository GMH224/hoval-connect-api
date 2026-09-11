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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store

from .api import HovalConnectApi
from .const import (
    CIRCUIT_TYPE_NAMES,
    CONF_HEALTH_CHECK_INTERVAL,
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DOMAIN,
    HEALTH_CHECK_INTERVAL_OPTIONS,
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
    Platform.WATER_HEATER,
]
# v1.0.0 removed Platform.SENSOR entirely: every sensor entity depended on
# telemetry (live-values, events, weather) this integration no longer polls
# — a separate CAN-bus HACS integration is now the source for that data. See
# docs/audit-v1.0.0.md and CHANGELOG.md. This is a breaking change: existing
# sensor.* entities from this integration will stop updating and eventually
# show as "not provided by the integration" in Settings > Devices & Services
# > Entities; removing them from the registry is a manual step for the user
# since Home Assistant does not do this automatically. The five entities the
# user confirmed depending on for automations (2026-09-10) are unaffected —
# select.*_program, number.*_weather_based_control_*, binary_sensor.*_error,
# and water_heater.* were never sensor.py entities.

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


def _get_health_check_interval(entry: HovalConnectConfigEntry) -> timedelta:
    """Get the health-check interval from options, or use the default.

    Reinstated at the user's explicit request (this same v1.0.0 release,
    before deployment — see CHANGELOG.md): v1.0.0 initially removed the
    old CONF_SCAN_INTERVAL entirely, since there was no longer a
    meaningful "poll rate" to tune for circuit/program/settings data. This
    is a narrower, differently-scoped setting — it only affects the
    cadence of the one lightweight reachability check this integration
    still runs on a schedule (see HEALTH_CHECK_INTERVAL's comment in
    const.py); it does not bring back telemetry polling of any kind.

    The stored value is coerced to int defensively, mirroring the old
    _get_scan_interval()'s reasoning: earlier builds — or an options form
    resubmission — could persist the interval as a string (dropdown values
    often arrive as strings from the frontend), which would otherwise
    raise TypeError in timedelta(). A non-numeric value falls back to the
    default so the integration always loads.

    Independent audit finding (2026-09, fourth round, HVC-006): the
    coerced integer was never checked against HEALTH_CHECK_INTERVAL_OPTIONS
    — the options FORM only ever offers those choices, but nothing stopped
    an out-of-band value (a hand-edited config-entry options file, a
    migration bug, or simply a persisted value from a build that offered
    different choices) from reaching this function. `0` or a negative
    value would hand DataUpdateCoordinator a non-positive update_interval
    (undefined/pathological scheduling); an enormous value would
    effectively disable health checks entirely; and a truly enormous one
    can make `timedelta()` itself raise `OverflowError`, which the
    original `except (TypeError, ValueError)` never caught. Any persisted
    value outside the supported options — not just non-numeric ones — now
    falls back to the default.
    """
    default_s = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    try:
        seconds = int(entry.options.get(CONF_HEALTH_CHECK_INTERVAL, default_s))
    except (TypeError, ValueError):
        seconds = default_s
    if seconds not in HEALTH_CHECK_INTERVAL_OPTIONS:
        _LOGGER.warning(
            "Persisted health_check_interval=%r is not one of the supported "
            "options %s; using the default of %d seconds instead.",
            seconds,
            HEALTH_CHECK_INTERVAL_OPTIONS,
            default_s,
        )
        seconds = default_s
    try:
        return timedelta(seconds=seconds)
    except OverflowError:
        # Unreachable in practice now that `seconds` is constrained to
        # HEALTH_CHECK_INTERVAL_OPTIONS above, but kept as defense in depth
        # in case that constant is ever changed to include a pathological
        # value — timedelta() itself can raise OverflowError for an
        # absurdly large integer, which is not a TypeError or ValueError.
        return timedelta(seconds=DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS)


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

    # v0.24.0: HovalConnectApi takes hass (to run its requests-in-executor
    # calls), not an aiohttp session — see api.py's module docstring for why
    # this integration deliberately does not use Home Assistant's shared
    # aiohttp session.
    api = HovalConnectApi(hass, entry.data["email"], entry.data["password"])

    # Everything from here until entry.runtime_data is actually assigned
    # (below) is wrapped in try/except specifically to close `api`'s
    # requests.Session on any failure (independent audit finding, 2026-09).
    # Before this fix: if async_config_entry_first_refresh() raised (auth
    # failure, timeout, circuit-list error, anything), entry.runtime_data
    # was never set, so async_unload_entry() — the only other place that
    # calls api.aclose() — never ran for this attempt. The session (and its
    # connection pool) leaked until Python's garbage collector eventually
    # caught it, which is non-deterministic and, given Home Assistant retries
    # failed config entry setups automatically, could accumulate multiple
    # abandoned sessions during a persistent failure (e.g. wrong credentials)
    # well before GC catches up.
    try:
        health_store = Store(hass, HEALTH_STORAGE_VERSION, HEALTH_STORAGE_KEY)

        coordinator = HovalDataCoordinator(hass, entry, api, health_store)
        # Reinstated at the user's explicit request (this same v1.0.0
        # release) — see _get_health_check_interval()'s docstring for why
        # this is a narrower, differently-scoped setting than the old,
        # fully-removed CONF_SCAN_INTERVAL, not a reversal of the decision
        # to stop polling telemetry.
        coordinator.update_interval = _get_health_check_interval(entry)

        # Restore persisted health counters (total_polls, total_failures, EMA, etc.)
        # BEFORE the first refresh so sensors show historical context immediately.
        #
        # Deliberately tolerant of ANY load failure, not just a missing file.
        # HEALTH_STORAGE_VERSION was bumped 1 -> 2 in v1.0.0 (schema changed),
        # and HA's Store helper does NOT silently discard a version mismatch on
        # its own the way an earlier comment here assumed — verified directly
        # against Store's source (homeassistant/helpers/storage.py): without an
        # overridden _async_migrate_func (which this integration has never
        # provided), a version mismatch raises UnsupportedStorageVersionError
        # (reading a NEWER file than requested, e.g. after downgrading from a
        # later release) or a re-raised NotImplementedError (reading an OLDER
        # file, e.g. after upgrading into a version bump like this one) instead
        # of returning None. Without this try/except, EITHER direction —
        # upgrading past a version bump, or rolling back afterwards — would
        # make async_setup_entry raise here and the whole integration fail to
        # load, not just lose historical counters. See docs/audit-v1.0.0.md.
        try:
            stored_health = await health_store.async_load()
        except Exception:  # noqa: BLE001 — see comment above: any failure here must degrade to a fresh start, never block setup
            _LOGGER.warning(
                "Could not load persisted health counters (likely a version "
                "mismatch from an upgrade or rollback) — starting fresh.",
                exc_info=True,
            )
            stored_health = None
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
    except BaseException:
        await api.aclose()
        raise

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
    # Independent audit finding (2026-09, "more" report, finding #7): cancel
    # any post-write refresh task that might still be sleeping through its
    # 2-second settle delay BEFORE closing the API session below — otherwise
    # it could wake up afterwards and try to use an already-closed
    # requests.Session.
    await coordinator.async_shutdown()
    # v0.24.0: release the requests.Session()'s connection pool. Harmless to
    # skip (Python would eventually garbage-collect it), but tidy shutdown is
    # cheap and consistent with how an aiohttp session would have been
    # managed by HA itself before this integration switched transports.
    await entry.runtime_data.api.aclose()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
