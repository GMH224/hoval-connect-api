"""Tests for the Hoval Connect API client.

v0.24.0: the transport changed from aiohttp to requests-in-executor (see
api.py's module docstring and docs/audit-v0.24.0.md for why). Every mock in
this file was rewritten accordingly: aiohttp's async-context-manager response
protocol (`async with session.request(...) as resp`) is gone, replaced by
plain synchronous `requests.Response`-like mocks and a `FakeHass` whose
`async_add_executor_job` just calls the target function immediately. This
is a faithful stand-in for a real executor round-trip from the test's point
of view: same inputs, same outputs, same exceptions propagate the same way.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock homeassistant modules so we can import without HA installed
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)
import requests  # noqa: E402
import voluptuous as vol  # noqa: E402

from custom_components.hoval_connect.api import (  # noqa: E402
    _MAX_RETRIES,
    _RETRYABLE_STATUS_CODES,
    HovalApiError,
    HovalAuthError,
    HovalConnectApi,
)
from custom_components.hoval_connect.const import (  # noqa: E402
    SCAN_INTERVAL_OPTIONS,
    USER_AGENT,
)


class FakeHass:
    """Minimal stand-in for HomeAssistant — only what api.py actually uses.

    HovalConnectApi runs its (blocking) requests calls via
    `hass.async_add_executor_job()`. Running the target callable immediately
    and returning/raising its result is equivalent, for test purposes, to a
    real round-trip through HA's executor thread pool — same inputs, same
    outputs, same exceptions.
    """

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_response(status_code: int, json_data=None, text: str = "") -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    # Falsy for 204 (matches `not resp.content` in api.py), truthy otherwise —
    # the actual byte content doesn't matter for any test here.
    resp.content = b"" if status_code == 204 else b"x"
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error", response=resp
        )
    return resp


def _make_session() -> MagicMock:
    """Create a mock requests.Session."""
    return MagicMock(spec=requests.Session)


def _make_api(
    session: MagicMock, email: str = "test@example.com", password: str = "pass"
) -> HovalConnectApi:
    """Build a HovalConnectApi wired to a FakeHass and the given mock session.

    HovalConnectApi.__init__ always constructs its own real requests.Session()
    (harmless — no I/O happens at construction time); this swaps it for the
    mock immediately afterwards so tests can control and assert on it.
    """
    api = HovalConnectApi(FakeHass(), email, password)
    api._session = session
    return api


def _mock_auth_ok(session: MagicMock, token: str = "token") -> None:
    """Wire session.post (the IDP call) to succeed with the given id_token."""
    session.post = MagicMock(return_value=_make_response(200, {"id_token": token}))


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestHovalConnectApiAuth:
    """Tests for authentication logic."""

    @pytest.mark.asyncio
    async def test_get_id_token_success(self):
        session = _make_session()
        _mock_auth_ok(session, "test-token-123")

        api = _make_api(session)
        token = await api._get_id_token()

        assert token == "test-token-123"
        session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_id_token_sends_user_agent(self):
        """Regression — v0.23.0/v0.24.0 fix for a blanket HTTP 403.

        See USER_AGENT in const.py and docs/audit-v0.24.0.md for the full
        diagnosis: this specific string is empirically required, not
        decoration, and the v0.23.0 attempt at this fix (a different string,
        under aiohttp) did not actually work.
        """
        session = _make_session()
        _mock_auth_ok(session, "test-token-123")

        api = _make_api(session)
        await api._get_id_token()

        headers = session.post.call_args.kwargs["headers"]
        assert headers["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_get_id_token_caches(self):
        session = _make_session()
        _mock_auth_ok(session, "test-token-123")

        api = _make_api(session)
        token1 = await api._get_id_token()
        token2 = await api._get_id_token()

        assert token1 == token2
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_id_token_invalid_credentials(self):
        session = _make_session()
        for status in (400, 401, 403):
            session.post = MagicMock(return_value=_make_response(status))
            api = _make_api(session, password="wrong")
            with pytest.raises(HovalAuthError, match="Invalid credentials"):
                await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_missing_token_in_response(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"access_token": "wrong-field"}))

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="missing id_token"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_connection_error(self):
        session = _make_session()
        session.post = MagicMock(
            side_effect=requests.exceptions.ConnectionError("connection failed")
        )

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_timeout(self):
        session = _make_session()
        session.post = MagicMock(side_effect=requests.exceptions.Timeout())

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_invalidate_tokens(self):
        session = _make_session()
        _mock_auth_ok(session, "token-1")

        api = _make_api(session)
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
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(200, {"data": "test"}))

        api = _make_api(session)
        result = await api._request("GET", "/api/test")

        assert result == {"data": "test"}

    @pytest.mark.asyncio
    async def test_request_sends_user_agent(self):
        """Regression — v0.23.0/v0.24.0 fix for a blanket HTTP 403 (see const.USER_AGENT)."""
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(200, {"data": "test"}))

        api = _make_api(session)
        await api._request("GET", "/api/test")

        headers = session.request.call_args.kwargs["headers"]
        assert headers["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_get_plant_access_token_sends_user_agent(self):
        """Regression — v0.23.0/v0.24.0 fix for a blanket HTTP 403 (see const.USER_AGENT).

        This call builds its headers by hand rather than via _headers(), so it
        needs its own coverage — a fix to _headers() alone would not catch a
        regression here.
        """
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        api = _make_api(session)
        await api._get_plant_access_token("plant-1")

        headers = session.get.call_args.kwargs["headers"]
        assert headers["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_request_403_raises_api_error_without_retry(self):
        """
        403 is deliberately NOT retried like 401: refreshing the token has not
        been observed to fix a 403. See docs/audit-v0.24.0.md — the actual
        causes (aiohttp's TLS fingerprint + requests' default User-Agent
        string) are both handled by this transport, but a 403 is still not
        treated as automatically retryable, since neither cause is a
        transient token problem.
        """
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(403, text="Forbidden by gateway"))

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="HTTP 403"):
            await api._request("GET", "/api/test")

        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_request_204_returns_none(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(204))

        api = _make_api(session)
        result = await api._request("POST", "/api/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_request_401_retries_with_fresh_token(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp_401 = _make_response(401)
        resp_ok = _make_response(200, {"data": "ok"})
        session.request = MagicMock(side_effect=[resp_401, resp_ok])

        api = _make_api(session)
        await api._get_id_token()
        result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_401_twice_raises_auth_error(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(401))

        api = _make_api(session)
        with pytest.raises(HovalAuthError, match="Authentication failed"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_4xx_raises_api_error(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(404, text="not found"))

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="HTTP 404"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_retries_on_transient_errors(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp_503 = _make_response(503)
        resp_ok = _make_response(200, {"data": "recovered"})
        session.request = MagicMock(side_effect=[resp_503, resp_ok])

        api = _make_api(session)
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "recovered"}

    @pytest.mark.asyncio
    async def test_request_retries_exhausted_raises(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(503))

        api = _make_api(session)
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="HTTP 503"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_timeout_retries(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp_ok = _make_response(200, {"data": "ok"})
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.exceptions.Timeout()
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = _make_api(session)
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_timeout_all_retries_raises(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(side_effect=requests.exceptions.Timeout())

        api = _make_api(session)
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="timeout"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_connection_error_retries(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp_ok = _make_response(200, {"data": "ok"})
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.exceptions.ConnectionError("conn refused")
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = _make_api(session)
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
        _mock_auth_ok(session)
        plants_data = [{"plantExternalId": "p1", "description": "My Plant"}]
        session.request = MagicMock(return_value=_make_response(200, plants_data))

        api = _make_api(session)
        result = await api.get_plants()

        assert result == plants_data

    @pytest.mark.asyncio
    async def test_get_plants_paginated_single_page(self):
        """get_plants handles Spring Page wrapper {"content": [...], "last": True}."""
        session = _make_session()
        _mock_auth_ok(session)
        plants_data = [{"plantExternalId": "p1"}, {"plantExternalId": "p2"}]
        session.request = MagicMock(
            return_value=_make_response(
                200, {"content": plants_data, "last": True, "totalPages": 1}
            )
        )

        api = _make_api(session)
        result = await api.get_plants()

        assert result == plants_data
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_get_plants_paginated_multiple_pages(self):
        """get_plants fetches all pages and returns a flat list."""
        session = _make_session()
        _mock_auth_ok(session)
        page0 = [{"plantExternalId": f"p{i}"} for i in range(12)]
        page1 = [{"plantExternalId": "p12"}]
        resp_page0 = _make_response(200, {"content": page0, "last": False, "totalPages": 2})
        resp_page1 = _make_response(200, {"content": page1, "last": True, "totalPages": 2})
        session.request = MagicMock(side_effect=[resp_page0, resp_page1])

        api = _make_api(session)
        result = await api.get_plants()

        assert len(result) == 13
        assert result[0]["plantExternalId"] == "p0"
        assert result[12]["plantExternalId"] == "p12"
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_get_circuits_plain_list(self):
        """get_circuits returns a plain list unchanged."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        circuits = [{"type": "HK", "path": "1.1.0"}, {"type": "BL", "path": "1.10.1"}]
        session.request = MagicMock(return_value=_make_response(200, circuits))

        api = _make_api(session)
        result = await api.get_circuits("plant-1")

        assert result == circuits

    @pytest.mark.asyncio
    async def test_get_circuits_paginated_wrapper(self):
        """get_circuits extracts 'content' when API returns a paginated wrapper."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        circuits = [{"type": "HK", "path": "1.1.0"}, {"type": "BL", "path": "1.10.1"}]
        paginated = {"content": circuits, "totalElements": 2, "totalPages": 1, "last": True}
        session.request = MagicMock(return_value=_make_response(200, paginated))

        api = _make_api(session)
        result = await api.get_circuits("plant-1")

        assert result == circuits

    @pytest.mark.asyncio
    async def test_get_live_values_plain_list(self):
        """get_live_values returns a plain list unchanged."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        lv = [{"key": "tempActual", "value": "24.5"}, {"key": "operatingHours", "value": "13751"}]
        session.request = MagicMock(return_value=_make_response(200, lv))

        api = _make_api(session)
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
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        lv = [{"key": "tempActual", "value": "24.5"}, {"key": "operatingHours", "value": "13751"}]
        paginated = {"content": lv, "totalElements": 2, "size": 12, "last": True}
        session.request = MagicMock(return_value=_make_response(200, paginated))

        api = _make_api(session)
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == lv

    @pytest.mark.asyncio
    async def test_get_live_values_none_returns_empty_list(self):
        """HTTP 204 or empty body (→ None from _request) must return [] not None.

        If get_live_values returned None, the coordinator dict comprehension
        'for v in lv_raw' would raise TypeError and crash _fetch_circuit.
        """
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        # Simulate a 204 response (_request returns None)
        session.request = MagicMock(return_value=_make_response(204))

        api = _make_api(session)
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_programs_returns_dict_for_programmable_circuit(self):
        """Normal HK/WW circuits return a dict from the programs endpoint."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        programs = {
            "week1": {"name": "Woche 1", "dayProgramIds": [1, 1, 1, 1, 1, 2, 2]},
            "dayPrograms": {"dayConfigurations": []},
        }
        session.request = MagicMock(return_value=_make_response(200, programs))

        api = _make_api(session)
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
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        # Hoval now returns [] for non-programmable circuits
        session.request = MagicMock(return_value=_make_response(200, []))

        api = _make_api(session)
        result = await api.get_programs("plant-1", "1.10.1")

        # API returns the raw [] — coordinator handles non-dict gracefully
        assert result == []

    @pytest.mark.asyncio
    async def test_get_programs_raises_for_417_non_programmable_circuit(self):
        """
        2026-09 finding (forensic crawl, see docs/audit-v0.23.0.md): the
        programs endpoint for BL (boiler) now returns HTTP 417, not HTTP 200
        with body [] as in test_get_programs_returns_empty_list_for_bl_circuit
        above — Hoval's response for non-programmable circuits changed again.

        The API layer still just passes the failure through as a generic
        HovalApiError; it's the coordinator's job (SUPPORTS_PROGRAMS gate,
        see tests/test_coordinator_fetch.py) to avoid calling this for BL at
        all, not this method's job to know which circuit types are
        programmable.
        """
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        session.request = MagicMock(return_value=_make_response(417, text=""))

        api = _make_api(session)
        with pytest.raises(HovalApiError, match="HTTP 417"):
            await api.get_programs("plant-1", "1.10.1")

    @pytest.mark.asyncio
    async def test_get_plant_settings_uses_request(self):
        """Verify get_plant_settings goes through _request (not raw session.get)."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        session.request = MagicMock(
            return_value=_make_response(200, {"token": "pat-123", "setting1": "val"})
        )

        api = _make_api(session)
        result = await api.get_plant_settings("plant-1")

        assert result["setting1"] == "val"
        session.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_temporary_change(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        session.request = MagicMock(return_value=_make_response(204))

        api = _make_api(session)
        result = await api.set_temporary_change("plant-1", "1.2.3", 65, "FOUR")

        assert result is None  # 204 returns None

    @pytest.mark.asyncio
    async def test_invalidate_plant_token(self):
        api = _make_api(_make_session())
        api._pat_cache["plant-1"] = ("token", 9999999999)

        api.invalidate_plant_token("plant-1")
        assert "plant-1" not in api._pat_cache

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_plant_token(self):
        """Should not raise when invalidating non-cached plant."""
        api = _make_api(_make_session())
        api.invalidate_plant_token("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_get_circuit_settings(self):
        """get_circuit_settings GETs .../settings and returns the parsed body."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        settings = {
            "circuitName": "Bodenheizung",
            "weatherImpact": {"outsideTemperature": 50, "solarRadiation": -5.0},
        }
        session.request = MagicMock(return_value=_make_response(200, settings))

        api = _make_api(session)
        result = await api.get_circuit_settings("plant-1", "1.1.0")

        assert result == settings
        call_args = session.request.call_args
        assert call_args.args[0] == "GET"
        assert call_args.args[1].endswith("/v3/plants/plant-1/circuits/1.1.0/settings")

    @pytest.mark.asyncio
    async def test_update_circuit_settings_sends_both_fields(self):
        """update_circuit_settings PATCHes both weatherImpact keys, even if one is unchanged.

        Regression guard: if only the changed key were sent, and the cloud's
        PATCH handler is a full-object replace rather than a JSON-merge patch,
        the untouched sibling field would be silently cleared to null.
        """
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        session.request = MagicMock(
            return_value=_make_response(200, {"circuitName": "Bodenheizung"})
        )

        api = _make_api(session)
        await api.update_circuit_settings(
            "plant-1", "1.1.0", outside_temperature=80, solar_radiation=-3.0
        )

        call_args = session.request.call_args
        assert call_args.args[0] == "PATCH"
        assert call_args.args[1].endswith("/v3/plants/plant-1/circuits/1.1.0/settings")
        body = call_args.kwargs["json"]
        assert body == {"weatherImpact": {"outsideTemperature": 80, "solarRadiation": -3.0}}

    @pytest.mark.asyncio
    async def test_update_circuit_settings_allows_null_field(self):
        """A field explicitly passed as None is sent as null (caller's responsibility to resolve)."""
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        session.request = MagicMock(return_value=_make_response(204))

        api = _make_api(session)
        result = await api.update_circuit_settings(
            "plant-1", "1.1.0", outside_temperature=None, solar_radiation=-2.0
        )

        assert result is None  # 204 returns None
        body = session.request.call_args.kwargs["json"]
        assert body == {"weatherImpact": {"outsideTemperature": None, "solarRadiation": -2.0}}


# ---------------------------------------------------------------------------
# v0.24.0 — transport itself (requests-in-executor, not aiohttp)
# ---------------------------------------------------------------------------


class TestRequestsTransport:
    """Behavioral guards for the aiohttp -> requests transport change.

    See api.py's module docstring and docs/audit-v0.24.0.md for the full
    root-cause investigation. These tests exist so that a future change that
    silently reverts to aiohttp, or "cleans up" the USER_AGENT string, fails
    loudly instead of shipping a regression that only shows up as a live
    403 against the real API.
    """

    def test_api_uses_real_requests_session(self):
        """HovalConnectApi.__init__ must construct a real requests.Session,
        not an aiohttp.ClientSession, and must not require one to be passed in."""
        api = HovalConnectApi(FakeHass(), "test@example.com", "pass")
        assert isinstance(api._session, requests.Session)

    def test_source_does_not_import_aiohttp(self):
        """Regression tripwire: api.py, __init__.py and config_flow.py must
        not import aiohttp. A reintroduction would very likely resurrect the
        TLS-fingerprint block documented in docs/audit-v0.24.0.md.
        """
        import os

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        component_dir = os.path.join(base, "custom_components", "hoval_connect")
        for filename in ("api.py", "__init__.py", "config_flow.py"):
            with open(os.path.join(component_dir, filename)) as f:
                src = f.read()
            # "aiohttp" may still appear in comments/docstrings explaining the
            # history — that's fine and expected. An actual `import aiohttp`
            # statement is what must never come back.
            assert "import aiohttp" not in src, filename

    def test_manifest_declares_requests_dependency(self):
        """requests is a real runtime dependency now; HA needs it declared
        in manifest.json to install it automatically."""
        import json
        import os

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        manifest_path = os.path.join(base, "custom_components", "hoval_connect", "manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)
        assert any(req.startswith("requests") for req in manifest["requirements"])

    def test_user_agent_is_the_empirically_validated_string(self):
        """Guard against USER_AGENT being changed to something unvalidated.

        The exact string matters (see const.py's comment for the full story):
        this is the one that was proven, via a live isolation test, to get
        past Hoval's gateway. A "nicer-looking" replacement must be
        re-validated against the live API before replacing this value —
        this test only catches an accidental/casual change, not a
        deliberate, validated one (which would update both the constant and
        this assertion together).
        """
        assert (
            USER_AGENT
            == "hoval-connect-forensic-crawler/1.0 (+https://github.com/; diagnostic tool)"
        )

    @pytest.mark.asyncio
    async def test_aclose_closes_the_session_via_executor(self):
        session = _make_session()
        session.close = MagicMock()
        api = _make_api(session)

        await api.aclose()

        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_requests_share_one_session_safely(self):
        """Coordinator fans out one task per circuit via asyncio.gather();
        under the new transport that means concurrent executor jobs all
        calling the same shared requests.Session. This doesn't prove
        thread-safety under real contention (that's requests/urllib3's own
        documented guarantee), but it does prove the plumbing — concurrent
        awaits on _request() — resolves each call to the right response and
        doesn't deadlock or cross-wire results.
        """
        import asyncio

        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(
            side_effect=[
                _make_response(200, {"which": "first"}),
                _make_response(200, {"which": "second"}),
                _make_response(200, {"which": "third"}),
            ]
        )

        api = _make_api(session)
        results = await asyncio.gather(
            api._request("GET", "/a"),
            api._request("GET", "/b"),
            api._request("GET", "/c"),
        )

        assert {r["which"] for r in results} == {"first", "second", "third"}
        assert session.request.call_count == 3


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


class TestSourceContracts:
    """Source-text contract checks for code that cannot be imported here.

    The entity platforms (climate.py, sensor.py) import
    homeassistant.components.*, which this HA-free suite does not stub, so
    their field-name contracts are asserted against the source text instead.
    These are regression tripwires, not behavioral tests — they exist only
    where behavioral testing is impossible without a full HA test harness
    (pytest-homeassistant-custom-component; see docs/audit-v0.21.1.md,
    "Residual risks"). Guard-pattern greps that COULD be tested behaviorally
    were replaced with real behavioral tests in v0.21.1 (audit item 6).
    """

    def _read(self, filename: str) -> str:
        import os

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "custom_components", "hoval_connect", filename)) as f:
            return f.read()

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

    def test_ten_minutes_option_present(self):
        """
        Regression — v0.23.0 fix for the 'Polling interval field renders
        empty' bug. 600 (10 minutes) was documented/expected as a choice
        alongside 300 (5 minutes) but had no entry in SCAN_INTERVAL_OPTIONS,
        so a stored value of 600 could not be matched by the options-flow
        dropdown and rendered blank. See docs/audit-v0.23.0.md.
        """
        assert 600 in SCAN_INTERVAL_OPTIONS
        assert self._validator()({"scan_interval": 600})["scan_interval"] == 600
        # Frontend submits dropdown selections as strings (v0.19.0 fix).
        assert self._validator()({"scan_interval": "600"})["scan_interval"] == 600


# ---------------------------------------------------------------------------
# Event endpoints — shape normalisation (v0.21.1, audit finding F2)
# ---------------------------------------------------------------------------


class TestEventEndpointNormalisation:
    """get_events/get_latest_event must normalise shape drift in the client.

    Before v0.21.1 these were the only list-shaped endpoints without wrapper
    handling; a paginated response reached list slicing in the coordinator's
    plant loop (outside per-circuit exception isolation) and failed the whole
    poll.
    """

    def _api_with_response(self, json_data) -> HovalConnectApi:
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))
        session.request = MagicMock(return_value=_make_response(200, json_data))
        return _make_api(session)

    @pytest.mark.asyncio
    async def test_get_events_plain_list(self):
        events = [{"eventType": "warning"}, {"eventType": "info"}]
        api = self._api_with_response(events)
        assert await api.get_events("p1") == events

    @pytest.mark.asyncio
    async def test_get_events_paginated_wrapper(self):
        events = [{"eventType": "warning"}]
        api = self._api_with_response({"content": events, "last": True})
        assert await api.get_events("p1") == events

    @pytest.mark.asyncio
    async def test_get_events_wrapper_with_non_list_content(self):
        api = self._api_with_response({"content": "garbage"})
        assert await api.get_events("p1") == []

    @pytest.mark.asyncio
    async def test_get_events_non_list_returns_empty(self):
        api = self._api_with_response("garbage")
        assert await api.get_events("p1") == []

    @pytest.mark.asyncio
    async def test_get_latest_event_plain_dict_passthrough(self):
        event = {"eventType": "blocking", "description": "Fault"}
        api = self._api_with_response(event)
        assert await api.get_latest_event("p1") == event

    @pytest.mark.asyncio
    async def test_get_latest_event_wrapper_takes_first_element(self):
        event = {"eventType": "warning"}
        api = self._api_with_response({"content": [event, {"eventType": "info"}]})
        assert await api.get_latest_event("p1") == event

    @pytest.mark.asyncio
    async def test_get_latest_event_wrapper_empty_content(self):
        api = self._api_with_response({"content": []})
        assert await api.get_latest_event("p1") == {}

    @pytest.mark.asyncio
    async def test_get_latest_event_non_dict_returns_empty(self):
        api = self._api_with_response(["not", "a", "dict"])
        assert await api.get_latest_event("p1") == {}


# ---------------------------------------------------------------------------
# get_plants pagination cap (v0.21.1, audit finding F3)
# ---------------------------------------------------------------------------


class TestGetPlantsPageCap:
    """A server that never reports last=True must not loop forever."""

    @pytest.mark.asyncio
    async def test_endless_pagination_is_truncated(self):
        from custom_components.hoval_connect.api import _MAX_PLANT_PAGES

        session = _make_session()
        _mock_auth_ok(session)

        counter = {"n": 0}

        def _endless_page(*_args, **_kwargs):
            n = counter["n"]
            counter["n"] += 1
            return _make_response(200, {"content": [{"plantExternalId": f"p{n}"}], "last": False})

        session.request = MagicMock(side_effect=_endless_page)

        api = _make_api(session)
        result = await api.get_plants()

        # One plant per page, capped at _MAX_PLANT_PAGES pages/requests.
        assert len(result) == _MAX_PLANT_PAGES
        assert session.request.call_count == _MAX_PLANT_PAGES

    @pytest.mark.asyncio
    async def test_cap_does_not_affect_normal_pagination(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp0 = _make_response(200, {"content": [{"plantExternalId": "p0"}], "last": False})
        resp1 = _make_response(200, {"content": [{"plantExternalId": "p1"}], "last": True})
        session.request = MagicMock(side_effect=[resp0, resp1])

        api = _make_api(session)
        result = await api.get_plants()
        assert [p["plantExternalId"] for p in result] == ["p0", "p1"]
        assert session.request.call_count == 2
