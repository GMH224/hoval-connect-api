"""Tests for the Hoval Connect coordinator logic (pure functions).

These tests cover the pure utility functions that don't depend on Home
Assistant. The ``homeassistant.*`` namespace is stubbed centrally in
conftest.py — v0.21.1 removed this module's legacy per-module shim, which
HARD-overwrote ``sys.modules`` (not setdefault) and therefore replaced the
real exception stubs the coordinator-core tests rely on, making test results
depend on module import order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.hoval_connect.coordinator import (  # noqa: E402
    _V1_PROGRAM_MAP,
    HovalCircuitData,
    HovalConnectionHealth,
    _resolve_active_program_value,
    resolve_weather_impact_update,
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


# v1.0.0 removed resolve_fan_speed() as dead code (it was never called
# anywhere in the codebase — its only reference was a comment). Its
# dependency, HovalCircuitData.program_air_volume, was removed at the same
# time (pure telemetry, see docs/audit-v1.0.0.md), so this coverage went
# with it rather than being kept for a function nothing calls.


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
# v1.0.0 removed _parse_event()/HovalEventData/_is_problem_event() entirely
# — event-history telemetry with no write dependency, replaced by deriving
# plant.has_error directly from circuits' own hasError flags. See
# docs/audit-v1.0.0.md.
# ---------------------------------------------------------------------------


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
        h.record_error(ts, "timeout", "Poll timeout after 90 s")
        assert h.consecutive_failures == 1
        assert h.total_failures == 1
        assert h.auth_failures == 0
        assert h.last_error_type == "timeout"
        assert h.last_error_msg == "Poll timeout after 90 s"
        assert h.error_counts == {"timeout": 1}

    def test_record_error_auth_flag(self):
        h = HovalConnectionHealth()
        h.record_error(datetime.now(UTC), "auth", "Bad token", is_auth=True)
        assert h.auth_failures == 1

    def test_error_counts_accumulate(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_error(ts, "timeout", "t")
        h.record_error(ts, "timeout", "t")
        h.record_error(ts, "api", "a")
        assert h.error_counts == {"timeout": 2, "api": 1}

    # v1.0.0: per-circuit health tracking (get_circuit_health, HovalCircuitHealth)
    # was removed along with live-values polling — see docs/audit-v1.0.0.md.
    # Replaced by last_successful_contact_at, tested below.

    def test_record_successful_contact_sets_timestamp(self):
        h = HovalConnectionHealth()
        assert h.last_successful_contact_at is None
        ts = datetime.now(UTC)
        h.record_successful_contact(ts)
        assert h.last_successful_contact_at == ts

    def test_record_poll_success_also_records_contact(self):
        """A successful health check counts as contact too, not just writes."""
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_poll_success(ts, 42.0)
        assert h.last_successful_contact_at == ts

    def test_record_successful_contact_overwrites_older_timestamp(self):
        h = HovalConnectionHealth()
        older = datetime(2026, 1, 1, tzinfo=UTC)
        newer = datetime(2026, 6, 1, tzinfo=UTC)
        h.record_successful_contact(older)
        h.record_successful_contact(newer)
        assert h.last_successful_contact_at == newer

    def test_to_store_dict_includes_contact_timestamp(self):
        h = HovalConnectionHealth()
        ts = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        h.record_successful_contact(ts)
        d = h.to_store_dict()
        assert d["last_successful_contact_at"] == ts.isoformat()

    def test_to_store_dict_contact_none_when_never_contacted(self):
        h = HovalConnectionHealth()
        assert h.to_store_dict()["last_successful_contact_at"] is None

    def test_restore_from_store_full(self):
        h = HovalConnectionHealth()
        ts = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        h.restore_from_store(
            {
                "total_polls": 1000,
                "total_failures": 50,
                "auth_failures": 3,
                "error_counts": {"timeout": 10, "api": 40},
                "ema_latency_ms": 350.5,
                "last_successful_contact_at": ts.isoformat(),
            }
        )
        assert h.total_polls == 1000
        assert h.total_failures == 50
        assert h.auth_failures == 3
        assert h.error_counts == {"timeout": 10, "api": 40}
        assert h.ema_latency_ms == 350.5
        assert h.last_successful_contact_at == ts

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

    def test_restore_from_store_bad_contact_timestamp_graceful(self):
        """A corrupt/non-ISO contact timestamp must not crash startup."""
        h = HovalConnectionHealth()
        h.restore_from_store({"last_successful_contact_at": "not-a-timestamp"})
        assert h.last_successful_contact_at is None

    def test_restore_from_store_missing_contact_timestamp_stays_none(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"total_polls": 5})
        assert h.last_successful_contact_at is None

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

    def test_restore_ignores_infinite_ema(self):
        """Independent audit finding (2026-09, fourth round, HVC-012):
        `ema > 0` alone let +infinity through, since inf > 0 is True in
        Python — isfinite() must be checked too.
        """
        h = HovalConnectionHealth()
        h.restore_from_store({"ema_latency_ms": float("inf")})
        assert h.ema_latency_ms is None

    def test_restore_ignores_nan_ema(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"ema_latency_ms": float("nan")})
        assert h.ema_latency_ms is None

    def test_restore_from_store_naive_contact_timestamp_assumed_utc(self):
        """Independent audit finding (2026-09, fourth round, HVC-009): a
        timezone-naive persisted timestamp must not crash the diagnostic
        sensor later (dt_util.utcnow() - naive_datetime raises TypeError) —
        it's normalized to UTC-aware instead of being rejected outright.
        """
        h = HovalConnectionHealth()
        h.restore_from_store({"last_successful_contact_at": "2026-01-01T12:00:00"})
        assert h.last_successful_contact_at is not None
        assert h.last_successful_contact_at.tzinfo is not None
        # Subtracting an aware "now" must not raise.
        delta = datetime.now(UTC) - h.last_successful_contact_at
        assert delta.total_seconds() > 0

    def test_restore_from_store_aware_contact_timestamp_unchanged(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"last_successful_contact_at": "2026-01-01T12:00:00+00:00"})
        assert h.last_successful_contact_at.tzinfo is not None
        assert h.last_successful_contact_at.utcoffset().total_seconds() == 0

    def test_restore_error_counts_ignores_nan_entry_keeps_others(self):
        """Independent audit finding (2026-09, fourth round, HVC-012): a
        single corrupted entry (NaN/infinity — both real possibilities
        since Python's json module parses them by default) used to raise
        inside a dict comprehension with no protection, crashing the
        WHOLE restore, not just that one entry.
        """
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": {"timeout": float("nan"), "api": 3}})
        assert "timeout" not in h.error_counts
        assert h.error_counts == {"api": 3}

    def test_restore_error_counts_ignores_infinite_entry(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": {"timeout": float("inf")}})
        assert h.error_counts == {}

    def test_restore_error_counts_ignores_negative_entry(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": {"timeout": -5}})
        assert h.error_counts == {}

    def test_restore_main_counters_ignore_negative_values(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"total_polls": -100, "total_failures": 5})
        assert h.total_polls == 0
        assert h.total_failures == 5

    def test_restore_main_counters_ignore_non_finite_values(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"total_polls": float("nan")})
        assert h.total_polls == 0

    def test_as_diagnostic_dict_has_required_sections(self):
        h = HovalConnectionHealth()
        d = h.as_diagnostic_dict()
        assert {
            "last_success",
            "last_successful_contact_at",
            "last_error",
            "counters_since_startup",
            "rolling_1h_window",
            "latency_ms",
        } <= d.keys()
        assert "ema" in d["latency_ms"]
        assert "error_counts" in d["counters_since_startup"]

    def test_as_diagnostic_dict_contact_timestamp(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_successful_contact(ts)
        d = h.as_diagnostic_dict()
        assert d["last_successful_contact_at"] == ts.isoformat()

    def test_persist_roundtrip(self):
        """to_store_dict() → restore_from_store() is lossless for all counters."""
        h = HovalConnectionHealth()
        h.total_polls = 42
        h.total_failures = 7
        h.auth_failures = 2
        h.error_counts = {"auth": 2, "timeout": 5}
        h.ema_latency_ms = 123.4
        ts = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        h.record_successful_contact(ts)

        h2 = HovalConnectionHealth()
        h2.restore_from_store(h.to_store_dict())

        assert h2.total_polls == 42
        assert h2.total_failures == 7
        assert h2.auth_failures == 2
        assert h2.error_counts == {"auth": 2, "timeout": 5}
        assert h2.ema_latency_ms == 123.4
        assert h2.last_successful_contact_at == ts


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
            CIRCUIT_SETTINGS_CACHE_TTL,
            PROGRAM_CACHE_TTL,
        )

        # v1.0.0 removed EVENTS_CACHE_TTL / WEATHER_CACHE_TTL along with the
        # telemetry they cached — see docs/audit-v1.0.0.md. PROGRAM_CACHE_TTL
        # and CIRCUIT_SETTINGS_CACHE_TTL remain: both still back real control
        # data (program names, weatherImpact) fetched at startup and after
        # writes.
        for ttl in (PROGRAM_CACHE_TTL, CIRCUIT_SETTINGS_CACHE_TTL):
            assert isinstance(ttl, timedelta)
            assert ttl.total_seconds() > 0


# ---------------------------------------------------------------------------
# v0.21.0 — weather-based control (Eco <-> Comfort weighting sliders)
# ---------------------------------------------------------------------------
class TestClampWeatherImpact:
    """The weather-impact sliders must never send an out-of-band value to the API."""

    def test_outside_temperature_below_min_clamps_up(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_outside_temperature

        assert clamp_weather_impact_outside_temperature(-5) == 0
        assert clamp_weather_impact_outside_temperature(0) == 0

    def test_outside_temperature_above_max_clamps_down(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_outside_temperature

        assert clamp_weather_impact_outside_temperature(150) == 100

    def test_outside_temperature_in_band_passthrough(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_outside_temperature

        assert clamp_weather_impact_outside_temperature(0) == 0
        assert clamp_weather_impact_outside_temperature(50) == 50
        assert clamp_weather_impact_outside_temperature(100) == 100

    def test_outside_temperature_returns_int(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_outside_temperature

        result = clamp_weather_impact_outside_temperature(42.9)
        assert result == 42
        assert isinstance(result, int)

    def test_solar_radiation_below_min_clamps_up(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_solar_radiation

        assert clamp_weather_impact_solar_radiation(-20) == -10.0

    def test_solar_radiation_above_max_clamps_down(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_solar_radiation

        assert clamp_weather_impact_solar_radiation(5) == 0.0
        assert clamp_weather_impact_solar_radiation(0.1) == 0.0

    def test_solar_radiation_in_band_passthrough(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_solar_radiation

        assert clamp_weather_impact_solar_radiation(-10) == -10.0
        assert clamp_weather_impact_solar_radiation(-5) == -5.0
        assert clamp_weather_impact_solar_radiation(0) == 0.0

    def test_solar_radiation_returns_float(self):
        from custom_components.hoval_connect.const import clamp_weather_impact_solar_radiation

        result = clamp_weather_impact_solar_radiation(-3)
        assert isinstance(result, float)


class TestCircuitSettingsCacheTtl:
    def test_settings_cache_ttl_is_sane(self):
        from datetime import timedelta

        from custom_components.hoval_connect.const import CIRCUIT_SETTINGS_CACHE_TTL

        assert isinstance(CIRCUIT_SETTINGS_CACHE_TTL, timedelta)
        assert CIRCUIT_SETTINGS_CACHE_TTL.total_seconds() > 0

    def test_hk_supports_weather_impact(self):
        from custom_components.hoval_connect.const import CIRCUIT_TYPE_HK, SUPPORTS_WEATHER_IMPACT

        assert CIRCUIT_TYPE_HK in SUPPORTS_WEATHER_IMPACT

    def test_hv_does_not_support_weather_impact(self):
        """HV (ventilation) has no thermal/comfort weighting concept; must be excluded."""
        from custom_components.hoval_connect.const import CIRCUIT_TYPE_HV, SUPPORTS_WEATHER_IMPACT

        assert CIRCUIT_TYPE_HV not in SUPPORTS_WEATHER_IMPACT


class TestResolveWeatherImpactUpdate:
    """resolve_weather_impact_update() must always resolve a full pair to PATCH.

    Regression coverage for the "PATCH is not confirmed to be a JSON-merge
    patch" risk: dragging one slider must never silently clear the other's
    current value.
    """

    def test_only_outside_temperature_changed_preserves_solar(self):
        outside, solar = resolve_weather_impact_update(
            30, -4.0, outside_temperature=80, solar_radiation=None
        )
        assert outside == 80
        assert solar == -4.0

    def test_only_solar_radiation_changed_preserves_outside(self):
        outside, solar = resolve_weather_impact_update(
            30, -4.0, outside_temperature=None, solar_radiation=-2.0
        )
        assert outside == 30
        assert solar == -2.0

    def test_neither_changed_passes_through_unmodified(self):
        outside, solar = resolve_weather_impact_update(30, -4.0)
        assert outside == 30
        assert solar == -4.0

    def test_no_current_value_and_no_change_is_none(self):
        """First-ever write to a field the API has never reported (None current)."""
        outside, solar = resolve_weather_impact_update(None, None)
        assert outside is None
        assert solar is None

    def test_requested_outside_temperature_is_clamped(self):
        outside, _solar = resolve_weather_impact_update(
            None, None, outside_temperature=500, solar_radiation=None
        )
        assert outside == 100

    def test_requested_solar_radiation_is_clamped(self):
        _outside, solar = resolve_weather_impact_update(
            None, None, outside_temperature=None, solar_radiation=-99
        )
        assert solar == -10.0

    def test_both_fields_can_be_changed_at_once(self):
        outside, solar = resolve_weather_impact_update(
            30, -4.0, outside_temperature=10, solar_radiation=-1.0
        )
        assert outside == 10
        assert solar == -1.0

    def test_hvc019_corrupted_sibling_outside_temperature_is_reclamped(self):
        """Independent audit finding (2026-09, fourth round, HVC-019): the
        sibling value (from cache/override/circuit data, not the field the
        user is actually changing) used to be forwarded completely as-is.
        An out-of-range sibling must be clamped, same as a fresh value
        would be.
        """
        outside, solar = resolve_weather_impact_update(
            500, -4.0, outside_temperature=None, solar_radiation=-2.0
        )
        assert outside == 100  # clamped into range, not sent as 500
        assert solar == -2.0

    def test_hvc019_corrupted_sibling_solar_radiation_is_reclamped(self):
        outside, solar = resolve_weather_impact_update(
            30, -99.0, outside_temperature=10, solar_radiation=None
        )
        assert outside == 10
        assert solar == -10.0  # clamped, not sent as -99

    def test_hvc019_non_finite_sibling_becomes_none_not_propagated(self):
        """A genuinely unusable sibling value (NaN — from a corrupted cache
        entry, say) must degrade to None rather than crash the write or
        forward garbage.
        """
        outside, solar = resolve_weather_impact_update(
            float("nan"), -4.0, outside_temperature=None, solar_radiation=-2.0
        )
        assert outside is None
        assert solar == -2.0

    def test_hvc019_wrong_type_sibling_becomes_none(self):
        outside, solar = resolve_weather_impact_update(
            "garbage", -4.0, outside_temperature=None, solar_radiation=-2.0
        )
        assert outside is None
        assert solar == -2.0

    def test_hvc019_valid_sibling_within_range_is_unaffected(self):
        """Sanity check: a sibling that's already valid must not be altered
        beyond ordinary clamping (which is a no-op for an in-range value).
        """
        outside, solar = resolve_weather_impact_update(
            42, -3.5, outside_temperature=None, solar_radiation=-2.0
        )
        assert outside == 42
        assert solar == -2.0


class TestHovalCircuitDataWeatherImpactDefaults:
    """New HovalCircuitData fields must default safely for circuit types that don't use them."""

    def test_defaults(self):
        circuit = HovalCircuitData(circuit_type="HV", path="1.1.0", name="Vent")
        assert circuit.weather_impact_supported is False
        assert circuit.weather_impact_outside_temperature is None
        assert circuit.weather_impact_solar_radiation is None


# ---------------------------------------------------------------------------
# _resolve_active_program_value — schema-drift robustness (v0.21.1, audit F1)
# ---------------------------------------------------------------------------


class TestResolveActiveProgramRobustness:
    """Nested schema drift must degrade to None fields, never raise.

    Each case below crashed before v0.21.1 (KeyError/AttributeError inside
    _fetch_circuit → gather(return_exceptions=True) silently discarded the
    whole circuit, including its already-fetched live values).
    """

    NOW = datetime(2026, 7, 20, 10, 0)  # a Monday

    def test_day_config_missing_id(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"name": "no-id"}]},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        # Config unusable → week resolves, day/value do not.
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_week_entry_is_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
            "week1": ["oops"],
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_week_entry_missing(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_day_program_ids_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "x", "phases": []}]},
            "week1": {"name": "W1", "dayProgramIds": "1,2,3"},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_phase_missing_start(self):
        programs = {
            "dayPrograms": {
                "dayConfigurations": [{"id": 1, "name": "Day", "phases": [{"value": 40}]}]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phase_not_a_dict(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "Day", "phases": ["oops"]}]},
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phases_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": [{"id": 1, "name": "Day", "phases": {"bad": 1}}]},
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_phase_time_values_not_numeric(self):
        programs = {
            "dayPrograms": {
                "dayConfigurations": [
                    {
                        "id": 1,
                        "name": "Day",
                        "phases": [
                            {
                                "start": {"hours": "x", "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 60,
                            }
                        ],
                    }
                ]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Day", None)

    def test_day_configurations_not_a_list(self):
        programs = {
            "dayPrograms": {"dayConfigurations": {"bad": "shape"}},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_day_config_entry_not_a_dict(self):
        programs = {
            "dayPrograms": {"dayConfigurations": ["oops", 42]},
            "week1": {"name": "W1", "dayProgramIds": [1]},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", None, None)

    def test_day_programs_not_a_dict(self):
        programs = {"dayPrograms": ["oops"], "week1": {"name": "W1"}}
        assert _resolve_active_program_value(programs, self.NOW) == (None, None, None)

    def test_mixed_valid_and_invalid_day_configs(self):
        """Valid configs are still resolved when malformed siblings exist."""
        programs = {
            "dayPrograms": {
                "dayConfigurations": [
                    {"name": "no-id"},
                    {
                        "id": 1,
                        "name": "Good",
                        "phases": [
                            {
                                "start": {"hours": 6, "minutes": 0},
                                "end": {"hours": 22, "minutes": 0},
                                "value": 55,
                            }
                        ],
                    },
                ]
            },
            "week1": {"name": "W1", "dayProgramIds": [1] * 7},
        }
        assert _resolve_active_program_value(programs, self.NOW) == ("W1", "Good", 55)


# ---------------------------------------------------------------------------
# v1.0.0 removed _parse_event()/_is_problem_event() entirely along with
# event-history telemetry. See docs/audit-v1.0.0.md.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HovalConnectionHealth — public poll-recording API (v0.21.1, audit F9)
# ---------------------------------------------------------------------------


class TestConnectionHealthPollRecording:
    def test_attempt_then_success(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_poll_attempt(ts)
        h.record_poll_success(ts, 123.0)
        assert h.total_polls == 1
        assert h.total_failures == 0
        assert h.consecutive_failures == 0
        assert h.poll_latency_ms == 123.0
        assert h.ema_latency_ms == 123.0
        assert h.failure_rate_1h == 0.0
        assert h.availability_1h == 100.0
        assert h.last_success == ts

    def test_attempt_then_error(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_poll_attempt(ts)
        h.record_error(ts, "api", "boom")
        assert h.total_polls == 1
        assert h.total_failures == 1
        assert h.consecutive_failures == 1
        assert h.failure_rate_1h == 100.0

    def test_success_resets_consecutive_failures(self):
        h = HovalConnectionHealth()
        ts = datetime.now(UTC)
        h.record_poll_attempt(ts)
        h.record_error(ts, "timeout", "t")
        h.record_poll_attempt(ts)
        h.record_poll_success(ts, 90.0)
        assert h.consecutive_failures == 0
        assert h.total_failures == 1
        assert h.failure_rate_1h == 50.0


# ---------------------------------------------------------------------------
# ICS-006 (v1.0.1): malformed error_counts CONTAINER
# ---------------------------------------------------------------------------


class TestIcs006ErrorCountsContainer:
    """Round 4's HVC-012 validated every VALUE inside error_counts but still
    called .items() on whatever the container happened to be. A persisted
    list/string/number raised AttributeError — and restore_from_store() is
    called outside the try/except guarding the LOAD, so it propagated out of
    async_setup_entry and blocked the integration from loading at all.
    """

    def test_list_container_does_not_raise(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": ["timeout", "api"]})
        assert h.error_counts == {}

    def test_string_container_does_not_raise(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": "corrupted"})
        assert h.error_counts == {}

    def test_numeric_container_does_not_raise(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": 42})
        assert h.error_counts == {}

    def test_null_container_does_not_raise(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": None})
        assert h.error_counts == {}

    def test_other_counters_still_restore_despite_bad_container(self):
        """A corrupted error_counts must not cost the counters around it."""
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": "corrupted", "total_polls": 50})
        assert h.total_polls == 50

    def test_valid_container_still_works(self):
        h = HovalConnectionHealth()
        h.restore_from_store({"error_counts": {"timeout": 3}})
        assert h.error_counts == {"timeout": 3}


# ---------------------------------------------------------------------------
# ICS-004 (v1.0.1): weather-impact READ-path normalizers
# ---------------------------------------------------------------------------


class TestIcs004Normalizers:
    """Unit-level coverage of the two helpers the six read sites now share."""

    def test_outside_temperature_rejects_non_numeric(self):
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_outside_temperature as norm,
        )

        for bad in ("abc", [], {}, None, True):
            assert norm(bad) is None

    def test_outside_temperature_rejects_non_finite(self):
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_outside_temperature as norm,
        )

        assert norm(float("nan")) is None
        assert norm(float("inf")) is None

    def test_outside_temperature_clamps_into_band(self):
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_outside_temperature as norm,
        )

        assert norm(500) == 100
        assert norm(-500) == 0

    def test_outside_temperature_passes_valid_through(self):
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_outside_temperature as norm,
        )

        assert norm(70) == 70

    def test_solar_radiation_rejects_and_clamps(self):
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_solar_radiation as norm,
        )

        assert norm("abc") is None
        assert norm(float("nan")) is None
        assert norm(99) == 0.0
        assert norm(-99) == -10.0
        assert norm(-3.5) == -3.5

    def test_numeric_strings_are_accepted(self):
        """Defensive nicety inherited from _coerce_finite_number()."""
        from custom_components.hoval_connect.coordinator import (
            normalize_weather_impact_outside_temperature as norm,
        )

        assert norm("70") == 70
