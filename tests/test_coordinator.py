"""Tests for the Hoval Connect coordinator logic (pure functions).

These tests cover the pure utility functions that don't depend on Home Assistant.
They can be run without homeassistant installed by using sys.path manipulation.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
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

# ---------------------------------------------------------------------------
# _resolve_active_program_value
# ---------------------------------------------------------------------------


class TestResolveActiveProgramValue:
    """Tests for _resolve_active_program_value()."""

    def _make_programs(self, phases: list[dict] | None = None, day_name: str = "Normal") -> dict:
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
            "week1": {"name": "Woche 1", "dayProgramIds": [1, 1, 1, 1, 1, 2, 2]},
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

    # --- Normal operation ---

    def test_monday_morning(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 10, 0))
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 60

    def test_monday_night(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 23, 30))
        assert week == "Woche 1"
        assert day == "Normal"
        assert value == 30

    def test_saturday_uses_weekend_config(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 13, 12, 0))
        assert week == "Woche 1"
        assert day == "Weekend"
        assert value == 50

    def test_week2_active_program_uses_week2_schedule(self):
        """week2 users must not be resolved against week1 schedule (regression)."""
        programs = self._make_programs()
        programs["week2"] = {
            "name": "Woche 2",
            "dayProgramIds": [2, 2, 2, 2, 2, 2, 2],  # all days → Weekend
        }
        week, day, value = _resolve_active_program_value(
            programs, datetime(2024, 1, 8, 12, 0), active_program="week2"
        )
        assert week == "Woche 2"
        assert day == "Weekend"
        assert value == 50

    def test_week1_active_program_explicit(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(
            programs, datetime(2024, 1, 8, 10, 0), active_program="week1"
        )
        assert week == "Woche 1"
        assert value == 60

    def test_none_active_program_defaults_to_week1(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(
            programs, datetime(2024, 1, 8, 10, 0), active_program=None
        )
        assert week == "Woche 1"

    def test_no_matching_phase_returns_none_value(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 4, 0))
        assert week == "Woche 1"
        assert day == "Normal"
        assert value is None

    def test_phase_boundary_start_inclusive(self):
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 6, 0))
        assert value == 60

    def test_phase_boundary_end_exclusive(self):
        """End minute is exclusive: time exactly at 22:00 enters the next phase."""
        programs = self._make_programs()
        week, day, value = _resolve_active_program_value(programs, datetime(2024, 1, 8, 22, 0))
        assert value == 30

    def test_empty_programs_dict(self):
        week, day, value = _resolve_active_program_value({}, datetime(2024, 1, 8, 10, 0))
        assert (week, day, value) == (None, None, None)

    def test_empty_day_configurations(self):
        week, day, value = _resolve_active_program_value(
            {"dayPrograms": {"dayConfigurations": []}}, datetime(2024, 1, 8, 10, 0)
        )
        assert (week, day, value) == (None, None, None)

    # --- Non-dict programs: regression tests for v0.16.x bugs ---

    def test_none_programs_returns_all_none(self):
        """programs=None (HTTP 204 / empty body) must not raise AttributeError.

        Hoval's API started returning HTTP 204 for non-programmable circuits
        (e.g. BL/boiler) in May 2026.  _request() maps 204 → Python None.
        """
        week, day, value = _resolve_active_program_value(None, datetime(2024, 1, 8, 10, 0))
        assert (week, day, value) == (None, None, None)

    def test_empty_list_programs_returns_all_none(self):
        """programs=[] (HTTP 200 with body []) must not raise AttributeError.

        PRIMARY regression fixed in v0.16.2 / v0.17.0.  Hoval's May 2026
        change made the programs endpoint return [] for non-programmable circuits.
        v0.16.1 guarded only against None; [] passed the old guard and crashed
        at [].get('dayPrograms', {}) → AttributeError → BL silently dropped.
        """
        week, day, value = _resolve_active_program_value([], datetime(2024, 1, 8, 10, 0))
        assert (week, day, value) == (None, None, None)

    def test_int_programs_returns_all_none(self):
        """Any non-dict value must be handled gracefully (defensive)."""
        week, day, value = _resolve_active_program_value(42, datetime(2024, 1, 8, 10, 0))
        assert (week, day, value) == (None, None, None)

    def test_string_programs_returns_all_none(self):
        week, day, value = _resolve_active_program_value("programs", datetime(2024, 1, 8, 10, 0))
        assert (week, day, value) == (None, None, None)


# ---------------------------------------------------------------------------
# resolve_fan_speed
# ---------------------------------------------------------------------------


class TestResolveFanSpeed:
    """Tests for resolve_fan_speed()."""

    def _circuit(self, **kwargs) -> HovalCircuitData:
        return HovalCircuitData(circuit_type="HV", path="1.2.3", name="Test", **kwargs)

    def test_none_circuit_returns_default(self):
        assert resolve_fan_speed(None) == 40

    def test_live_air_volume(self):
        assert resolve_fan_speed(self._circuit(live_values={"airVolume": "65"})) == 65

    def test_live_air_volume_float_truncates(self):
        assert resolve_fan_speed(self._circuit(live_values={"airVolume": "72.9"})) == 72

    def test_live_zero_falls_through_to_target_value(self):
        c = self._circuit(live_values={"airVolume": "0"}, target_value=50)
        assert resolve_fan_speed(c) == 50

    def test_target_value_fallback(self):
        assert resolve_fan_speed(self._circuit(target_value=80)) == 80

    def test_program_air_volume_fallback(self):
        assert resolve_fan_speed(self._circuit(program_air_volume=55.0)) == 55

    def test_all_none_returns_default(self):
        assert resolve_fan_speed(self._circuit()) == 40

    def test_minimum_is_default_when_all_zero(self):
        c = self._circuit(live_values={"airVolume": "0"}, target_value=0, program_air_volume=0.0)
        assert resolve_fan_speed(c) == 40


# ---------------------------------------------------------------------------
# _V1_PROGRAM_MAP
# ---------------------------------------------------------------------------


class TestV1ProgramMap:
    def test_tte_controlled_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("tteControlled", "tteControlled") == "week1"

    def test_time_programs_maps_to_week1(self):
        assert _V1_PROGRAM_MAP.get("timePrograms", "timePrograms") == "week1"

    def test_v3_values_pass_through(self):
        for v3_key in ("week1", "week2", "ecoMode", "standby", "constant"):
            assert _V1_PROGRAM_MAP.get(v3_key, v3_key) == v3_key

    def test_none_passes_through(self):
        assert _V1_PROGRAM_MAP.get(None, None) is None


# ---------------------------------------------------------------------------
# _parse_event / HovalEventData / _is_problem_event
# ---------------------------------------------------------------------------


class TestParseEvent:
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
        assert _parse_event({"eventType": "warning", "timeResolved": None}).is_active is True

    def test_inactive_when_resolved(self):
        ev = _parse_event({"eventType": "warning", "timeResolved": "2026-02-17T12:00:00Z"})
        assert ev.is_active is False

    def test_active_when_time_resolved_missing(self):
        assert _parse_event({"eventType": "blocking"}).is_active is True

    def test_parse_empty_dict_all_none(self):
        ev = _parse_event({})
        assert ev.event_type is None
        assert ev.is_active is True  # no timeResolved → active

    def test_default_event_data_is_active(self):
        assert HovalEventData().is_active is True

    def test_resolved_event_data(self):
        assert HovalEventData(time_resolved="2026-02-17T12:00:00Z").is_active is False


class TestIsProblemEvent:
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

    def test_offline_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type="offline")) is False

    def test_none_is_not_problem(self):
        assert _is_problem_event(None) is False

    def test_none_event_type_is_not_problem(self):
        assert _is_problem_event(HovalEventData(event_type=None)) is False


# ---------------------------------------------------------------------------
# HovalCircuitHealth
# ---------------------------------------------------------------------------


class TestHovalCircuitHealth:
    def test_initial_state(self):
        ch = HovalCircuitHealth()
        assert ch.total_polls == 0
        assert ch.total_failures == 0
        assert ch.consecutive_failures == 0
        assert ch.failure_rate_1h is None
        assert ch.availability_1h is None

    def test_record_success(self):
        ch = HovalCircuitHealth()
        ts = datetime.now(UTC)
        ch.record_success(ts)
        assert ch.total_polls == 1
        assert ch.total_failures == 0
        assert ch.consecutive_failures == 0
        assert ch.last_success == ts
        assert ch.failure_rate_1h == 0.0
        assert ch.availability_1h == 100.0

    def test_record_failure(self):
        ch = HovalCircuitHealth()
        ts = datetime.now(UTC)
        ch.record_failure(ts, "HTTP 503")
        assert ch.total_polls == 1
        assert ch.total_failures == 1
        assert ch.consecutive_failures == 1
        assert ch.last_failure == ts
        assert ch.last_error == "HTTP 503"
        assert ch.failure_rate_1h == 100.0
        assert ch.availability_1h == 0.0

    def test_consecutive_failures_resets_on_success(self):
        ch = HovalCircuitHealth()
        ts = datetime.now(UTC)
        ch.record_failure(ts, "err")
        ch.record_failure(ts, "err")
        assert ch.consecutive_failures == 2
        ch.record_success(ts)
        assert ch.consecutive_failures == 0
        assert ch.total_failures == 2  # cumulative never resets

    def test_mixed_polls_failure_rate(self):
        ch = HovalCircuitHealth()
        ts = datetime.now(UTC)
        for _ in range(8):
            ch.record_success(ts)
        for _ in range(2):
            ch.record_failure(ts, "err")
        assert ch.failure_rate_1h == 20.0
        assert ch.availability_1h == 80.0

    def test_error_truncated_to_200_chars(self):
        ch = HovalCircuitHealth()
        ch.record_failure(datetime.now(UTC), "x" * 300)
        assert len(ch.last_error) == 200

    def test_to_store_dict_only_cumulative_counters(self):
        ch = HovalCircuitHealth()
        ts = datetime.now(UTC)
        ch.record_success(ts)
        ch.record_failure(ts, "err")
        d = ch.to_store_dict()
        assert set(d.keys()) == {"total_polls", "total_failures"}
        assert d["total_polls"] == 2
        assert d["total_failures"] == 1

    def test_restore_from_store_valid_data(self):
        ch = HovalCircuitHealth()
        ch.restore_from_store({"total_polls": 500, "total_failures": 42})
        assert ch.total_polls == 500
        assert ch.total_failures == 42
        assert ch.consecutive_failures == 0  # session-only — not restored

    def test_restore_from_store_bad_string_graceful(self):
        """Corrupt store data must not crash the integration on startup."""
        ch = HovalCircuitHealth()
        ch.restore_from_store({"total_polls": "not-a-number", "total_failures": "bad"})
        assert ch.total_polls == 0
        assert ch.total_failures == 0

    def test_restore_from_store_missing_keys_defaults_to_zero(self):
        ch = HovalCircuitHealth()
        ch.restore_from_store({})
        assert ch.total_polls == 0
        assert ch.total_failures == 0


# ---------------------------------------------------------------------------
# HovalConnectionHealth
# ---------------------------------------------------------------------------


class TestHovalConnectionHealth:
    def test_ema_initialises_on_first_sample(self):
        h = HovalConnectionHealth()
        assert h.ema_latency_ms is None
        h.update_ema(200.0)
        assert h.ema_latency_ms == 200.0

    def test_ema_converges_toward_new_value(self):
        h = HovalConnectionHealth()
        h.update_ema(1000.0)
        for _ in range(50):
            h.update_ema(100.0)
        assert h.ema_latency_ms < 200.0

    def test_ema_is_smooth_against_single_spike(self):
        h = HovalConnectionHealth()
        for _ in range(20):
            h.update_ema(100.0)
        h.update_ema(10000.0)
        assert h.ema_latency_ms < 1500.0

    def test_record_error_increments_all_counters(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h._record_error(ts, "timeout", "Poll timeout after 90 s")
        assert h.consecutive_failures == 1
        assert h.total_failures == 1
        assert h.auth_failures == 0
        assert h.last_error_type == "timeout"
        assert h.last_error_msg == "Poll timeout after 90 s"
        assert h.error_counts == {"timeout": 1}

    def test_record_error_auth_flag(self):
        h = HovalConnectionHealth()
        h._record_error(datetime.now(UTC), "auth", "Bad token", is_auth=True)
        assert h.auth_failures == 1

    def test_error_counts_accumulate(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h._record_error(ts, "timeout", "t")
        h._record_error(ts, "timeout", "t")
        h._record_error(ts, "api", "a")
        assert h.error_counts == {"timeout": 2, "api": 1}

    def test_circuit_health_lazy_creation(self):
        h = HovalConnectionHealth()
        ch = h.get_circuit_health("1.2.3")
        assert isinstance(ch, HovalCircuitHealth)
        assert h.get_circuit_health("1.2.3") is ch  # same object

    def test_to_store_dict_includes_circuits(self):
        h = HovalConnectionHealth()
        h.total_polls = 10
        h.ema_latency_ms = 250.0
        ch = h.get_circuit_health("1.2.3")
        ch.record_success(datetime.now(UTC))
        d = h.to_store_dict()
        assert d["total_polls"] == 10
        assert d["ema_latency_ms"] == 250.0
        assert d["circuits"]["1.2.3"]["total_polls"] == 1

    def test_restore_from_store_full(self):
        h = HovalConnectionHealth()
        h.restore_from_store(
            {
                "total_polls": 1000,
                "total_failures": 50,
                "auth_failures": 3,
                "error_counts": {"timeout": 10, "api": 40},
                "ema_latency_ms": 350.5,
                "circuits": {"2.3.4": {"total_polls": 200, "total_failures": 5}},
            }
        )
        assert h.total_polls == 1000
        assert h.total_failures == 50
        assert h.auth_failures == 3
        assert h.error_counts == {"timeout": 10, "api": 40}
        assert h.ema_latency_ms == 350.5
        assert h._circuit_health["2.3.4"].total_polls == 200

    def test_restore_from_store_bad_int_graceful(self):
        """Corrupt storage values must not crash the integration."""
        h = HovalConnectionHealth()
        h.restore_from_store(
            {
                "total_polls": "bad",
                "total_failures": None,
                "auth_failures": [],
            }
        )
        assert h.total_polls == 0
        assert h.total_failures == 0
        assert h.auth_failures == 0

    def test_restore_strips_unknown_error_types(self):
        h = HovalConnectionHealth()
        h.restore_from_store(
            {
                "error_counts": {"timeout": 1, "INJECTION_ATTACK": 99, "api": 2},
            }
        )
        assert "INJECTION_ATTACK" not in h.error_counts
        assert h.error_counts == {"timeout": 1, "api": 2}

    def test_restore_ignores_bad_ema(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"ema_latency_ms": -5})
        assert h.ema_latency_ms is None

    def test_as_diagnostic_dict_has_required_sections(self):
        h = HovalConnectionHealth()
        d = h.as_diagnostic_dict()
        assert {
            "last_success",
            "last_error",
            "counters_since_startup",
            "rolling_1h_window",
            "latency_ms",
            "circuits",
        } <= d.keys()
        assert "ema" in d["latency_ms"]
        assert "error_counts" in d["counters_since_startup"]

    def test_as_diagnostic_dict_circuit_section(self):
        h = HovalConnectionHealth()
        h.get_circuit_health("5.6.7").record_success(datetime.now(UTC))
        d = h.as_diagnostic_dict()
        assert d["circuits"]["5.6.7"]["total_polls"] == 1

    def test_persist_roundtrip(self):
        """to_store_dict() → restore_from_store() is lossless for all counters."""
        h = HovalConnectionHealth()
        h.total_polls = 42
        h.total_failures = 7
        h.auth_failures = 2
        h.error_counts = {"auth": 2, "timeout": 5}
        h.ema_latency_ms = 123.4
        ch = h.get_circuit_health("9.9.9")
        ch.total_polls = 10
        ch.total_failures = 1

        h2 = HovalConnectionHealth()
        h2.restore_from_store(h.to_store_dict())

        assert h2.total_polls == 42
        assert h2.total_failures == 7
        assert h2.auth_failures == 2
        assert h2.error_counts == {"auth": 2, "timeout": 5}
        assert h2.ema_latency_ms == 123.4
        assert h2._circuit_health["9.9.9"].total_polls == 10


# ---------------------------------------------------------------------------
# clamp_hv_air_volume (H-3 regression)
# ---------------------------------------------------------------------------
class TestClampHvAirVolume:
    """The HV fan must never send an out-of-band air-volume to the device."""

    def test_below_min_clamps_up(self):
        from custom_components.hoval_connect.const import clamp_hv_air_volume

        assert clamp_hv_air_volume(5) == 15
        assert clamp_hv_air_volume(0) == 15
        assert clamp_hv_air_volume(14) == 15

    def test_above_max_clamps_down(self):
        from custom_components.hoval_connect.const import clamp_hv_air_volume

        assert clamp_hv_air_volume(120) == 100

    def test_in_band_passthrough(self):
        from custom_components.hoval_connect.const import clamp_hv_air_volume

        assert clamp_hv_air_volume(15) == 15
        assert clamp_hv_air_volume(55) == 55
        assert clamp_hv_air_volume(100) == 100


# ---------------------------------------------------------------------------
# v0.19.0 — plant-level cache TTL constants
# ---------------------------------------------------------------------------
class TestCacheTtls:
    def test_ttls_are_sane(self):
        from datetime import timedelta

        from custom_components.hoval_connect.const import (
            EVENTS_CACHE_TTL,
            PROGRAM_CACHE_TTL,
            WEATHER_CACHE_TTL,
        )

        for ttl in (EVENTS_CACHE_TTL, WEATHER_CACHE_TTL, PROGRAM_CACHE_TTL):
            assert isinstance(ttl, timedelta)
            assert ttl.total_seconds() > 0
        # Weather changes most slowly, events fastest of the three plant caches.
        assert WEATHER_CACHE_TTL >= EVENTS_CACHE_TTL
