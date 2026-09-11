"""Sensor platform for Hoval Connect.

Independent user request (2026-09, after the original v1.0.0 telemetry
removal): removing ALL telemetry sensors in v1.0.0 (see
docs/audit-v1.0.0.md) turned out to go further than actually wanted — with
sensor.py gone entirely, there was zero at-a-glance current-value or
API-health visibility left in this integration at all, relying completely
on a separate CAN-bus integration for even a basic "what's the current
temperature" glance. This file is a deliberately narrow, curated revival,
not a full sensor.py restoration:

- Per-circuit CURRENT VALUE sensors (actual value + target/setpoint),
  for HK/WW (temperature) and HV (air volume %) circuits. These use
  `circuit.actual_value`/`circuit.target_value`, which are ALREADY part of
  the circuits-list response this integration fetches for control purposes
  regardless (see the "more" audit report, HVC-003 in
  docs/audit-v1.0.0.md) — zero additional API calls, zero change to the
  polling architecture. BL circuits are excluded: their actual/target
  values are consistently null/meaningless in practice.
- API HEALTH diagnostic sensors (last success, poll latency, failure
  rate, last error type) reading straight from the coordinator's already-
  computed `connection_health` — again nothing new fetched, just exposed.

Explicitly NOT restored: live_values (get_live_values), weather
(get_weather), events (get_plant_events/get_latest_event), energy/hours
counters, or anything else that would require reintroducing scheduled
telemetry polling. Those remain intentionally out of scope — see
docs/audit-v1.0.0.md for why.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .const import CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, CIRCUIT_TYPE_WW
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator, HovalPlantData

# Circuit types whose actual_value/target_value are meaningful enough to
# show as sensors, and the unit each one is expressed in. BL is
# deliberately excluded — its values are consistently null/0.0 in
# practice, per live diagnostics data, not just in theory.
_VALUE_UNIT_BY_CIRCUIT_TYPE: dict[str, str] = {
    CIRCUIT_TYPE_HK: UnitOfTemperature.CELSIUS,
    CIRCUIT_TYPE_WW: UnitOfTemperature.CELSIUS,
    CIRCUIT_TYPE_HV: PERCENTAGE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval sensor entities."""
    coordinator = entry.runtime_data.coordinator
    plant_devices = entry.runtime_data.plant_devices
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device_id = plant_devices.async_get_device_id(plant_id, plant_data)

            uid_last_success = f"{plant_id}_api_last_success"
            uid_latency = f"{plant_id}_api_poll_latency"
            uid_failure_rate = f"{plant_id}_api_failure_rate"
            uid_last_error = f"{plant_id}_api_last_error"
            if uid_last_success not in known:
                known.add(uid_last_success)
                entities.append(HovalApiLastSuccess(coordinator, plant_id, plant_data))
            if uid_latency not in known:
                known.add(uid_latency)
                entities.append(HovalApiPollLatency(coordinator, plant_id, plant_data))
            if uid_failure_rate not in known:
                known.add(uid_failure_rate)
                entities.append(HovalApiFailureRate(coordinator, plant_id, plant_data))
            if uid_last_error not in known:
                known.add(uid_last_error)
                entities.append(HovalApiLastError(coordinator, plant_id, plant_data))

            for path, circuit in plant_data.circuits.items():
                if circuit.circuit_type not in _VALUE_UNIT_BY_CIRCUIT_TYPE:
                    continue
                uid_actual = f"{plant_id}_{path}_actual_value"
                uid_target = f"{plant_id}_{path}_target_value"
                if uid_actual not in known:
                    known.add(uid_actual)
                    entities.append(
                        HovalCircuitActualValue(
                            coordinator, plant_id, plant_device_id, path, circuit
                        )
                    )
                if uid_target not in known:
                    known.add(uid_target)
                    entities.append(
                        HovalCircuitTargetValue(
                            coordinator, plant_id, plant_device_id, path, circuit
                        )
                    )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class _HovalCircuitValueSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Shared base for the two per-circuit current-value sensors."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_device_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_device_info = circuit_device_info(plant_id, plant_device_id, circuit_data)
        unit = _VALUE_UNIT_BY_CIRCUIT_TYPE.get(circuit_data.circuit_type)
        self._attr_native_unit_of_measurement = unit
        if unit == UnitOfTemperature.CELSIUS:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Unavailable if the circuit itself has disappeared from the coordinator."""
        return super().available and self._circuit is not None


class HovalCircuitActualValue(_HovalCircuitValueSensor):
    """Current (actual) temperature or air-volume reading for a circuit.

    Sourced from `circuit.actual_value` — free, already part of the
    circuits-list response fetched for control purposes; see this file's
    module docstring.
    """

    _attr_translation_key = "circuit_actual_value"

    def __init__(self, coordinator, plant_id, plant_device_id, circuit_path, circuit_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_device_id, circuit_path, circuit_data)
        self._attr_unique_id = f"{plant_id}_{circuit_path}_actual_value"

    @property
    def native_value(self) -> float | None:
        """Return the circuit's current actual value."""
        circuit = self._circuit
        return circuit.actual_value if circuit is not None else None


class HovalCircuitTargetValue(_HovalCircuitValueSensor):
    """Current target/setpoint temperature or air-volume for a circuit.

    Sourced from `circuit.target_value` — free, already part of the
    circuits-list response fetched for control purposes (and for writes);
    see this file's module docstring. Diagnostic category: this duplicates
    what each circuit's own climate/fan/water_heater entity already shows
    as its target, so it's kept out of the main dashboard by default.
    """

    _attr_translation_key = "circuit_target_value"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, plant_id, plant_device_id, circuit_path, circuit_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_device_id, circuit_path, circuit_data)
        self._attr_unique_id = f"{plant_id}_{circuit_path}_target_value"

    @property
    def native_value(self) -> float | None:
        """Return the circuit's current target value."""
        circuit = self._circuit
        return circuit.target_value if circuit is not None else None


class _HovalApiHealthSensor(CoordinatorEntity[HovalDataCoordinator], SensorEntity):
    """Shared base for the four API-health diagnostic sensors.

    All four read straight from the coordinator's already-computed
    `connection_health` — nothing new is fetched. Always available, like
    binary_sensor.py's HovalCloudApiProblem: these ARE the diagnostics
    about API availability, so they should not themselves go unavailable
    exactly when the API is having trouble.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def available(self) -> bool:
        """Always available — see class docstring."""
        return True


class HovalApiLastSuccess(_HovalApiHealthSensor):
    """Timestamp of the last successful poll (health check or write)."""

    _attr_translation_key = "api_last_success"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, plant_id, plant_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_data)
        self._attr_unique_id = f"{plant_id}_api_last_success"

    @property
    def native_value(self):
        """Return the last successful poll's timestamp."""
        return self.coordinator.connection_health.last_success


class HovalApiPollLatency(_HovalApiHealthSensor):
    """Most recent successful poll's round-trip latency, in milliseconds.

    Extra attributes carry the rolling average, p95, and EMA (smoothed)
    latency for anyone who wants more than the single latest sample.
    """

    _attr_translation_key = "api_poll_latency"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, plant_id, plant_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_data)
        self._attr_unique_id = f"{plant_id}_api_poll_latency"

    @property
    def native_value(self) -> float | None:
        """Return the most recent poll's latency."""
        return self.coordinator.connection_health.poll_latency_ms

    @property
    def extra_state_attributes(self) -> dict[str, float | None]:
        """Expose the rolling average/p95/EMA latency alongside the raw last value."""
        health = self.coordinator.connection_health
        return {
            "average_ms": health.avg_latency_ms,
            "p95_ms": health.p95_latency_ms,
            "ema_ms": health.ema_latency_ms,
        }


class HovalApiFailureRate(_HovalApiHealthSensor):
    """Rolling 1-hour poll failure rate, as a percentage.

    The rolling 1-hour window is more actionable than a since-startup
    average (exposed as an attribute instead) — a stretch of failures an
    hour ago shouldn't keep looking as bad as it did at the time, once
    things have recovered.
    """

    _attr_translation_key = "api_failure_rate"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, plant_id, plant_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_data)
        self._attr_unique_id = f"{plant_id}_api_failure_rate"

    @property
    def native_value(self) -> float | None:
        """Return the rolling 1-hour failure rate percentage."""
        return self.coordinator.connection_health.failure_rate_1h

    @property
    def extra_state_attributes(self) -> dict[str, float | int | None]:
        """Expose the since-startup overall rate and raw counters too."""
        health = self.coordinator.connection_health
        overall = (
            round(health.total_failures / health.total_polls * 100, 1)
            if health.total_polls
            else None
        )
        return {
            "overall_failure_rate_pct": overall,
            "total_polls": health.total_polls,
            "total_failures": health.total_failures,
        }


class HovalApiLastError(_HovalApiHealthSensor):
    """Type of the most recent API error, if any.

    Deliberately exposes only the error TYPE and its timestamp as an
    attribute — not the raw error message. Some error messages elsewhere
    in this integration embed a circuit path or plant ID; those are
    explicitly redacted in the diagnostics EXPORT (see diagnostics.py),
    and exposing the unredacted text here as a live entity attribute
    would defeat that — attributes are visible in the Logbook, History,
    and to anyone with dashboard access, not just a one-time export.
    """

    _attr_translation_key = "api_last_error"

    def __init__(self, coordinator, plant_id, plant_data):
        """Initialize the sensor."""
        super().__init__(coordinator, plant_id, plant_data)
        self._attr_unique_id = f"{plant_id}_api_last_error"

    @property
    def native_value(self) -> str:
        """Return the last error's type, or "none" if there hasn't been one."""
        return self.coordinator.connection_health.last_error_type or "none"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the last error's timestamp."""
        last_error_time = self.coordinator.connection_health.last_error_time
        return {"time": last_error_time.isoformat() if last_error_time else None}
