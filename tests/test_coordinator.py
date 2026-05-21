"""Tests for the Hoval Connect coordinator pure functions.

These tests cover pure utility functions that have no Home Assistant dependency.
They run without homeassistant installed via sys.path module mocking.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock

# Use setdefault so that if test_api.py already registered these mocks in the
# same pytest process we reuse them rather than overwriting (which would break
# modules that already imported from the first mock object).
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)
sys.modules.setdefault("aiohttp", ha_mock)
sys.modules.setdefault("voluptuous", ha_mock)

from custom_components.hoval_connect.coordinator import (  # noqa: E402
    _V1_PROGRAM_MAP,
    HovalCircuitData,
    HovalEventData,
    _is_problem_event,
    _parse_event,
    _resolve_active_program_value,
    resolve_fan_speed,
)


class TestResolveFanSpeed:
    """Tests for resolve_fan_speed()."""

    def test_none_circuit_returns_default(self):
        assert resolve_fan_speed(None) == 40

    def test_live_air_volume(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   live_values={"airVolume": "65"})
        assert resolve_fan_speed(circuit) == 65

    def test_live_air_volume_float(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   live_values={"airVolume": "72.5"})
        assert resolve_fan_speed(circuit) == 72

    def test_live_zero_falls_through(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   live_values={"airVolume": "0"}, target_value=50)
        assert resolve_fan_speed(circuit) == 50

    def test_target_value_fallback(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   target_value=80)
        assert resolve_fan_speed(circuit) == 80

    def test_program_air_volume_fallback(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   program_air_volume=55.0)
        assert resolve_fan_speed(circuit) == 55

    def test_all_none_returns_default(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T")
        assert resolve_fan_speed(circuit) == 40

    def test_minimum_is_default(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.2.3", name="T",
                                   live_values={"airVolume": "0"},
                                   target_value=0, program_air_volume=0.0)
        assert resolve_fan_speed(circuit) == 40


class TestResolveActiveProgramValue:
    """Tests for _resolve_active_program_value()."""

    def _make_programs(self, phases=None, day_name="Normal"):
        """Build a minimal programs structure with both week1 and week2."""
        if phases is None:
            phases = [
                {"start": {"hours": 6, "minutes": 0},
                 "end": {"hours": 22, "minutes": 0}, "value": 60},
                {"start": {"hours": 22, "minutes": 0},
                 "end": {"hours": 23, "minutes": 59}, "value": 30},
            ]
        return {
            "week1": {"name": "Woche 1", "dayProgramIds": [1, 1, 1, 1, 1, 2, 2]},
            "week2": {"name": "Woche 2", "dayProgramIds": [3, 3, 3, 3, 3, 3, 3]},
            "dayPrograms": {
                "dayConfigurations": [
                    {"id": 1, "name": day_name, "phases": phases},
                    {"id": 2, "name": "Weekend", "phases": [
                        {"start": {"hours": 8, "minutes": 0},
                         "end": {"hours": 22, "minutes": 0}, "value": 50},
                    ]},
                    {"id": 3, "name": "Week2Day", "phases": [
                        {"start": {"hours": 7, "minutes": 0},
                         "end": {"hours": 21, "minutes": 0}, "value": 75},
                    ]},
                ],
            },
        }

    def test_monday_morning_week1(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday
        week, day, value = _resolve_active_program_value(programs, now, "week1")
        assert week == "Woche 1" and day == "Normal" and value == 60

    def test_monday_night_week1(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 23, 30)
        week, day, value = _resolve_active_program_value(programs, now, "week1")
        assert week == "Woche 1" and day == "Normal" and value == 30

    def test_saturday_week1(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 13, 12, 0)  # Saturday
        week, day, value = _resolve_active_program_value(programs, now, "week1")
        assert week == "Woche 1" and day == "Weekend" and value == 50

    def test_week2_uses_week2_schedule(self):
        """Circuit running week2 must resolve against the week2 schedule."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday, inside Week2Day phase
        week, day, value = _resolve_active_program_value(programs, now, "week2")
        assert week == "Woche 2" and day == "Week2Day" and value == 75

    def test_non_schedule_programs_fall_back_to_week1(self):
        """constant/ecoMode/standby/None should all resolve against week1."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)
        for prog in ("constant", "ecoMode", "standby", None):
            week, _, _ = _resolve_active_program_value(programs, now, prog)
            assert week == "Woche 1", f"Expected week1 fallback for {prog!r}"

    def test_active_program_defaults_to_none(self):
        """active_program defaults to None → week1 fallback."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1" and value == 60

    def test_no_matching_phase(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 4, 0)  # 4 AM, before first phase
        week, day, value = _resolve_active_program_value(programs, now, "week1")
        assert week == "Woche 1" and day == "Normal" and value is None

    def test_empty_programs(self):
        week, day, value = _resolve_active_program_value({}, datetime(2024, 1, 8, 10, 0))
        assert week is None and day is None and value is None

    def test_empty_day_configurations(self):
        programs = {"dayPrograms": {"dayConfigurations": []}}
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 10, 0))
        assert week is None and day is None and value is None

    def test_phase_boundary_start(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 6, 0)  # exactly at phase start
        _, _, value = _resolve_active_program_value(programs, now, "week1")
        assert value == 60

    def test_phase_boundary_end(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 22, 0)  # exactly at next phase start
        _, _, value = _resolve_active_program_value(programs, now, "week1")
        assert value == 30


class TestV1ProgramMap:
    """Tests for _V1_PROGRAM_MAP normalization."""

    def test_tte_controlled_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("tteControlled", "tteControlled") == "week1"

    def test_time_programs_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("timePrograms", "timePrograms") == "week1"

    def test_v3_values_pass_through(self):
        for v3_key in ("week1", "week2", "ecoMode", "standby", "constant"):
            assert _V1_PROGRAM_MAP.get(v3_key, v3_key) == v3_key

    def test_none_passes_through(self):
        assert _V1_PROGRAM_MAP.get(None, None) is None


class TestParseEvent:
    """Tests for _parse_event() and HovalEventData."""

    def test_parse_full_event(self):
        raw = {
            "eventType": "warning",
            "description": "Filterwechsel erforderlich",
            "timeOccurred": "2026-02-17T10:30:00Z",
            "timeResolved": None,
            "sourcePath": "520.50.0",
            "code": 12345,
        }
        ev = _parse_event(raw)
        assert ev.event_type == "warning"
        assert ev.description == "Filterwechsel erforderlich"
        assert ev.time_occurred == "2026-02-17T10:30:00Z"
        assert ev.time_resolved is None
        assert ev.source_path == "520.50.0"
        assert ev.code == 12345

    def test_active_when_not_resolved(self):
        ev = _parse_event({"eventType": "warning", "timeResolved": None})
        assert ev.is_active is True

    def test_inactive_when_resolved(self):
        ev = _parse_event({"eventType": "warning", "timeResolved": "2026-02-17T12:00:00Z"})
        assert ev.is_active is False

    def test_active_when_time_resolved_missing(self):
        ev = _parse_event({"eventType": "blocking"})
        assert ev.is_active is True

    def test_parse_empty_dict(self):
        ev = _parse_event({})
        assert ev.event_type is None and ev.is_active is True


class TestIsProblemEvent:
    """Tests for problem event classification."""

    def test_active_blocking_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="blocking")) is True

    def test_active_locking_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="locking")) is True

    def test_active_warning_is_problem(self):
        assert _is_problem_event(HovalEventData(event_type="warning")) is True

    def test_resolved_warning_is_not_problem(self):
        ev = HovalEventData(event_type="warning", time_resolved="2026-02-17T12:00:00Z")
        assert _is_problem_event(ev) is False

    def test_info_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type="info")) is False

    def test_none_is_not_problem(self):
        assert _is_problem_event(None) is False

    def test_none_event_type_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type=None)) is False
