"""Shared pytest fixtures / import shims for the Hoval Connect test suite.

The test modules are designed to run WITHOUT Home Assistant installed by
stubbing the ``homeassistant.*`` namespace. Two kinds of stub are used:

1. **Real minimal classes** for the machinery the integration *subclasses or
   raises* — ``DataUpdateCoordinator``, ``UpdateFailed``,
   ``ConfigEntryAuthFailed``, ``HomeAssistantError`` — plus real functions for
   ``homeassistant.util.dt``.  MagicMock is NOT usable for these:

   - Subclassing a MagicMock "class" silently turns the subclass itself into a
     mock, shadowing every real method it defines.  That is why, before
     v0.21.1, ``HovalDataCoordinator._fetch_all_data`` could not be tested at
     all and the suite fell back to grepping the source code for guard
     patterns.
   - ``raise UpdateFailed(...)`` with a MagicMock "exception class" is a
     ``TypeError`` (exceptions must derive from ``BaseException``).

2. **MagicMock modules** for everything else the package imports at import
   time (``Store``, ``Platform``, device registry, …) which the tested code
   paths never actually exercise.

Test modules may still call ``sys.modules.setdefault`` themselves; this
conftest is imported first by pytest, so the real stubs below always win.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock

from . import ha_stubs

# ---------------------------------------------------------------------------
# Real stubs
# ---------------------------------------------------------------------------


class StubDataUpdateCoordinator:
    """Minimal real stand-in for HA's DataUpdateCoordinator.

    Only what HovalDataCoordinator's __init__ and the tested code paths need:
    attribute storage, generic-subscription support, and an awaitable
    async_request_refresh.
    """

    def __init__(self, hass, logger, *, name=None, update_interval=None, **_kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    def __class_getitem__(cls, _item):
        # DataUpdateCoordinator[HovalData] → the class itself.
        return cls

    async def async_request_refresh(self) -> None:
        """No-op; tests patch or inspect the coordinator directly."""


class StubUpdateFailed(Exception):
    """Real exception stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class StubConfigEntryAuthFailed(Exception):
    """Real exception stand-in for homeassistant.exceptions.ConfigEntryAuthFailed."""


class StubHomeAssistantError(Exception):
    """Real exception stand-in for homeassistant.exceptions.HomeAssistantError."""


class StubConfigEntryError(Exception):
    """Real exception stand-in for homeassistant.exceptions.ConfigEntryError."""


def _dispatcher_send(_hass, _signal, *_args) -> None:
    """No-op stand-in for async_dispatcher_send; tests monkeypatch to observe."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _register(name: str, module: types.ModuleType | MagicMock) -> None:
    sys.modules.setdefault(name, module)


# --- homeassistant.helpers.update_coordinator (real stubs + mock entity base)
_uc = types.ModuleType("homeassistant.helpers.update_coordinator")
_uc.DataUpdateCoordinator = StubDataUpdateCoordinator
_uc.UpdateFailed = StubUpdateFailed
# v2.2.0: the compatibility suite imports the entity platforms, which subclass
# CoordinatorEntity, so this must be a real class rather than a MagicMock.
_uc.CoordinatorEntity = ha_stubs.StubCoordinatorEntity
_register("homeassistant.helpers.update_coordinator", _uc)

# --- homeassistant.exceptions (real exception classes)
_exc = types.ModuleType("homeassistant.exceptions")
_exc.ConfigEntryAuthFailed = StubConfigEntryAuthFailed
_exc.HomeAssistantError = StubHomeAssistantError
_exc.ConfigEntryError = StubConfigEntryError
_register("homeassistant.exceptions", _exc)

# --- homeassistant.helpers.dispatcher (real no-op functions)
_disp = types.ModuleType("homeassistant.helpers.dispatcher")
_disp.async_dispatcher_send = _dispatcher_send
_disp.async_dispatcher_connect = MagicMock()
_register("homeassistant.helpers.dispatcher", _disp)

# --- homeassistant.util.dt (real datetime helpers)
_dt = types.ModuleType("homeassistant.util.dt")
_dt.utcnow = _utcnow
_dt.now = _now
_dt.parse_datetime = _parse_datetime
_register("homeassistant.util.dt", _dt)

_util = types.ModuleType("homeassistant.util")
_util.dt = _dt
_register("homeassistant.util", _util)

# --- everything else the package pulls in at import time.
#
# v2.2.0: the modules the migration touches (device registry, config entries,
# constants, entity platforms) now get realistic stubs from ha_stubs rather than
# a bare MagicMock, so the compatibility suite can import the entity platforms
# and config flow and assert on real behaviour. Everything else still resolves
# to MagicMock via _LenientModule.__getattr__.
_core = ha_stubs.make_module(
    "homeassistant.core",
    # Must be a real identity decorator: @callback wrapping a MagicMock would
    # replace the decorated function with a mock.
    callback=lambda func: func,
    HomeAssistant=MagicMock(),
)

_dr_mod = ha_stubs.make_module(
    "homeassistant.helpers.device_registry",
    DeviceInfo=ha_stubs.DeviceInfo,
    DeviceEntry=ha_stubs.StubDeviceEntry,
    async_get=MagicMock(),
)

_ce_mod = ha_stubs.make_module(
    "homeassistant.config_entries",
    ConfigFlow=ha_stubs.StubConfigFlow,
    OptionsFlow=ha_stubs.StubOptionsFlow,
    OptionsFlowWithReload=ha_stubs.StubOptionsFlowWithReload,
    ConfigFlowResult=dict,
)

_const_mod = ha_stubs.make_module(
    "homeassistant.const",
    PERCENTAGE=ha_stubs.PERCENTAGE,
    UnitOfRatio=ha_stubs.UnitOfRatio,
    UnitOfTemperature=ha_stubs.UnitOfTemperature,
    UnitOfEnergy=ha_stubs.UnitOfEnergy,
    UnitOfTime=ha_stubs.UnitOfTime,
    EntityCategory=ha_stubs.EntityCategory,
    Platform=ha_stubs.Platform,
    # Mirrors the running HA version; the v2.2.0 floor check reads these.
    MAJOR_VERSION=2026,
    MINOR_VERSION=9,
)

_helpers_mod = ha_stubs.make_module("homeassistant.helpers")
_components_mod = ha_stubs.make_module("homeassistant.components")
_ha_mod = ha_stubs.make_module("homeassistant")

_LENIENT_MODULES: dict[str, types.ModuleType] = {
    "homeassistant": _ha_mod,
    "homeassistant.core": _core,
    "homeassistant.const": _const_mod,
    "homeassistant.config_entries": _ce_mod,
    "homeassistant.helpers": _helpers_mod,
    "homeassistant.helpers.device_registry": _dr_mod,
    "homeassistant.helpers.storage": ha_stubs.make_module("homeassistant.helpers.storage"),
    "homeassistant.helpers.aiohttp_client": ha_stubs.make_module(
        "homeassistant.helpers.aiohttp_client"
    ),
    "homeassistant.helpers.entity_platform": ha_stubs.make_module(
        "homeassistant.helpers.entity_platform"
    ),
    "homeassistant.components": _components_mod,
    "homeassistant.components.sensor": ha_stubs.make_module(
        "homeassistant.components.sensor",
        SensorEntityDescription=ha_stubs.SensorEntityDescription,
        SensorEntity=ha_stubs.make_entity_base("SensorEntity"),
    ),
    "homeassistant.components.number": ha_stubs.make_module(
        "homeassistant.components.number",
        NumberEntityDescription=ha_stubs.NumberEntityDescription,
        NumberEntity=ha_stubs.make_entity_base("NumberEntity"),
    ),
    "homeassistant.components.binary_sensor": ha_stubs.make_module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntityDescription=ha_stubs.BinarySensorEntityDescription,
        BinarySensorEntity=ha_stubs.make_entity_base("BinarySensorEntity"),
    ),
    "homeassistant.components.diagnostics": ha_stubs.make_module(
        "homeassistant.components.diagnostics"
    ),
    "homeassistant.components.climate": ha_stubs.make_module(
        "homeassistant.components.climate",
        ClimateEntity=ha_stubs.make_entity_base("ClimateEntity"),
    ),
    "homeassistant.components.fan": ha_stubs.make_module(
        "homeassistant.components.fan",
        FanEntity=ha_stubs.make_entity_base("FanEntity"),
    ),
    "homeassistant.components.select": ha_stubs.make_module(
        "homeassistant.components.select",
        SelectEntity=ha_stubs.make_entity_base("SelectEntity"),
    ),
    "homeassistant.components.water_heater": ha_stubs.make_module(
        "homeassistant.components.water_heater",
        WaterHeaterEntity=ha_stubs.make_entity_base("WaterHeaterEntity"),
    ),
}

for _name, _module in _LENIENT_MODULES.items():
    _register(_name, _module)

# `from homeassistant.helpers import device_registry as dr` resolves the
# attribute on the parent package before falling back to the import system, so
# the parent must point at the same stub object the submodule is registered as.
for _dotted in list(_LENIENT_MODULES) + [
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.dispatcher",
    "homeassistant.util",
    "homeassistant.exceptions",
]:
    if "." not in _dotted:
        continue
    _parent_name, _, _child = _dotted.rpartition(".")
    _parent = sys.modules.get(_parent_name)
    _child_mod = sys.modules.get(_dotted)
    if _parent is not None and _child_mod is not None:
        setattr(_parent, _child, _child_mod)
