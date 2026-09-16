"""Tests for the v1.0.2 fixes to custom_components/hoval_connect/api.py.

Each test class is named after the finding it verifies (see
docs/audit-v1.0.2.md). Uses the same mocking conventions as test_api.py.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

from custom_components.hoval_connect.api import (  # noqa: E402
    HovalApiError,
    HovalConnectApi,
    _CircuitBreaker,
    _require_identifier,
    _retry_delay,
    _url,
    redact_remote_error_body,
)


class FakeHass:
    """Same contract as test_api.py's FakeHass: run the target immediately."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class SlowFakeHass:
    """Executor stand-in that yields control (via asyncio.sleep) before
    running `func`, so a Task wrapping it can be cancelled from outside
    while genuinely still "in flight" — used to test that _run_blocking's
    asyncio.shield() correctly decouples caller-cancellation from job
    completion (ICS-CRIT-001/002).
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def async_add_executor_job(self, func, *args):
        self.started.set()
        await asyncio.sleep(self.delay)
        result = func(*args)
        self.finished.set()
        return result


def _make_response(status_code: int, json_data=None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = b"" if status_code == 204 else b"x"
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error", response=resp
        )
    return resp


def _make_session() -> MagicMock:
    return MagicMock(spec=requests.Session)


def _make_api(session, hass=None) -> HovalConnectApi:
    api = HovalConnectApi(hass or FakeHass(), "test@example.com", "pass")
    api._session = session
    return api


def _mock_auth_ok(session, token: str = "token") -> None:
    session.post = MagicMock(return_value=_make_response(200, {"id_token": token}))


class TestCritOneAndTwoDrainOnClose:
    """ICS-CRIT-001 / ICS-CRIT-002: aclose() must not race in-flight jobs,
    and cancelling a caller must not be mistaken for job completion."""

    @pytest.mark.asyncio
    async def test_aclose_rejects_new_requests_while_closing(self):
        session = _make_session()
        api = _make_api(session)
        api._closing = True
        with pytest.raises(HovalApiError, match="closing"):
            await api._run_blocking(lambda: "should not run")

    @pytest.mark.asyncio
    async def test_inflight_counter_tracks_a_normal_call(self):
        session = _make_session()
        api = _make_api(session)
        assert api._inflight == 0
        result = await api._run_blocking(lambda: 42)
        assert result == 42
        assert api._inflight == 0
        assert api._drain_event.is_set()

    @pytest.mark.asyncio
    async def test_cancelling_the_caller_does_not_decrement_until_job_finishes(self):
        hass = SlowFakeHass(delay=0.1)
        session = _make_session()
        api = _make_api(session, hass=hass)

        caller = asyncio.ensure_future(api._run_blocking(lambda: "done"))
        await hass.started.wait()
        assert api._inflight == 1

        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        # The caller was cancelled, but the "underlying thread" (the sleep
        # inside SlowFakeHass) has NOT finished yet — this is the crux of
        # CRIT-001/002: cancellation of the awaiter must not be conflated
        # with the job actually stopping.
        assert api._inflight == 1
        assert not hass.finished.is_set()

        await hass.finished.wait()
        # Give the done-callback a tick to run.
        await asyncio.sleep(0)
        assert api._inflight == 0
        assert api._drain_event.is_set()

    @pytest.mark.asyncio
    async def test_aclose_waits_for_inflight_job_before_closing_session(self):
        hass = SlowFakeHass(delay=0.1)
        session = _make_session()
        api = _make_api(session, hass=hass)

        task = asyncio.ensure_future(api._run_blocking(lambda: "ok"))
        await hass.started.wait()

        close_task = asyncio.ensure_future(api.aclose())
        await asyncio.sleep(0)
        # aclose() must not have closed the session yet — a job is in flight.
        session.close.assert_not_called()

        await task
        await close_task
        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_gives_up_after_drain_timeout(self):
        hass = SlowFakeHass(delay=1.0)  # outlives the drain timeout below
        session = _make_session()
        api = _make_api(session, hass=hass)
        with patch("custom_components.hoval_connect.api._CLOSE_DRAIN_TIMEOUT", 0.05):
            leftover = asyncio.ensure_future(api._run_blocking(lambda: "irrelevant"))
            await hass.started.wait()
            await api.aclose()
        session.close.assert_called_once()
        # Clean up the still-running background job so it doesn't leak into
        # (or slow down) later tests/teardown.
        await hass.finished.wait()
        await leftover


class TestCrit006NoRetryOnWrites:
    """ICS-CRIT-006: writes must not be retried after an ambiguous outcome."""

    @pytest.mark.asyncio
    async def test_post_not_retried_on_timeout(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(side_effect=requests.exceptions.Timeout())
        api = _make_api(session)
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="1 attempt"),
        ):
            await api._request(
                "POST", _url("v3", "plants", "p1", "circuits", "c1", "programs", "week1")
            )
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_post_not_retried_on_503(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(503))
        api = _make_api(session)
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="HTTP 503"),
        ):
            await api._request("POST", _url("v3", "plants", "p1"))
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_get_is_still_retried_on_timeout(self):
        session = _make_session()
        _mock_auth_ok(session)
        resp_ok = _make_response(200, {"ok": True})
        session.request = MagicMock(side_effect=[requests.exceptions.Timeout(), resp_ok])
        api = _make_api(session)
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", _url("v3", "plants", "p1"))
        assert result == {"ok": True}
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_post_401_is_still_retried_once(self):
        """A 401 is a definite pre-write rejection, not an ambiguous outcome —
        still safe (and necessary) to retry once with a fresh token even
        for a write."""
        session = _make_session()
        _mock_auth_ok(session)
        resp_401 = _make_response(401)
        resp_ok = _make_response(200, {"ok": True})
        session.request = MagicMock(side_effect=[resp_401, resp_ok])
        api = _make_api(session)
        result = await api._request("POST", _url("v3", "plants", "p1"))
        assert result == {"ok": True}
        assert session.request.call_count == 2


class TestHigh002And003IdentifierValidationAndUrlQuoting:
    def test_require_identifier_rejects_empty(self):
        with pytest.raises(HovalApiError):
            _require_identifier("", "plant_id")

    def test_require_identifier_rejects_non_string(self):
        with pytest.raises(HovalApiError):
            _require_identifier(123, "plant_id")

    def test_require_identifier_rejects_control_characters(self):
        with pytest.raises(HovalApiError):
            _require_identifier("abc\r\ninjected", "plant_id")

    def test_require_identifier_rejects_overlong(self):
        with pytest.raises(HovalApiError):
            _require_identifier("x" * 200, "plant_id")

    def test_require_identifier_accepts_normal_value(self):
        assert _require_identifier("plant-123", "plant_id") == "plant-123"

    def test_url_percent_encodes_path_separators(self):
        url = _url("v3", "plants", "p1/../secret", "circuits")
        assert "p1%2F..%2Fsecret" in url
        assert "/secret/" not in url

    @pytest.mark.asyncio
    async def test_get_circuits_rejects_malicious_plant_id(self):
        session = _make_session()
        api = _make_api(session)
        with pytest.raises(HovalApiError):
            await api.get_circuits("../../admin")


class TestHigh004ResponseSizeBound:
    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self):
        session = _make_session()
        _mock_auth_ok(session)
        big_resp = _make_response(200, {"ok": True})
        with patch("custom_components.hoval_connect.api._MAX_RESPONSE_BYTES", 2):
            big_resp.content = b"way more than two bytes"
            session.request = MagicMock(return_value=big_resp)
            api = _make_api(session)
            with pytest.raises(HovalApiError, match="exceeds"):
                await api._request("GET", _url("v3", "plants", "p1"))

    @pytest.mark.asyncio
    async def test_normal_body_passes(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(200, {"ok": True}))
        api = _make_api(session)
        assert await api._request("GET", _url("v3", "plants", "p1")) == {"ok": True}


class TestHigh013PerPageItemCap:
    @pytest.mark.asyncio
    async def test_oversized_single_page_rejected(self):
        session = _make_session()
        _mock_auth_ok(session)
        huge_page = {"content": [{"plantExternalId": str(i)} for i in range(500)], "last": True}
        session.request = MagicMock(return_value=_make_response(200, huge_page))
        api = _make_api(session)
        with pytest.raises(HovalApiError, match="exceeding"):
            await api.get_plants()

    @pytest.mark.asyncio
    async def test_normal_page_still_works(self):
        session = _make_session()
        _mock_auth_ok(session)
        page = {"content": [{"plantExternalId": "1"}], "last": True}
        session.request = MagicMock(return_value=_make_response(200, page))
        api = _make_api(session)
        result = await api.get_plants()
        assert result == [{"plantExternalId": "1"}]


class TestHigh014ProgramAndDurationValidation:
    @pytest.mark.asyncio
    async def test_set_program_rejects_unknown_program(self):
        session = _make_session()
        api = _make_api(session)
        with pytest.raises(HovalApiError, match="Invalid program"):
            await api.set_program("p1", "c1", "; rm -rf /")

    @pytest.mark.asyncio
    async def test_set_program_accepts_known_program(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat"}))
        session.request = MagicMock(return_value=_make_response(200, {"ok": True}))
        api = _make_api(session)
        await api.set_program("p1", "c1", "week1")

    @pytest.mark.asyncio
    async def test_set_temporary_change_rejects_unknown_duration(self):
        session = _make_session()
        api = _make_api(session)
        with pytest.raises(HovalApiError, match="Invalid duration"):
            await api.set_temporary_change("p1", "c1", 21.0, duration="NEXT_WEEK")

    @pytest.mark.asyncio
    async def test_set_temporary_change_accepts_legacy_duration(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat"}))
        session.request = MagicMock(return_value=_make_response(200, {"ok": True}))
        api = _make_api(session)
        await api.set_temporary_change("p1", "c1", 21.0, duration="FOUR")
        sent_body = session.request.call_args.kwargs["json"]
        assert sent_body["duration"] == "fourHours"


class TestHigh016JitterAndRetryAfter:
    def test_retry_delay_uses_retry_after_when_sane(self):
        assert _retry_delay(0, "3") == 3.0

    def test_retry_delay_ignores_garbage_retry_after(self):
        delay = _retry_delay(0, "not-a-number")
        assert 0 < delay <= 10

    def test_retry_delay_ignores_absurd_retry_after(self):
        delay = _retry_delay(0, "99999")
        assert delay <= 10

    def test_retry_delay_has_jitter(self):
        delays = {_retry_delay(1, None) for _ in range(20)}
        assert len(delays) > 1  # not all identical -> jitter is present


class TestHigh017CircuitBreaker:
    def test_opens_after_threshold_failures(self):
        breaker = _CircuitBreaker(failure_threshold=3, cooldown=60)
        for _ in range(3):
            assert breaker.allow()
            breaker.record_failure()
        assert not breaker.allow()

    def test_closes_immediately_on_success(self):
        breaker = _CircuitBreaker(failure_threshold=2, cooldown=60)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.allow()  # only 1 consecutive failure since the success

    def test_reopens_after_cooldown(self, monkeypatch):
        breaker = _CircuitBreaker(failure_threshold=1, cooldown=10)
        breaker.record_failure()
        assert not breaker.allow()
        original_monotonic = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + 11)
        assert breaker.allow()

    @pytest.mark.asyncio
    async def test_request_fails_fast_when_breaker_open(self):
        session = _make_session()
        _mock_auth_ok(session)
        session.request = MagicMock(return_value=_make_response(503))
        api = _make_api(session)
        api._breaker._open_until = time.monotonic() + 60
        with pytest.raises(HovalApiError, match="Circuit breaker open"):
            await api._request("GET", _url("v3", "plants", "p1"))
        session.request.assert_not_called()


class TestMed005Redaction:
    def test_redacts_email(self):
        assert "user@example.com" not in redact_remote_error_body("error for user@example.com")

    def test_redacts_bearer_token(self):
        body = "Authorization: Bearer sk-abcdef1234567890"
        assert "sk-abcdef1234567890" not in redact_remote_error_body(body)

    def test_truncates_long_body(self):
        assert len(redact_remote_error_body("x" * 10000)) <= 200

    def test_leaves_plain_text_alone(self):
        assert redact_remote_error_body("Forbidden") == "Forbidden"
