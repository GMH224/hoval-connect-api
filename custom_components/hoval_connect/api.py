"""Async HTTP client for the Hoval Connect cloud API.

Authentication flow
-------------------
1. ``_get_id_token()`` exchanges email/password for a short-lived JWT via the
   SAP IAS OAuth2 password-grant endpoint.  The token is cached for
   ``ID_TOKEN_TTL`` (25 min with safety margin).
2. ``_get_plant_access_token()`` exchanges the id_token for a per-plant PAT
   returned by ``/v1/plants/{plantId}/settings``.  Cached for ``PLANT_TOKEN_TTL``
   (12 min with safety margin).
3. A 401 on any data endpoint triggers a single transparent re-auth cycle
   (``_retry=True``).

Resilience
----------
* Split connect/read timeouts so dead TCP connections are detected in 8 s.
* Transient errors (5xx, 429, ``TimeoutError``, ``ClientError``) are retried up
  to ``_MAX_RETRIES`` times with exponential back-off.
* Auth errors (4xx from the IDP) are NOT retried — bad credentials do not fix
  themselves.

Communication statistics
------------------------
If a ``HovalApiStats`` instance is provided at construction time (or created
automatically), every HTTP event is recorded:

* ``record_call()``    — at the start of every outbound attempt
* ``record_retry()``   — before every back-off sleep
* ``record_timeout()`` — on every ``TimeoutError`` catch (including mid-retry)
* ``record_error()``   — on every terminal failure (all retries exhausted)
* ``record_success()`` — on every valid 2xx response

Access the stats object via ``api.stats``.
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
# Retry / timeout configuration
# ---------------------------------------------------------------------------

# Keep retries low: each attempt can take up to (_CONNECT_TIMEOUT + _READ_TIMEOUT)
# seconds.  More than 2 retries make the coordinator hang long enough to trigger
# HA's ConfigEntryNotReady / watchdog during startup.
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 0.5  # seconds; doubled on each subsequent attempt

# HTTP status codes that indicate a transient server-side problem worth retrying.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Split timeouts: fail fast on dead connections; allow longer for slow reads.
# Worst-case per attempt: _CONNECT_TIMEOUT + _READ_TIMEOUT = 28 s.
# With 2 retries: ~28 + 0.5 + 28 ≈ 57 s max per endpoint call.
_CONNECT_TIMEOUT = 8   # seconds to establish the TCP connection
_READ_TIMEOUT = 20     # seconds to receive the full response body

# Jitter applied to every retry sleep so that parallel circuit requests that
# fail simultaneously do not all retry at the exact same instant
# (thundering-herd suppression).  Delay = base * 2^attempt * uniform(MIN, MAX).
_RETRY_JITTER_MIN = 0.5
_RETRY_JITTER_MAX = 1.0


def _jittered_delay(attempt: int) -> float:
    """Return a jittered exponential back-off delay for the given attempt index.

    Formula: ``_RETRY_BASE_DELAY * 2^attempt * uniform(JITTER_MIN, JITTER_MAX)``
    Ranges (attempt 0 / 1): ~0.25–0.50 s / ~0.50–1.00 s.
    """
    return _RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(
        _RETRY_JITTER_MIN, _RETRY_JITTER_MAX
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HovalAuthError(Exception):
    """Raised when authentication fails due to bad credentials or a rejected token.

    This is a permanent failure — retrying without fixing the credentials will
    not help.  The coordinator converts it to ``ConfigEntryAuthFailed``.
    """


class HovalApiError(Exception):
    """Raised for transient or structural API failures.

    Covers network errors, timeouts (after all retries are exhausted), unexpected
    HTTP status codes, and malformed responses.  The coordinator converts it to
    ``UpdateFailed``.
    """


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class HovalConnectApi:
    """Async HTTP client for the Hoval Connect cloud API.

    Args:
        session:  Shared aiohttp session provided by HA via
                  ``async_get_clientsession(hass)``.
        email:    Hoval Connect account email address.
        password: Hoval Connect account password.
        stats:    Optional ``HovalApiStats`` instance for communication monitoring.
                  If ``None``, a private instance is created automatically so
                  callers that do not need to expose the stats can still use the
                  client without any extra setup.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        stats: HovalApiStats | None = None,
    ) -> None:
        """Initialise the API client."""
        self._session = session
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._id_token_exp: float = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}
        # Always guarantee a non-None stats object so callers need no None-checks.
        self.stats: HovalApiStats = stats if stats is not None else HovalApiStats()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _get_id_token(self) -> str:
        """Fetch or return the cached id_token via OAuth2 password grant.

        Cache hit path: returns immediately, no network call, no stats recorded.

        Cache miss path: performs a POST to the SAP IAS token endpoint.  Stats
        are recorded for every attempt (``record_call``), any timeout
        (``record_timeout``), retries (``record_retry``), terminal failures
        (``record_error``), and success (``record_success``).

        ``HovalAuthError`` is raised immediately on 4xx from the IDP and is
        never retried — incorrect credentials do not fix themselves.
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
                # Successful response — exit retry loop
                self.stats.record_success()
                break
            except HovalAuthError:
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                if isinstance(err, TimeoutError):
                    self.stats.record_timeout()
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "Auth request failed (attempt %d/%d), retrying in %.1fs: %s",
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
        """Fetch or return the cached plant access token (PAT).

        Cache hit path: returns immediately, no network call.

        Cache miss path: fetches ``/v1/plants/{plant_id}/settings`` which
        returns the PAT in the response body.  Stats are recorded for every
        attempt, timeout, retry, failure, and success.

        A 401 from this endpoint means the id_token was already rejected.  The
        id_token cache is invalidated and ``HovalAuthError`` is raised immediately
        (retrying with the same stale token would fail identically).

        Note: ``/v1/plants/{plant_id}/settings`` is called WITHOUT
        ``X-Plant-Access-Token`` because that is exactly the token being fetched.
        Do not add ``plant_id=plant_id`` to this call.
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
                if isinstance(err, TimeoutError):
                    self.stats.record_timeout()
                if attempt < _MAX_RETRIES - 1:
                    delay = _jittered_delay(attempt)
                    _LOGGER.warning(
                        "PAT request failed for plant %s (attempt %d/%d), retrying in %.1fs: %s",
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
        """Build request headers, refreshing auth tokens as needed.

        Args:
            plant_id: When provided, the plant access token is fetched and added
                      as ``X-Plant-Access-Token``.  Omit for endpoints that only
                      require the bearer id_token (e.g. the events endpoints).
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
        """Make an authenticated API request with token refresh and transient-error back-off.

        Stats recording
        ---------------
        ``record_call()``    — once at the start of every outbound attempt.
        ``record_timeout()`` — on every ``TimeoutError`` (including mid-retry).
        ``record_retry()``   — before every back-off sleep.
        ``record_error()``   — on every terminal failure.
        ``record_success()`` — on every successful 2xx response.

        Args:
            _retry: Internal flag — ``False`` on the recursive 401-retry call to
                    prevent infinite recursion.
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
                        msg = f"Authentication failed (401 on retry) for {method} {path}"
                        self.stats.record_error(msg)
                        raise HovalAuthError("Authentication failed")

                    if resp.status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _jittered_delay(attempt)
                        _LOGGER.warning(
                            "Transient error HTTP %s on %s %s, retrying in %.1fs (%d/%d)",
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

                    # Treat 204 No Content and explicit empty bodies as success
                    # with no payload.  Also catches missing Content-Length header
                    # (aiohttp returns None), preventing a ContentTypeError from
                    # resp.json() on an empty body — a known Hoval API quirk.
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
                        "Request timeout on %s %s (attempt %d/%d), retrying in %.1fs",
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
                        "Connection error on %s %s (attempt %d/%d), retrying in %.1fs: %s",
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

        # This line is unreachable in practice (the loop always returns or
        # raises before exhausting attempts), but satisfies the type checker.
        msg = f"Request failed after {_MAX_RETRIES} retries on {method} {path}"
        self.stats.record_error(msg)
        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_plants(self) -> list[dict[str, Any]]:
        """Return the paginated list of plants for the authenticated account.

        Endpoint: ``GET /api/my-plants``
        Auth: id_token only (no PAT required).
        """
        return await self._request("GET", "/api/my-plants", params={"size": "12", "page": "0"})

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Return plant settings.  Side-effect: refreshes the PAT cache."""
        return await self._request("GET", f"/v1/plants/{plant_id}/settings", plant_id=plant_id)

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Return all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21.  v3 is the only path
        that still works.  Response shape changed — see coordinator field mapping.
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
        """Return the latest live telemetry values for a circuit."""
        return await self._request(
            "GET",
            f"/v3/api/statistics/live-values/{plant_id}",
            plant_id=plant_id,
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
        )

    async def get_events(self, plant_id: str) -> list[dict[str, Any]]:
        """Return the current error/warning event list for a plant.

        Note: intentionally called WITHOUT ``plant_id=plant_id`` so only the
        id_token (Bearer) is sent.  The ``/v1/plant-events/`` paths do NOT
        require ``X-Plant-Access-Token``.  Adding ``plant_id=`` here would
        inject an unwanted PAT header and may cause 401 errors on this endpoint.
        """
        return await self._request("GET", f"/v1/plant-events/{plant_id}")

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Return the most recent event for a plant.

        Note: intentionally called WITHOUT ``plant_id=plant_id`` — see
        ``get_events()`` for the rationale.
        """
        return await self._request("GET", f"/v1/plant-events/latest/{plant_id}")

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Return the weather forecast for the plant's geographic location."""
        return await self._request(
            "GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id
        )

    async def set_circuit_mode(self, plant_id: str, circuit_path: str, mode: str) -> Any:
        """Set a circuit's operation mode.

        v1 had separate endpoints per mode (``.../standby``, ``.../manual``,
        ``.../reset``).  v3 unifies them under ``.../programs/{program}``.
        The ``reset`` program no longer exists; use ``reset_circuit()`` instead.
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

        v3 endpoint: ``POST .../circuits/{circuitPath}/temporary-change``
        Body: ``{"value": <float>, "duration": "fourHours" | "midnight"}``

        For HV, ``value`` is air-volume percentage (15–100).
        For HK, ``value`` is target temperature in °C.

        The historical ``FOUR`` / ``MIDNIGHT`` enum values from stored options
        are accepted for backward compatibility and translated to v3 camelCase.
        """
        duration_v3 = {"FOUR": "fourHours", "MIDNIGHT": "midnight"}.get(
            duration, duration[:1].lower() + duration[1:]
        )
        body = {"value": value, "duration": duration_v3}
        _LOGGER.debug(
            "set_temporary_change: plant=%s circuit=%s body=%s", plant_id, circuit_path, body
        )
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
        _LOGGER.debug(
            "reset_temporary_change: plant=%s circuit=%s", plant_id, circuit_path
        )
        result = await self._request(
            "DELETE",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            plant_id=plant_id,
        )
        _LOGGER.debug("reset_temporary_change: completed successfully")
        return result

    async def reset_circuit(self, plant_id: str, circuit_path: str, program: str = "week1") -> Any:
        """Resume a configured time program (defaults to week1).

        The v1 ``POST .../{circuitPath}/reset`` endpoint that auto-selected the
        active time program no longer exists.  v3 requires the caller to specify
        the program explicitly.  Pass ``program="week2"`` for the second schedule.
        """
        return await self.set_program(plant_id, circuit_path, program)

    async def set_program(self, plant_id: str, circuit_path: str, program: str) -> Any:
        """Activate a named program on a circuit.

        Endpoint: ``POST /v3/plants/{plantExternalId}/circuits/{circuitPath}/programs/{program}``

        Valid program values: ``constant``, ``ecoMode``, ``standby``, ``week1``,
        ``week2``, ``manual``, ``externalConstant``.
        """
        _LOGGER.debug(
            "set_program: plant=%s circuit=%s program=%s", plant_id, circuit_path, program
        )
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
        """Invalidate the cached PAT for a plant (called when it goes offline)."""
        self._pat_cache.pop(plant_id, None)

    def invalidate_tokens(self) -> None:
        """Force a full re-authentication on the next request."""
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
