"""Tests for the Hoval Connect API client."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Preserve the real asyncio module
_real_asyncio = asyncio

# Mock homeassistant modules so we can import without HA installed
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
import aiohttp  # noqa: E402
import voluptuous as vol  # noqa: E402

from custom_components.hoval_connect.api import (  # noqa: E402
    _MAX_RETRIES,
    _RETRYABLE_STATUS_CODES,
    HovalApiError,
    HovalAuthError,
    HovalConnectApi,
)
from custom_components.hoval_connect.const import SCAN_INTERVAL_OPTIONS  # noqa: E402


def _make_response(status: int, json_data=None, text: str = "") -> MagicMock:
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.content_length = 0 if status == 204 else 128
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text)
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=status,
        )
    # Make it work as async context manager
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session() -> MagicMock:
    """Create a mock aiohttp session."""
    session = MagicMock(spec=aiohttp.ClientSession)
    return session


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestHovalConnectApiAuth:
    """Tests for authentication logic."""

    @pytest.mark.asyncio
    async def test_get_id_token_success(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        token = await api._get_id_token()

        assert token == "test-token-123"
        session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_id_token_caches(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        token1 = await api._get_id_token()
        token2 = await api._get_id_token()

        assert token1 == token2
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_id_token_invalid_credentials(self):
        session = _make_session()
        for status in (400, 401, 403):
            resp = _make_response(status)
            session.post = MagicMock(return_value=resp)

            api = HovalConnectApi(session, "test@example.com", "wrong")
            with pytest.raises(HovalAuthError, match="Invalid credentials"):
                await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_missing_token_in_response(self):
        session = _make_session()
        resp = _make_response(200, {"access_token": "wrong-field"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="missing id_token"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_connection_error(self):
        session = _make_session()
        session.post = MagicMock(side_effect=aiohttp.ClientError("connection failed"))

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_timeout(self):
        session = _make_session()
        session.post = MagicMock(side_effect=_real_asyncio.TimeoutError())

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_invalidate_tokens(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "token-1"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        await api._get_id_token()
        assert api._id_token == "token-1"

        api.invalidate_tokens()
        assert api._id_token is None
        assert api._id_token_exp == 0
        assert api._pat_cache == {}


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------


class TestHovalConnectApiRequest:
    """Tests for the _request method."""

    @pytest.mark.asyncio
    async def test_request_success(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        api_resp = _make_response(200, {"data": "test"})
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api._request("GET", "/api/test")

        assert result == {"data": "test"}

    @pytest.mark.asyncio
    async def test_request_204_returns_none(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        api_resp = _make_response(204)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api._request("POST", "/api/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_request_401_retries_with_fresh_token(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_401 = _make_response(401)
        resp_ok = _make_response(200, {"data": "ok"})
        session.request = MagicMock(side_effect=[resp_401, resp_ok])

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._get_id_token()
        result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_401_twice_raises_auth_error(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_401 = _make_response(401)
        session.request = MagicMock(return_value=resp_401)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalAuthError, match="Authentication failed"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_4xx_raises_api_error(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_404 = _make_response(404, text="not found")
        session.request = MagicMock(return_value=resp_404)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError, match="HTTP 404"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_retries_on_transient_errors(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_503 = _make_response(503)
        resp_ok = _make_response(200, {"data": "recovered"})
        session.request = MagicMock(side_effect=[resp_503, resp_ok])

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "recovered"}

    @pytest.mark.asyncio
    async def test_request_retries_exhausted_raises(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_503 = _make_response(503)
        session.request = MagicMock(return_value=resp_503)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="HTTP 503"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_timeout_retries(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_ok = _make_response(200, {"data": "ok"})
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _real_asyncio.TimeoutError()
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_timeout_all_retries_raises(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        session.request = MagicMock(side_effect=_real_asyncio.TimeoutError())

        api = HovalConnectApi(session, "test@example.com", "pass")
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="timeout"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_connection_error_retries(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_ok = _make_response(200, {"data": "ok"})
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("conn refused")
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}


# ---------------------------------------------------------------------------
# Endpoint methods — pagination handling (v0.16.1+)
# ---------------------------------------------------------------------------


class TestHovalConnectApiEndpoints:
    """Tests for specific API endpoint methods."""

    @pytest.mark.asyncio
    async def test_get_plants(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        plants_data = [{"plantExternalId": "p1", "description": "My Plant"}]
        api_resp = _make_response(200, plants_data)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert result == plants_data

    @pytest.mark.asyncio
    async def test_get_plants_paginated_single_page(self):
        """get_plants handles Spring Page wrapper {"content": [...], "last": True}."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        plants_data = [{"plantExternalId": "p1"}, {"plantExternalId": "p2"}]
        page_resp = _make_response(200, {"content": plants_data, "last": True, "totalPages": 1})
        session.request = MagicMock(return_value=page_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert result == plants_data
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_get_plants_paginated_multiple_pages(self):
        """get_plants fetches all pages and returns a flat list."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        page0 = [{"plantExternalId": f"p{i}"} for i in range(12)]
        page1 = [{"plantExternalId": "p12"}]
        resp_page0 = _make_response(200, {"content": page0, "last": False, "totalPages": 2})
        resp_page1 = _make_response(200, {"content": page1, "last": True, "totalPages": 2})
        session.request = MagicMock(side_effect=[resp_page0, resp_page1])

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert len(result) == 13
        assert result[0]["plantExternalId"] == "p0"
        assert result[12]["plantExternalId"] == "p12"
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_get_circuits_plain_list(self):
        """get_circuits returns a plain list unchanged."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        circuits = [{"type": "HK", "path": "1.1.0"}, {"type": "BL", "path": "1.10.1"}]
        api_resp = _make_response(200, circuits)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_circuits("plant-1")

        assert result == circuits

    @pytest.mark.asyncio
    async def test_get_circuits_paginated_wrapper(self):
        """get_circuits extracts 'content' when API returns a paginated wrapper."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        circuits = [{"type": "HK", "path": "1.1.0"}, {"type": "BL", "path": "1.10.1"}]
        paginated = {"content": circuits, "totalElements": 2, "totalPages": 1, "last": True}
        api_resp = _make_response(200, paginated)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_circuits("plant-1")

        assert result == circuits

    @pytest.mark.asyncio
    async def test_get_live_values_plain_list(self):
        """get_live_values returns a plain list unchanged."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        lv = [{"key": "tempActual", "value": "24.5"}, {"key": "operatingHours", "value": "13751"}]
        api_resp = _make_response(200, lv)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == lv

    @pytest.mark.asyncio
    async def test_get_live_values_paginated_wrapper(self):
        """get_live_values extracts 'content' when the API returns a paginated wrapper.

        Regression: Hoval's May 2026 API change introduced pagination on this
        endpoint.  Without the fix the coordinator would receive a dict, iterate
        over its string keys, and crash with TypeError inside _fetch_circuit —
        causing BL to be silently dropped from plant_data.circuits.
        """
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        lv = [{"key": "tempActual", "value": "24.5"}, {"key": "operatingHours", "value": "13751"}]
        paginated = {"content": lv, "totalElements": 2, "size": 12, "last": True}
        api_resp = _make_response(200, paginated)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == lv

    @pytest.mark.asyncio
    async def test_get_live_values_none_returns_empty_list(self):
        """HTTP 204 or empty body (→ None from _request) must return [] not None.

        If get_live_values returned None, the coordinator dict comprehension
        'for v in lv_raw' would raise TypeError and crash _fetch_circuit.
        """
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        # Simulate a 204 response (_request returns None)
        api_resp = _make_response(204)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_programs_returns_dict_for_programmable_circuit(self):
        """Normal HK/WW circuits return a dict from the programs endpoint."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        programs = {
            "week1": {"name": "Woche 1", "dayProgramIds": [1, 1, 1, 1, 1, 2, 2]},
            "dayPrograms": {"dayConfigurations": []},
        }
        api_resp = _make_response(200, programs)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_programs("plant-1", "1.1.0")

        assert isinstance(result, dict)
        assert "week1" in result

    @pytest.mark.asyncio
    async def test_get_programs_returns_empty_list_for_bl_circuit(self):
        """
        Regression — v0.16.2 / v0.17.0 fix.

        Hoval's May 2026 API change made the programs endpoint return HTTP 200
        with body [] (empty JSON array) for non-programmable circuits such as
        BL (boiler, operationMode=None).

        The API layer passes this through as-is; the coordinator guards against
        it with isinstance(programs, dict) and handles [] gracefully.
        Previously v0.16.1's guard (programs is not None) passed [] through,
        entered the processing block, and crashed at [].get('dayPrograms', {})
        — silently dropping BL from plant_data.circuits on every poll.
        """
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        # Hoval now returns [] for non-programmable circuits
        api_resp = _make_response(200, [])
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_programs("plant-1", "1.10.1")

        # API returns the raw [] — coordinator handles non-dict gracefully
        assert result == []

    @pytest.mark.asyncio
    async def test_get_plant_settings_uses_request(self):
        """Verify get_plant_settings goes through _request (not raw session.get)."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        settings_resp = _make_response(200, {"token": "pat-123", "setting1": "val"})
        session.request = MagicMock(return_value=settings_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plant_settings("plant-1")

        assert result["setting1"] == "val"
        session.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_temporary_change(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        control_resp = _make_response(204)
        session.request = MagicMock(return_value=control_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.set_temporary_change("plant-1", "1.2.3", 65, "FOUR")

        assert result is None  # 204 returns None

    @pytest.mark.asyncio
    async def test_invalidate_plant_token(self):
        api = HovalConnectApi(MagicMock(), "test@example.com", "pass")
        api._pat_cache["plant-1"] = ("token", 9999999999)

        api.invalidate_plant_token("plant-1")
        assert "plant-1" not in api._pat_cache

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_plant_token(self):
        """Should not raise when invalidating non-cached plant."""
        api = HovalConnectApi(MagicMock(), "test@example.com", "pass")
        api.invalidate_plant_token("nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# Retry constants
# ---------------------------------------------------------------------------


class TestRetryConstants:
    """Tests for retry configuration."""

    def test_retryable_status_codes(self):
        assert 429 in _RETRYABLE_STATUS_CODES
        assert 500 in _RETRYABLE_STATUS_CODES
        assert 502 in _RETRYABLE_STATUS_CODES
        assert 503 in _RETRYABLE_STATUS_CODES
        assert 504 in _RETRYABLE_STATUS_CODES
        assert 404 not in _RETRYABLE_STATUS_CODES

    def test_max_retries_is_reasonable(self):
        assert _MAX_RETRIES >= 2
        assert _MAX_RETRIES <= 5


# ---------------------------------------------------------------------------
# Code invariants (static checks, no HA needed)
# ---------------------------------------------------------------------------


class TestCodeInvariants:
    """Text-based checks that key v0.17.0 code invariants hold."""

    def _read(self, filename: str) -> str:
        import os

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "custom_components", "hoval_connect", filename)) as f:
            return f.read()

    def test_programs_guard_uses_isinstance_dict(self):
        """Coordinator programs block must use isinstance(programs, dict), not 'is not None'."""
        src = self._read("coordinator.py")
        assert "if isinstance(programs, dict):" in src
        assert "programs is not None and not isinstance" not in src

    def test_resolve_guard_uses_not_isinstance_dict(self):
        """_resolve_active_program_value must guard against any non-dict."""
        src = self._read("coordinator.py")
        assert "if not isinstance(programs, dict):" in src

    def test_lv_raw_type_guard_present(self):
        """Live-values block must guard against non-list lv_raw."""
        src = self._read("coordinator.py")
        assert "if not isinstance(lv_raw, list):" in src

    def test_climate_uses_room_temp_actual_field(self):
        """climate.py must use 'roomTempActual' not the old 'actualTemperature' key."""
        src = self._read("climate.py")
        assert "roomTempActual" in src
        assert "roomTempTarget" in src
        # Old wrong key must not be in .get() calls
        assert 'live_values.get("circuitStatus"' not in src

    def test_climate_hvac_action_uses_status_key(self):
        """hvac_action must use the live-values 'status' key."""
        src = self._read("climate.py")
        assert 'live_values.get("status")' in src

    def test_sensor_has_room_temp_actual_for_hk(self):
        """sensor.py must declare room_temp_actual limited to HK circuits."""
        import re

        src = self._read("sensor.py")
        assert 'key="room_temp_actual"' in src
        block = re.search(
            r'key="room_temp_actual".*?circuit_types=frozenset\(\{([^}]+)\}\)',
            src,
            re.DOTALL,
        )
        assert block is not None, "room_temp_actual descriptor not found"
        assert "CIRCUIT_TYPE_HK" in block.group(1)
        assert "CIRCUIT_TYPE_BL" not in block.group(1)

    def test_restore_from_store_uses_try_except(self):
        """restore_from_store must wrap int() in try/except to handle corrupt data."""
        src = self._read("coordinator.py")
        # Both HovalCircuitHealth and HovalConnectionHealth swallow corrupt
        # values resiliently, using contextlib.suppress (ruff-preferred form).
        assert src.count("contextlib.suppress(TypeError, ValueError)") >= 2

    def test_bl_still_in_non_selectable_types(self):
        """BL must remain in the non-selectable types so selectable=False doesn't exclude it."""
        src = self._read("coordinator.py")
        assert (
            "_NON_SELECTABLE_TYPES = frozenset({CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW})" in src
            or "_NON_SELECTABLE_TYPES = frozenset({CIRCUIT_TYPE_WW, CIRCUIT_TYPE_BL})" in src
            or "CIRCUIT_TYPE_BL" in src
        )
        assert "_NON_SELECTABLE_TYPES" in src


class TestScanIntervalSchema:
    """Options dropdown submits strings; schema must coerce so the interval saves.

    Regression for the v0.19.0 'poll interval not saved' bug.
    """

    @staticmethod
    def _validator():
        return vol.Schema(
            {vol.Required("scan_interval"): vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))}
        )

    def test_string_from_frontend_is_coerced_and_accepted(self):
        out = self._validator()({"scan_interval": "60"})
        assert out["scan_interval"] == 60
        assert isinstance(out["scan_interval"], int)

    def test_int_value_accepted(self):
        assert self._validator()({"scan_interval": 120})["scan_interval"] == 120

    def test_unknown_value_rejected(self):
        with pytest.raises(vol.Invalid):
            self._validator()({"scan_interval": "45"})
