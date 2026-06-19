"""Shared pytest fixtures / import shims for the Hoval Connect test suite.

The test modules are designed to run WITHOUT Home Assistant installed by
mocking the ``homeassistant.*`` namespace. This conftest registers the full
set of submodules the package imports at import time (notably
``homeassistant.helpers.storage``, pulled in by ``__init__.py``) so test
collection succeeds in CI, which does not install Home Assistant.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.entity_platform",
    "homeassistant.util",
    "homeassistant.util.dt",
]

for _name in _HA_MODULES:
    sys.modules.setdefault(_name, MagicMock())
