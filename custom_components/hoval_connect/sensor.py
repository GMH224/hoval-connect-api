"""Sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from .const import CIRCUIT_TYPE_BL, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, CIRCUIT_TYPE_WW
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalConnectionHealth,
    HovalDataCoordinator,
    HovalPlantData,
)


@dataclass(frozen=True, kw_only=True)
class HovalSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval sensor entity."""

    value_fn: Callable[[HovalCircuitData], Any | None]
    circuit_types: frozenset[str] | None = None


@dataclass(frozen=True, kw_only=True)
class HovalPlantSensorEntityDescription(SensorEntityDescription):
    """Describe a Hoval plant-level sensor entity."""

    value_fn: Callable[[HovalPlantData], Any | None]


@dataclass(frozen=True, kw_only=True)
class HovalConnectionSensorDescription(SensorEntityDescription):
    """Describe a Hoval API connection health sensor.

    These sensors read from coordinator.connection_health (a persistent
    HovalConnectionHealth dataclass on the coordinator) rather than from
    coordinator.data.  They remain available even when the last poll failed,
    because tracking failures is their entire purpose.
    """

    value_fn: Callable[[HovalConnectionHealth], Any | None]


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
    # --- Per-circuit reliability diagnostics ---
    # Reflect live-values fetch health for this specific circuit, so users can
    # pinpoint a single flaky circuit without exporting the full diagnostics JSON.
    HovalSensorEntityDescription(
        key="circuit_failure_rate_1h",
        translation_key="circuit_failure_rate_1h",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi-alert",
        # None until at least one poll in the 1-hour window — same sentinel
        # behaviour as the plant-level failure_rate_1h sensor.
        value_fn=lambda c: c.circuit_failure_rate_1h,
    ),
    HovalSensorEntityDescription(
        key="circuit_availability_1h",
        translation_key="circuit_availability_1h",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi-check",
        value_fn=lambda c: c.circuit_availability_1h,
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

# ---------------------------------------------------------------------------
# API connection health sensors
# ---------------------------------------------------------------------------
# These sensors expose the coordinator's persistent HovalConnectionHealth state
# to HA, enabling dashboards and automations to observe and alert on
# connection quality with the Hoval cloud (which is known to be flaky).
#
# Key design choices:
#   - Attached to the plant device (natural parent for cloud-API diagnostics).
#   - All have EntityCategory.DIAGNOSTIC so they are hidden by default.
#   - HovalConnectionSensor overrides `available` to always return True —
#     the whole point of these sensors is to report failures, so marking them
#     unavailable on a poll error would defeat the purpose.
#   - Counters use TOTAL_INCREASING so HA's energy/stats UI can chart them
#     over time and alert on rate-of-change (e.g. >5 failures/hour).
# ---------------------------------------------------------------------------

CONNECTION_SENSOR_DESCRIPTIONS: tuple[HovalConnectionSensorDescription, ...] = (
    # --- Timestamps ---
    HovalConnectionSensorDescription(
        key="api_last_success",
        translation_key="api_last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-check-outline",
        value_fn=lambda h: h.last_success,
    ),
    HovalConnectionSensorDescription(
        key="api_last_error_time",
        translation_key="api_last_error_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-alert",
        value_fn=lambda h: h.last_error_time,
    ),
    # --- Error details ---
    HovalConnectionSensorDescription(
        key="api_last_error",
        translation_key="api_last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-network-outline",
        value_fn=lambda h: h.last_error_msg,
    ),
    HovalConnectionSensorDescription(
        key="api_last_error_type",
        translation_key="api_last_error_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:help-network-outline",
        value_fn=lambda h: h.last_error_type,
    ),
    # --- Failure counters ---
    HovalConnectionSensorDescription(
        key="api_consecutive_failures",
        translation_key="api_consecutive_failures",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:connection",
        value_fn=lambda h: h.consecutive_failures,
    ),
    HovalConnectionSensorDescription(
        key="api_total_failures",
        translation_key="api_total_failures",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-off-outline",
        value_fn=lambda h: h.total_failures,
    ),
    HovalConnectionSensorDescription(
        key="api_auth_failures",
        translation_key="api_auth_failures",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:lock-alert-outline",
        value_fn=lambda h: h.auth_failures,
    ),
    # --- Poll statistics ---
    HovalConnectionSensorDescription(
        key="api_total_polls",
        translation_key="api_total_polls",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:refresh-circle",
        value_fn=lambda h: h.total_polls,
    ),
    # --- Performance: single-poll ---
    HovalConnectionSensorDescription(
        key="api_poll_latency",
        translation_key="api_poll_latency",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:timer-outline",
        # Only meaningful after the first successful poll; None until then.
        value_fn=lambda h: h.poll_latency_ms,
    ),
    # --- Performance: rolling statistics (last _LATENCY_HISTORY_SIZE successful polls) ---
    HovalConnectionSensorDescription(
        key="api_ema_latency",
        translation_key="api_ema_latency",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chart-timeline-variant",
        # EMA persists across HA restarts (unlike avg/p95 which reset).
        # None only on a brand-new install before the first successful poll.
        value_fn=lambda h: h.ema_latency_ms,
    ),
    HovalConnectionSensorDescription(
        key="api_avg_latency",
        translation_key="api_avg_latency",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:timer-sand",
        value_fn=lambda h: h.avg_latency_ms,
    ),
    HovalConnectionSensorDescription(
        key="api_p95_latency",
        translation_key="api_p95_latency",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:timer-alert-outline",
        value_fn=lambda h: h.p95_latency_ms,
    ),
    # --- 1-hour rolling rates ---
    HovalConnectionSensorDescription(
        key="api_failure_rate_1h",
        translation_key="api_failure_rate_1h",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-percent-outline",
        # None until the first poll in the 1-hour window is recorded.
        value_fn=lambda h: h.failure_rate_1h,
    ),
    HovalConnectionSensorDescription(
        key="api_auth_failure_rate_1h",
        translation_key="api_auth_failure_rate_1h",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:lock-percent-outline",
        value_fn=lambda h: h.auth_failure_rate_1h,
    ),
    HovalConnectionSensorDescription(
        key="api_availability_1h",
        translation_key="api_availability_1h",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-check-variant-outline",
        value_fn=lambda h: h.availability_1h,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities."""
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

            # API connection health sensors (one set per plant, attached to plant device)
            for description in CONNECTION_SENSOR_DESCRIPTIONS:
                uid = f"{plant_id}_{description.key}"
                if uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalConnectionSensor(coordinator, plant_id, plant_data, description)
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
        """Return the sensor value."""
        circuit = self._circuit
        if circuit is None:
            return None
        val = self.entity_description.value_fn(circuit)
        if val is None:
            return None
        # String sensors (program names, operation mode) return as-is
        if self.entity_description.native_unit_of_measurement is None:
            return str(val)
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


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
    def native_value(self) -> datetime | float | str | None:
        """Return the sensor value."""
        plant = self._plant
        if plant is None:
            return None
        val = self.entity_description.value_fn(plant)
        if val is None:
            return None
        # TIMESTAMP sensors (e.g. latest_event_time) receive a raw ISO 8601
        # string from the API.  HA requires a datetime object for this device
        # class; returning a plain string causes state errors.
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP and isinstance(
            val, str
        ):
            return dt_util.parse_datetime(val)
        if self.entity_description.native_unit_of_measurement is None and not isinstance(
            val, (int, float)
        ):
            return str(val)
        try:
            return float(val) if isinstance(val, (int, float)) else val
        except (ValueError, TypeError):
            return None


class HovalConnectionSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Hoval API connection health sensor.

    Reads from coordinator.connection_health (a HovalConnectionHealth dataclass
    that persists across polls) rather than from coordinator.data.

    Stays available even when the last poll failed — surfacing failure counts,
    error messages, and latency data is the entire reason these sensors exist.
    Building automations on `api_consecutive_failures` lets you alert when the
    Hoval cloud is misbehaving without writing any template sensors.
    """

    _attr_has_entity_name = True
    entity_description: HovalConnectionSensorDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
        description: HovalConnectionSensorDescription,
    ) -> None:
        """Initialize the connection health sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{description.key}"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def available(self) -> bool:
        """Always available — tracking failures is the purpose of this sensor.

        CoordinatorEntity would set available=False whenever last_update_success
        is False, which is exactly when these sensors are most useful.
        """
        return True

    @property
    def native_value(self) -> datetime | float | int | str | None:
        """Return the health metric value."""
        health = self.coordinator.connection_health
        val = self.entity_description.value_fn(health)
        if val is None:
            return None
        # datetime values (TIMESTAMP device class) — return as-is; HA accepts them
        if isinstance(val, datetime):
            return val
        # Numeric values — return as-is
        if isinstance(val, (int, float)):
            return val
        # String values (error message, error type)
        return str(val)[:255]
