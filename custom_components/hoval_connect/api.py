"""API client for Hoval Connect.

TRANSPORT: requests-in-executor, not aiohttp (v0.24.0)
--------------------------------------------------------
This client deliberately does NOT use aiohttp, which is unusual for a Home
Assistant integration and should not be "cleaned up" back to aiohttp without
re-reading this note and docs/audit-v0.24.0.md in full.

Root cause (v0.23.0 -> v0.24.0): Hoval's Azure Application Gateway started
blocking this integration with a blanket HTTP 403 on every endpoint. Two
independent, empirically-isolated causes were found by testing single
variables at a time against the live API:

1. aiohttp's TLS connection fingerprint is blocked outright, regardless of
   headers. Confirmed across FOUR separate configurations, all against the
   real API, all HTTP 403: aiohttp's default connector; aiohttp + an
   explicit User-Agent; aiohttp + Accept/Accept-Encoding/Connection headers
   matching what `requests` sends by default; aiohttp with its TLS context
   rebuilt from urllib3's own cipher list (urllib3.util.ssl_.create_urllib3_context()).
   That last one matches requests' cipher suite exactly and STILL failed, so
   this is not fixable by cipher/header tuning from within aiohttp — it's
   some other property of aiohttp's TLS handshake or connection handling.
2. `requests`' own DEFAULT User-Agent ("python-requests/X.Y.Z") is
   independently blocked — almost certainly a WAF signature rule against
   well-known scripting-tool default identities, extremely common on API
   gateways. Confirmed by isolating this one variable: an otherwise
   byte-identical plain `requests` script got HTTP 403 with the default
   User-Agent and HTTP 200 with a custom one, with nothing else changed.

Combined, the ONLY configuration empirically proven to work end-to-end
against the live API is: the `requests` library, with an explicit non-default
User-Agent. That is what this file does. See USER_AGENT in const.py for the
specific string in use and why it must not be changed casually.

Since `requests` is a blocking library, every call is wrapped in
`hass.async_add_executor_job()` — Home Assistant's sanctioned mechanism for
running blocking code from async integrations — so the integration remains
non-blocking from HA's perspective even though the actual HTTP client
underneath is synchronous. A single `requests.Session()` is created once and
reused for the lifetime of this client (matching the coordinator's existing
"fan out one task per circuit" concurrency pattern); this is safe because
requests/urllib3's connection pool is explicitly designed for concurrent use
from multiple threads.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote

import requests
from homeassistant.core import HomeAssistant

from .const import (
    BASE_URL,
    CLIENT_ID,
    ID_TOKEN_TTL,
    IDP_URL,
    PLANT_TOKEN_TTL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Retry configuration for transient errors.
# NOTE ON SEMANTICS: _MAX_RETRIES is the TOTAL number of attempts, not the
# number of *additional* retries. With _MAX_RETRIES = 2 the request is tried
# at most twice (one initial attempt + one retry). Each attempt can take up to
# (_CONNECT_TIMEOUT + _READ_TIMEOUT) seconds, so worst case ≈
#   _CONNECT_TIMEOUT + _READ_TIMEOUT + _RETRY_BASE_DELAY + _CONNECT_TIMEOUT + _READ_TIMEOUT
#   = 8 + 20 + 0.5 + 8 + 20 = ~56.5 s for two attempts.
# Kept low so the coordinator does not hang past HA's ConfigEntryNotReady /
# watchdog window during startup.
# (The name is retained — not renamed to _MAX_ATTEMPTS — because the public
#  test-suite imports it by this name.)
_MAX_RETRIES = 2  # total attempts (see note above)
_RETRY_BASE_DELAY = 0.5  # seconds, doubled before each subsequent attempt
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# ICS audit v1.0.1 (ICS-CRIT-006): a timeout, connection error, or retryable
# status code does not tell us whether the remote side already committed a
# write before the response was lost. Retrying a GET/HEAD is always safe
# (nothing changes if it's repeated); retrying a POST/PATCH/DELETE risks
# executing the same physical command twice. Only these two methods may be
# retried after an *ambiguous* outcome (timeout / connection error /
# retryable status code). A definite pre-flight failure while still
# acquiring headers (no request has been sent to the resource yet) is a
# different situation and is retried for every method — see _request().
_SAFE_RETRY_METHODS = {"GET", "HEAD"}

# Split timeouts: fail fast on dead connections, allow longer for slow reads.
# requests accepts this as a (connect, read) tuple directly.
# Total worst-case per attempt: _CONNECT_TIMEOUT + _READ_TIMEOUT = 28 s.
# With 2 retries: ~28 + 0.5 + 28 = ~57 s max for a single endpoint.
_CONNECT_TIMEOUT = 8  # seconds to establish the TCP connection
_READ_TIMEOUT = 20  # seconds to receive the full response body
_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# ICS-CRIT-002 (audit v1.0.1): worst case for a single non-retried attempt
# is _CONNECT_TIMEOUT + _READ_TIMEOUT (~28s); give real in-flight work a
# fair chance to finish before aclose() gives up and closes the session
# out from under it anyway.
_CLOSE_DRAIN_TIMEOUT = 35  # seconds

# Hard upper bound on my-plants pagination (audit finding F3, v0.21.1).
# 50 pages x 12 plants/page = 600 plants — far beyond any real account.
# Without a cap, a misbehaving (or tampered-with) server that keeps answering
# `"last": false` with non-empty content would loop get_plants() forever and
# grow the result list without bound. The coordinator's 90 s outer timeout
# would contain that, but the config-flow validation path has no outer guard,
# so the cap must live here in the client.
_MAX_PLANT_PAGES = 50

# ICS audit v1.0.2 (ICS-HIGH-013, downgraded from the original v1.0.1 draft
# to Medium — a total-pagination cap already existed via _MAX_PLANT_PAGES
# and already failed closed; the real gap was narrower: nothing checked
# that a single page didn't contain far more items than the requested page
# size). Set generously above the requested page size (12) so a compliant
# server is never affected; only a server that ignores "size" and returns
# an absurd single page is rejected.
_MAX_PLANTS_PER_PAGE = 200

# ICS audit v1.0.1 (ICS-HIGH-004 / ICS-HIGH-015): a compromised/misbehaving
# upstream returning a very large body should not be buffered/parsed
# without limit. Real payloads here are small JSON objects/lists (a handful
# of plants/circuits), so this is generous headroom, not a tight budget.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MiB

# ICS audit v1.0.1 (ICS-HIGH-014): the only program/duration values the
# cloud API is documented to accept. Direct API callers (not just the UI
# entities layered on top) must not be able to smuggle an arbitrary string
# into the request path/body.
_VALID_PROGRAMS = frozenset(
    {"constant", "ecoMode", "standby", "week1", "week2", "manual", "externalConstant"}
)
_VALID_DURATIONS_V3 = frozenset({"fourHours", "midnight"})
_VALID_DURATIONS_LEGACY = frozenset({"FOUR", "MIDNIGHT"})

# ICS audit v1.0.1 (ICS-MED-005): patterns that must never reach regular HA
# logs verbatim, even truncated. Deliberately conservative (a few false
# positives redacted is fine; a leaked credential/token is not).
_REDACT_PATTERNS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # emails
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),  # JWTs
    re.compile(r"(?i)\b(bearer|token|password|secret)\b\s*[:=]?\s*[\"']?[A-Za-z0-9._-]{6,}"),
)
_LOG_BODY_MAX = 200  # was 500 (ICS-MED-005): keep just enough for triage


def redact_remote_error_body(body: str) -> str:
    """Best-effort redaction of a remote error body before it reaches logs.

    Not a full PII/secret scanner — a pragmatic, conservative filter for the
    identifier/credential shapes most likely to appear in a cloud error
    body (account emails, bearer/JWT tokens). See ICS-MED-005.
    """
    redacted = body
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:_LOG_BODY_MAX]


def _require_identifier(value: Any, field: str) -> str:
    """Validate a plant/circuit identifier before it reaches a URL (ICS-HIGH-002).

    Deliberately narrow: reject non-strings, empty strings, absurdly long
    values, and raw control characters (header/log injection). This is not
    a full schema layer — see docs/audit-v1.0.2.md — just enough that a
    malformed identifier fails loudly here instead of silently becoming a
    dict key, a URL fragment, or a log/entity-id fragment.
    """
    if not isinstance(value, str) or not value or len(value) > 128:
        raise HovalApiError(f"Invalid {field}: {value!r}")
    if any(ch in value for ch in "\r\n\t"):
        raise HovalApiError(f"Invalid {field}: contains control characters")
    return value


def _url(*segments: str) -> str:
    """Build a BASE_URL-relative URL from already-validated path segments.

    ICS-HIGH-003: every segment (including the fixed literal ones this
    module writes itself, e.g. "v3", "plants") is percent-encoded
    individually so a malformed identifier (e.g. containing "/", "?", "#")
    cannot change which resource is actually addressed.
    """
    return BASE_URL + "/" + "/".join(quote(str(s), safe="") for s in segments)


def _retry_after_header(resp: Any) -> Any:
    """Best-effort read of a Retry-After header from a response/test-double."""
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        return headers.get("Retry-After")
    except AttributeError:
        return None


def _retry_delay(attempt: int, retry_after: Any) -> float:
    """Compute the backoff delay for retry `attempt` (0-indexed).

    ICS-HIGH-016 (audit v1.0.1): honours a server-supplied `Retry-After`
    (capped, since a misbehaving/malicious value must not stall the
    integration indefinitely) and adds jitter to the exponential backoff so
    a fleet of installations hitting the same transient failure at the same
    time do not all retry in lockstep. `retry_after` may be a raw header
    value (str), a test double, or None/garbage — anything that doesn't
    parse as a sane positive number is ignored in favour of the computed
    backoff.
    """
    if retry_after is not None:
        try:
            parsed = float(retry_after)
            if 0 < parsed <= 60:
                return parsed
        except (TypeError, ValueError):
            pass
    base = _RETRY_BASE_DELAY * (2**attempt)
    return min(base + random.uniform(0, 0.25 * base), 10.0)


class _CircuitBreaker:
    """Minimal failure-count breaker gating `_request()` (ICS-HIGH-017).

    Not a full half-open/probe implementation — deliberately simple: after
    `failure_threshold` consecutive terminal failures (retries already
    exhausted, or a non-retryable error), the breaker opens for `cooldown`
    seconds. While open, `_request()` fails immediately without touching
    the network/executor. Any successful response closes it again
    immediately (`failures` resets to 0).
    """

    def __init__(self, failure_threshold: int = 5, cooldown: float = 60.0) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open_until = time.monotonic() + self._cooldown
            _LOGGER.warning(
                "Circuit breaker open after %d consecutive failures; "
                "failing fast for %.0fs before trying the cloud API again",
                self._failures,
                self._cooldown,
            )


class HovalAuthError(Exception):
    """Authentication error."""


class HovalApiError(Exception):
    """General API error."""


class HovalConnectApi:
    """Client for the Hoval Connect cloud API.

    Not "async" in the traditional sense internally (see module docstring for
    why) but every public method is a coroutine, matching the previous
    aiohttp-based client's interface exactly — no caller outside this file
    (coordinator.py, config_flow.py) needed to change how it calls this class.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
    ) -> None:
        """Initialize the API client.

        Takes `hass` (to schedule blocking requests calls via
        `hass.async_add_executor_job`) instead of an aiohttp session — see
        module docstring. Creating a plain `requests.Session()` here is safe
        to do synchronously: it allocates local objects only and performs no
        I/O of its own.
        """
        self._hass = hass
        self._session = requests.Session()
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._id_token_exp: float = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}
        # Single-flight locks so a burst of concurrent requests (the coordinator
        # fans out one task per circuit) triggers at most ONE token refresh
        # instead of a thundering herd of identical auth calls against the
        # rate-limited identity provider. Separate locks for the ID token and the
        # per-plant access token avoid any re-entrant deadlock, because
        # _get_plant_access_token() calls _get_id_token() while holding its own.
        self._id_token_lock = asyncio.Lock()
        # Independent audit finding (2026-09, fourth round, HVC-014): this
        # used to be ONE global asyncio.Lock() shared across every plant on
        # the account, not one per plant. A multi-plant account would
        # serialize plant-access-token acquisition across entirely
        # unrelated plants — if two plants both need a fresh token at
        # startup, the second plant's acquisition would wait for the
        # first's to finish even though they're independent HTTP calls to
        # independent endpoints. Combined with retries and the coordinator's
        # 90-second overall timeout, this could meaningfully extend (or in
        # a bad case, blow) startup for a multi-plant account. Now one lock
        # per plant_id, created on first use via setdefault() — atomic in
        # asyncio's single-threaded model (no await between the dict check
        # and the assignment), so no race in creating a plant's first lock
        # even if two circuits of the same new plant request one
        # simultaneously.
        self._pat_locks: dict[str, asyncio.Lock] = {}

        # ICS audit v1.0.1/v1.0.2 (ICS-CRIT-001 / ICS-CRIT-002): cancelling
        # the coroutine that is *awaiting* an executor job does not stop the
        # underlying worker thread — it keeps running `requests` I/O against
        # `self._session` regardless. Previously nothing tracked that, so
        # `aclose()` could close the session out from under a still-running
        # request (a real race: threads sharing one `requests.Session`), and
        # `coordinator.async_shutdown()` cancelling a "committed" control
        # task gave no guarantee the underlying HTTP call had actually
        # stopped. `_run_blocking()` below fixes both by tracking real job
        # completion independently of whether the *caller* was cancelled.
        self._closing = False
        self._inflight = 0
        self._drain_event = asyncio.Event()
        self._drain_event.set()  # set == "nothing in flight"

        # ICS-HIGH-017: isolates local executor/retry capacity from a
        # prolonged cloud outage. See _CircuitBreaker and _request().
        self._breaker = _CircuitBreaker()

    async def _run_blocking(self, func) -> Any:
        """Run blocking `func` in HA's executor, tracked for a safe shutdown.

        Unlike a bare `await self._hass.async_add_executor_job(func)`, the
        underlying job is wrapped in its own Task and awaited via
        `asyncio.shield()`. If the *caller* of this method is cancelled
        (e.g. by `coordinator.async_shutdown()` cancelling a control-write
        task), the shield absorbs that cancellation — the inner Task, and
        the real OS thread running `func`, keep running to completion
        exactly as they would have anyway (cancellation cannot stop a
        thread already executing blocking I/O). The difference is that
        `self._inflight` is only decremented when the job *actually*
        finishes, not when some caller's await of it was cancelled — so
        `aclose()` can genuinely wait for in-flight work to finish before
        closing `self._session` out from under it. See ICS-CRIT-001/002.
        """
        if self._closing:
            raise HovalApiError("API client is closing; request aborted")
        self._inflight += 1
        self._drain_event.clear()
        inner = asyncio.ensure_future(self._hass.async_add_executor_job(func))

        def _on_done(_task: asyncio.Task) -> None:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._drain_event.set()

        inner.add_done_callback(_on_done)
        return await asyncio.shield(inner)

    def _sync_post(
        self, url: str, *, data: dict[str, str], headers: dict[str, str]
    ) -> requests.Response:
        """Blocking POST — used only for the IDP auth call.

        MUST only ever be invoked via `hass.async_add_executor_job()`. See
        `_sync_request`'s docstring for why reading the returned Response
        afterwards on the event loop thread is safe.
        """
        return self._session.post(url, data=data, headers=headers, timeout=_TIMEOUT)

    def _sync_get(self, url: str, *, headers: dict[str, str]) -> requests.Response:
        """Blocking GET — used only for the plant-access-token fetch.

        MUST only ever be invoked via `hass.async_add_executor_job()`.
        """
        return self._session.get(url, headers=headers, timeout=_TIMEOUT)

    def _sync_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_data: Any = None,
    ) -> requests.Response:
        """Blocking generic request — used by every method via _request().

        MUST only ever be invoked via `hass.async_add_executor_job()`. Never
        call this directly from a coroutine running on the event loop — it
        blocks the calling thread for the full duration of the request.

        Returns the raw `requests.Response`. Reading `.status_code`, `.text`,
        `.headers`, and calling `.json()` on it afterwards from the event
        loop thread is safe and does not touch the network again: by the
        time `session.request()` returns, the full response body is already
        buffered in memory (this client never passes `stream=True`), so
        those are pure in-memory operations.

        ICS-HIGH-004 / ICS-HIGH-015 (audit v1.0.1): a response is bounded
        against `_MAX_RESPONSE_BYTES` here, and — for a response `_request()`
        will actually need to parse — JSON-decoded here too, both while
        still on this executor thread. This keeps a large/adversarial body
        from being parsed on the event loop; `_request()` reads the
        precomputed `_precomputed_json`/`_precomputed_json_error` instead of
        calling `.json()` itself.
        """
        resp = self._session.request(
            method, url, headers=headers, params=params, json=json_data, timeout=_TIMEOUT
        )
        content_length = getattr(resp, "headers", None)
        content_length = content_length.get("Content-Length") if content_length else None
        if content_length is not None:
            try:
                if int(content_length) > _MAX_RESPONSE_BYTES:
                    raise HovalApiError(
                        f"API response declared Content-Length {content_length} "
                        f"exceeds the {_MAX_RESPONSE_BYTES} byte safety limit"
                    )
            except (TypeError, ValueError):
                pass  # non-numeric/absent header (or a test double) — fall through
        body = resp.content
        if body is not None and len(body) > _MAX_RESPONSE_BYTES:
            raise HovalApiError(
                f"API response body ({len(body)} bytes) exceeds the "
                f"{_MAX_RESPONSE_BYTES} byte safety limit"
            )
        resp._precomputed_json = None
        resp._precomputed_json_error = None
        if body and resp.status_code < 400:
            try:
                resp._precomputed_json = resp.json()
            except ValueError as err:
                resp._precomputed_json_error = err
        return resp

    async def aclose(self) -> None:
        """Close the underlying requests session's connection pool.

        Called from async_unload_entry(). session.close() is typically fast
        but is still blocking socket-cleanup work, so it runs on the executor
        for consistency with every other call in this class rather than
        assuming it's always instantaneous.

        ICS-CRIT-002 (audit v1.0.1): first marks the client as closing (so
        no *new* blocking job can start via `_run_blocking()`), then waits
        for every job already in flight to genuinely finish — not just for
        whichever coroutine happened to be awaiting it — before touching
        `self._session`. Bounded by `_CLOSE_DRAIN_TIMEOUT` so a single
        wedged request cannot block config-entry unload forever; if that
        timeout is hit the session is still closed (the alternative, an
        integration that can never unload, is worse), but this is now the
        deliberate last resort rather than the routine case.
        """
        self._closing = True
        if self._inflight > 0:
            _LOGGER.debug("aclose(): waiting for %d in-flight request(s) to finish", self._inflight)
            try:
                await asyncio.wait_for(self._drain_event.wait(), timeout=_CLOSE_DRAIN_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning(
                    "aclose(): %d request(s) still in flight after %ds; closing the session anyway",
                    self._inflight,
                    _CLOSE_DRAIN_TIMEOUT,
                )
        await self._hass.async_add_executor_job(self._session.close)

    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant.

        Uses double-checked locking: the fast path returns the cached token
        without acquiring the lock; only a refresh serialises through
        _id_token_lock so concurrent callers don't each hit the IDP.
        """
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

        async with self._id_token_lock:
            # Re-check inside the lock: another coroutine may have refreshed
            # while we were waiting to acquire it.
            if self._id_token and time.time() < self._id_token_exp:
                return self._id_token

            try:
                resp = await self._run_blocking(
                    functools.partial(
                        self._sync_post,
                        IDP_URL,
                        data={
                            "grant_type": "password",
                            "client_id": CLIENT_ID,
                            "username": self._email,
                            "password": self._password,
                            "scope": "openid",
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "User-Agent": USER_AGENT,
                        },
                    )
                )
                if resp.status_code in (400, 401, 403):
                    _LOGGER.warning("IDP auth failed (HTTP %s)", resp.status_code)
                    raise HovalAuthError(f"Invalid credentials (HTTP {resp.status_code})")
                resp.raise_for_status()
                data = resp.json()
            except HovalAuthError:
                raise
            except (requests.exceptions.RequestException, TimeoutError) as err:
                raise HovalApiError(f"Connection error during authentication: {err}") from err

            if not isinstance(data, dict) or "id_token" not in data:
                keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                _LOGGER.error("IDP response missing id_token. Got: %s", keys)
                raise HovalApiError("IDP response missing id_token")

            self._id_token = data["id_token"]
            self._id_token_exp = time.time() + ID_TOKEN_TTL.total_seconds()
            return self._id_token

    async def _get_plant_access_token(self, plant_id: str) -> str:
        """Get or refresh the plant access token (double-checked locking).

        Independent audit finding (2026-09, "more" report, finding #6): a
        401 here used to immediately raise HovalAuthError ("credentials are
        bad"), even though the main request path (_request()) treats the
        identical signal — a 401 — as "the bearer token simply expired,
        refresh it and retry", not a credentials problem. Those are
        genuinely different situations, but this method previously could
        not tell them apart, so ANY 401 fetching the plant token was always
        treated as the worse one, surfacing as an unnecessary config-entry
        auth failure instead of recovering automatically like the main path
        already does. At most one retry, mirroring that same path's own
        single-refresh semantics (see _request()'s docstring in this file).
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with self._pat_locks.setdefault(plant_id, asyncio.Lock()):
            # Re-check inside the lock in case a concurrent caller refreshed it.
            cached = self._pat_cache.get(plant_id)
            if cached and time.time() < cached[1]:
                return cached[0]

            for attempt in range(2):
                id_token = await self._get_id_token()
                try:
                    resp = await self._run_blocking(
                        functools.partial(
                            self._sync_get,
                            _url("v1", "plants", plant_id, "settings"),  # ICS-HIGH-003
                            headers={
                                "Authorization": f"Bearer {id_token}",
                                "User-Agent": USER_AGENT,
                            },
                        )
                    )
                    if resp.status_code == 401:
                        self._id_token = None
                        if attempt == 0:
                            _LOGGER.debug(
                                "ID token rejected while fetching plant access "
                                "token for %s; refreshing and retrying once.",
                                plant_id,
                            )
                            continue
                        raise HovalAuthError("ID token rejected")
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (HovalAuthError, HovalApiError):
                    raise
                except (requests.exceptions.RequestException, TimeoutError) as err:
                    raise HovalApiError(f"Connection error fetching plant token: {err}") from err

            if not isinstance(data, dict) or "token" not in data:
                keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                _LOGGER.error("Plant settings response missing 'token'. Got: %s", keys)
                raise HovalApiError("Plant settings response missing 'token'")

            token = data["token"]
            self._pat_cache[plant_id] = (token, time.time() + PLANT_TOKEN_TTL.total_seconds())
            return token

    async def _headers(self, plant_id: str | None = None) -> dict[str, str]:
        """Build request headers with auth tokens.

        Includes an explicit User-Agent on every request. See the module
        docstring and USER_AGENT in const.py: this is not optional decoration
        — it is one of the two empirically-confirmed requirements for this
        API to respond at all.
        """
        id_token = await self._get_id_token()
        headers = {"Authorization": f"Bearer {id_token}", "User-Agent": USER_AGENT}
        if plant_id:
            pat = await self._get_plant_access_token(plant_id)
            headers["X-Plant-Access-Token"] = pat
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        plant_id: str | None = None,
        params: dict[str, str] | None = None,
        json_data: Any = None,
    ) -> Any:
        """Make an authenticated API request with token retry and transient error backoff.

        Independent audit finding (2026-09, HVC-ICS-006 / HVC-ICS-007): this
        method used to recurse into a fresh `_request()` call on a 401 (via
        a `_retry` flag), which started a BRAND NEW `for attempt in
        range(_MAX_RETRIES)` loop — so a 401 followed by one transient
        error could produce up to 3 total HTTP attempts against a budget
        `_MAX_RETRIES`'s own comment documents as 2 TOTAL (HVC-ICS-006).
        Separately, `headers = await self._headers(plant_id)` used to run
        OUTSIDE this method's retry try/except entirely, so a transient
        network blip while acquiring/refreshing the ID token or
        plant-access-token (both real network calls) bypassed this loop's
        retry/backoff completely and failed on the very first hiccup —
        the opposite failure mode from the 401 one (HVC-ICS-007).

        Fixed by removing the recursive self-call: a 401 now does
        `continue` within this same loop instead, so one shared `attempt`
        counter governs the 401-triggered refresh and any transient-error
        retries together, capping total HTTP attempts at `_MAX_RETRIES` no
        matter which kind of failure occurs first. Header acquisition now
        has its own try/except inside the same loop, sharing the same
        retry budget and exponential backoff as the main request — a
        genuinely bad credential (`HovalAuthError`) still fails immediately
        without wasting a retry on something retrying can't fix, but a
        transient connection problem while fetching a token
        (`HovalApiError`) is now retried like any other transient failure.

        ICS-CRIT-006 (audit v1.0.1): `path` is the FULL absolute URL, built
        by the caller via `_url()` with validated/quoted segments (this
        method no longer does its own string interpolation — ICS-HIGH-003).
        Retrying after a *received* answer for a write (a retryable 4xx/5xx
        status code) or after an *ambiguous* answer (timeout/connection
        error, where the remote may already have committed the write before
        the response was lost) is only safe for methods where repeating the
        call cannot duplicate a physical action — GET/HEAD. POST/PATCH/
        DELETE get exactly one attempt for those triggers; a 401 (a
        definite, immediate, pre-business-logic rejection, never a
        "maybe-committed" outcome) is still retried once for every method,
        as before.

        ICS-HIGH-017 (audit v1.0.1): a simple failure-count circuit breaker
        gates the retry loop itself. While open, requests fail immediately
        without touching the network/executor at all, so a prolonged cloud
        outage cannot keep consuming local executor capacity or retry
        budget across every entity's poll/control calls.
        """
        if not self._breaker.allow():
            raise HovalApiError(
                f"Circuit breaker open (cloud API unavailable); refusing {method} {path} "
                "without attempting the network"
            )
        url = path
        # ICS-CRIT-006: governs ONLY the "ambiguous/received-error-outcome"
        # retries below (timeout, connection error, retryable status code).
        # The 401-refresh-and-retry path is independent of this and always
        # gets its one retry regardless of method — see docstring above.
        safe_to_retry = method.upper() in _SAFE_RETRY_METHODS
        # At most one 401-triggered token refresh is attempted per call —
        # matches the old `_retry=False` guard's intent (don't loop forever
        # refreshing a token that keeps getting rejected), just enforced
        # within a single shared loop instead of via recursion.
        token_refreshed = False

        for attempt in range(_MAX_RETRIES):
            try:
                # Rebuild headers on every attempt so a token that expires
                # mid-retry cycle is refreshed automatically rather than
                # sending a stale bearer token that will be rejected with 401.
                headers = await self._headers(plant_id)
            except HovalAuthError:
                raise  # genuinely bad credentials — retrying will not help
            except HovalApiError as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    _LOGGER.warning(
                        "Transient error acquiring auth headers for %s %s, "
                        "retrying in %.1fs (%d/%d): %s",
                        method,
                        path,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                        err,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            try:
                resp = await self._run_blocking(
                    functools.partial(
                        self._sync_request,
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json_data=json_data,
                    )
                )
                _LOGGER.debug("API %s %s → HTTP %s", method, path, resp.status_code)
                if resp.status_code == 401:
                    self._id_token = None
                    if plant_id:
                        self._pat_cache.pop(plant_id, None)
                    if not token_refreshed and attempt < _MAX_RETRIES - 1:
                        token_refreshed = True
                        _LOGGER.debug("Token expired, refreshing and retrying")
                        continue
                    self._breaker.record_failure()
                    raise HovalAuthError("Authentication failed")
                if resp.status_code == 403:
                    # Not retried: unlike 401 (expired token), a 403 has not
                    # been observed to be fixed by refreshing tokens — see the
                    # module docstring for the two confirmed causes this
                    # pointed to (now both addressed by this transport). If
                    # this fires again, both diagnosed causes have been ruled
                    # out, so start over from docs/audit-v0.24.0.md rather
                    # than assuming it's a third variant of the same headers
                    # issue.
                    body = redact_remote_error_body(resp.text)  # ICS-MED-005
                    _LOGGER.warning(
                        "API %s %s -> HTTP 403 (Forbidden). If this persists "
                        "after upgrading, please capture this log line and "
                        "report it: %s",
                        method,
                        path,
                        body,
                    )
                    raise HovalApiError(f"API request failed: HTTP 403: {body}")
                if (
                    resp.status_code in _RETRYABLE_STATUS_CODES
                    and safe_to_retry  # ICS-CRIT-006: never re-send a write on a 5xx/429
                    and attempt < _MAX_RETRIES - 1
                ):
                    delay = _retry_delay(attempt, _retry_after_header(resp))
                    _LOGGER.warning(
                        "Transient error HTTP %s on %s %s, retrying in %.1fs (%d/%d)",
                        resp.status_code,
                        method,
                        path,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    body = redact_remote_error_body(resp.text)  # ICS-MED-005
                    _LOGGER.debug("API error body: %s", body)
                    self._breaker.record_failure()
                    raise HovalApiError(f"API request failed: HTTP {resp.status_code}")
                self._breaker.record_success()
                if resp.status_code == 204 or not resp.content:
                    return None
                if resp._precomputed_json_error is not None:
                    raise HovalApiError(
                        f"Invalid JSON in API response: {resp._precomputed_json_error}"
                    ) from resp._precomputed_json_error
                return resp._precomputed_json
            except (HovalAuthError, HovalApiError):
                raise
            except requests.exceptions.Timeout as err:
                if safe_to_retry and attempt < _MAX_RETRIES - 1:
                    delay = _retry_delay(attempt, None)
                    _LOGGER.warning(
                        "Request timeout on %s %s (attempt %d/%d), retrying in %.1fs",
                        method,
                        path,
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                _LOGGER.warning(
                    "Request timeout on %s %s after %d attempt(s)%s",
                    method,
                    path,
                    attempt + 1,
                    "" if safe_to_retry else " (not retried: non-idempotent method)",
                )
                self._breaker.record_failure()
                raise HovalApiError(
                    f"Request timeout after {attempt + 1} attempt(s): {err}"
                ) from err
            except requests.exceptions.RequestException as err:
                if safe_to_retry and attempt < _MAX_RETRIES - 1:
                    delay = _retry_delay(attempt, None)
                    _LOGGER.warning(
                        "Connection error on %s %s (attempt %d/%d), retrying in %.1fs: %s",
                        method,
                        path,
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                        err,
                    )
                    await asyncio.sleep(delay)
                    continue
                _LOGGER.warning(
                    "Connection error on %s %s after %d attempt(s)%s: %s",
                    method,
                    path,
                    attempt + 1,
                    "" if safe_to_retry else " (not retried: non-idempotent method)",
                    err,
                )
                self._breaker.record_failure()
                raise HovalApiError(
                    f"Connection error after {attempt + 1} attempt(s): {err}"
                ) from err

        self._breaker.record_failure()
        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    async def get_plants(self) -> list[dict[str, Any]]:
        """Get list of user's plants, fetching all pages.

        Hoval's /api/my-plants endpoint was updated in May 2026 to enforce a
        maximum page size of 12 items.  The response may be:
          - A plain list (old API shape) — returned as-is.
          - A Spring/Page wrapper {"content": [...], "last": bool, ...} — the
            integration iterates all pages and returns a flat list.

        Fail-closed regression guard (independent audit finding, 2026-09,
        "more" report, finding #2): a dict response with no "content" key
        at all used to silently become [] via `.get("content", [])` — the
        same class of bug already fixed for get_circuits() (see that
        method's docstring), just missed here. Since this feeds
        _fetch_all_data()/_health_check() directly, a malformed-but-HTTP-200
        response could silently wipe every known plant (see the fix in
        those two methods for the second half of this same failure mode).
        Any dict without a list "content" key, or any non-list/non-dict
        response, now raises HovalApiError instead of quietly returning
        fewer plants than actually exist.
        """
        all_plants: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await self._request(
                "GET", _url("api", "my-plants"), params={"size": "12", "page": str(page)}
            )
            if isinstance(result, list):
                # Old (pre-pagination) API shape: plain list, no further pages.
                return result
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                raise HovalApiError(
                    f"Unexpected get_plants response shape on page {page}: "
                    f"{type(result).__name__} (expected a list, or a dict with a list "
                    "'content' key)"
                )
            content = result["content"]
            # ICS-HIGH-013 (audit v1.0.2, downgraded from the original v1.0.1
            # draft to Medium — see docs/audit-v1.0.2.md): _MAX_PLANT_PAGES
            # below already bounds total *pages*/requests and fails closed.
            # This closes the narrower remaining gap: nothing previously
            # checked that a single page didn't contain far more items than
            # the requested "size": a server ignoring that parameter could
            # return one absurdly large page and still pass every other
            # check here.
            if len(content) > _MAX_PLANTS_PER_PAGE:
                raise HovalApiError(
                    f"get_plants page {page} returned {len(content)} items, "
                    f"exceeding the {_MAX_PLANTS_PER_PAGE}-item safety limit for a "
                    "single page (requested size=12) — refusing to trust this response"
                )
            all_plants.extend(content)
            # "last" is False when more pages exist; True (or absent) means done.
            if result.get("last", True) or not content:
                break
            page += 1
            if page >= _MAX_PLANT_PAGES:
                # Independent audit finding (2026-09, fourth round,
                # HVC-013): this used to log a warning and `break`,
                # returning the PARTIAL list collected so far as if it were
                # a complete, successful result — converting a detected
                # upstream pagination fault (the log message's own words)
                # into apparently-successful partial account topology. The
                # coordinator would have no way to tell "this account
                # genuinely has this many plants" from "the cloud kept
                # paginating forever and we gave up partway", and could
                # believe the omitted plants simply don't exist. Raises
                # instead — inconsistent with every other place in this
                # method that already fails closed on a detected anomaly
                # (the response-shape check above), which this one path had
                # been missed for.
                raise HovalApiError(
                    f"get_plants pagination exceeded {_MAX_PLANT_PAGES} pages "
                    f"({len(all_plants)} plants collected before giving up) — "
                    "the cloud kept reporting more pages available, which is "
                    "almost certainly an upstream fault. Refusing to return "
                    "partial account topology."
                )
        return all_plants

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Get plant settings (also refreshes PAT as side effect)."""
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        return await self._request(
            "GET",
            _url("v1", "plants", plant_id, "settings"),
            plant_id=plant_id,  # ICS-HIGH-003
        )

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Get all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21; v3 is the only path that
        still works. Response shape changed: see coordinator field mapping.

        The v3 endpoint may return either a plain list or a paginated wrapper
        {"content": [...], ...}. Both shapes are normalised to a list here so
        the coordinator always receives a plain list.

        Fail-closed regression guard (independent audit finding, 2026-09):
        a response of an unrecognised shape — a dict with no "content" key,
        a bare string, null, a number — used to silently normalise to [],
        which the coordinator then treated as "this plant genuinely has no
        circuits" rather than "the API returned something we don't
        understand". Under v0.24.0's frequent polling that self-corrected
        within the next cycle; under v1.0.0 (circuits fetched once at
        startup, then only after a write) a single bad response during
        startup could leave every circuit entity missing until a restart.
        Any shape other than a plain list, or a dict that actually has a
        list "content" key, now raises HovalApiError instead.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        result = await self._request(
            "GET",
            _url("v3", "plants", plant_id, "circuits"),
            plant_id=plant_id,  # ICS-HIGH-003
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            _LOGGER.debug(
                "get_circuits returned paginated wrapper for plant %s; extracting 'content'",
                plant_id,
            )
            return result["content"]
        raise HovalApiError(
            f"Unexpected circuits response shape for plant {plant_id}: "
            f"{type(result).__name__} (expected a list, or a dict with a list 'content' key)"
        )

    async def get_programs(self, plant_id: str, circuit_path: str) -> Any:
        """Get time programs for a circuit."""
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        return await self._request(
            "GET",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "programs"),  # HIGH-003
            plant_id=plant_id,
        )

    # v1.0.0 removed get_live_values(), get_events(), get_latest_event(), and
    # get_weather() — pure telemetry endpoints with no write dependency,
    # never called by coordinator.py once it stopped polling telemetry on a
    # schedule (a separate CAN-bus HACS integration covers that data now).
    # See docs/audit-v1.0.0.md. If a future release needs one of these back,
    # the previous implementations are in git history / v0.24.0's api.py.

    async def get_circuit_settings(self, plant_id: str, circuit_path: str) -> dict[str, Any]:
        """Get circuit settings (currently: circuitName + weatherImpact).

        GET /v3/plants/{plantExternalId}/circuits/{circuitPath}/settings

        `weatherImpact` holds the "weather based control" Eco<->Comfort
        weighting introduced in the Hoval Connect app in 2026-07:
            {"outsideTemperature": <int 0..100>, "solarRadiation": <float -10..0>}
        Either sub-field (or the whole `weatherImpact` object) may be null for
        circuit types/firmware versions that don't support it. As of the
        v0.23.0 forensic crawl this key can also be absent entirely — see
        coordinator.py's "weatherImpact" in settings check.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        return await self._request(
            "GET",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "settings"),  # HIGH-003
            plant_id=plant_id,
        )

    async def update_circuit_settings(
        self,
        plant_id: str,
        circuit_path: str,
        *,
        outside_temperature: int | None = None,
        solar_radiation: float | None = None,
    ) -> Any:
        """Update the weather-based control weighting for a circuit.

        PATCH /v3/plants/{plantExternalId}/circuits/{circuitPath}/settings
        Body: {"weatherImpact": {"outsideTemperature": <int|null>, "solarRadiation": <float|null>}}

        The cloud's PATCH endpoint for CircuitSettingsDTO is not confirmed to
        be a JSON-merge-patch — sending only the changed sub-field could
        overwrite the other one with null. Callers (see
        HovalDataCoordinator.async_set_weather_impact) MUST resolve both
        values (current + requested change) before calling this method; this
        method always sends both keys it was given so the request body never
        implicitly clears a value the caller didn't intend to touch.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        body = {
            "weatherImpact": {
                "outsideTemperature": outside_temperature,
                "solarRadiation": solar_radiation,
            }
        }
        _LOGGER.debug(
            "update_circuit_settings: plant=%s circuit=%s body=%s",
            plant_id,
            circuit_path,
            body,
        )
        result = await self._request(
            "PATCH",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "settings"),  # HIGH-003
            plant_id=plant_id,
            json_data=body,
        )
        _LOGGER.debug("update_circuit_settings: completed successfully")
        return result

    async def set_circuit_mode(self, plant_id: str, circuit_path: str, mode: str) -> Any:
        """Set circuit operation mode (standby or manual).

        v1 had separate endpoints per mode (.../standby, .../manual, .../reset).
        v3 unifies them under .../programs/{program}. The 'reset' mode no longer
        exists; use reset_circuit() to resume the schedule.
        """
        if mode == "reset":
            raise HovalApiError(
                "set_circuit_mode('reset') is no longer supported by the cloud API; "
                "call reset_circuit() to resume the time program."
            )
        return await self.set_program(plant_id, circuit_path, mode)

    async def set_temporary_change(
        self, plant_id: str, circuit_path: str, value: float, duration: str = "FOUR"
    ) -> Any:
        """Set a temporary value override (works alongside an active time program).

        v3: POST .../{circuitPath}/temporary-change with JSON body
            {"value": <float>, "duration": "fourHours" | "midnight"}
        For HV the value is the air volume percentage (15..100); for HK it is the
        temperature in degrees Celsius (e.g. 21.5).

        The historical FOUR / MIDNIGHT enum values from stored options are accepted
        for backwards compatibility and translated to the v3 camelCase form.

        ICS-HIGH-014 (audit v1.0.1): `duration` is validated against the
        known enum (legacy or v3 form) — an arbitrary string is no longer
        silently transformed and sent to the cloud.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        if duration in _VALID_DURATIONS_LEGACY:
            duration_v3 = {"FOUR": "fourHours", "MIDNIGHT": "midnight"}[duration]
        elif duration in _VALID_DURATIONS_V3:
            duration_v3 = duration
        else:
            raise HovalApiError(
                f"Invalid duration: {duration!r}; expected one of "
                f"{sorted(_VALID_DURATIONS_LEGACY | _VALID_DURATIONS_V3)}"
            )
        body = {"value": value, "duration": duration_v3}
        _LOGGER.debug(
            "set_temporary_change: plant=%s circuit=%s body=%s",
            plant_id,
            circuit_path,
            body,
        )
        result = await self._request(
            "POST",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "temporary-change"),
            plant_id=plant_id,
            json_data=body,
        )
        _LOGGER.debug("set_temporary_change: completed successfully")
        return result

    async def reset_temporary_change(self, plant_id: str, circuit_path: str) -> Any:
        """Cancel an active temporary override and resume the underlying program.

        v3: DELETE /v3/plants/{plantId}/circuits/{circuitPath}/temporary-change
        Replaces the removed v1 .../temporary-change/reset POST.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        _LOGGER.debug(
            "reset_temporary_change: plant=%s circuit=%s",
            plant_id,
            circuit_path,
        )
        result = await self._request(
            "DELETE",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "temporary-change"),
            plant_id=plant_id,
        )
        _LOGGER.debug("reset_temporary_change: completed successfully")
        return result

    async def reset_circuit(self, plant_id: str, circuit_path: str, program: str = "week1") -> Any:
        """Resume a configured time program (defaults to week1).

        The v1 POST .../{circuitPath}/reset endpoint that auto-picked the active
        time program no longer exists. v3 requires the caller to choose a specific
        program. Pass program="week2" to switch to the second weekly schedule.
        """
        return await self.set_program(plant_id, circuit_path, program)

    async def set_program(self, plant_id: str, circuit_path: str, program: str) -> Any:
        """Activate a specific program on a circuit.

        POST /v3/plants/{plantExternalId}/circuits/{circuitPath}/programs/{program}
        Program enum: constant, ecoMode, standby, week1, week2, manual, externalConstant.

        ICS-HIGH-014 (audit v1.0.1): `program` is validated against
        `_VALID_PROGRAMS` — this used to be interpolated straight into the
        URL path with no check at all.
        """
        plant_id = _require_identifier(plant_id, "plant_id")  # ICS-HIGH-002
        circuit_path = _require_identifier(circuit_path, "circuit_path")
        if program not in _VALID_PROGRAMS:
            raise HovalApiError(
                f"Invalid program: {program!r}; expected one of {sorted(_VALID_PROGRAMS)}"
            )
        _LOGGER.debug(
            "set_program: plant=%s circuit=%s program=%s",
            plant_id,
            circuit_path,
            program,
        )
        result = await self._request(
            "POST",
            _url("v3", "plants", plant_id, "circuits", circuit_path, "programs", program),
            plant_id=plant_id,
        )
        _LOGGER.debug("set_program: completed successfully")
        return result

    def invalidate_plant_token(self, plant_id: str) -> None:
        """Invalidate the cached PAT for a specific plant."""
        self._pat_cache.pop(plant_id, None)

    def prune_plant_caches(self, valid_plant_ids: set[str]) -> None:
        """Drop cached tokens/locks for plants no longer part of the account.

        ICS-HIGH-010 (audit v1.0.1): `_pat_cache`/`_pat_locks` previously
        grew forever, keyed by every plant_id ever seen. Called by the
        coordinator after each successful full topology refresh with the
        current, live set of plant IDs. A lock currently held (a PAT
        refresh in progress for that very plant) is left alone —
        vanishingly unlikely for a plant simultaneously reported gone, but
        never safe to remove out from under an active `async with`.
        """
        for pid in [p for p in self._pat_cache if p not in valid_plant_ids]:
            self._pat_cache.pop(pid, None)
        for pid in [
            p
            for p, lock in self._pat_locks.items()
            if p not in valid_plant_ids and not lock.locked()
        ]:
            self._pat_locks.pop(pid, None)

    def invalidate_tokens(self) -> None:
        """Force token refresh on next request."""
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
