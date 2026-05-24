"""Tests for the Hoval Connect coordinator logic (pure functions).

These tests cover the pure utility functions that don't depend on Home Assistant.
They can be run without homeassistant installed by using sys.path manipulation.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock

# Mock homeassistant modules so we can import the coordinator's pure functions
ha_mock = MagicMock()
sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.config_entries"] = ha_mock
sys.modules["homeassistant.const"] = ha_mock
sys.modules["homeassistant.core"] = ha_mock
sys.modules["homeassistant.exceptions"] = ha_mock
sys.modules["homeassistant.helpers"] = ha_mock
sys.modules["homeassistant.helpers.update_coordinator"] = ha_mock
sys.modules["homeassistant.helpers.aiohttp_client"] = ha_mock
sys.modules["homeassistant.helpers.device_registry"] = ha_mock
sys.modules["homeassistant.helpers.dispatcher"] = ha_mock
sys.modules["homeassistant.util"] = ha_mock
sys.modules["homeassistant.util.dt"] = ha_mock
sys.modules["aiohttp"] = ha_mock
sys.modules["voluptuous"] = ha_mock

# Now we can import the pure functions and dataclasses
from custom_components.hoval_connect.coordinator import (  # noqa: E402
    _V1_PROGRAM_MAP,
    HovalCircuitData,
    HovalCircuitHealth,
    HovalConnectionHealth,
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
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "65"},
        )
        assert resolve_fan_speed(circuit) == 65

    def test_live_air_volume_float(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "72.5"},
        )
        assert resolve_fan_speed(circuit) == 72

    def test_live_zero_falls_through(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "0"},
            target_value=50,
        )
        assert resolve_fan_speed(circuit) == 50

    def test_target_value_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            target_value=80,
        )
        assert resolve_fan_speed(circuit) == 80

    def test_program_air_volume_fallback(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            program_air_volume=55.0,
        )
        assert resolve_fan_speed(circuit) == 55

    def test_all_none_returns_default(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
        )
        assert resolve_fan_speed(circuit) == 40

    def test_minimum_is_one(self):
        circuit = HovalCircuitData(
            circuit_type="HV",
            path="1.2.3",
            name="Test",
            live_values={"airVolume": "0"},
            target_value=0,
            program_air_volume=0.0,
        )
        assert resolve_fan_speed(circuit) == 40  # falls through to default


class TestResolveActiveProgramValue:
    """Tests for _resolve_active_program_value()."""

    def _make_programs(
        self,
        phases: list[dict] | None = None,
        day_name: str = "Normal",
    ) -> dict:
        """Build a minimal programs structure."""
        if phases is None:
            phases = [
                {
                    "start": {"hours": 6, "minutes": 0},
                    "end": {"hours": 22, "minutes": 0},
                    "value": 60,
                },
                {
                    "start": {"hours": 22, "minutes": 0},
                    "end": {"hours": 23, "minutes": 59},
                    "value": 30,
                },
            ]
        return {
            "week1": {
                "name": "Woche 1",
                "dayProgramIds": [1, 1, 1, 1, 1, 2, 2],  # Mon-Fri=1, Sat-Sun=2
            },
            "dayPrograms": {
                "dayConfigurations": [
                    {"id": 1, "name": day_name, "phases": phases},
                    {
                        "id": 2,
                        "name": "Weekend",
                        "phases": [
                            {
                                "start": {"hours": 8, "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 50,
                            },
                        ],
                    },
                ],
            },
        }

    def test_monday_morning(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 60

    def test_monday_night(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 23, 30)  # Monday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 30

    def test_saturday(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 13, 12, 0)  # Saturday
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Weekend"
        assert value == 50

    def test_week2_active_program_uses_week2_schedule(self):
        """Bug fix: week2 users were always resolved against week1 schedule."""
        programs = self._make_programs()
        # week2 has a different name and dayProgramIds than week1
        programs["week2"] = {
            "name": "Woche 2",
            "dayProgramIds": [2, 2, 2, 2, 2, 2, 2],  # All days → Weekend day config
        }
        now = datetime(2024, 1, 8, 12, 0)  # Monday
        week, day, value = _resolve_active_program_value(
            programs, now, active_program="week2"
        )
        assert week == "Woche 2"
        assert day == "Weekend"
        assert value == 50  # Weekend phase value

    def test_week1_active_program_explicit(self):
        """Passing active_program='week1' explicitly behaves identically to default."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)  # Monday
        week, day, value = _resolve_active_program_value(
            programs, now, active_program="week1"
        )
        assert week == "Woche 1"
        assert value == 60

    def test_none_active_program_defaults_to_week1(self):
        """active_program=None (default) still resolves week1."""
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now, active_program=None)
        assert week == "Woche 1"

    def test_no_matching_phase(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 4, 0)  # Monday 4 AM
        week, day, value = _resolve_active_program_value(programs, now)
        assert week == "Woche 1"
        assert day == "Normal"
        assert value is None

    def test_empty_programs(self):
        programs = {}
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week is None
        assert day is None
        assert value is None

    def test_empty_day_configurations(self):
        programs = {"dayPrograms": {"dayConfigurations": []}}
        now = datetime(2024, 1, 8, 10, 0)
        week, day, value = _resolve_active_program_value(programs, now)
        assert week is None
        assert day is None
        assert value is None

    def test_phase_boundary_start(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 6, 0)  # Exactly at phase start
        week, day, value = _resolve_active_program_value(programs, now)
        assert value == 60

    def test_phase_boundary_end(self):
        programs = self._make_programs()
        now = datetime(2024, 1, 8, 22, 0)  # Exactly at phase end/next start
        week, day, value = _resolve_active_program_value(programs, now)
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
        """If API doesn't return timeResolved at all, event is active."""
        ev = _parse_event({"eventType": "blocking"})
        assert ev.is_active is True

    def test_parse_empty_dict(self):
        ev = _parse_event({})
        assert ev.event_type is None
        assert ev.description is None
        assert ev.time_occurred is None
        assert ev.time_resolved is None
        assert ev.source_path is None
        assert ev.code is None
        assert ev.is_active is True  # no timeResolved → active

    def test_default_event_data_is_active(self):
        """Default HovalEventData has no timeResolved so is active."""
        ev = HovalEventData()
        assert ev.is_active is True

    def test_resolved_event_data(self):
        ev = HovalEventData(time_resolved="2026-02-17T12:00:00Z")
        assert ev.is_active is False


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

    def test_info_and_offline_are_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type="info")) is False
        assert _is_problem_event(HovalEventData(event_type="offline")) is False

    def test_none_is_not_problem(self):
        assert _is_problem_event(None) is False

    def test_none_event_type_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type=None)) is False


class TestHovalCircuitHealth:
    """Tests for HovalCircuitHealth — per-circuit reliability tracking."""

    def test_initial_state(self):
        ch = HovalCircuitHealth()
        assert ch.total_polls == 0
        assert ch.total_failures == 0
        assert ch.consecutive_failures == 0
        assert ch.failure_rate_1h is None  # no data yet
        assert ch.availability_1h is None

    def test_record_success(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ts = datetime.now(timezone.utc)
        ch.record_success(ts)
        assert ch.total_polls == 1
        assert ch.total_failures == 0
        assert ch.consecutive_failures == 0
        assert ch.last_success == ts
        assert ch.failure_rate_1h == 0.0
        assert ch.availability_1h == 100.0

    def test_record_failure(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ts = datetime.now(timezone.utc)
        ch.record_failure(ts, "HTTP 503")
        assert ch.total_polls == 1
        assert ch.total_failures == 1
        assert ch.consecutive_failures == 1
        assert ch.last_failure == ts
        assert ch.last_error == "HTTP 503"
        assert ch.failure_rate_1h == 100.0
        assert ch.availability_1h == 0.0

    def test_consecutive_failures_resets_on_success(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ts = datetime.now(timezone.utc)
        ch.record_failure(ts, "err")
        ch.record_failure(ts, "err")
        assert ch.consecutive_failures == 2
        ch.record_success(ts)
        assert ch.consecutive_failures == 0
        assert ch.total_failures == 2  # cumulative doesn't reset

    def test_mixed_polls_failure_rate(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ts = datetime.now(timezone.utc)
        for _ in range(8):
            ch.record_success(ts)
        for _ in range(2):
            ch.record_failure(ts, "err")
        assert ch.failure_rate_1h == 20.0
        assert ch.availability_1h == 80.0

    def test_error_truncated_to_200_chars(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ch.record_failure(datetime.now(timezone.utc), "x" * 300)
        assert len(ch.last_error) == 200

    def test_to_store_dict_only_cumulative(self):
        from datetime import timezone
        ch = HovalCircuitHealth()
        ch.record_success(datetime.now(timezone.utc))
        ch.record_failure(datetime.now(timezone.utc), "err")
        d = ch.to_store_dict()
        assert set(d.keys()) == {"total_polls", "total_failures"}
        assert d["total_polls"] == 2
        assert d["total_failures"] == 1

    def test_restore_from_store(self):
        ch = HovalCircuitHealth()
        ch.restore_from_store({"total_polls": 500, "total_failures": 42})
        assert ch.total_polls == 500
        assert ch.total_failures == 42
        assert ch.consecutive_failures == 0  # not restored — session-only

    def test_restore_ignores_bad_keys(self):
        ch = HovalCircuitHealth()
        ch.restore_from_store({"total_polls": "not-a-number"})
        assert ch.total_polls == 0  # int() of "not-a-number" raises → falls back to 0


class TestHovalConnectionHealthV2:
    """Tests for v0.16.0 additions to HovalConnectionHealth."""

    def _make_health(self):
        from datetime import timezone
        return HovalConnectionHealth()

    def test_ema_initialises_on_first_sample(self):
        h = self._make_health()
        assert h.ema_latency_ms is None
        h.update_ema(200.0)
        assert h.ema_latency_ms == 200.0

    def test_ema_converges_toward_new_value(self):
        h = self._make_health()
        h.update_ema(1000.0)
        # Feed 100 ms samples — EMA should drift down from 1000
        for _ in range(50):
            h.update_ema(100.0)
        assert h.ema_latency_ms < 200.0  # well below starting value

    def test_ema_is_smooth(self):
        """A single spike should not dominate the EMA."""
        h = self._make_health()
        for _ in range(20):
            h.update_ema(100.0)
        baseline = h.ema_latency_ms
        h.update_ema(10000.0)  # spike
        assert h.ema_latency_ms < 1500.0  # 1090 expected (0.1*10000+0.9*100); 1500 is a conservative bound

    def test_record_error_helper(self):
        from datetime import timezone
        h = self._make_health()
        ts = datetime.now(timezone.utc)
        h._record_error(ts, "timeout", "Poll timeout after 90 s")
        assert h.consecutive_failures == 1
        assert h.total_failures == 1
        assert h.auth_failures == 0
        assert h.last_error_type == "timeout"
        assert h.last_error_msg == "Poll timeout after 90 s"
        assert h.error_counts == {"timeout": 1}

    def test_record_error_auth_flag(self):
        from datetime import timezone
        h = self._make_health()
        ts = datetime.now(timezone.utc)
        h._record_error(ts, "auth", "Bad token", is_auth=True)
        assert h.auth_failures == 1
        assert h.error_counts == {"auth": 1}

    def test_error_counts_accumulate(self):
        from datetime import timezone
        h = self._make_health()
        ts = datetime.now(timezone.utc)
        h._record_error(ts, "timeout", "t")
        h._record_error(ts, "timeout", "t")
        h._record_error(ts, "api", "a")
        assert h.error_counts == {"timeout": 2, "api": 1}

    def test_circuit_health_lazy_creation(self):
        h = self._make_health()
        assert "1.2.3" not in h._circuit_health
        ch = h.get_circuit_health("1.2.3")
        assert isinstance(ch, HovalCircuitHealth)
        # Same object on subsequent calls
        assert h.get_circuit_health("1.2.3") is ch

    def test_to_store_dict_includes_circuits(self):
        from datetime import timezone
        h = self._make_health()
        h.total_polls = 10
        h.ema_latency_ms = 250.0
        ch = h.get_circuit_health("1.2.3")
        ch.record_success(datetime.now(timezone.utc))
        d = h.to_store_dict()
        assert d["total_polls"] == 10
        assert d["ema_latency_ms"] == 250.0
        assert "1.2.3" in d["circuits"]
        assert d["circuits"]["1.2.3"]["total_polls"] == 1

    def test_restore_from_store_full(self):
        h = self._make_health()
        data = {
            "total_polls": 1000,
            "total_failures": 50,
            "auth_failures": 3,
            "error_counts": {"timeout": 10, "api": 40},
            "ema_latency_ms": 350.5,
            "circuits": {
                "2.3.4": {"total_polls": 200, "total_failures": 5},
            },
        }
        h.restore_from_store(data)
        assert h.total_polls == 1000
        assert h.total_failures == 50
        assert h.auth_failures == 3
        assert h.error_counts == {"timeout": 10, "api": 40}
        assert h.ema_latency_ms == 350.5
        assert h._circuit_health["2.3.4"].total_polls == 200
        assert h._circuit_health["2.3.4"].total_failures == 5

    def test_restore_strips_unknown_error_types(self):
        h = self._make_health()
        h.restore_from_store({
            "error_counts": {"timeout": 1, "INJECTION_ATTACK": 99, "api": 2},
        })
        assert "INJECTION_ATTACK" not in h.error_counts
        assert h.error_counts == {"timeout": 1, "api": 2}

    def test_restore_ignores_bad_ema(self):
        h = self._make_health()
        h.restore_from_store({"ema_latency_ms": -5})
        assert h.ema_latency_ms is None  # negative value rejected

    def test_as_diagnostic_dict_structure(self):
        h = self._make_health()
        d = h.as_diagnostic_dict()
        assert "last_success" in d
        assert "last_error" in d
        assert "counters_since_startup" in d
        assert "rolling_1h_window" in d
        assert "latency_ms" in d
        assert "circuits" in d
        assert "ema" in d["latency_ms"]
        assert "error_counts" in d["counters_since_startup"]

    def test_as_diagnostic_dict_circuit_section(self):
        from datetime import timezone
        h = self._make_health()
        ch = h.get_circuit_health("5.6.7")
        ch.record_success(datetime.now(timezone.utc))
        d = h.as_diagnostic_dict()
        assert "5.6.7" in d["circuits"]
        assert d["circuits"]["5.6.7"]["total_polls"] == 1

    def test_persist_roundtrip(self):
        """to_store_dict() → restore_from_store() is a lossless roundtrip."""
        from datetime import timezone
        h = self._make_health()
        h.total_polls = 42
        h.total_failures = 7
        h.auth_failures = 2
        h.error_counts = {"auth": 2, "timeout": 5}
        h.ema_latency_ms = 123.4
        ch = h.get_circuit_health("9.9.9")
        ch.total_polls = 10
        ch.total_failures = 1

        stored = h.to_store_dict()
        h2 = HovalConnectionHealth()
        h2.restore_from_store(stored)

        assert h2.total_polls == 42
        assert h2.total_failures == 7
        assert h2.auth_failures == 2
        assert h2.error_counts == {"auth": 2, "timeout": 5}
        assert h2.ema_latency_ms == 123.4
        assert h2._circuit_health["9.9.9"].total_polls == 10
