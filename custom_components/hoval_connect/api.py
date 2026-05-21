"""Async API client for Hoval Connect.

``HovalConnectApi`` is the single point of contact with the Hoval Connect
cloud API.  Every outbound HTTP call goes through ``_request`` (data endpoints)
or the two auth helpers ``_get_id_token`` / ``_get_plant_access_token``.

Communication metrics
---------------------
If a ``HovalApiStats`` instance is provided at construction time, every HTTP
event is recorded:

* ``record_call()``    — at the start of each outbound request attempt
* ``record_retry()``   — before each retry sleep (after a transient failure)
* ``record_timeout()`` — on every ``TimeoutError`` catch (including interim retries)
* ``record_error()``   — when a request terminates in failure (all retries exhausted
                          or non-retryable error)
* ``record_success()`` — when a request returns a valid response

The stats object is safe to access concurrently from the HA event loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import aiohttp

from .api_stats import HovalApiStats
from .const import (
    BASE_URL,
    CLIENT_ID,
    ID_TOKEN_TTL,
    IDP_URL,
    PLANT_TOKEN_TTL,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / timeout constants
# ---------------------------------------------------------------------------

# Keep retries low: each attempt can take up to (_CONNECT_TIMEOUT + _READ_TIMEOUT)
# seconds.  More than 2 retries make the coordinator hang long enough to trigger
# HA's ConfigEntryNotReady / watchdog during startup.
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 0.5  # seconds; actual delay = base * 2^attempt * jitter

# Jitter factor applied to every retry delay.  Each retry sleeps for a random
# duration in [base * JITTER_MIN, base * JITTER_MAX] rather than a fixed value.
# This prevents synchronised retry bursts (thundering-herd) when all circuit
# requests fail at the same moment.
_RETRY_JITTER_MIN = 0.5
_RETRY_JITTER_MAX = 1.0

# HTTP status codes that are safe to retry (server-side transient errors).
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Split timeouts: fail fast on dead TCP connections, allow longer for slow reads.
# Total worst-case per attempt: _CONNECT_TIMEOUT + _READ_TIMEOUT = 28 s.
# With 2 retries: ~28 + delay + 28 ≈ 57 s max per endpoint call.
_CONNECT_TIMEOUT = 8   # seconds to establish the TCP connection
_READ_TIMEOUT = 20     # seconds to receive the full response body


def _jittered_delay(attempt: int) -> float:
    """Return a jittered exponential-backoff delay for the given attempt index.

    Formula: ``_RETRY_BASE_DELAY * 2^attempt * uniform(JITTER_MIN, JITTER_MAX)``

    Example ranges (attempt 0 / 1): ~0.25–0.50 s / ~0.50–1.00 s.
    All retry sleeps in this module call this helper so the jitter strategy
    is applied consistently across ``_request``, ``_get_id_token``, and
    ``_get_plant_access_token``.
    """
    return _RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(
        _RETRY_JITTER_MIN, _RETRY_JITTER_MAX
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HovalAuthError(Exception):
    """Raised when authentication fails (bad credentials or rejected token).

    This is a permanent error — retrying without fixing credentials will not
    help.  It is converted to ``ConfigEntryAuthFailed`` by the coordinator.
    """


class HovalApiError(Exception):
    """Raised for transient or structural API failures.

    Includes network errors, timeouts (after all retries), unexpected HTTP
    status codes, and malformed responses.  The coordinator converts this to
    ``UpdateFailed``.
    """


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class HovalConnectApi:
    """Async HTTP client for the Hoval Connect cloud API.

    Authentication
    --------------
    The integration uses Hoval's SAP IAS OAuth2 password-grant flow:

    1. ``_get_id_token()`` exchanges email/password for a short-lived JWT
       (``id_token``, valid ~25 min with safety margin).
    2. ``_get_plant_access_token()`` exchanges the id_token for a per-plant
       PAT (valid ~12 min with safety margin) sent as ``X-Plant-Access-Token``.

    Both tokens are cached in memory and refreshed transparently before expiry.
    A 401 response from any endpoint triggers an immediate token invalidation
    and one transparent retry (``_retry=True``).

    Resilience
    ----------
    * All HTTP calls use split connect/read timeouts.
    * All transient failures (5xx, 429, ``TimeoutError``, ``ClientError``)
      are retried up to ``_MAX_RETRIES`` times with jittered exponential backoff.
    * Auth errors (4xx from the IDP) are NOT retried.
    * ``HovalApiStats`` records every HTTP event for the diagnostic sensors.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        stats: HovalApiStats | None = None,
    ) -> None:
        """Initialise the API client.

        Args:
            session: Shared aiohttp session from HA (via ``async_get_clientsession``).
            email:   Hoval Connect account email.
            password: Hoval Connect account password.
            stats:   Optional stats collector.  If ``None``, a new instance is
                     created.  Callers that want to expose the stats to HA sensors
                     should create ``HovalApiStats()`` themselves and pass it here.
        """
        self._session = session
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._id_token_exp: float = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}
        self.stats: HovalApiStats = stats if stats is not None else HovalApiStats()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant.

        Cache hit: returns immediately without a network call.
        Cache miss: makes a POST to the SAP IAS token endpoint.

        Retries transient network/timeout failures with jittered backoff.
        ``HovalAuthError`` (4xx from IDP) is raised immediately — bad
        credentials will not fix themselves.
        """
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

        timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
        data: dict[str, Any] = {}

        for attempt in range(_MAX_RETRIES):
            self.stats.record_call()
            try:
                async with self._session.post(
                    IDP_URL,
                    data={
                        "grant_type": "password",
                        "client_id": CLIENT_ID,
                        "username": self._email,
                        "password": self._password,
                        "scope": "openid",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=timeout,
                ) as resp:
                    if resp.status in (400, 401, 403):
                        msg = f"IDP auth failed: HTTP {resp.status}"
                        _LOGGER.warning(msg)
                        self.stats.record_error(msg)
                        raise HovalAuthError(f"Invalid credentials (HTTP {resp.status})")
                    resp.raise_for_status()
                    data = await resp.json()
                # Success — break out of retry loop
                self.stats.record_success()
                break
            except HovalAuthError:
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                is_timeout = isinstance(err, TimeoutError)
                if is_timeout:
                    self.stats.record_timeout()
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "Auth request failed (attempt %d/%d), retrying in %.2fs: %s",
                        attempt + 1, _MAX_RETRIES, delay, err,
                    )
                    self.stats.record_retry()
                    await asyncio.sleep(delay)
                    continue
                msg = f"Auth failed after {_MAX_RETRIES} attempts: {err}"
                self.stats.record_error(msg)
                raise HovalApiError(msg) from err

        if "id_token" not in data:
            msg = f"IDP response missing id_token (keys: {list(data.keys())})"
            _LOGGER.error(msg)
            self.stats.record_error(msg)
            raise HovalApiError("IDP response missing id_token")

        self._id_token = data["id_token"]
        self._id_token_exp = time.time() + ID_TOKEN_TTL.total_seconds()
        return self._id_token

    async def _get_plant_access_token(self, plant_id: str) -> str:
        """Get or refresh the plant access token (PAT) for a specific plant.

        Cache hit: returns immediately without a network call.
        Cache miss: fetches ``/v1/plants/{plant_id}/settings`` which returns
        the PAT in the response body.

        Retries transient failures with jittered backoff.  A 401 response
        means the ID token was already rejected — invalidate it and raise
        ``HovalAuthError`` immediately (retrying with the same stale token
        would fail identically).

        Note: ``/v1/plants/{plant_id}/settings`` intentionally does NOT send
        ``X-Plant-Access-Token`` — the PAT itself is what this endpoint returns.
        """
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        id_token = await self._get_id_token()
        timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
        data: dict[str, Any] = {}

        for attempt in range(_MAX_RETRIES):
            self.stats.record_call()
            try:
                async with self._session.get(
                    f"{BASE_URL}/v1/plants/{plant_id}/settings",
                    headers={"Authorization": f"Bearer {id_token}"},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 401:
                        self._id_token = None
                        msg = "ID token rejected by PAT endpoint"
                        self.stats.record_error(msg)
                        raise HovalAuthError(msg)
                    resp.raise_for_status()
                    data = await resp.json()
                self.stats.record_success()
                break
            except (HovalAuthError, HovalApiError):
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                is_timeout = isinstance(err, TimeoutError)
                if is_timeout:
                    self.stats.record_timeout()
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "PAT request failed for plant %s (attempt %d/%d), retrying in %.2fs: %s",
                        plant_id, attempt + 1, _MAX_RETRIES, delay, err,
                    )
                    self.stats.record_retry()
                    await asyncio.sleep(delay)
                    continue
                msg = f"PAT fetch failed after {_MAX_RETRIES} attempts: {err}"
                self.stats.record_error(msg)
                raise HovalApiError(msg) from err

        token = data["token"]
        self._pat_cache[plant_id] = (token, time.time() + PLANT_TOKEN_TTL.total_seconds())
        return token

    async def _headers(self, plant_id: str | None = None) -> dict[str, str]:
        """Build request headers, fetching/refreshing auth tokens as needed.

        Args:
            plant_id: If provided, also fetches the plant access token (PAT)
                      and adds it as ``X-Plant-Access-Token``.  Omit for
                      endpoints that only require the bearer id_token.
        """
        id_token = await self._get_id_token()
        headers = {"Authorization": f"Bearer {id_token}"}
        if plant_id:
            pat = await self._get_plant_access_token(plant_id)
            headers["X-Plant-Access-Token"] = pat
        return headers

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        plant_id: str | None = None,
        params: dict[str, str] | None = None,
        json_data: Any = None,
        _retry: bool = True,
    ) -> Any:
        """Make an authenticated API request with token retry and transient-error backoff.

        Flow
        ----
        1. Fetch auth headers (token cache hit or refresh).
        2. Attempt the HTTP call up to ``_MAX_RETRIES`` times.
        3. On 401: invalidate tokens and recurse once with ``_retry=False``.
        4. On retryable 5xx/429 or network error: sleep with jitter and retry.
        5. On success: return parsed JSON (or ``None`` for empty responses).

        Stats recording
        ---------------
        ``record_call()``    — once at the start of each outbound HTTP attempt.
        ``record_timeout()`` — on every ``TimeoutError`` (including mid-retry).
        ``record_retry()``   — before each retry sleep.
        ``record_error()``   — on terminal failure (all retries exhausted).
        ``record_success()`` — on a successful 2xx response.

        Args:
            _retry: Internal flag — set to ``False`` on the recursive 401-retry
                    call to prevent infinite recursion.
        """
        headers = await self._headers(plant_id)
        url = f"{BASE_URL}{path}"
        timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)

        for attempt in range(_MAX_RETRIES):
            self.stats.record_call()
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=timeout,
                ) as resp:
                    _LOGGER.debug("API %s %s → HTTP %s", method, path, resp.status)

                    if resp.status == 401:
                        # Token expired mid-session — invalidate and retry once.
                        self._id_token = None
                        if plant_id:
                            self._pat_cache.pop(plant_id, None)
                        if _retry:
                            _LOGGER.debug("Token expired, refreshing and retrying")
                            return await self._request(
                                method, path, plant_id, params, json_data, _retry=False,
                            )
                        msg = "Authentication failed (401 on retry)"
                        self.stats.record_error(msg)
                        raise HovalAuthError("Authentication failed")

                    if resp.status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _jittered_delay(attempt)
                        _LOGGER.warning(
                            "Transient error HTTP %s on %s %s, retrying in %.2fs (%d/%d)",
                            resp.status, method, path, delay, attempt + 1, _MAX_RETRIES,
                        )
                        self.stats.record_retry()
                        await asyncio.sleep(delay)
                        continue

                    if resp.status >= 400:
                        body = await resp.text()
                        _LOGGER.debug("API error body: %s", body[:500])
                        msg = f"API request failed: HTTP {resp.status} on {method} {path}"
                        self.stats.record_error(msg)
                        raise HovalApiError(f"API request failed: HTTP {resp.status}")

                    # Treat 204 No Content and any response whose body is absent
                    # or empty as a successful no-data reply.
                    # ``not resp.content_length`` catches both explicit 0 and a
                    # missing Content-Length header (where aiohttp returns None),
                    # avoiding a ContentTypeError on resp.json() — a known Hoval quirk.
                    if resp.status == 204 or not resp.content_length:
                        self.stats.record_success()
                        return None

                    result = await resp.json()
                    self.stats.record_success()
                    return result

            except (HovalAuthError, HovalApiError):
                raise
            except TimeoutError as err:
                self.stats.record_timeout()
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "Request timeout on %s %s (attempt %d/%d), retrying in %.2fs",
                        method, path, attempt + 1, _MAX_RETRIES, delay,
                    )
                    self.stats.record_retry()
                    await asyncio.sleep(delay)
                    continue
                msg = f"Request timeout after {_MAX_RETRIES} attempts on {method} {path}"
                _LOGGER.warning(msg)
                self.stats.record_error(msg)
                raise HovalApiError(
                    f"Request timeout after {_MAX_RETRIES} attempts: {err}"
                ) from err
            except aiohttp.ClientError as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "Connection error on %s %s (attempt %d/%d), retrying in %.2fs: %s",
                        method, path, attempt + 1, _MAX_RETRIES, delay, err,
                    )
                    self.stats.record_retry()
                    await asyncio.sleep(delay)
                    continue
                msg = f"Connection error after {_MAX_RETRIES} attempts on {method} {path}: {err}"
                _LOGGER.warning(msg)
                self.stats.record_error(msg)
                raise HovalApiError(
                    f"Connection error after {_MAX_RETRIES} attempts: {err}"
                ) from err

        msg = f"Request failed after {_MAX_RETRIES} retries on {method} {path}"
        self.stats.record_error(msg)
        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_plants(self) -> list[dict[str, Any]]:
        """Return the list of plants associated with the authenticated account.

        Endpoint: ``GET /api/my-plants``
        Auth: id_token only (no PAT required).
        """
        return await self._request("GET", "/api/my-plants", params={"size": "12", "page": "0"})

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Return plant settings.  Also has the side-effect of refreshing the PAT."""
        return await self._request("GET", f"/v1/plants/{plant_id}/settings", plant_id=plant_id)

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Return all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21; v3 is the only path
        that still works.  Response shape changed: see coordinator field mapping
        in ``coordinator.py``.
        """
        return await self._request("GET", f"/v3/plants/{plant_id}/circuits", plant_id=plant_id)

    async def get_programs(self, plant_id: str, circuit_path: str) -> Any:
        """Return the time-program configuration for a circuit."""
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs",
            plant_id=plant_id,
        )

    async def get_live_values(
        self, plant_id: str, circuit_path: str, circuit_type: str
    ) -> list[dict[str, str]]:
        """Return live telemetry values for a circuit."""
        return await self._request(
            "GET",
            f"/v3/api/statistics/live-values/{plant_id}",
            plant_id=plant_id,
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
        )

    async def get_events(self, plant_id: str) -> list[dict[str, Any]]:
        """Return the current error/warning event list for a plant.

        Note: intentionally omits ``plant_id=`` so only the id_token (Bearer)
        is sent.  The ``/v1/plant-events/`` paths do NOT require
        ``X-Plant-Access-Token``.  Do not add ``plant_id=`` here — it would
        inject an unwanted PAT header and may cause 401 errors on this endpoint.
        """
        return await self._request("GET", f"/v1/plant-events/{plant_id}")

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Return the most recent event for a plant.

        Note: intentionally omits ``plant_id=`` — see ``get_events()`` for rationale.
        """
        return await self._request("GET", f"/v1/plant-events/latest/{plant_id}")

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Return the weather forecast for the plant's location."""
        return await self._request(
            "GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id
        )

    async def set_circuit_mode(self, plant_id: str, circuit_path: str, mode: str) -> Any:
        """Set a circuit's operation mode (standby, manual, etc.).

        v1 had separate endpoints per mode (.../standby, .../manual, .../reset).
        v3 unifies them under ``.../programs/{program}``.  The ``reset`` mode
        no longer exists; use ``reset_circuit()`` to resume the schedule.
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
        """Set a temporary value override alongside the active time program.

        v3: ``POST .../circuits/{circuitPath}/temporary-change``
        Body: ``{"value": <float>, "duration": "fourHours" | "midnight"}``

        For HV, ``value`` is the air-volume percentage (15–100).
        For HK, ``value`` is the temperature in °C.

        The legacy ``FOUR`` / ``MIDNIGHT`` enum values are accepted for
        backwards compatibility and translated to v3 camelCase.
        """
        duration_v3 = {"FOUR": "fourHours", "MIDNIGHT": "midnight"}.get(
            duration, duration[:1].lower() + duration[1:]
        )
        body = {"value": value, "duration": duration_v3}
        _LOGGER.debug("set_temporary_change: plant=%s circuit=%s body=%s", plant_id, circuit_path, body)
        result = await self._request(
            "POST",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            plant_id=plant_id,
            json_data=body,
        )
        _LOGGER.debug("set_temporary_change: completed successfully")
        return result

    async def reset_temporary_change(self, plant_id: str, circuit_path: str) -> Any:
        """Cancel an active temporary override and resume the underlying program.

        v3: ``DELETE /v3/plants/{plantId}/circuits/{circuitPath}/temporary-change``
        Replaces the removed v1 ``.../temporary-change/reset`` POST.
        """
        _LOGGER.debug("reset_temporary_change: plant=%s circuit=%s", plant_id, circuit_path)
        result = await self._request(
            "DELETE",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            plant_id=plant_id,
        )
        _LOGGER.debug("reset_temporary_change: completed successfully")
        return result

    async def reset_circuit(self, plant_id: str, circuit_path: str, program: str = "week1") -> Any:
        """Resume a configured time program (defaults to week1).

        The v1 ``POST .../{circuitPath}/reset`` endpoint that auto-picked the
        active time program no longer exists.  v3 requires the caller to choose
        a specific program.  Pass ``program="week2"`` to switch to the second
        weekly schedule.
        """
        return await self.set_program(plant_id, circuit_path, program)

    async def set_program(self, plant_id: str, circuit_path: str, program: str) -> Any:
        """Activate a specific named program on a circuit.

        Endpoint: ``POST /v3/plants/{plantExternalId}/circuits/{circuitPath}/programs/{program}``

        Valid program values: ``constant``, ``ecoMode``, ``standby``, ``week1``,
        ``week2``, ``manual``, ``externalConstant``.
        """
        _LOGGER.debug("set_program: plant=%s circuit=%s program=%s", plant_id, circuit_path, program)
        result = await self._request(
            "POST",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs/{program}",
            plant_id=plant_id,
        )
        _LOGGER.debug("set_program: completed successfully")
        return result

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def invalidate_plant_token(self, plant_id: str) -> None:
        """Invalidate the cached PAT for a specific plant.

        Called by the coordinator when a plant goes offline so that a fresh
        token is fetched when the plant comes back online.
        """
        self._pat_cache.pop(plant_id, None)

    def invalidate_tokens(self) -> None:
        """Invalidate all cached tokens, forcing a full re-authentication.

        Used by the re-auth flow and test helpers.
        """
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
