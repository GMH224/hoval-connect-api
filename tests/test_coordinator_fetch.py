"""Behavioral tests for HovalDataCoordinator's async fetch core (v0.21.1).

Before v0.21.1 the coordinator's `_fetch_all_data` / `_async_update_data`
paths — the most complex code in the integration — had no tests at all,
because conftest.py stubbed `DataUpdateCoordinator` with a MagicMock, which
makes the subclass itself a mock. conftest.py now provides a minimal *real*
base class, so the real methods run here against a scripted fake API.

Covers audit items 1, 2 and 6:
- F1: nested program-schema drift degrades program fields, never drops a circuit
- F2: event/weather shape drift never fails the whole poll
- plus the previously untested happy path: circuit filtering, v1 program
  mapping, live-values parsing, caches, dynamic-discovery signal, error
  classification in `_async_update_data`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed  # real stub (conftest)
from homeassistant.helpers.update_coordinator import UpdateFailed  # real stub (conftest)

import custom_components.hoval_connect.coordinator as coord_mod
from custom_components.hoval_connect.api import HovalApiError, HovalAuthError
from custom_components.hoval_connect.coordinator import (
    ERROR_TYPE_API,
    ERROR_TYPE_AUTH,
    ERROR_TYPE_CIRCUIT_LIST,
    ERROR_TYPE_UNKNOWN,
    HovalCircuitData,
    HovalDataCoordinator,
    _CircuitListError,
    resolve_resume_program,
)

# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------

_VALID_PROGRAMS = {
    "week1": {
        "name": "Woche 1",
        "dayProgramIds": [1, 1, 1, 1, 1, 1, 1],
    },
    "week2": {"name": "Woche 2", "dayProgramIds": [1, 1, 1, 1, 1, 1, 1]},
    "dayPrograms": {
        "dayConfigurations": [
            {
                "id": 1,
                "name": "Normal",
                "phases": [
                    {
                        "start": {"hours": 0, "minutes": 0},
                        "end": {"hours": 23, "minutes": 59},
                        "value": 60,
                    }
                ],
            }
        ]
    },
}


class FakeApi:
    """Scripted stand-in for HovalConnectApi.

    Every response is an attribute so individual tests can rewrite one
    endpoint's behavior; callables are awaited-and-raised via _maybe().

    v1.0.0 removed get_live_values/get_latest_event/get_events/get_weather
    from the real API client entirely (pure telemetry, no write dependency —
    see docs/audit-v1.0.0.md), so this fake no longer implements them either.
    """

    def __init__(self) -> None:
        self.plants_response: Any = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": True}
        ]
        self.circuits_response: Any = [
            {
                "type": "HV",
                "path": "hv-1",
                "name": "Ventilation",
                "selectable": True,
                "activeProgram": "tteControlled",  # v1 value → must map to week1
                "operationMode": "REGULAR",
                "targetValue": 55,
            },
            {
                "type": "BL",
                "path": "bl-1",
                "name": "Boiler",
                "selectable": False,  # non-selectable but supported via _NON_SELECTABLE_TYPES
            },
            {"type": "SOL", "path": "sol-1", "name": "Solar", "selectable": True},  # unsupported
            {"type": "HK", "name": "No path"},  # missing path → skipped
        ]
        self.programs_response: Any = _VALID_PROGRAMS
        self.settings_response: Any = {
            "circuitName": "HK",
            "weatherImpact": {"outsideTemperature": 70, "solarRadiation": -3.5},
        }
        self.update_settings_response: Any = None
        self.calls: list[str] = []
        self.invalidated: list[str] = []

    @staticmethod
    async def _maybe(value: Any) -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_plants(self):
        self.calls.append("plants")
        return await self._maybe(self.plants_response)

    async def get_circuits(self, plant_id):
        self.calls.append(f"circuits:{plant_id}")
        return await self._maybe(self.circuits_response)

    async def get_programs(self, plant_id, path):
        self.calls.append(f"programs:{path}")
        return await self._maybe(self.programs_response)

    async def get_circuit_settings(self, plant_id, path):
        self.calls.append(f"settings:{path}")
        return await self._maybe(self.settings_response)

    async def update_circuit_settings(self, plant_id, path, **kwargs):
        self.calls.append(f"update_settings:{path}")
        return await self._maybe(self.update_settings_response)

    def invalidate_plant_token(self, plant_id):
        self.invalidated.append(plant_id)


def _make_coordinator(api: FakeApi | None = None) -> tuple[HovalDataCoordinator, FakeApi]:
    api = api or FakeApi()
    # v2.2.0: the coordinator takes the config entry explicitly instead of
    # relying on HA's current_entry ContextVar.
    hass = MagicMock()

    # Independent audit finding (2026-09, final round, P2): a bare
    # MagicMock's async_create_task() records the call and DISCARDS the
    # coroutine it was handed, never awaiting it — which made every test
    # that triggers a post-write refresh leak an un-awaited coroutine and
    # emit "coroutine ... was never awaited" at interpreter shutdown. One
    # warning for the whole run, attributed to whichever test happened to
    # be running during garbage collection, which is why it wandered
    # between test names and was easy to keep ignoring.
    #
    # Closing the coroutine is the honest fake here: it disposes of it
    # deterministically (no warning, no leak) without pretending to run
    # it. Tests that genuinely need the task to EXECUTE already override
    # hass.async_create_task with asyncio.ensure_future themselves — see
    # TestBackgroundTaskTracking — so this default must not schedule
    # anything, or those tests would stop exercising what they claim to.
    def _discard_coroutine(coro):
        coro.close()
        return MagicMock()

    hass.async_create_task = MagicMock(side_effect=_discard_coroutine)
    coordinator = HovalDataCoordinator(hass, MagicMock(), api, MagicMock())
    return coordinator, api


# ---------------------------------------------------------------------------
# _fetch_all_data — happy path
# ---------------------------------------------------------------------------


class TestIsSelectableContractField:
    """Independent audit finding (2026-09, "more" report, finding #1):
    docs/openapi-v3.json's CircuitV3DTO declares `isSelectable` as
    REQUIRED and `selectable` as merely optional — a valid response could
    omit the legacy field entirely.
    """

    @pytest.mark.asyncio
    async def test_circuit_with_only_isselectable_is_discovered(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HV",
                "path": "hv-1",
                "name": "Ventilation",
                "isSelectable": True,
                # deliberately no "selectable" key at all
            }
        ]
        data = await coordinator._fetch_all_data()
        assert "hv-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_circuit_with_only_isselectable_false_is_skipped(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {"type": "HV", "path": "hv-1", "name": "Ventilation", "isSelectable": False}
        ]
        data = await coordinator._fetch_all_data()
        assert "hv-1" not in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_legacy_selectable_only_still_works(self):
        """Backward compatibility: a response with only the old field
        (as every response captured so far has) must keep working exactly
        as before.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {"type": "HV", "path": "hv-1", "name": "Ventilation", "selectable": True}
        ]
        data = await coordinator._fetch_all_data()
        assert "hv-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_isselectable_takes_precedence_over_conflicting_selectable(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HV",
                "path": "hv-1",
                "name": "Ventilation",
                "isSelectable": True,
                "selectable": False,
            }
        ]
        data = await coordinator._fetch_all_data()
        assert "hv-1" in data.plants["p1"].circuits


class TestFetchAllDataHappyPath:
    @pytest.mark.asyncio
    async def test_supported_circuits_parsed(self):
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()

        plant = data.plants["p1"]
        assert plant.name == "Test Plant"
        assert plant.is_online is True
        # HV (selectable) and BL (non-selectable but allowed); SOL unsupported,
        # HK without path skipped.
        assert set(plant.circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_v1_program_value_normalised(self):
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].circuits["hv-1"].active_program == "week1"

    @pytest.mark.asyncio
    async def test_program_names_extracted(self):
        """v1.0.0: only program_names survives from the programs response —
        active_week_name/active_day_program_name/program_air_volume (pure
        telemetry) and live_values (never fetched at all anymore) are gone.
        See docs/audit-v1.0.0.md.
        """
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits["hv-1"]
        assert hv.live_values == {}  # always empty now — see HovalCircuitData
        assert hv.program_names == {"week1": "Woche 1", "week2": "Woche 2"}

    @pytest.mark.asyncio
    async def test_has_error_derived_from_circuits_not_events(self):
        """v1.0.0: plant.has_error comes from circuits' own hasError flags —
        no get_events()/get_latest_event() call exists anymore at all.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HV",
                "path": "hv-1",
                "name": "Ventilation",
                "selectable": True,
                "hasError": True,
            }
        ]
        data = await coordinator._fetch_all_data()
        plant = data.plants["p1"]
        assert plant.has_error is True
        assert not any(c.startswith(("events:", "latest_event:", "weather:")) for c in api.calls)

    @pytest.mark.asyncio
    async def test_no_error_when_no_circuit_reports_one(self):
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].has_error is False

    @pytest.mark.asyncio
    async def test_new_circuit_signal_fired_once(self, monkeypatch):
        signals: list[Any] = []
        monkeypatch.setattr(
            coord_mod, "async_dispatcher_send", lambda hass, sig, *a: signals.append(sig)
        )
        coordinator, _api = _make_coordinator()
        await coordinator._fetch_all_data()
        assert len(signals) == 1  # first discovery fires
        await coordinator._fetch_all_data()
        assert len(signals) == 1  # no new circuits → no second signal

    @pytest.mark.asyncio
    async def test_program_cache_reused_within_ttl(self):
        coordinator, api = _make_coordinator()
        await coordinator._fetch_all_data()
        await coordinator._fetch_all_data()
        # Programs cached (5 min TTL) — fetched once across two calls.
        assert api.calls.count("programs:hv-1") == 1

    @pytest.mark.asyncio
    async def test_mode_override_cleared_after_success(self):
        coordinator, _api = _make_coordinator()
        coordinator.set_mode_override("p1", "hv-1", "standby")
        await coordinator._fetch_all_data()
        assert coordinator.get_mode_override("p1", "hv-1") is None

    @pytest.mark.asyncio
    async def test_offline_plant_skips_circuit_calls_and_invalidates_token(self):
        coordinator, api = _make_coordinator()
        api.plants_response = [{"plantExternalId": "p1", "description": "Off", "isOnline": False}]
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].is_online is False
        assert data.plants["p1"].circuits == {}
        assert api.invalidated == ["p1"]
        assert not any(c.startswith("circuits") for c in api.calls)

    @pytest.mark.asyncio
    async def test_plant_without_id_skipped(self):
        coordinator, api = _make_coordinator()
        api.plants_response = [{"description": "nameless"}]
        data = await coordinator._fetch_all_data()
        assert data.plants == {}


# ---------------------------------------------------------------------------
# _fetch_all_data — degradation paths (audit F1 acceptance tests)
# ---------------------------------------------------------------------------


class TestFetchAllDataDegradation:
    @pytest.mark.asyncio
    async def test_f1_malformed_programs_keep_circuit(self):
        """AUDIT F1: nested program drift must not drop the circuit."""
        coordinator, api = _make_coordinator()
        api.programs_response = {
            "dayPrograms": {"dayConfigurations": [{"name": "no-id"}]},
            "week1": ["wrong-shape"],
        }
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits.get("hv-1")
        assert hv is not None, "circuit must survive program schema drift"
        assert hv.program_names == {}

    @pytest.mark.asyncio
    async def test_programs_empty_list_keeps_circuit(self):
        """BL-style HTTP 200 [] response (May 2026 regression) stays fixed."""
        coordinator, api = _make_coordinator()
        api.programs_response = []
        data = await coordinator._fetch_all_data()
        assert "hv-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_programs_error_keeps_circuit(self):
        coordinator, api = _make_coordinator()
        api.programs_response = HovalApiError("programs down")
        data = await coordinator._fetch_all_data()
        assert "hv-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_circuit_list_failure_raises_circuit_list_error(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = HovalApiError("410 gone")
        with pytest.raises(_CircuitListError):
            await coordinator._fetch_all_data()

    @pytest.mark.asyncio
    async def test_hvc001_null_element_in_circuit_list_does_not_abort_refresh(self):
        """Independent audit finding (2026-09, fourth round, HVC-001): a
        non-dict element (null, a string, ...) anywhere in the circuits
        list must not crash the whole refresh — it happens before
        per-circuit gather() isolation even begins.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "selectable": True},
            None,
            "garbage",
            42,
        ]
        data = await coordinator._fetch_all_data()  # must not raise
        assert "hk-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_hvc002_duplicate_circuit_path_keeps_first_seen(self):
        """Independent audit finding (2026-09, fourth round, HVC-002): two
        circuits reporting the same path must not silently overwrite each
        other with no trace.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "First",
                "selectable": True,
                "targetValue": 21,
            },
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Second",
                "selectable": True,
                "targetValue": 99,
            },
        ]
        data = await coordinator._fetch_all_data()
        assert len(data.plants["p1"].circuits) == 1
        assert data.plants["p1"].circuits["hk-1"].name == "First"

    @pytest.mark.asyncio
    async def test_hvc008_non_finite_actual_value_becomes_none(self):
        """Independent audit finding (2026-09, fourth round, HVC-008): a
        non-finite/malformed targetValue or actualValue must not reach
        HovalCircuitData unfiltered.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "actualValue": float("nan"),
                "targetValue": "not-a-number",
            }
        ]
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.actual_value is None
        assert hk.target_value is None

    @pytest.mark.asyncio
    async def test_hvc008_valid_numeric_string_is_coerced(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "actualValue": "21.5",
                "targetValue": 22,
            }
        ]
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.actual_value == 21.5
        assert hk.target_value == 22.0

    @pytest.mark.asyncio
    async def test_hvc016_non_string_program_name_is_ignored(self):
        """Independent audit finding (2026-09, fourth round, HVC-016): a
        malformed (non-string, but truthy) program name must not reach
        program_names, where it would later crash select.py's
        disambiguation logic (which requires hashable/string values).
        """
        coordinator, api = _make_coordinator()
        api.programs_response = {
            "week1": {"name": ["a", "list"]},
            "week2": {"name": {"nested": "object"}},
        }
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits["hv-1"]
        assert hv.program_names == {}

    @pytest.mark.asyncio
    async def test_hvc016_whitespace_only_name_is_ignored(self):
        coordinator, api = _make_coordinator()
        api.programs_response = {"week1": {"name": "   "}}
        data = await coordinator._fetch_all_data()
        assert "week1" not in data.plants["p1"].circuits["hv-1"].program_names

    @pytest.mark.asyncio
    async def test_hvc016_valid_string_name_still_works(self):
        coordinator, api = _make_coordinator()
        api.programs_response = {"week1": {"name": "  Winter  "}}
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].circuits["hv-1"].program_names["week1"] == "Winter"


# ---------------------------------------------------------------------------
# _async_update_data — error classification & health accounting
# ---------------------------------------------------------------------------


class TestAsyncUpdateData:
    @pytest.mark.asyncio
    async def test_success_records_health_and_schedules_save(self):
        coordinator, _api = _make_coordinator()
        result = await coordinator._async_update_data()
        health = coordinator.connection_health
        assert "p1" in result.plants
        assert health.total_polls == 1
        assert health.total_failures == 0
        assert health.consecutive_failures == 0
        assert health.poll_latency_ms is not None
        assert health.ema_latency_ms is not None
        assert health.failure_rate_1h == 0.0
        coordinator._health_store.async_delay_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_auth_error_raises_config_entry_auth_failed(self):
        coordinator, api = _make_coordinator()
        api.plants_response = HovalAuthError("bad creds")
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        health = coordinator.connection_health
        assert health.auth_failures == 1
        assert health.last_error_type == ERROR_TYPE_AUTH
        assert health.error_counts == {ERROR_TYPE_AUTH: 1}

    @pytest.mark.asyncio
    async def test_circuit_list_error_classified_separately(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = HovalApiError("410 gone")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert coordinator.connection_health.last_error_type == ERROR_TYPE_CIRCUIT_LIST

    @pytest.mark.asyncio
    async def test_generic_api_error_raises_update_failed(self):
        coordinator, api = _make_coordinator()
        api.plants_response = HovalApiError("500")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert coordinator.connection_health.last_error_type == ERROR_TYPE_API

    @pytest.mark.asyncio
    async def test_unexpected_error_recorded_and_reraised(self):
        coordinator, api = _make_coordinator()
        api.plants_response = ValueError("schema surprise")
        with pytest.raises(ValueError):
            await coordinator._async_update_data()
        health = coordinator.connection_health
        assert health.last_error_type == ERROR_TYPE_UNKNOWN
        assert "ValueError" in (health.last_error_msg or "")

    @pytest.mark.asyncio
    async def test_failure_then_success_resets_streak(self):
        coordinator, api = _make_coordinator()
        api.plants_response = HovalApiError("500")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        api.plants_response = [{"plantExternalId": "p1", "description": "Back", "isOnline": True}]
        await coordinator._async_update_data()
        health = coordinator.connection_health
        assert health.total_polls == 2
        assert health.total_failures == 1
        assert health.consecutive_failures == 0
        assert health.failure_rate_1h == 50.0


# ---------------------------------------------------------------------------
# HK settings fetch (weather impact) through the fetch pipeline
# ---------------------------------------------------------------------------


class TestMultiPlantCircuitPathCollision:
    """Independent audit finding (2026-09, HVC-001): caches/overrides must be
    keyed by (plant_id, circuit_path), not circuit_path alone. Two plants
    can share a circuit path (there is nothing in the API that guarantees
    otherwise) — a single-plant account can never hit this, but nothing
    guarantees Hoval's account model stays that way (e.g. a future plant
    split, such as AC being separated from heating). FakeApi's get_circuits
    ignores plant_id and returns the same circuits_response regardless of
    which plant asked, which conveniently reproduces exactly this scenario:
    two plants ("p1", "p2") both reporting a circuit at the same path.
    """

    def _two_plant_api(self) -> FakeApi:
        api = FakeApi()
        api.plants_response = [
            {"plantExternalId": "p1", "description": "Plant One", "isOnline": True},
            {"plantExternalId": "p2", "description": "Plant Two", "isOnline": True},
        ]
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",  # same path in both plants
                "name": "Heating",
                "selectable": True,
                "activeProgram": "week1",
                "operationMode": "REGULAR",
            }
        ]
        api.settings_response = {
            "circuitName": "Heating",
            "weatherImpact": {"outsideTemperature": 70, "solarRadiation": -3.5},
        }
        return api

    @pytest.mark.asyncio
    async def test_mode_override_does_not_leak_across_plants(self):
        coordinator, _api = _make_coordinator(self._two_plant_api())
        await coordinator._fetch_all_data()

        coordinator.set_mode_override("p1", "hk-1", "standby")

        assert coordinator.get_mode_override("p1", "hk-1") == "standby"
        assert coordinator.get_mode_override("p2", "hk-1") is None

    @pytest.mark.asyncio
    async def test_weather_impact_override_does_not_leak_across_plants(self):
        coordinator, _api = _make_coordinator(self._two_plant_api())
        await coordinator._fetch_all_data()

        await coordinator.async_set_weather_impact("p1", "hk-1", outside_temperature=99)

        override_p1 = coordinator.get_weather_impact_override("p1", "hk-1")
        override_p2 = coordinator.get_weather_impact_override("p2", "hk-1")
        assert override_p1 is not None
        assert override_p1["outsideTemperature"] == 99
        assert override_p2 is None

    @pytest.mark.asyncio
    async def test_program_and_settings_caches_do_not_leak_across_plants(self):
        coordinator, api = _make_coordinator(self._two_plant_api())
        await coordinator._fetch_all_data()

        # Both plants' circuits were fetched independently — one call per
        # plant, not deduplicated by path alone.
        assert api.calls.count("settings:hk-1") == 2
        assert api.calls.count("programs:hk-1") == 2

        # A second fetch within the cache TTL must not re-fetch either
        # plant's circuit (cache hits for both, independently).
        api.calls.clear()
        await coordinator._fetch_all_data(fetch_started_at=time.monotonic())
        assert "settings:hk-1" not in api.calls
        assert "programs:hk-1" not in api.calls

    @pytest.mark.asyncio
    async def test_control_and_refresh_requires_plant_id_as_keyword(self):
        """Signature guard: async_control_and_refresh's parameters are all
        keyword-only after `coro` specifically so a call site that wasn't
        updated for this fix fails loudly (TypeError) instead of silently
        passing plant_id positionally into the wrong parameter.
        """
        coordinator, _api = _make_coordinator(self._two_plant_api())
        await coordinator._fetch_all_data()

        async def _noop():
            return None

        coro = _noop()
        try:
            with pytest.raises(TypeError):
                # Old call shape: circuit_path positional, no plant_id at all.
                await coordinator.async_control_and_refresh(coro, "hk-1", "standby")
        finally:
            coro.close()


class TestBackgroundTaskTracking:
    """Independent audit finding (2026-09, "more" report, finding #7, and
    fourth round, HVC-003): every fire-and-forget post-write refresh task —
    and, since HVC-003, every entity's debounced control-write task too —
    must be tracked so async_shutdown() can cancel any still-pending one
    before the API session is closed.
    """

    @pytest.mark.asyncio
    async def test_create_tracked_task_returns_the_task(self):
        """HVC-003: the method must return the created task, not just
        schedule it — fan.py/number.py need their own local reference (for
        their existing debounce-cancel-on-new-value logic) in addition to
        the coordinator's shared tracking.
        """
        coordinator, _api = _make_coordinator()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        async def _quick():
            return None

        task = coordinator.create_tracked_task(_quick())
        assert isinstance(task, asyncio.Task)
        await task

    @pytest.mark.asyncio
    async def test_an_externally_held_tracked_task_is_still_cancelled_on_shutdown(self):
        """Simulates fan.py/number.py's actual usage pattern: the caller
        keeps its own reference (as `self._debounce_task`) while the
        coordinator also tracks the same task — both must see the same
        object, and async_shutdown() must still be able to cancel it via
        its own tracking, independent of whatever the caller does with its
        copy of the reference.
        """
        coordinator, _api = _make_coordinator()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        was_cancelled = False

        async def _debounced_write():
            nonlocal was_cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                was_cancelled = True
                raise

        # Mirrors `self._debounce_task = self.coordinator.create_tracked_task(...)`
        entity_local_reference = coordinator.create_tracked_task(_debounced_write())
        await asyncio.sleep(0)  # let it actually start running

        await coordinator.async_shutdown()

        assert was_cancelled is True
        assert entity_local_reference.cancelled()

    @pytest.mark.asyncio
    async def test_create_tracked_task_adds_to_background_tasks(self):
        coordinator, _api = _make_coordinator()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        async def _sleeper():
            await asyncio.sleep(10)

        coordinator.create_tracked_task(_sleeper())
        assert len(coordinator._background_tasks) == 1

        for task in list(coordinator._background_tasks):
            task.cancel()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_completed_task_is_discarded_automatically(self):
        coordinator, _api = _make_coordinator()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        async def _quick():
            return None

        coordinator.create_tracked_task(_quick())
        await asyncio.sleep(0.01)  # let it complete and the done-callback fire
        assert len(coordinator._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_async_shutdown_cancels_pending_tasks(self):
        coordinator, _api = _make_coordinator()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        was_cancelled = False

        async def _sleeper():
            nonlocal was_cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                was_cancelled = True
                raise

        coordinator.create_tracked_task(_sleeper())
        await asyncio.sleep(0)  # let the task actually start running first
        await coordinator.async_shutdown()

        assert was_cancelled is True
        assert len(coordinator._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_async_shutdown_with_no_pending_tasks_is_a_noop(self):
        coordinator, _api = _make_coordinator()
        await coordinator.async_shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_write_schedules_a_tracked_task_not_a_bare_one(self):
        """End-to-end: async_control_and_refresh's own scheduled refresh
        task must go through create_tracked_task(), not a bare
        hass.async_create_task() call that bypasses tracking entirely.
        """
        coordinator, _api = _make_coordinator()
        await coordinator._async_update_data()
        coordinator.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)

        async def _noop_coro():
            return None

        await coordinator.async_control_and_refresh(
            _noop_coro(), plant_id="p1", circuit_path="hv-1", mode_override="standby"
        )
        assert len(coordinator._background_tasks) == 1

        for task in list(coordinator._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


class TestResolveResumeProgram:
    """Independent audit finding (2026-09, HVC-ICS-008 + "more" report
    finding #8): resolve_resume_program() now does a fresh, targeted GET
    before resolving, rather than trusting a potentially-stale coordinator
    snapshot.
    """

    @pytest.mark.asyncio
    async def test_fresh_week2_is_preferred_over_stale_cached_week1(self):
        api = FakeApi()
        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "activeProgram": "week2"}
        ]
        stale_cached_circuit = HovalCircuitData(
            circuit_type="HK", path="hk-1", name="Heating", active_program="week1"
        )

        result = await resolve_resume_program(api, "p1", "hk-1", stale_cached_circuit)

        assert result == "week2"

    @pytest.mark.asyncio
    async def test_fresh_week1_confirmed_even_if_cache_said_week2(self):
        api = FakeApi()
        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "activeProgram": "week1"}
        ]
        stale_cached_circuit = HovalCircuitData(
            circuit_type="HK", path="hk-1", name="Heating", active_program="week2"
        )

        result = await resolve_resume_program(api, "p1", "hk-1", stale_cached_circuit)

        assert result == "week1"

    @pytest.mark.asyncio
    async def test_falls_back_to_cached_value_when_fresh_fetch_fails(self):
        api = FakeApi()
        api.circuits_response = HovalApiError("cloud down")
        cached_circuit = HovalCircuitData(
            circuit_type="HK", path="hk-1", name="Heating", active_program="week2"
        )

        result = await resolve_resume_program(api, "p1", "hk-1", cached_circuit)

        assert result == "week2"

    @pytest.mark.asyncio
    async def test_falls_back_to_week1_default_when_fetch_fails_and_no_cache(self):
        api = FakeApi()
        api.circuits_response = HovalApiError("cloud down")

        result = await resolve_resume_program(api, "p1", "hk-1", None)

        assert result == "week1"

    @pytest.mark.asyncio
    async def test_circuit_not_found_in_fresh_response_falls_back_to_cache(self):
        api = FakeApi()
        api.circuits_response = [
            {"type": "HK", "path": "some-other-circuit", "name": "Other", "activeProgram": "week1"}
        ]
        cached_circuit = HovalCircuitData(
            circuit_type="HK", path="hk-1", name="Heating", active_program="week2"
        )

        result = await resolve_resume_program(api, "p1", "hk-1", cached_circuit)

        assert result == "week2"

    @pytest.mark.asyncio
    async def test_legacy_v1_program_value_is_normalised(self):
        """A fresh response using a v1-style activeProgram value must still
        be mapped through _V1_PROGRAM_MAP, same as the main fetch path.
        """
        api = FakeApi()
        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "activeProgram": "tteControlled"}
        ]

        result = await resolve_resume_program(api, "p1", "hk-1", None)

        assert result == "week1"  # tteControlled maps to week1, never week2


class TestFetchWeatherImpact:
    def _hk_api(self) -> FakeApi:
        api = FakeApi()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "activeProgram": "week1",
                "operationMode": "REGULAR",
            }
        ]
        return api

    @pytest.mark.asyncio
    async def test_hk_settings_fetched_and_parsed(self):
        coordinator, api = _make_coordinator(self._hk_api())
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is True
        assert hk.weather_impact_outside_temperature == 70
        assert hk.weather_impact_solar_radiation == -3.5
        assert "settings:hk-1" in api.calls

    @pytest.mark.asyncio
    async def test_hv_circuit_never_fetches_settings(self):
        coordinator, api = _make_coordinator()
        await coordinator._fetch_all_data()
        assert not any(c.startswith("settings:") for c in api.calls)

    @pytest.mark.asyncio
    async def test_settings_error_falls_back_to_cache(self):
        coordinator, api = _make_coordinator(self._hk_api())
        await coordinator._fetch_all_data()
        # Expire the settings cache so the next poll re-fetches — and fails.
        cached = coordinator._settings_cache[("p1", "hk-1")]
        coordinator._settings_cache[("p1", "hk-1")] = (cached[0], 0.0)
        api.settings_response = HovalApiError("down")
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is True  # stale cache reused
        assert hk.weather_impact_outside_temperature == 70

    @pytest.mark.asyncio
    async def test_missing_weather_impact_key_reports_unsupported(self):
        """
        Regression — v0.23.0 fix. A forensic crawl (2026-09) found the cloud
        now returns settings responses with NO 'weatherImpact' key at all for
        every circuit tested (only 'circuitName'), where it previously always
        included the key (with real values, or null sub-fields). Before the
        fix, `isinstance(settings, dict)` alone was enough to mark the
        feature supported, so this shape would have kept the number entities
        "available" showing an unknown value forever, and any control action
        would try to PATCH a field the cloud no longer has.
        """
        api = self._hk_api()
        api.settings_response = {"circuitName": "Heating"}  # no weatherImpact key
        coordinator, api = _make_coordinator(api)
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is False
        assert hk.weather_impact_outside_temperature is None
        assert hk.weather_impact_solar_radiation is None

    @pytest.mark.asyncio
    async def test_weather_impact_key_present_but_null_still_supported(self):
        """
        Sibling case to the one above: the key being present with null
        sub-fields (a circuit that legitimately has neither weighting set
        yet) must be treated differently from the key being absent entirely
        — this is exactly why the fix checks for key presence rather than
        just truthiness of the fetched value.
        """
        api = self._hk_api()
        api.settings_response = {
            "circuitName": "Heating",
            "weatherImpact": {"outsideTemperature": None, "solarRadiation": None},
        }
        coordinator, api = _make_coordinator(api)
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is True
        assert hk.weather_impact_outside_temperature is None
        assert hk.weather_impact_solar_radiation is None

    @pytest.mark.asyncio
    async def test_cached_settings_missing_weather_impact_key_falls_back_unsupported(self):
        """Same key-presence check applies to the cached-fallback branch."""
        api = self._hk_api()
        coordinator, api = _make_coordinator(api)
        await coordinator._fetch_all_data()

        # Expire the cache so the next poll re-fetches, then fail that
        # re-fetch — forcing the cached-fallback branch to run. Poison the
        # cached value itself to simulate a cache populated before this fix
        # against an old-shape response (or simply to match the new no-key
        # server response for this sibling test).
        coordinator._settings_cache[("p1", "hk-1")] = ({"circuitName": "Heating"}, 0.0)
        api.settings_response = HovalApiError("down")

        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is False


# ---------------------------------------------------------------------------
# SUPPORTS_PROGRAMS — BL (boiler) circuits never call get_programs (v0.23.0)
# ---------------------------------------------------------------------------


class TestSupportsProgramsGate:
    """
    Regression — v0.23.0 fix. A forensic crawl (2026-09) found the cloud
    returns HTTP 417 for GET .../circuits/{path}/programs on BL (boiler)
    circuits every time, never 200 — BL has no schedule of its own. Before
    the fix this was already handled gracefully (the exception lands in
    results["programs"] via gather(return_exceptions=True) and is logged at
    debug level), but the coordinator still made the call, and cache-refresh
    still repeated the failure every PROGRAM_CACHE_TTL.

    The second test below locks in a real bug caught during review of the
    fix itself: gating need_programs on circuit type as well as cache
    freshness means a BL circuit's `cached_prog` is now always None (nothing
    is ever cached for it) — an un-guarded `results["programs"] =
    cached_prog[0]` fallback would raise TypeError on the very first poll.
    """

    @pytest.mark.asyncio
    async def test_bl_circuit_never_calls_get_programs(self):
        coordinator, api = _make_coordinator()  # default fixture includes bl-1
        data = await coordinator._fetch_all_data()

        assert not any(c.startswith("programs:bl-1") for c in api.calls)
        # HV (a supported, programmable type) still gets fetched as normal.
        assert "programs:hv-1" in api.calls
        # And the circuit itself is still present and otherwise populated —
        # excluding it from programs must not exclude it from anything else.
        assert "bl-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_bl_circuit_survives_repeated_polls_with_empty_program_cache(self):
        """
        The regression this guards: without `cached_prog is not None` in the
        fallback condition, this would raise TypeError on the very first
        poll already — but a second poll is included too, since a cache
        that stays permanently empty for this circuit type is the whole
        point of the fix and must not develop a different failure over time.
        """
        coordinator, _api = _make_coordinator()
        await coordinator._fetch_all_data()
        data = await coordinator._fetch_all_data()  # must not raise
        assert "bl-1" in data.plants["p1"].circuits


# ---------------------------------------------------------------------------
# v1.0.0 — _async_update_data dispatch: full resync vs. minimal health check
# ---------------------------------------------------------------------------


class TestEmptyPlantsGuard:
    """Independent audit finding (2026-09, "more" report, finding #3): an
    anomalous-but-well-formed empty get_plants() response must not
    silently wipe every known plant. Requires
    HovalDataCoordinator._EMPTY_PLANTS_CONFIRMATION_THRESHOLD consecutive
    empty responses before actually accepting the wipe.
    """

    @pytest.mark.asyncio
    async def test_single_empty_response_does_not_wipe_topology(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        assert set(first.plants["p1"].circuits) == {"hv-1", "bl-1"}

        api.plants_response = []
        second = await coordinator._async_update_data()

        assert set(second.plants["p1"].circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_confirmed_empty_response_eventually_wipes_topology(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first

        api.plants_response = []
        for _ in range(coordinator._EMPTY_PLANTS_CONFIRMATION_THRESHOLD - 1):
            result = await coordinator._async_update_data()
            coordinator.data = result
            assert result.plants  # not yet wiped

        result = await coordinator._async_update_data()
        assert result.plants == {}

    @pytest.mark.asyncio
    async def test_non_empty_response_resets_the_counter(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first

        api.plants_response = []
        result = await coordinator._async_update_data()  # one strike
        coordinator.data = result
        assert coordinator._consecutive_empty_plants == 1

        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": True}
        ]
        result = await coordinator._async_update_data()  # back to normal
        coordinator.data = result
        assert coordinator._consecutive_empty_plants == 0

    @pytest.mark.asyncio
    async def test_first_ever_fetch_with_zero_plants_is_trusted_immediately(self):
        """A genuinely new/empty account has nothing to lose by accepting
        an empty response right away — the guard only distrusts an empty
        response when plants were PREVIOUSLY known.
        """
        coordinator, api = _make_coordinator()
        api.plants_response = []
        result = await coordinator._async_update_data()
        assert result.plants == {}
        assert coordinator._did_initial_discovery is True

    @pytest.mark.asyncio
    async def test_fetch_all_data_also_applies_the_guard(self):
        """The same guard must protect the write-triggered full-fetch path,
        not just the scheduled health check.
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._fetch_all_data()
        coordinator.data = first

        api.plants_response = []
        result = await coordinator._fetch_all_data()
        assert set(result.plants["p1"].circuits) == {"hv-1", "bl-1"}


class TestCircuitValuesRefreshOnHealthCheck:
    """The Option A fix (docs/audit-v1.0.0.md § 19).

    Before this, `_health_check()` carried each plant's circuits dict
    forward completely unchanged, so sensor.py's `actual_value` was frozen
    at whatever the last FULL refresh produced — a temperature sensor that
    silently never updated. These tests pin the corrected behavior AND the
    cost constraint that shaped it (one circuits call per plant; no
    per-circuit telemetry calls).
    """

    @pytest.mark.asyncio
    async def test_actual_value_updates_between_scheduled_checks(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "actualValue": 21.0,
                "targetValue": 20.0,
            }
        ]
        first = await coordinator._async_update_data()
        coordinator.data = first
        assert first.plants["p1"].circuits["hk-1"].actual_value == 21.0

        # The house warms up; the next SCHEDULED check must see it.
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "actualValue": 23.5,
                "targetValue": 20.0,
            }
        ]
        second = await coordinator._async_update_data()

        assert second.plants["p1"].circuits["hk-1"].actual_value == 23.5

    @pytest.mark.asyncio
    async def test_health_check_makes_one_circuits_call_not_one_per_circuit(self):
        """The cost constraint: get_circuits() returns every circuit in a
        single call. This must not regress into per-circuit telemetry
        calls, which is what the user explicitly ruled out.
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        api.calls.clear()

        await coordinator._async_update_data()

        assert api.calls.count("circuits:p1") == 1
        assert not any(c.startswith("live:") for c in api.calls)

    @pytest.mark.asyncio
    async def test_operation_mode_and_program_also_refresh(self):
        """Not just the numbers: the fields driving climate/select state
        must track reality too, or the entities lie in a subtler way.
        """
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "operationMode": "regular",
                "activeProgram": "week1",
            }
        ]
        first = await coordinator._async_update_data()
        coordinator.data = first

        api.circuits_response = [
            {
                "type": "HK",
                "path": "hk-1",
                "name": "Heating",
                "selectable": True,
                "operationMode": "standby",
                "activeProgram": "week2",
            }
        ]
        second = await coordinator._async_update_data()

        circuit = second.plants["p1"].circuits["hk-1"]
        assert circuit.operation_mode == "standby"
        assert circuit.active_program == "week2"

    @pytest.mark.asyncio
    async def test_slow_changing_fields_are_not_clobbered(self):
        """program_names / weather_impact_* come from the full-refresh path
        (programs + settings endpoints, which a health check does NOT
        call). Refreshing values must leave them intact, not blank them.
        """
        coordinator, _api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        before = first.plants["p1"].circuits["hv-1"].program_names
        assert before  # sanity: the full refresh populated them

        second = await coordinator._async_update_data()

        assert second.plants["p1"].circuits["hv-1"].program_names == before

    @pytest.mark.asyncio
    async def test_circuits_failure_does_not_fail_the_health_check(self):
        """A transient circuits-list failure must not flip everything
        unavailable — get_plants() already succeeded, so the cloud is
        demonstrably reachable and the check's primary job is done.
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        original = first.plants["p1"].circuits["hv-1"].actual_value

        api.circuits_response = HovalApiError("transient blip")
        second = await coordinator._async_update_data()  # must not raise

        # Previous values kept rather than wiped.
        assert second.plants["p1"].circuits["hv-1"].actual_value == original

    @pytest.mark.asyncio
    async def test_offline_plant_skips_the_circuits_call(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first

        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": False}
        ]
        api.calls.clear()
        await coordinator._async_update_data()

        assert api.calls == ["plants"]

    @pytest.mark.asyncio
    async def test_has_error_recomputed_from_refreshed_circuits(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "selectable": True, "hasError": False}
        ]
        first = await coordinator._async_update_data()
        coordinator.data = first
        assert first.plants["p1"].has_error is False

        api.circuits_response = [
            {"type": "HK", "path": "hk-1", "name": "Heating", "selectable": True, "hasError": True}
        ]
        second = await coordinator._async_update_data()

        assert second.plants["p1"].has_error is True

    @pytest.mark.asyncio
    async def test_unknown_circuit_in_response_is_ignored_not_added(self):
        """Discovering a genuinely NEW circuit needs the full path
        (programs, settings, entity creation, SIGNAL_NEW_CIRCUITS). The
        value-refresh path must not half-create one.
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        known = set(first.plants["p1"].circuits)

        api.circuits_response = [
            {"type": "HK", "path": "brand-new", "name": "New", "selectable": True}
        ]
        second = await coordinator._async_update_data()

        assert set(second.plants["p1"].circuits) == known

    @pytest.mark.asyncio
    async def test_malformed_values_in_refresh_are_coerced_to_none(self):
        """HVC-008's guard must apply on this path too, not just the full
        refresh — otherwise NaN reaches the sensors by a different route.
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first

        api.circuits_response = [
            {
                "type": "HV",
                "path": "hv-1",
                "name": "Ventilation",
                "selectable": True,
                "actualValue": float("nan"),
                "targetValue": "garbage",
            }
        ]
        second = await coordinator._async_update_data()

        circuit = second.plants["p1"].circuits["hv-1"]
        assert circuit.actual_value is None
        assert circuit.target_value is None


class TestTopologyChangeDetection:
    """Independent audit finding (2026-09, HVC-ICS-001): a plant that was
    offline during initial discovery, or a brand-new plant that appears
    after startup, must not remain entity-less indefinitely. _health_check()
    now detects either condition and upgrades itself to a real discovery
    fetch for that one cycle, instead of depending on some unrelated write
    to incidentally trigger one (which might never happen).
    """

    @pytest.mark.asyncio
    async def test_plant_offline_at_startup_then_online_triggers_discovery(self):
        coordinator, api = _make_coordinator()
        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": False}
        ]
        first = await coordinator._async_update_data()  # startup
        coordinator.data = first
        assert first.plants["p1"].circuits == {}
        assert "circuits:p1" not in api.calls  # offline plants skip circuit calls

        # Plant comes online on the next scheduled health check.
        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": True}
        ]
        api.calls.clear()
        second = await coordinator._async_update_data()

        assert "circuits:p1" in api.calls  # upgraded to a real discovery fetch
        assert set(second.plants["p1"].circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_new_plant_appearing_after_startup_triggers_discovery(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()  # startup — only p1 exists
        coordinator.data = first
        assert set(first.plants) == {"p1"}

        # A second plant appears — e.g. Hoval splitting the account.
        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": True},
            {"plantExternalId": "p2", "description": "New Plant", "isOnline": True},
        ]
        api.calls.clear()
        second = await coordinator._async_update_data()

        assert set(second.plants) == {"p1", "p2"}
        assert "circuits:p2" in api.calls
        assert set(second.plants["p2"].circuits) == {"hv-1", "bl-1"}
        # p1 was untouched by the topology change but still gets a real
        # discovery fetch this cycle too (simplest correct behavior — the
        # upgrade re-runs full discovery for everything, not just the
        # plant that changed).
        assert "circuits:p1" in api.calls

    @pytest.mark.asyncio
    async def test_plant_going_offline_does_not_trigger_discovery(self):
        """The asymmetric case: going online is a topology change worth a
        real fetch; going offline is not — there's nothing new to discover,
        and the existing light health check already handles it correctly
        (circuits carried forward, is_online updated).
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()  # startup — online
        coordinator.data = first
        assert set(first.plants["p1"].circuits) == {"hv-1", "bl-1"}

        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": False}
        ]
        api.calls.clear()
        second = await coordinator._async_update_data()

        assert api.calls == ["plants"]  # still just a light check
        assert second.plants["p1"].is_online is False
        # Circuits carried forward unchanged, not wiped just because the
        # plant went offline.
        assert set(second.plants["p1"].circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_no_topology_change_stays_a_light_check(self):
        """Sibling/regression case for the dispatch test above: an
        already-known, already-online plant with no changes must not
        trigger the upgrade — otherwise every single scheduled tick would
        silently become a full fetch again, defeating the whole point.

        "Light" means no programs/settings calls and no per-circuit calls —
        NOT zero circuit calls: since docs/audit-v1.0.0.md § 19, a light
        check also makes one get_circuits() call per online plant to
        refresh live current values. The distinction that matters for this
        test is that it did not escalate to _fetch_all_data().
        """
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()  # startup
        coordinator.data = first

        api.calls.clear()
        await coordinator._async_update_data()

        assert api.calls == ["plants", "circuits:p1"]
        assert not any(c.startswith(("programs:", "settings:")) for c in api.calls)


class TestHealthCheckDispatch:
    """The core new mechanism this release exists for.

    _async_update_data() must do a full (telemetry-trimmed) resync exactly
    once at startup and again whenever a write requests it
    (_pending_full_refresh), and the minimal _health_check() (one auth call,
    one get_plants(), nothing circuit-specific) on every other scheduled
    tick. See coordinator.py's _async_update_data docstring and
    docs/audit-v1.0.0.md.
    """

    @pytest.mark.asyncio
    async def test_first_call_does_full_discovery(self):
        coordinator, api = _make_coordinator()
        assert coordinator._did_initial_discovery is False

        data = await coordinator._async_update_data()

        assert coordinator._did_initial_discovery is True
        assert "circuits:p1" in api.calls
        assert set(data.plants["p1"].circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_second_scheduled_call_is_a_light_health_check_only(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()  # startup: full discovery
        coordinator.data = first  # normally done by DataUpdateCoordinator.async_refresh()
        api.calls.clear()

        await coordinator._async_update_data()  # scheduled tick: light check

        # plants + ONE circuits call per online plant (to refresh live
        # current values — see _refresh_circuit_values and
        # docs/audit-v1.0.0.md § 19). Deliberately NOT programs/settings,
        # and deliberately not a per-circuit call: those stay on the
        # full-refresh path only.
        assert api.calls == ["plants", "circuits:p1"]

    @pytest.mark.asyncio
    async def test_health_check_preserves_existing_circuits_unchanged(self):
        coordinator, _api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first  # normally done by DataUpdateCoordinator.async_refresh()
        original_circuits = first.plants["p1"].circuits

        second = await coordinator._async_update_data()

        # Same circuit objects carried forward, not refetched/rebuilt.
        assert second.plants["p1"].circuits is original_circuits

    @pytest.mark.asyncio
    async def test_health_check_updates_is_online(self):
        coordinator, api = _make_coordinator()
        first = await coordinator._async_update_data()
        coordinator.data = first
        assert coordinator.data.plants["p1"].is_online is True

        api.plants_response = [
            {"plantExternalId": "p1", "description": "Test Plant", "isOnline": False}
        ]
        data = await coordinator._async_update_data()

        assert data.plants["p1"].is_online is False
        # Circuits are still carried forward even though the plant went offline —
        # unlike a full resync, the light health check never invalidates them.
        assert set(data.plants["p1"].circuits) == {"hv-1", "bl-1"}

    @pytest.mark.asyncio
    async def test_health_check_recomputes_has_error_from_existing_circuits(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = [
            {"type": "HV", "path": "hv-1", "name": "V", "selectable": True, "hasError": True}
        ]
        first = await coordinator._async_update_data()
        coordinator.data = first
        assert first.plants["p1"].has_error is True

        second = await coordinator._async_update_data()  # light health check
        assert second.plants["p1"].has_error is True  # recomputed, not lost

    @pytest.mark.asyncio
    async def test_pending_full_refresh_forces_a_real_resync(self):
        coordinator, api = _make_coordinator()
        await coordinator._async_update_data()  # startup
        api.calls.clear()

        coordinator._pending_full_refresh_since = time.monotonic()
        await coordinator._async_update_data()

        assert "circuits:p1" in api.calls  # full resync happened, not a light check
        assert coordinator._pending_full_refresh_since is None  # cleared after success

    @pytest.mark.asyncio
    async def test_pending_full_refresh_stays_set_if_the_resync_fails(self):
        coordinator, api = _make_coordinator()
        await coordinator._async_update_data()  # startup

        coordinator._pending_full_refresh_since = time.monotonic()
        api.circuits_response = HovalApiError("down")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        # Must retry the full resync next time, not silently downgrade to a
        # light check just because this attempt failed.
        assert coordinator._pending_full_refresh_since is not None

    @pytest.mark.asyncio
    async def test_second_write_during_first_refresh_is_not_lost(self):
        """HVC-002 regression: a second write's refresh request, made while
        an earlier write's own refresh is still fetching, must survive that
        earlier refresh's completion instead of being silently cleared.
        """
        coordinator, api = _make_coordinator()
        await coordinator._async_update_data()  # startup

        # Simulate write A's _do_refresh() having set the flag just before
        # its _async_update_data() call started fetching...
        write_a_requested_at = time.monotonic()
        coordinator._pending_full_refresh_since = write_a_requested_at

        # ...and write B's _do_refresh() setting a NEWER timestamp WHILE
        # write A's fetch is conceptually still in flight. Since FakeApi's
        # calls are synchronous (no real concurrency in this test), we
        # simulate "during" by advancing the flag between capturing
        # do_full_refresh's snapshot and the fetch completing — which is
        # exactly what _async_update_data does internally via
        # refresh_requested_at, so bumping the field here before awaiting
        # is a faithful reproduction of the race.
        original_get_circuits = api.get_circuits

        async def _get_circuits_and_race(plant_id):
            # Write B's request lands here, mid-fetch.
            coordinator._pending_full_refresh_since = time.monotonic()
            return await original_get_circuits(plant_id)

        api.get_circuits = _get_circuits_and_race

        await coordinator._async_update_data()  # serves write A's request

        # Write B's later timestamp must survive — NOT cleared by write A's
        # refresh completing, since write A's fetch could not possibly have
        # captured write B's effect.
        assert coordinator._pending_full_refresh_since is not None
        assert coordinator._pending_full_refresh_since > write_a_requested_at

        # And the next tick must therefore still do a full resync, not
        # silently downgrade to a health-check-only cycle.
        api.calls.clear()
        await coordinator._async_update_data()
        assert "circuits:p1" in api.calls

    @pytest.mark.asyncio
    async def test_mode_override_set_during_refresh_is_not_wiped(self):
        """HVC-002 regression: an optimistic mode override set for a
        DIFFERENT circuit while a refresh is already fetching must not be
        wiped by that refresh's unconditional clear — the refresh's own
        snapshot was taken before the override's write happened, so
        clearing it would show stale data with no optimistic value to
        cover the gap until the next successful refresh.
        """
        coordinator, api = _make_coordinator()
        await coordinator._async_update_data()  # startup — hv-1, bl-1 exist

        original_get_circuits = api.get_circuits

        async def _get_circuits_and_race(plant_id):
            # A write "lands" for bl-1 while this refresh is mid-fetch —
            # after the refresh's fetch_started_at was captured, before its
            # data snapshot (built from this very call's return value) is
            # finalised.
            coordinator.set_mode_override("p1", "bl-1", "standby")
            return await original_get_circuits(plant_id)

        api.get_circuits = _get_circuits_and_race
        coordinator._pending_full_refresh_since = time.monotonic()

        await coordinator._async_update_data()

        # The override for bl-1 must still be present — a plain
        # self._mode_override.clear() would have wiped it.
        assert coordinator.get_mode_override("p1", "bl-1") == "standby"

    @pytest.mark.asyncio
    async def test_mode_override_set_before_refresh_is_still_cleared(self):
        """Sibling case: an override that was already stale BEFORE this
        refresh started fetching must still be cleared as before — only
        overrides racing with an in-flight fetch are protected.
        """
        coordinator, _api = _make_coordinator()
        await coordinator._async_update_data()  # startup

        coordinator.set_mode_override("p1", "hv-1", "standby")
        # Force the next call to be a real full refresh, not a light health
        # check — only a full refresh (_fetch_all_data) ever touches
        # _mode_override at all.
        coordinator._pending_full_refresh_since = time.monotonic()
        await coordinator._async_update_data()

        assert coordinator.get_mode_override("p1", "hv-1") is None

    @pytest.mark.asyncio
    async def test_failed_startup_discovery_is_retried_next_call(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = HovalApiError("down")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert coordinator._did_initial_discovery is False

        api.circuits_response = [{"type": "HV", "path": "hv-1", "name": "V", "selectable": True}]
        data = await coordinator._async_update_data()  # must attempt full discovery again
        assert coordinator._did_initial_discovery is True
        assert "hv-1" in data.plants["p1"].circuits

    @pytest.mark.asyncio
    async def test_health_check_success_records_contact(self):
        coordinator, _api = _make_coordinator()
        await coordinator._async_update_data()  # startup
        assert coordinator.connection_health.last_successful_contact_at is not None

        before = coordinator.connection_health.last_successful_contact_at
        await coordinator._async_update_data()  # health check
        after = coordinator.connection_health.last_successful_contact_at

        assert after >= before

    @pytest.mark.asyncio
    async def test_health_check_does_not_call_get_programs_or_get_circuit_settings(self):
        """The whole point: the recurring scheduled call must be minimal."""
        coordinator, api = _make_coordinator()
        await coordinator._async_update_data()  # startup — calls programs/settings
        api.calls.clear()

        await coordinator._async_update_data()  # scheduled tick

        assert not any(c.startswith(("programs:", "settings:")) for c in api.calls)
