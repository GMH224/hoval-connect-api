"""Sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .api_stats import HovalApiStats
from .const import CIRCUIT_TYPE_BL, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, CIRCUIT_TYPE_WW
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator, HovalPlantData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HovalSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval sensor entity."""

    value_fn: Callable[[HovalCircuitData], Any | None]
    circuit_types: frozenset[str] | None = None


@dataclass(frozen=True, kw_only=True)
class HovalPlantSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval plant-level sensor entity."""

    value_fn: Callable[[HovalPlantData], Any | None]


CIRCUIT_SENSOR_DESCRIPTIONS: tuple[HovalSensorEntityDescription, ...] = (
    HovalSensorEntityDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.live_values.get("outsideTemperature"),
    ),
    HovalSensorEntityDescription(
        key="exhaust_temperature",
        translation_key="exhaust_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("exhaustTemp"),
    ),
    HovalSensorEntityDescription(
        key="air_volume",
        translation_key="air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("airVolume"),
    ),
    HovalSensorEntityDescription(
        key="humidity_actual",
        translation_key="humidity_actual",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("humidityActual"),
    ),
    HovalSensorEntityDescription(
        key="humidity_target",
        translation_key="humidity_target",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.live_values.get("humidityTarget"),
    ),
    HovalSensorEntityDescription(
        key="operation_mode",
        translation_key="operation_mode",
        icon="mdi:cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.operation_mode,
    ),
    HovalSensorEntityDescription(
        key="active_week_program",
        translation_key="active_week_program",
        icon="mdi:calendar-week",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_week_name,
    ),
    HovalSensorEntityDescription(
        key="active_day_program",
        translation_key="active_day_program",
        icon="mdi:calendar-today",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.active_day_program_name,
    ),
    HovalSensorEntityDescription(
        key="program_air_volume",
        translation_key="program_air_volume",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HV}),
        value_fn=lambda c: c.program_air_volume,
    ),
    # HK additional sensors
    HovalSensorEntityDescription(
        key="flow_temp_actual",
        translation_key="flow_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("outgoingTempActual"),
    ),
    HovalSensorEntityDescription(
        key="flow_temp_target",
        translation_key="flow_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("outgoingTempTarget"),
    ),
    HovalSensorEntityDescription(
        key="room_temp_target",
        translation_key="room_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.live_values.get("roomTempTarget"),
    ),
    # BL (Boiler/Heat Pump) sensors
    HovalSensorEntityDescription(
        key="boiler_temp_actual",
        translation_key="boiler_temp_actual",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("tempActual"),
    ),
    HovalSensorEntityDescription(
        key="boiler_temp_target",
        translation_key="boiler_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("tempTarget"),
    ),
    HovalSensorEntityDescription(
        key="return_temperature",
        translation_key="return_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("returnTemperature"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours",
        translation_key="operating_hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHours"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours_over_50",
        translation_key="operating_hours_over_50",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-fast",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHoursOver50"),
    ),
    HovalSensorEntityDescription(
        key="operating_hours_el_heater",
        translation_key="operating_hours_el_heater",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-electric-outline",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operatingHoursElHeater"),
    ),
    HovalSensorEntityDescription(
        key="operation_cycles_el_heater",
        translation_key="operation_cycles_el_heater",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operationCyclesElHeater"),
    ),
    HovalSensorEntityDescription(
        key="heat_amount_el_heater",
        translation_key="heat_amount_el_heater",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("heatAmountElHeater"),
    ),
    HovalSensorEntityDescription(
        key="energy_el_heater",
        translation_key="energy_el_heater",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("energyElHeater"),
    ),
    HovalSensorEntityDescription(
        key="el_heater_active",
        translation_key="el_heater_active",
        icon="mdi:lightning-bolt",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("elHeaterActive"),
    ),
    HovalSensorEntityDescription(
        key="operation_cycles",
        translation_key="operation_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("operationCycles"),
    ),
    HovalSensorEntityDescription(
        key="heat_amount",
        translation_key="heat_amount",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("heatAmount"),
    ),
    HovalSensorEntityDescription(
        key="total_energy",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MEGA_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.live_values.get("totalEnergy"),
    ),
    # Status diagnostic sensors — sourced from the circuit list response, not live values
    HovalSensorEntityDescription(
        key="circuit_status_bl",
        translation_key="circuit_status_bl",
        icon="mdi:heat-pump",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_BL}),
        value_fn=lambda c: c.circuit_status,
    ),
    HovalSensorEntityDescription(
        key="circuit_status_hk",
        translation_key="circuit_status_hk",
        icon="mdi:radiator",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_HK}),
        value_fn=lambda c: c.circuit_status,
    ),
    HovalSensorEntityDescription(
        key="circuit_status_ww",
        translation_key="circuit_status_ww",
        icon="mdi:water-boiler",
        entity_category=EntityCategory.DIAGNOSTIC,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.circuit_status,
    ),
    # WW (Warm Water) sensors
    HovalSensorEntityDescription(
        key="ww_temp_target",
        translation_key="ww_temp_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempTarget"),
    ),
    HovalSensorEntityDescription(
        key="ww_temp_top",
        translation_key="ww_temp_top",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempSf1Actual"),
    ),
    HovalSensorEntityDescription(
        key="ww_temp_bottom",
        translation_key="ww_temp_bottom",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        circuit_types=frozenset({CIRCUIT_TYPE_WW}),
        value_fn=lambda c: c.live_values.get("tempSf2Actual"),
    ),
)

PLANT_SENSOR_DESCRIPTIONS: tuple[HovalPlantSensorEntityDescription, ...] = (
    HovalPlantSensorEntityDescription(
        key="latest_event_type",
        translation_key="latest_event_type",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.event_type if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_message",
        translation_key="latest_event_message",
        icon="mdi:message-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.description if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="latest_event_time",
        translation_key="latest_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.latest_event.time_occurred if p.latest_event else None,
    ),
    HovalPlantSensorEntityDescription(
        key="active_events",
        translation_key="active_events",
        icon="mdi:alert",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: sum(1 for e in p.events if e.is_active),
    ),
    HovalPlantSensorEntityDescription(
        key="weather_condition",
        translation_key="weather_condition",
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda p: p.weather.weather_type if p.weather else None,
    ),
    HovalPlantSensorEntityDescription(
        key="weather_temperature",
        translation_key="weather_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.weather.outside_temperature if p.weather else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities.

    Creates three groups of sensors:

    1. **Circuit sensors** (``HovalCircuitSensor``) — per-circuit telemetry
       such as temperatures, air volume, humidity, and program names.
    2. **Plant sensors** (``HovalPlantSensor``) — plant-level information
       such as latest event details, active event count, and weather.
    3. **API communication sensors** (``HovalApiStatsSensor``) — integration
       health metrics attached to the plant device as ``DIAGNOSTIC`` entities.
       These read from ``coordinator.api.stats`` rather than coordinator data
       so they accurately reflect raw API behaviour.
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            # Circuit-level sensors
            for path, circuit in plant_data.circuits.items():
                for description in CIRCUIT_SENSOR_DESCRIPTIONS:
                    if (
                        description.circuit_types is not None
                        and circuit.circuit_type not in description.circuit_types
                    ):
                        continue
                    uid = f"{plant_id}_{path}_{description.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalCircuitSensor(coordinator, plant_id, path, circuit, description)
                    )

            # Plant-level sensors
            for description in PLANT_SENSOR_DESCRIPTIONS:
                uid = f"{plant_id}_{description.key}"
                if uid in known:
                    continue
                known.add(uid)
                entities.append(HovalPlantSensor(coordinator, plant_id, plant_data, description))

            # API communication / health sensors (created once per plant)
            for description in COMMS_SENSOR_DESCRIPTIONS:
                uid = f"{plant_id}_{description.key}"
                if uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalApiStatsSensor(coordinator, plant_id, plant_data, description)
                )

        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalCircuitSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval circuit sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.key}"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value.

        Attempts numeric conversion for every value so that counter sensors
        (``operation_cycles``, ``operation_cycles_el_heater``) that have no
        ``native_unit_of_measurement`` but carry ``state_class=TOTAL_INCREASING``
        still return a number rather than a string.  HA rejects string states
        on measurement/counter sensors, causing the entity to show as unknown.

        Fallback chain:
        1. Already a number → return as-is.
        2. String that converts to float → return float.
        3. Non-numeric string (program name, status label) → return str.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        val = self.entity_description.value_fn(circuit)
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return val
        # Try numeric conversion (handles live-value strings like "123.4")
        try:
            return float(val)
        except (ValueError, TypeError):
            # Non-numeric — return as string (operation_mode, program names, etc.)
            return str(val)


class HovalPlantSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval plant-level sensor entity."""

    _attr_has_entity_name = True
    entity_description: HovalPlantSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
        description: HovalPlantSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{description.key}"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def _plant(self) -> HovalPlantData | None:
        """Get current plant data from coordinator."""
        return self.coordinator.data.plants.get(self._plant_id)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._plant is not None

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value.

        For TIMESTAMP sensors (``latest_event_time``) the Hoval API returns an
        ISO-8601 string such as ``"2026-02-17T10:30:00Z"``.  HA's TIMESTAMP
        device class requires a timezone-aware ``datetime`` object — passing the
        raw string causes the entity to show as ``unknown``.  We parse it here
        with ``dt_util.parse_datetime`` which handles the UTC ``Z`` suffix.
        """
        plant = self._plant
        if plant is None:
            return None
        val = self.entity_description.value_fn(plant)
        if val is None:
            return None
        # TIMESTAMP sensors need a proper datetime, not a raw string.
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP and isinstance(
            val, str
        ):
            parsed = dt_util.parse_datetime(val)
            if parsed is None:
                _LOGGER.debug("Could not parse timestamp value: %r", val)
            return parsed
        if self.entity_description.native_unit_of_measurement is None and not isinstance(
            val, (int, float)
        ):
            return str(val)
        try:
            return float(val) if isinstance(val, (int, float)) else val
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# API communication / health sensors
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class HovalCommsSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval API communication health sensor.

    ``stat_fn`` accepts the ``HovalApiStats`` instance and returns the sensor
    value.  Using a distinct field name (``stat_fn`` rather than ``value_fn``)
    avoids any potential conflict with the parent ``SensorEntityDescription``
    in future HA versions.
    """

    stat_fn: Callable[[HovalApiStats], Any | None]


COMMS_SENSOR_DESCRIPTIONS: tuple[HovalCommsSensorEntityDescription, ...] = (
    # --- Rolling-window rate metrics (last 60 minutes) ---
    HovalCommsSensorEntityDescription(
        key="api_calls_hour",
        translation_key="api_calls_hour",
        icon="mdi:api",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.calls_last_hour,
    ),
    HovalCommsSensorEntityDescription(
        key="api_timeouts_hour",
        translation_key="api_timeouts_hour",
        icon="mdi:timer-off-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.timeouts_last_hour,
    ),
    HovalCommsSensorEntityDescription(
        key="api_errors_hour",
        translation_key="api_errors_hour",
        icon="mdi:cloud-alert",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.errors_last_hour,
    ),
    HovalCommsSensorEntityDescription(
        key="api_retries_hour",
        translation_key="api_retries_hour",
        icon="mdi:reload",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.retries_last_hour,
    ),
    HovalCommsSensorEntityDescription(
        key="api_failure_ratio",
        translation_key="api_failure_ratio",
        icon="mdi:percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.failure_ratio_last_hour,
    ),
    # --- Lifetime counters (never reset within a session) ---
    HovalCommsSensorEntityDescription(
        key="api_total_calls",
        translation_key="api_total_calls",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.total_calls,
    ),
    HovalCommsSensorEntityDescription(
        key="api_total_errors",
        translation_key="api_total_errors",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.total_errors,
    ),
    # --- Timestamp sensors ---
    HovalCommsSensorEntityDescription(
        key="api_last_success",
        translation_key="api_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.last_success_time,
    ),
    HovalCommsSensorEntityDescription(
        key="api_last_error_time",
        translation_key="api_last_error_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.last_error_time,
    ),
    # --- Last error message (plain string, no unit) ---
    HovalCommsSensorEntityDescription(
        key="api_last_error_message",
        translation_key="api_last_error_message",
        icon="mdi:message-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s.last_error_message,
    ),
    # --- Current poll interval (reflects user configuration) ---
    HovalCommsSensorEntityDescription(
        key="api_poll_interval",
        translation_key="api_poll_interval",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        stat_fn=lambda s: s._poll_interval_seconds,  # set by coordinator wrapper below
    ),
)


class HovalApiStatsSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Sensor entity for Hoval API communication health metrics.

    Reads from the ``HovalApiStats`` object stored on ``coordinator.api.stats``.
    Extends ``CoordinatorEntity`` so that:

    * It updates automatically on every coordinator poll cycle.
    * It becomes unavailable if the coordinator enters a prolonged error state.
    * It respects the integration's unload / reload lifecycle.

    The ``stat_fn`` on the entity description receives the ``HovalApiStats``
    instance and returns the sensor value.  Timestamp sensors return
    ``datetime`` objects directly; numeric sensors return ``int`` or ``float``;
    string sensors return ``str``.
    """

    _attr_has_entity_name = True
    entity_description: HovalCommsSensorEntityDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
        description: HovalCommsSensorEntityDescription,
    ) -> None:
        """Initialise the API stats sensor.

        Args:
            coordinator: The shared data coordinator (provides ``coordinator.api.stats``).
            plant_id:    Plant identifier, used to build ``unique_id`` and ``DeviceInfo``.
            plant_data:  Current plant data, used to build ``DeviceInfo``.
            description: Entity description including the ``stat_fn`` callable.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{description.key}"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def _stats(self) -> HovalApiStats:
        """Return the stats object from the API client on the coordinator."""
        return self.coordinator.api.stats

    @property
    def native_value(self) -> Any:
        """Return the sensor value via the description's stat_fn.

        Timestamp sensors (``device_class=TIMESTAMP``) return the ``datetime``
        object from the stats collector directly — no additional parsing required
        because ``HovalApiStats`` stores them as ``datetime.now(timezone.utc)``.

        String sensors (``last_error_message``) return ``str`` or ``None``.
        Numeric sensors return ``int`` or ``float``.
        """
        try:
            val = self.entity_description.stat_fn(self._stats)
        except Exception:  # noqa: BLE001
            return None
        if val is None:
            return None
        # TIMESTAMP — return datetime directly
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return val
        # String sensors (no unit, not a number) — coerce to str
        if (
            self.entity_description.native_unit_of_measurement is None
            and not isinstance(val, (int, float))
        ):
            return str(val)
        return val
