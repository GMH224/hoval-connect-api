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
_uc.CoordinatorEntity = MagicMock()  # entity platforms are not imported in tests
_register("homeassistant.helpers.update_coordinator", _uc)

# --- homeassistant.exceptions (real exception classes)
_exc = types.ModuleType("homeassistant.exceptions")
_exc.ConfigEntryAuthFailed = StubConfigEntryAuthFailed
_exc.HomeAssistantError = StubHomeAssistantError
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

# --- everything else the package pulls in at import time: MagicMock is fine
_HA_MOCK_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
]

for _name in _HA_MOCK_MODULES:
    _register(_name, MagicMock())
