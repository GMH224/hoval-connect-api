"""Async API client for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .const import (
    BASE_URL,
    CLIENT_ID,
    ID_TOKEN_TTL,
    IDP_URL,
    PLANT_TOKEN_TTL,
)

_LOGGER = logging.getLogger(__name__)

# Retry configuration for transient errors.
# Keep retries low: each attempt can take up to (_CONNECT_TIMEOUT + _READ_TIMEOUT)
# seconds. More than 2 retries make the coordinator hang long enough to trigger
# HA's ConfigEntryNotReady / watchdog during startup.
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 0.5  # seconds, doubled on each retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Split timeouts: fail fast on dead connections, allow longer for slow reads.
# Total worst-case per attempt: _CONNECT_TIMEOUT + _READ_TIMEOUT = 28 s.
# With 2 retries: ~28 + 0.5 + 28 = ~57 s max for a single endpoint.
_CONNECT_TIMEOUT = 8   # seconds to establish the TCP connection
_READ_TIMEOUT = 20     # seconds to receive the full response body


class HovalAuthError(Exception):
    """Authentication error."""


class HovalApiError(Exception):
    """General API error."""


class HovalConnectApi:
    """Async client for the Hoval Connect cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._id_token_exp: float = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}

    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant."""
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

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
                timeout=aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT),
            ) as resp:
                if resp.status in (400, 401, 403):
                    _LOGGER.warning("IDP auth failed (HTTP %s)", resp.status)
                    raise HovalAuthError(f"Invalid credentials (HTTP {resp.status})")
                resp.raise_for_status()
                data = await resp.json()
        except HovalAuthError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HovalApiError(f"Connection error during authentication: {err}") from err

        if "id_token" not in data:
            _LOGGER.error("IDP response missing id_token. Keys: %s", list(data.keys()))
            raise HovalApiError("IDP response missing id_token")

        self._id_token = data["id_token"]
        self._id_token_exp = time.time() + ID_TOKEN_TTL.total_seconds()
        return self._id_token

    async def _get_plant_access_token(self, plant_id: str) -> str:
        """Get or refresh the plant access token."""
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        id_token = await self._get_id_token()
        try:
            async with self._session.get(
                f"{BASE_URL}/v1/plants/{plant_id}/settings",
                headers={"Authorization": f"Bearer {id_token}"},
                timeout=aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    self._id_token = None
                    raise HovalAuthError("ID token rejected")
                resp.raise_for_status()
                data = await resp.json()
        except (HovalAuthError, HovalApiError):
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HovalApiError(f"Connection error fetching plant token: {err}") from err

        token = data["token"]
        self._pat_cache[plant_id] = (token, time.time() + PLANT_TOKEN_TTL.total_seconds())
        return token

    async def _headers(self, plant_id: str | None = None) -> dict[str, str]:
        """Build request headers with auth tokens."""
        id_token = await self._get_id_token()
        headers = {"Authorization": f"Bearer {id_token}"}
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
        # Use separate connect and read timeouts so a dead server is detected
        # quickly (connect) while still allowing slow-but-alive responses (read).
        timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)

        for attempt in range(_MAX_RETRIES):
            # Rebuild headers on every attempt so a token that expires mid-retry
            # cycle is refreshed automatically rather than sending a stale bearer
            # token that will be rejected with 401.
            headers = await self._headers(plant_id)
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
                    if resp.status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BASE_DELAY * (2**attempt)
                        _LOGGER.warning(
                            "Transient error HTTP %s on %s %s, retrying in %.1fs (%d/%d)",
                            resp.status,
                            method,
                            path,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        _LOGGER.debug("API error body: %s", body[:500])
                        raise HovalApiError(f"API request failed: HTTP {resp.status}")
                    if resp.status == 204 or resp.content_length == 0:
                        return None
                    return await resp.json()
            except (HovalAuthError, HovalApiError):
                raise
            except TimeoutError as err:
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
                _LOGGER.warning("Request timeout on %s %s after %d attempts", method, path, _MAX_RETRIES)
                raise HovalApiError(f"Request timeout after {_MAX_RETRIES} attempts: {err}") from err
            except aiohttp.ClientError as err:
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
                _LOGGER.warning("Connection error on %s %s after %d attempts: %s", method, path, _MAX_RETRIES, err)
                raise HovalApiError(f"Connection error after {_MAX_RETRIES} attempts: {err}") from err

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
        result = await self._request(
            "GET", f"/v3/plants/{plant_id}/circuits", plant_id=plant_id
        )
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
        """Get plant error events."""
        return await self._request("GET", f"/v1/plant-events/{plant_id}", plant_id=plant_id)

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Get latest plant event."""
        return await self._request("GET", f"/v1/plant-events/latest/{plant_id}", plant_id=plant_id)

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Get weather forecast for plant location."""
        return await self._request("GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id)

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
