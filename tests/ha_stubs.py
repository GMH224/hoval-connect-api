"""Realistic stand-ins for the Home Assistant APIs the v2.2.0 migration touches.

``conftest.py`` stubs most of the ``homeassistant`` namespace with MagicMock,
which is fine for the coordinator/API tests because those code paths never touch
it. The compatibility tests added in v2.2.0 do: they import the entity platforms
and the config flow, which subclass HA base classes and build entity
descriptions. MagicMock cannot stand in for either — subclassing a MagicMock
silently turns the subclass into a mock, and a MagicMock "dataclass base" cannot
be inherited by a real ``@dataclass``.

The stubs below therefore mirror the shape of the real HA 2026.9 objects closely
enough that the integration's own logic is what actually gets exercised:

* ``DeviceInfo`` is a plain ``dict`` factory, exactly as a ``TypedDict`` is at
  runtime, so tests can assert on the keys the integration emits.
* ``_DEVICE_INFO_KEYS`` mirrors the real 2026.9 ``DeviceInfo`` TypedDict, letting
  a test fail if the integration ever emits a key HA removed.
* ``DeviceRegistry`` records calls and hands back entries with real ``.id``
  values, so the plant-device resolver's caching and registration behaviour is
  observable.
* ``OptionsFlowWithReload`` carries the ``automatic_reload`` flag HA checks.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# homeassistant.helpers.device_registry
# ---------------------------------------------------------------------------

# The exact key set of homeassistant.helpers.device_registry.DeviceInfo in HA
# 2026.9.0. `via_device`, `default_name`, `default_model` and
# `default_manufacturer` are deliberately absent — they were removed from the
# TypedDict, which is what makes the v2.2.0 migration necessary.
_DEVICE_INFO_KEYS = frozenset(
    {
        "configuration_url",
        "connections",
        "entry_type",
        "identifiers",
        "manufacturer",
        "model",
        "model_id",
        "name",
        "serial_number",
        "suggested_area",
        "sw_version",
        "hw_version",
        "translation_key",
        "translation_placeholders",
        "via_device_id",
    }
)


def DeviceInfo(**kwargs: Any) -> dict[str, Any]:  # noqa: N802 - mirrors HA's TypedDict
    """Stand in for HA's DeviceInfo TypedDict (a plain dict at runtime)."""
    return dict(kwargs)


@dataclass
class StubDeviceEntry:
    """Minimal stand-in for homeassistant.helpers.device_registry.DeviceEntry."""

    id: str
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    via_device_id: str | None = None
    name: str | None = None
    manufacturer: str | None = None
    model: str | None = None


class StubDeviceRegistry:
    """Records async_get_or_create calls and issues stable device IDs.

    Mirrors the parts of HA's registry the integration depends on: identifiers
    identify a device, repeat calls return the same entry, and an unresolvable
    ``via_device_id`` raises — the behaviour that makes parent-before-child
    registration ordering load-bearing.
    """

    def __init__(self) -> None:
        self.devices: dict[frozenset[tuple[str, str]], StubDeviceEntry] = {}
        self.calls: list[dict[str, Any]] = []
        self._next_id = 0

    def async_get_or_create(self, **kwargs: Any) -> StubDeviceEntry:
        """Create or return a device, validating the modern DeviceInfo contract."""
        self.calls.append(kwargs)

        unexpected = set(kwargs) - _DEVICE_INFO_KEYS - {"config_entry_id", "config_subentry_id"}
        if unexpected:
            # HA 2026.9's async_get_or_create raises TypeError for unknown
            # keywords; via_device survives only via an explicit kwargs.pop().
            raise TypeError(
                f"async_get_or_create() got unexpected keyword arguments {sorted(unexpected)}"
            )

        via = kwargs.get("via_device_id")
        if via is not None and via not in {d.id for d in self.devices.values()}:
            raise ValueError(f"via_device_id {via} is not a registered device id")

        key = frozenset(kwargs.get("identifiers") or set())
        if key in self.devices:
            return self.devices[key]

        self._next_id += 1
        entry = StubDeviceEntry(
            id=f"dev{self._next_id}",
            identifiers=set(key),
            via_device_id=via,
            name=kwargs.get("name"),
            manufacturer=kwargs.get("manufacturer"),
            model=kwargs.get("model"),
        )
        self.devices[key] = entry
        return entry


# ---------------------------------------------------------------------------
# homeassistant.config_entries
# ---------------------------------------------------------------------------


class StubOptionsFlow:
    """Stand-in for homeassistant.config_entries.OptionsFlow."""

    automatic_reload: bool = False

    def async_create_entry(self, *, title: str, data: Any) -> dict[str, Any]:
        """Mirror the CREATE_ENTRY flow result."""
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
        """Mirror the FORM flow result."""
        return {"type": "form", **kwargs}


class StubOptionsFlowWithReload(StubOptionsFlow):
    """Stand-in for OptionsFlowWithReload.

    HA raises ValueError when a config entry has update listeners and an options
    flow of this type saves options, so ``automatic_reload`` is the flag the
    compatibility tests assert on.
    """

    automatic_reload: bool = True


class StubConfigFlow:
    """Stand-in for homeassistant.config_entries.ConfigFlow."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Accept the `domain=DOMAIN` class keyword HA's ConfigFlow takes."""
        super().__init_subclass__()


# ---------------------------------------------------------------------------
# homeassistant.const
# ---------------------------------------------------------------------------


class UnitOfRatio(StrEnum):
    """Mirror of HA 2026.9's UnitOfRatio."""

    PARTS_PER_MILLION = "ppm"
    PARTS_PER_BILLION = "ppb"
    PERCENTAGE = "%"


class UnitOfTemperature(StrEnum):
    """Subset of HA's UnitOfTemperature."""

    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class UnitOfEnergy(StrEnum):
    """Subset of HA's UnitOfEnergy."""

    KILO_WATT_HOUR = "kWh"
    WATT_HOUR = "Wh"
    MEGA_WATT_HOUR = "MWh"


class UnitOfTime(StrEnum):
    """Subset of HA's UnitOfTime."""

    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"
    DAYS = "d"
    MILLISECONDS = "ms"


class EntityCategory(StrEnum):
    """Mirror of HA's EntityCategory."""

    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class Platform(StrEnum):
    """Subset of HA's Platform enum."""

    BINARY_SENSOR = "binary_sensor"
    CLIMATE = "climate"
    FAN = "fan"
    NUMBER = "number"
    SELECT = "select"
    SENSOR = "sensor"
    WATER_HEATER = "water_heater"


# HA still exports this as a live constant (PERCENTAGE = UnitOfRatio.PERCENTAGE.value).
PERCENTAGE = UnitOfRatio.PERCENTAGE.value


# ---------------------------------------------------------------------------
# Entity descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EntityDescription:
    """Stand-in for HA's EntityDescription base."""

    key: str
    device_class: Any = None
    entity_category: Any = None
    icon: str | None = None
    name: str | None = None
    translation_key: str | None = None
    entity_registry_enabled_default: bool = True


@dataclass(frozen=True, kw_only=True)
class SensorEntityDescription(EntityDescription):
    """Stand-in for HA's SensorEntityDescription."""

    native_unit_of_measurement: str | None = None
    state_class: Any = None
    suggested_display_precision: int | None = None


@dataclass(frozen=True, kw_only=True)
class NumberEntityDescription(EntityDescription):
    """Stand-in for HA's NumberEntityDescription."""

    native_unit_of_measurement: str | None = None
    native_min_value: float | None = None
    native_max_value: float | None = None
    native_step: float | None = None
    mode: Any = None


@dataclass(frozen=True, kw_only=True)
class BinarySensorEntityDescription(EntityDescription):
    """Stand-in for HA's BinarySensorEntityDescription."""


class StubEntity:
    """Minimal stand-in for HA's Entity base.

    Real classes are required here rather than MagicMock: the platforms subclass
    both an entity base and CoordinatorEntity, and two MagicMock bases produce a
    metaclass conflict at import time.
    """

    _attr_has_entity_name: bool = False
    _attr_unique_id: str | None = None
    _attr_device_info: dict[str, Any] | None = None

    @property
    def device_info(self) -> dict[str, Any] | None:
        """Return the entity's device info."""
        return self._attr_device_info

    @property
    def unique_id(self) -> str | None:
        """Return the entity's unique ID."""
        return self._attr_unique_id


class StubCoordinatorEntity(StubEntity):
    """Stand-in for CoordinatorEntity, including its generic subscript form."""

    def __init__(self, coordinator: Any, context: Any = None) -> None:
        """Store the coordinator like the real base class does."""
        self.coordinator = coordinator
        self.coordinator_context = context

    def __class_getitem__(cls, _item: Any) -> type[StubCoordinatorEntity]:
        """CoordinatorEntity[HovalDataCoordinator] -> the class itself."""
        return cls

    def async_write_ha_state(self) -> None:
        """No-op state write."""

    async def async_added_to_hass(self) -> None:
        """No-op lifecycle hook."""


def make_entity_base(name: str) -> type[StubEntity]:
    """Build a distinct real entity base class for a platform."""
    return type(name, (StubEntity,), {})


class _LenientModule(types.ModuleType):
    """Module whose unknown attributes resolve to MagicMock.

    The platforms import a long tail of enums and feature flags that the
    migration does not touch; auto-mocking them keeps the stub surface focused
    on what is actually under test.
    """

    def __getattr__(self, name: str) -> Any:
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


def make_module(name: str, **attrs: Any) -> _LenientModule:
    """Build a lenient stub module with the given real attributes."""
    module = _LenientModule(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module
