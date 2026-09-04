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
    HovalDataCoordinator,
    _CircuitListError,
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
        self.live_values_response: Any = [{"key": "airVolume", "value": "45"}]
        self.programs_response: Any = _VALID_PROGRAMS
        self.settings_response: Any = {
            "circuitName": "HK",
            "weatherImpact": {"outsideTemperature": 70, "solarRadiation": -3.5},
        }
        self.latest_event_response: Any = {
            "eventType": "warning",
            "description": "Filter",
            "timeOccurred": "2026-07-19T10:00:00+00:00",
        }
        self.events_response: Any = [
            {"eventType": "warning", "description": "Filter"},
            {"eventType": "info", "description": "OK", "timeResolved": "2026-07-19T11:00:00+00:00"},
        ]
        self.weather_response: Any = [
            {"weatherType": "sunny", "outsideTemperature": 21.5, "outsideTemperatureMin": 12.0}
        ]
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

    async def get_live_values(self, plant_id, path, ctype):
        self.calls.append(f"live:{path}")
        return await self._maybe(self.live_values_response)

    async def get_programs(self, plant_id, path):
        self.calls.append(f"programs:{path}")
        return await self._maybe(self.programs_response)

    async def get_circuit_settings(self, plant_id, path):
        self.calls.append(f"settings:{path}")
        return await self._maybe(self.settings_response)

    async def get_latest_event(self, plant_id):
        self.calls.append(f"latest_event:{plant_id}")
        return await self._maybe(self.latest_event_response)

    async def get_events(self, plant_id):
        self.calls.append(f"events:{plant_id}")
        return await self._maybe(self.events_response)

    async def get_weather(self, plant_id):
        self.calls.append(f"weather:{plant_id}")
        return await self._maybe(self.weather_response)

    def invalidate_plant_token(self, plant_id):
        self.invalidated.append(plant_id)


def _make_coordinator(api: FakeApi | None = None) -> tuple[HovalDataCoordinator, FakeApi]:
    api = api or FakeApi()
    # v2.2.0: the coordinator takes the config entry explicitly instead of
    # relying on HA's current_entry ContextVar.
    coordinator = HovalDataCoordinator(MagicMock(), MagicMock(), api, MagicMock())
    return coordinator, api


# ---------------------------------------------------------------------------
# _fetch_all_data — happy path
# ---------------------------------------------------------------------------


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
    async def test_live_values_and_program_fields(self):
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits["hv-1"]
        assert hv.live_values == {"airVolume": "45"}
        assert hv.active_week_name == "Woche 1"
        assert hv.active_day_program_name == "Normal"
        assert hv.program_air_volume == 60
        assert hv.program_names == {"week1": "Woche 1", "week2": "Woche 2"}

    @pytest.mark.asyncio
    async def test_events_and_weather_parsed(self):
        coordinator, _api = _make_coordinator()
        data = await coordinator._fetch_all_data()
        plant = data.plants["p1"]
        assert plant.latest_event is not None
        assert plant.latest_event.event_type == "warning"
        assert len(plant.events) == 2
        assert plant.has_error is True  # active warning is a problem event
        assert plant.weather is not None
        assert plant.weather.outside_temperature == 21.5

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
    async def test_program_and_event_caches_reused_within_ttl(self):
        coordinator, api = _make_coordinator()
        await coordinator._fetch_all_data()
        await coordinator._fetch_all_data()
        # Programs, events, latest-event and weather fetched once; live values twice.
        assert api.calls.count("programs:hv-1") == 1
        assert api.calls.count("events:p1") == 1
        assert api.calls.count("latest_event:p1") == 1
        assert api.calls.count("weather:p1") == 1
        assert api.calls.count("live:hv-1") == 2

    @pytest.mark.asyncio
    async def test_mode_override_cleared_after_success(self):
        coordinator, _api = _make_coordinator()
        coordinator.set_mode_override("hv-1", "standby")
        await coordinator._fetch_all_data()
        assert coordinator.get_mode_override("hv-1") is None

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
# _fetch_all_data — degradation paths (audit F1 / F2 acceptance tests)
# ---------------------------------------------------------------------------


class TestFetchAllDataDegradation:
    @pytest.mark.asyncio
    async def test_f1_malformed_programs_keep_circuit_and_live_values(self):
        """AUDIT F1: nested program drift must not drop the circuit."""
        coordinator, api = _make_coordinator()
        api.programs_response = {
            "dayPrograms": {"dayConfigurations": [{"name": "no-id"}]},
            "week1": ["wrong-shape"],
        }
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits.get("hv-1")
        assert hv is not None, "circuit must survive program schema drift"
        assert hv.live_values == {"airVolume": "45"}
        assert hv.active_week_name is None
        assert hv.program_air_volume is None

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
    async def test_live_values_error_records_circuit_failure(self):
        coordinator, api = _make_coordinator()
        api.live_values_response = HovalApiError("boom")
        data = await coordinator._fetch_all_data()
        hv = data.plants["p1"].circuits["hv-1"]
        assert hv.live_values == {}
        assert hv.circuit_consecutive_failures == 1
        ch = coordinator.connection_health.get_circuit_health("hv-1")
        assert ch.total_failures == 1

    @pytest.mark.asyncio
    async def test_live_values_unexpected_dict_treated_as_empty(self):
        """The lv_raw type guard, previously only grep-asserted."""
        coordinator, api = _make_coordinator()
        api.live_values_response = {"unexpected": "dict"}
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].circuits["hv-1"].live_values == {}

    @pytest.mark.asyncio
    async def test_f2_events_shape_drift_does_not_fail_poll(self):
        """AUDIT F2: a hostile events shape must not take the poll down."""
        coordinator, api = _make_coordinator()
        # Simulate an unnormalised wrapper sneaking past the API layer.
        api.events_response = {"content": [{"eventType": "warning"}], "last": True}
        api.latest_event_response = ["not", "a", "dict"]
        data = await coordinator._fetch_all_data()  # must not raise
        plant = data.plants["p1"]
        assert plant.latest_event is None
        assert plant.events == []
        assert "hv-1" in plant.circuits  # rest of the poll intact

    @pytest.mark.asyncio
    async def test_f2_non_dict_entries_in_events_list_filtered(self):
        coordinator, api = _make_coordinator()
        api.events_response = ["garbage", {"eventType": "warning"}, 42]
        data = await coordinator._fetch_all_data()
        assert len(data.plants["p1"].events) == 1
        assert data.plants["p1"].events[0].event_type == "warning"

    @pytest.mark.asyncio
    async def test_events_failure_reuses_cache(self):
        coordinator, api = _make_coordinator()
        await coordinator._fetch_all_data()
        # Expire the events cache, then fail the endpoints.
        coordinator._events_cache["p1"] = (
            coordinator._events_cache["p1"][0],
            coordinator._events_cache["p1"][1],
            -10_000.0,
        )
        api.events_response = HovalApiError("down")
        api.latest_event_response = HovalApiError("down")
        data = await coordinator._fetch_all_data()
        # Cached events from the first poll are reused.
        assert data.plants["p1"].latest_event is not None
        assert len(data.plants["p1"].events) == 2

    @pytest.mark.asyncio
    async def test_weather_malformed_first_element_ignored(self):
        coordinator, api = _make_coordinator()
        api.weather_response = ["not-a-dict"]
        data = await coordinator._fetch_all_data()
        assert data.plants["p1"].weather is None

    @pytest.mark.asyncio
    async def test_circuit_list_failure_raises_circuit_list_error(self):
        coordinator, api = _make_coordinator()
        api.circuits_response = HovalApiError("410 gone")
        with pytest.raises(_CircuitListError):
            await coordinator._fetch_all_data()


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
        cached = coordinator._settings_cache["hk-1"]
        coordinator._settings_cache["hk-1"] = (cached[0], 0.0)
        api.settings_response = HovalApiError("down")
        data = await coordinator._fetch_all_data()
        hk = data.plants["p1"].circuits["hk-1"]
        assert hk.weather_impact_supported is True  # stale cache reused
        assert hk.weather_impact_outside_temperature == 70
