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
import time
from typing import Any

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

# Split timeouts: fail fast on dead connections, allow longer for slow reads.
# requests accepts this as a (connect, read) tuple directly.
# Total worst-case per attempt: _CONNECT_TIMEOUT + _READ_TIMEOUT = 28 s.
# With 2 retries: ~28 + 0.5 + 28 = ~57 s max for a single endpoint.
_CONNECT_TIMEOUT = 8  # seconds to establish the TCP connection
_READ_TIMEOUT = 20  # seconds to receive the full response body
_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# Hard upper bound on my-plants pagination (audit finding F3, v0.21.1).
# 50 pages x 12 plants/page = 600 plants — far beyond any real account.
# Without a cap, a misbehaving (or tampered-with) server that keeps answering
# `"last": false` with non-empty content would loop get_plants() forever and
# grow the result list without bound. The coordinator's 90 s outer timeout
# would contain that, but the config-flow validation path has no outer guard,
# so the cap must live here in the client.
_MAX_PLANT_PAGES = 50


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
        self._pat_lock = asyncio.Lock()

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
        """
        return self._session.request(
            method, url, headers=headers, params=params, json=json_data, timeout=_TIMEOUT
        )

    async def aclose(self) -> None:
        """Close the underlying requests session's connection pool.

        Called from async_unload_entry(). session.close() is typically fast
        but is still blocking socket-cleanup work, so it runs on the executor
        for consistency with every other call in this class rather than
        assuming it's always instantaneous.
        """
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
                resp = await self._hass.async_add_executor_job(
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
        """Get or refresh the plant access token (double-checked locking)."""
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with self._pat_lock:
            # Re-check inside the lock in case a concurrent caller refreshed it.
            cached = self._pat_cache.get(plant_id)
            if cached and time.time() < cached[1]:
                return cached[0]

            id_token = await self._get_id_token()
            try:
                resp = await self._hass.async_add_executor_job(
                    functools.partial(
                        self._sync_get,
                        f"{BASE_URL}/v1/plants/{plant_id}/settings",
                        headers={
                            "Authorization": f"Bearer {id_token}",
                            "User-Agent": USER_AGENT,
                        },
                    )
                )
                if resp.status_code == 401:
                    self._id_token = None
                    raise HovalAuthError("ID token rejected")
                resp.raise_for_status()
                data = resp.json()
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
        _retry: bool = True,
    ) -> Any:
        """Make an authenticated API request with token retry and transient error backoff."""
        url = f"{BASE_URL}{path}"

        for attempt in range(_MAX_RETRIES):
            # Rebuild headers on every attempt so a token that expires mid-retry
            # cycle is refreshed automatically rather than sending a stale bearer
            # token that will be rejected with 401.
            headers = await self._headers(plant_id)
            try:
                resp = await self._hass.async_add_executor_job(
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
                    if _retry:
                        _LOGGER.debug("Token expired, refreshing and retrying")
                        return await self._request(
                            method,
                            path,
                            plant_id,
                            params,
                            json_data,
                            _retry=False,
                        )
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
                    body = resp.text
                    _LOGGER.warning(
                        "API %s %s -> HTTP 403 (Forbidden). If this persists "
                        "after upgrading, please capture this log line and "
                        "the response body and report it: %s",
                        method,
                        path,
                        body[:500],
                    )
                    raise HovalApiError(f"API request failed: HTTP 403: {body[:500]}")
                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
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
                    body = resp.text
                    _LOGGER.debug("API error body: %s", body[:500])
                    raise HovalApiError(f"API request failed: HTTP {resp.status_code}")
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()
            except (HovalAuthError, HovalApiError):
                raise
            except requests.exceptions.Timeout as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
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
                    "Request timeout on %s %s after %d attempts", method, path, _MAX_RETRIES
                )
                raise HovalApiError(
                    f"Request timeout after {_MAX_RETRIES} attempts: {err}"
                ) from err
            except requests.exceptions.RequestException as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
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
                    "Connection error on %s %s after %d attempts: %s",
                    method,
                    path,
                    _MAX_RETRIES,
                    err,
                )
                raise HovalApiError(
                    f"Connection error after {_MAX_RETRIES} attempts: {err}"
                ) from err

        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    async def get_plants(self) -> list[dict[str, Any]]:
        """Get list of user's plants, fetching all pages.

        Hoval's /api/my-plants endpoint was updated in May 2026 to enforce a
        maximum page size of 12 items.  The response may be:
          - A plain list (old API shape) — returned as-is.
          - A Spring/Page wrapper {"content": [...], "last": bool, ...} — the
            integration iterates all pages and returns a flat list.
        """
        all_plants: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await self._request(
                "GET", "/api/my-plants", params={"size": "12", "page": str(page)}
            )
            if isinstance(result, list):
                # Old (pre-pagination) API shape: plain list, no further pages.
                return result
            if not isinstance(result, dict):
                _LOGGER.warning(
                    "Unexpected get_plants response type %s on page %d; aborting pagination",
                    type(result).__name__,
                    page,
                )
                break
            content = result.get("content", [])
            if not isinstance(content, list):
                _LOGGER.warning("get_plants 'content' is not a list (%s); stopping", type(content))
                break
            all_plants.extend(content)
            # "last" is False when more pages exist; True (or absent) means done.
            if result.get("last", True) or not content:
                break
            page += 1
            if page >= _MAX_PLANT_PAGES:
                _LOGGER.warning(
                    "get_plants pagination exceeded %d pages (%d plants so far); "
                    "truncating — the cloud keeps reporting more pages, which is "
                    "almost certainly an upstream fault",
                    _MAX_PLANT_PAGES,
                    len(all_plants),
                )
                break
        return all_plants

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Get plant settings (also refreshes PAT as side effect)."""
        return await self._request("GET", f"/v1/plants/{plant_id}/settings", plant_id=plant_id)

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Get all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21; v3 is the only path that
        still works. Response shape changed: see coordinator field mapping.

        The v3 endpoint may return either a plain list or a paginated wrapper
        {"content": [...], ...}. Both shapes are normalised to a list here so
        the coordinator always receives a plain list.
        """
        result = await self._request("GET", f"/v3/plants/{plant_id}/circuits", plant_id=plant_id)
        if isinstance(result, dict):
            _LOGGER.debug(
                "get_circuits returned paginated wrapper for plant %s; extracting 'content'",
                plant_id,
            )
            return result.get("content", [])
        return result if isinstance(result, list) else []

    async def get_programs(self, plant_id: str, circuit_path: str) -> Any:
        """Get time programs for a circuit."""
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs",
            plant_id=plant_id,
        )

    async def get_live_values(
        self, plant_id: str, circuit_path: str, circuit_type: str
    ) -> list[dict[str, str]]:
        """Get live sensor values for a circuit.

        The endpoint returns either a plain list of {"key": ..., "value": ...}
        objects or (after Hoval's May 2026 pagination enforcement) a wrapper
        {"content": [...], ...}. Both shapes are normalised to a list here.
        """
        result = await self._request(
            "GET",
            f"/v3/api/statistics/live-values/{plant_id}",
            plant_id=plant_id,
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
        )
        if isinstance(result, dict):
            _LOGGER.debug(
                "get_live_values returned paginated wrapper for circuit %s; extracting 'content'",
                circuit_path,
            )
            return result.get("content", [])
        return result if isinstance(result, list) else []

    async def get_events(self, plant_id: str) -> list[dict[str, Any]]:
        """Get plant error events.

        Normalised to a plain list, mirroring get_circuits()/get_live_values():
        Hoval's May 2026 pagination enforcement wrapped several list endpoints
        in {"content": [...], ...}. The events endpoints were not observed to
        change, but before v0.21.1 this method was the only list endpoint NOT
        hardened against the wrapper — and a wrapped response reached list
        slicing in the coordinator and failed the entire poll (audit finding
        F2). Any non-list, non-wrapper shape degrades to [].
        """
        result = await self._request("GET", f"/v1/plant-events/{plant_id}", plant_id=plant_id)
        if isinstance(result, dict):
            _LOGGER.debug(
                "get_events returned paginated wrapper for plant %s; extracting 'content'",
                plant_id,
            )
            content = result.get("content", [])
            return content if isinstance(content, list) else []
        return result if isinstance(result, list) else []

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Get latest plant event.

        Always returns a dict; {} means "no event available". If the cloud ever
        wraps this endpoint in the May 2026 pagination shape, the first content
        element is returned so callers keep receiving a single PlantEventDTO
        (audit finding F2 — shape drift must not propagate to the coordinator).
        """
        result = await self._request(
            "GET", f"/v1/plant-events/latest/{plant_id}", plant_id=plant_id
        )
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            _LOGGER.debug(
                "get_latest_event returned paginated wrapper for plant %s; taking first element",
                plant_id,
            )
            content = result["content"]
            return content[0] if content and isinstance(content[0], dict) else {}
        return result if isinstance(result, dict) else {}

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Get weather forecast for plant location."""
        return await self._request("GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id)

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
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/settings",
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
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/settings",
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
        """
        duration_v3 = {"FOUR": "fourHours", "MIDNIGHT": "midnight"}.get(
            duration, duration[:1].lower() + duration[1:]
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
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
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
        _LOGGER.debug(
            "reset_temporary_change: plant=%s circuit=%s",
            plant_id,
            circuit_path,
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

        The v1 POST .../{circuitPath}/reset endpoint that auto-picked the active
        time program no longer exists. v3 requires the caller to choose a specific
        program. Pass program="week2" to switch to the second weekly schedule.
        """
        return await self.set_program(plant_id, circuit_path, program)

    async def set_program(self, plant_id: str, circuit_path: str, program: str) -> Any:
        """Activate a specific program on a circuit.

        POST /v3/plants/{plantExternalId}/circuits/{circuitPath}/programs/{program}
        Program enum: constant, ecoMode, standby, week1, week2, manual, externalConstant.
        """
        _LOGGER.debug(
            "set_program: plant=%s circuit=%s program=%s",
            plant_id,
            circuit_path,
            program,
        )
        result = await self._request(
            "POST",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs/{program}",
            plant_id=plant_id,
        )
        _LOGGER.debug("set_program: completed successfully")
        return result

    def invalidate_plant_token(self, plant_id: str) -> None:
        """Invalidate the cached PAT for a specific plant."""
        self._pat_cache.pop(plant_id, None)

    def invalidate_tokens(self) -> None:
        """Force token refresh on next request."""
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
