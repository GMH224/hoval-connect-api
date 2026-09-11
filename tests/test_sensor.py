"""Tests for sensor.py — the deliberately narrow telemetry revival.

Independent user request (2026-09): after the original v1.0.0 telemetry
removal, sensor.py was recreated with two things — per-circuit current-
value sensors (actual_value/target_value, already-fetched data) and
API-health diagnostics (already-computed connection_health data). This
file exercises the entity classes directly (bypassing async_setup_entry,
consistent with this project's existing entity-testing approach), since
constructing them needs only a coordinator/plant_data/circuit_data, not a
full Home Assistant instance.

`.available` is deliberately not exercised here: it calls
`super().available`, which the shared test-harness stub
(StubCoordinatorEntity in tests/ha_stubs.py) does not implement — a
pre-existing gap shared by every other entity platform file in this
project (climate.py, fan.py, number.py, select.py, water_heater.py all
have the identical limitation), not something new to this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from custom_components.hoval_connect.sensor import (
    HovalApiFailureRate,
    HovalApiLastError,
    HovalApiLastSuccess,
    HovalApiPollLatency,
    HovalCircuitActualValue,
    HovalCircuitTargetValue,
)


@dataclass
class _FakeCircuit:
    circuit_type: str
    path: str
    name: str
    actual_value: float | None = None
    target_value: float | None = None


@dataclass
class _FakePlant:
    plant_id: str
    name: str
    circuits: dict[str, _FakeCircuit] = field(default_factory=dict)


@dataclass
class _FakeData:
    plants: dict[str, _FakePlant] = field(default_factory=dict)


class _FakeConnectionHealth:
    def __init__(self, **overrides) -> None:
        self.last_success = overrides.get("last_success")
        self.poll_latency_ms = overrides.get("poll_latency_ms")
        self.avg_latency_ms = overrides.get("avg_latency_ms")
        self.p95_latency_ms = overrides.get("p95_latency_ms")
        self.ema_latency_ms = overrides.get("ema_latency_ms")
        self.failure_rate_1h = overrides.get("failure_rate_1h")
        self.total_polls = overrides.get("total_polls", 0)
        self.total_failures = overrides.get("total_failures", 0)
        self.last_error_type = overrides.get("last_error_type")
        self.last_error_time = overrides.get("last_error_time")


class _FakeCoordinator:
    def __init__(self, data: _FakeData, health: _FakeConnectionHealth | None = None) -> None:
        self.data = data
        self.connection_health = health or _FakeConnectionHealth()


def _make_coordinator(circuit: _FakeCircuit, plant_id: str = "p1") -> _FakeCoordinator:
    plant = _FakePlant(plant_id=plant_id, name="Home", circuits={circuit.path: circuit})
    return _FakeCoordinator(_FakeData(plants={plant_id: plant}))


class TestHovalCircuitActualValue:
    def test_returns_circuits_actual_value(self):
        circuit = _FakeCircuit(circuit_type="HK", path="hk-1", name="Heating", actual_value=21.5)
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitActualValue(coordinator, "p1", "device-1", "hk-1", circuit)

        assert sensor.native_value == 21.5

    def test_returns_none_when_circuit_disappears(self):
        circuit = _FakeCircuit(circuit_type="HK", path="hk-1", name="Heating", actual_value=21.5)
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitActualValue(coordinator, "p1", "device-1", "hk-1", circuit)

        coordinator.data.plants["p1"].circuits.clear()
        assert sensor.native_value is None

    def test_unique_id_includes_plant_and_path(self):
        circuit = _FakeCircuit(circuit_type="HK", path="hk-1", name="Heating")
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitActualValue(coordinator, "p1", "device-1", "hk-1", circuit)

        assert sensor.unique_id == "p1_hk-1_actual_value"

    def test_hv_circuit_uses_percentage_unit(self):
        from homeassistant.const import PERCENTAGE

        circuit = _FakeCircuit(circuit_type="HV", path="hv-1", name="Ventilation")
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitActualValue(coordinator, "p1", "device-1", "hv-1", circuit)

        assert sensor._attr_native_unit_of_measurement == PERCENTAGE

    def test_hk_circuit_uses_celsius_and_temperature_device_class(self):
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfTemperature

        circuit = _FakeCircuit(circuit_type="HK", path="hk-1", name="Heating")
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitActualValue(coordinator, "p1", "device-1", "hk-1", circuit)

        assert sensor._attr_native_unit_of_measurement == UnitOfTemperature.CELSIUS
        assert sensor._attr_device_class == SensorDeviceClass.TEMPERATURE


class TestHovalCircuitTargetValue:
    def test_returns_circuits_target_value(self):
        circuit = _FakeCircuit(circuit_type="WW", path="ww-1", name="Hot water", target_value=48.0)
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitTargetValue(coordinator, "p1", "device-1", "ww-1", circuit)

        assert sensor.native_value == 48.0

    def test_unique_id_includes_plant_and_path(self):
        circuit = _FakeCircuit(circuit_type="WW", path="ww-1", name="Hot water")
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitTargetValue(coordinator, "p1", "device-1", "ww-1", circuit)

        assert sensor.unique_id == "p1_ww-1_target_value"

    def test_is_diagnostic_category(self):
        from homeassistant.const import EntityCategory

        circuit = _FakeCircuit(circuit_type="WW", path="ww-1", name="Hot water")
        coordinator = _make_coordinator(circuit)
        sensor = HovalCircuitTargetValue(coordinator, "p1", "device-1", "ww-1", circuit)

        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC


class TestHovalApiLastSuccess:
    def test_returns_connection_healths_last_success(self):
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        health = _FakeConnectionHealth(last_success=ts)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiLastSuccess(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value == ts

    def test_none_when_never_succeeded(self):
        coordinator = _FakeCoordinator(_FakeData(), _FakeConnectionHealth())
        sensor = HovalApiLastSuccess(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value is None


class TestHovalApiPollLatency:
    def test_native_value_is_last_latency(self):
        health = _FakeConnectionHealth(poll_latency_ms=850.0, avg_latency_ms=900.0)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiPollLatency(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value == 850.0

    def test_attributes_expose_average_p95_and_ema(self):
        health = _FakeConnectionHealth(
            poll_latency_ms=850.0, avg_latency_ms=900.0, p95_latency_ms=1200.0, ema_latency_ms=875.5
        )
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiPollLatency(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        attrs = sensor.extra_state_attributes
        assert attrs["average_ms"] == 900.0
        assert attrs["p95_ms"] == 1200.0
        assert attrs["ema_ms"] == 875.5


class TestHovalApiFailureRate:
    def test_native_value_is_rolling_1h_rate(self):
        health = _FakeConnectionHealth(failure_rate_1h=5.0)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiFailureRate(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value == 5.0

    def test_overall_rate_attribute_computed_from_totals(self):
        health = _FakeConnectionHealth(failure_rate_1h=0.0, total_polls=200, total_failures=10)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiFailureRate(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        attrs = sensor.extra_state_attributes
        assert attrs["overall_failure_rate_pct"] == 5.0
        assert attrs["total_polls"] == 200
        assert attrs["total_failures"] == 10

    def test_overall_rate_attribute_none_when_never_polled(self):
        health = _FakeConnectionHealth(total_polls=0)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiFailureRate(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.extra_state_attributes["overall_failure_rate_pct"] is None


class TestHovalApiLastError:
    def test_reports_none_string_when_no_error_ever_occurred(self):
        health = _FakeConnectionHealth(last_error_type=None)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiLastError(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value == "none"

    def test_reports_the_error_type_when_one_occurred(self):
        health = _FakeConnectionHealth(last_error_type="timeout")
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiLastError(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.native_value == "timeout"

    def test_time_attribute_is_isoformatted(self):
        ts = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        health = _FakeConnectionHealth(last_error_type="api", last_error_time=ts)
        coordinator = _FakeCoordinator(_FakeData(), health)
        sensor = HovalApiLastError(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.extra_state_attributes["time"] == ts.isoformat()

    def test_time_attribute_none_when_no_error(self):
        coordinator = _FakeCoordinator(_FakeData(), _FakeConnectionHealth())
        sensor = HovalApiLastError(coordinator, "p1", _FakePlant(plant_id="p1", name="Home"))

        assert sensor.extra_state_attributes["time"] is None

    def test_last_error_sensor_does_not_expose_raw_message(self):
        """Deliberate scope guard: only type + time are exposed, never the
        raw error message text (which can embed a circuit path or plant ID
        in some _LOGGER call sites elsewhere in this integration) — see
        HovalApiLastError's class docstring.
        """
        import inspect

        source = inspect.getsource(HovalApiLastError)
        assert "last_error_msg" not in source
