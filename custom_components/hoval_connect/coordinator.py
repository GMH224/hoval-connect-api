"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import (
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_WW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROGRAM_CACHE_TTL,
    SUPPORTED_CIRCUIT_TYPES,
)

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

_LOGGER = logging.getLogger(__name__)

# Circuit-breaker configuration.
# After _CB_THRESHOLD consecutive poll failures the breaker opens and poll
# attempts are skipped (no network traffic) until _CB_PROBE_INTERVAL seconds
# have elapsed, at which point one probe is allowed through.  If the probe
# succeeds the breaker closes; if it fails the interval resets.
_CB_THRESHOLD = 5
_CB_PROBE_INTERVAL = 300.0  # seconds between probe attempts

# Rolling window size for connection health rate sensors.
# All _poll_records older than this are excluded from rate calculations.
# Records older than 2× this value are pruned from the list.
_HEALTH_WINDOW_SECS = 3600.0

# v1 API returns different activeProgram values than v3.
# Normalize so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",  # time program active (v1 doesn't say which week)
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _resolve_active_program_value(
    programs: dict[str, Any], now: datetime
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day program name, and air volume.

    Returns (week_name, day_program_name, current_phase_value).
    """
    day_programs = programs.get("dayPrograms", {})
    day_configs = day_programs.get("dayConfigurations", [])
    if not day_configs:
        return None, None, None

    # Build lookup: id -> day config
    config_by_id: dict[int, dict] = {d["id"]: d for d in day_configs}

    # Determine which week is active (week1 by default)
    week = programs.get("week1", {})
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds", [])

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")

    # Find active phase based on current time
    current_minutes = now.hour * 60 + now.minute
    for phase in day_config.get("phases", []):
        start = phase["start"]
        end = phase["end"]
        start_min = start["hours"] * 60 + start["minutes"]
        end_min = end["hours"] * 60 + end["minutes"]
        if start_min <= current_minutes < end_min:
            return week_name, day_name, phase.get("value")

    return week_name, day_name, None


class _PollRecord(NamedTuple):
    """Single poll result stored in the rolling health window.

    ts:         time.monotonic() at the moment the poll completed.
                Note: monotonic time restarts with HA, so _poll_records is
                always scoped to the current HA session — cross-session history
                is not preserved, which is intentional.
    error_type: None for success; "timeout"/"auth"/"api"/"unknown" for failure.
    latency_ms: wall-clock duration of the poll in milliseconds. Populated only
                for successful polls; None for failures.
    """

    ts: float
    error_type: str | None
    latency_ms: float | None


@dataclass
class HovalConnectionHealth:
    """Tracks API connection health metrics across coordinator polls.

    Persists across individual poll cycles so cumulative counters survive
    transient failures.  Updated inside _async_update_data before any
    exception is re-raised, so sensors always reflect the latest state even
    when HA marks the coordinator as unavailable.

    Rolling-window properties (failure_rate_pct_1h, auth_failure_rate_pct_1h,
    p95_latency_ms_1h) are computed on read from _poll_records rather than
    being stored, so they never need explicit synchronisation.
    """

    # --- Timestamps ---
    last_success: datetime | None = None      # UTC datetime of last successful poll
    last_error_time: datetime | None = None   # UTC datetime of last poll error

    # --- Error details ---
    last_error_msg: str | None = None         # Short error string from last failure
    last_error_type: str | None = None        # "timeout" | "auth" | "api" | "unknown"

    # --- Cumulative counters ---
    consecutive_failures: int = 0            # Consecutive failed polls (reset on success)
    total_failures: int = 0                  # Total failed polls since HA startup
    total_polls: int = 0                     # Total poll attempts since HA startup
    auth_failures: int = 0                   # Total HovalAuthError occurrences since startup

    # --- Performance ---
    poll_latency_ms: float | None = None     # Duration of last successful full poll (ms)

    # --- Partial / sub-task failures ---
    # These capture silent failures inside gather(return_exceptions=True) that
    # don't abort the whole poll but leave some entities with stale data.
    partial_failures_last_poll: int = 0      # Failed sub-tasks in the most recent poll
    total_partial_failures: int = 0          # Cumulative sub-task failures since startup
    partial_failure_endpoints: str | None = None  # Comma-separated list of what failed

    # --- Rolling window ---
    # List of _PollRecord tuples (ts, error_type, latency_ms).
    # Coordinator appends one entry per actual poll (circuit-breaker-skipped
    # polls are NOT recorded here) and prunes entries older than 2× _HEALTH_WINDOW_SECS
    # to keep memory bounded (≤ ~120 entries at the default 60 s poll interval).
    _poll_records: list[_PollRecord] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def record_poll(
        self,
        ts: float,
        error_type: str | None,
        latency_ms: float | None = None,
    ) -> None:
        """Append a poll result and prune the history buffer.

        Args:
            ts:         time.monotonic() at poll completion.
            error_type: None for success; error category string for failures.
            latency_ms: Duration of successful poll in ms (omit for failures).
        """
        self._poll_records.append(_PollRecord(ts, error_type, latency_ms))
        # Prune anything older than 2× the window to prevent unbounded growth.
        cutoff = ts - _HEALTH_WINDOW_SECS * 2
        if len(self._poll_records) > 250:  # hard cap as safety valve
            self._poll_records = [r for r in self._poll_records if r.ts >= cutoff]

    def _window(self) -> list[_PollRecord]:
        """Return records within the rolling 1-hour window."""
        cutoff = time.monotonic() - _HEALTH_WINDOW_SECS
        return [r for r in self._poll_records if r.ts >= cutoff]

    # ------------------------------------------------------------------ #
    # Computed rolling-window properties                                   #
    # ------------------------------------------------------------------ #

    @property
    def failure_rate_pct_1h(self) -> float:
        """Failure % over the rolling 60-minute window.

        Returns 0.0 when no polls have been recorded yet, so sensors show 0
        rather than 'unknown' during the warmup period after HA restart.
        """
        window = self._window()
        if not window:
            return 0.0
        failures = sum(1 for r in window if r.error_type is not None)
        return round(failures / len(window) * 100, 1)

    @property
    def auth_failure_rate_pct_1h(self) -> float:
        """Auth-error % over the rolling 60-minute window.

        Counts only polls whose error_type == "auth" so transient API/timeout
        errors don't inflate the auth-specific rate.  Returns 0.0 on startup.
        """
        window = self._window()
        if not window:
            return 0.0
        auth_fails = sum(1 for r in window if r.error_type == "auth")
        return round(auth_fails / len(window) * 100, 1)

    @property
    def p95_latency_ms_1h(self) -> float | None:
        """95th-percentile successful poll latency over the last 60 minutes.

        Requires at least 5 successful-poll samples to return a meaningful
        value; returns None otherwise (shown as 'unknown' in HA).  At the
        default 60 s interval there are ~60 polls/hour, so P95 stabilises
        within the first few minutes after a clean startup.
        """
        window = self._window()
        latencies = sorted(r.latency_ms for r in window if r.latency_ms is not None)
        if len(latencies) < 5:
            return None
        idx = min(int(len(latencies) * 0.95), len(latencies) - 1)
        return latencies[idx]


@dataclass
class HovalEventData:
    """Parsed data for a plant event."""

    event_type: str | None = None
    description: str | None = None
    time_occurred: str | None = None
    time_resolved: str | None = None
    source_path: str | None = None
    code: int | None = None

    @property
    def is_active(self) -> bool:
        """Event is active when it has not been resolved."""
        return self.time_resolved is None


@dataclass
class HovalCircuitData:
    """Parsed data for a single circuit."""

    circuit_type: str
    path: str
    name: str
    operation_mode: str | None = None
    active_program: str | None = None
    # HV: air-volume percentage; HK: target temperature in °C. Coming from the
    # circuit list endpoint's `targetValue` (renamed from v1 `targetAirVolume`).
    target_value: float | None = None
    is_air_quality_guided: bool = False
    has_error: bool = False
    circuit_status: str | None = None
    live_values: dict[str, str] = field(default_factory=dict)
    active_week_name: str | None = None
    active_day_program_name: str | None = None
    program_air_volume: float | None = None
    # User-defined program names: API key → display name (e.g. "week1" → "Normal")
    program_names: dict[str, str] = field(default_factory=dict)
    # Sub-fetch failures: which internal endpoints failed silently for this circuit.
    # Populated by _fetch_circuit_data; used to track partial failures in health.
    failed_sub_fetches: list[str] = field(default_factory=list)


@dataclass
class HovalWeatherData:
    """Parsed weather forecast data for a plant."""

    weather_type: str | None = None
    outside_temperature: float | None = None
    outside_temperature_min: float | None = None


@dataclass
class HovalPlantData:
    """Parsed data for a single plant."""

    plant_id: str
    name: str
    is_online: bool = True
    has_error: bool = False
    circuits: dict[str, HovalCircuitData] = field(default_factory=dict)
    latest_event: HovalEventData | None = None
    events: list[HovalEventData] = field(default_factory=list)
    weather: HovalWeatherData | None = None


@dataclass
class HovalData:
    """Top-level data returned by the coordinator."""

    plants: dict[str, HovalPlantData] = field(default_factory=dict)


def _parse_event(raw: dict) -> HovalEventData:
    """Parse a PlantEventDTO dict into HovalEventData."""
    return HovalEventData(
        event_type=raw.get("eventType"),
        description=raw.get("description"),
        time_occurred=raw.get("timeOccurred"),
        time_resolved=raw.get("timeResolved"),
        source_path=raw.get("sourcePath"),
        code=raw.get("code"),
    )


def _is_problem_event(event: HovalEventData | None) -> bool:
    """Return True if event is active and represents a fault (blocking/locking/warning)."""
    return bool(
        event
        and event.is_active
        and event.event_type
        in (
            "blocking",
            "locking",
            "warning",
        )
    )


DEFAULT_FAN_SPEED = 40


def resolve_fan_speed(circuit: HovalCircuitData | None) -> int:
    """Resolve the best fan speed value for constant mode.

    Fallback chain: live airVolume → targetValue → program air volume → default.
    Always returns at least 1 (API rejects value=0).
    """
    if circuit is None:
        return DEFAULT_FAN_SPEED
    # Try live sensor value first
    val = circuit.live_values.get("airVolume")
    if val is not None:
        speed = int(float(val))
        if speed >= 1:
            return speed
    # Try target from circuit config
    if circuit.target_value is not None:
        speed = int(circuit.target_value)
        if speed >= 1:
            return speed
    # Try the currently active time program phase value
    if circuit.program_air_volume is not None:
        speed = int(circuit.program_air_volume)
        if speed >= 1:
            return speed
    return DEFAULT_FAN_SPEED


class HovalDataCoordinator(DataUpdateCoordinator[HovalData]):
    """Coordinator to fetch data from Hoval Connect API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: HovalConnectApi,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.control_lock = asyncio.Lock()
        # Optimistic mode override per circuit (set by control actions,
        # cleared on next poll). Key: circuit_path, value: operation mode string.
        self._mode_override: dict[str, str] = {}
        # Program cache: key=circuit_path, value=(programs_data, timestamp)
        self._program_cache: dict[str, tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()
        # API connection health — persists across poll cycles
        self._connection_health = HovalConnectionHealth()
        # Circuit-breaker state
        self._cb_open: bool = False
        self._cb_probe_after: float = 0.0  # monotonic time when probe is allowed

    @property
    def connection_health(self) -> HovalConnectionHealth:
        """Return the current API connection health snapshot."""
        return self._connection_health

    @property
    def circuit_breaker_open(self) -> bool:
        """Return whether the circuit breaker is currently open."""
        return self._cb_open

    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[circuit_path] = mode

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit."""
        return self._mode_override.get(circuit_path)

    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh."""
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        await asyncio.sleep(2)

        async def _do_refresh() -> None:
            try:
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        self.hass.async_create_task(_do_refresh())

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API, updating connection health on every attempt.

        Circuit breaker:
          After _CB_THRESHOLD consecutive failures the breaker opens.  While
          open, _async_update_data raises UpdateFailed immediately without
          making any network requests.  After _CB_PROBE_INTERVAL seconds one
          probe is allowed through; success closes the breaker, failure resets
          the probe timer.  Circuit-breaker-skipped calls are NOT counted in
          total_polls or recorded in the rolling window — they represent
          "intentionally skipped", not "attempt failed".

        Health counters:
          All counters are updated before exceptions are re-raised so that
          connection-health sensors always reflect the latest failure state
          even when HA marks the coordinator as unavailable.
        """
        # ------------------------------------------------------------------ #
        # Circuit-breaker gate — check BEFORE incrementing any counters       #
        # ------------------------------------------------------------------ #
        if self._cb_open:
            now_mono = time.monotonic()
            if now_mono < self._cb_probe_after:
                wait = self._cb_probe_after - now_mono
                _LOGGER.debug(
                    "Circuit breaker open — skipping poll (probe in %.0fs)", wait
                )
                raise UpdateFailed(
                    f"Circuit breaker open after {_CB_THRESHOLD} consecutive failures. "
                    f"Probe in {int(wait)}s."
                )
            # Probe window reached — allow one attempt through
            _LOGGER.info(
                "Circuit breaker: probe attempt (was open for %.0fs)",
                now_mono - (self._cb_probe_after - _CB_PROBE_INTERVAL),
            )
            self._cb_open = False

        _start = time.monotonic()
        self._connection_health.total_polls += 1

        try:
            async with asyncio.timeout(90):
                result = await self._fetch_all_data()

            # ---- Success ------------------------------------------------- #
            _end = time.monotonic()
            elapsed_ms = round((_end - _start) * 1000, 0)
            now_utc = dt_util.utcnow()
            h = self._connection_health
            h.last_success = now_utc
            h.consecutive_failures = 0
            h.poll_latency_ms = elapsed_ms
            h.record_poll(_end, None, elapsed_ms)
            _LOGGER.debug(
                "Poll succeeded in %.0f ms (total_polls=%d, failure_rate_1h=%.1f%%)",
                elapsed_ms,
                h.total_polls,
                h.failure_rate_pct_1h,
            )
            return result

        except TimeoutError as err:
            self._connection_health.consecutive_failures += 1
            self._connection_health.total_failures += 1
            self._connection_health.last_error_time = dt_util.utcnow()
            self._connection_health.last_error_type = "timeout"
            self._connection_health.last_error_msg = "Poll timeout after 90 s"
            self._connection_health.partial_failures_last_poll = 0
            self._connection_health.record_poll(time.monotonic(), "timeout")
            self._maybe_open_circuit_breaker()
            _LOGGER.warning(
                "Poll timed out (consecutive=%d, total_failures=%d, failure_rate_1h=%.1f%%)",
                self._connection_health.consecutive_failures,
                self._connection_health.total_failures,
                self._connection_health.failure_rate_pct_1h,
            )
            raise UpdateFailed(
                "Hoval API refresh timed out after 90 s — cloud may be unresponsive."
            ) from err

        except HovalAuthError as err:
            self._connection_health.consecutive_failures += 1
            self._connection_health.total_failures += 1
            self._connection_health.auth_failures += 1
            self._connection_health.last_error_time = dt_util.utcnow()
            self._connection_health.last_error_type = "auth"
            self._connection_health.last_error_msg = f"Auth error: {err}"[:200]
            self._connection_health.partial_failures_last_poll = 0
            self._connection_health.record_poll(time.monotonic(), "auth")
            self._maybe_open_circuit_breaker()
            _LOGGER.warning(
                "Auth failure (consecutive=%d, auth_failures=%d, auth_rate_1h=%.1f%%): %s",
                self._connection_health.consecutive_failures,
                self._connection_health.auth_failures,
                self._connection_health.auth_failure_rate_pct_1h,
                err,
            )
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err

        except HovalApiError as err:
            self._connection_health.consecutive_failures += 1
            self._connection_health.total_failures += 1
            self._connection_health.last_error_time = dt_util.utcnow()
            self._connection_health.last_error_type = "api"
            self._connection_health.last_error_msg = str(err)[:200]
            self._connection_health.partial_failures_last_poll = 0
            self._connection_health.record_poll(time.monotonic(), "api")
            self._maybe_open_circuit_breaker()
            _LOGGER.warning(
                "API error during poll (consecutive=%d, total_failures=%d): %s",
                self._connection_health.consecutive_failures,
                self._connection_health.total_failures,
                err,
            )
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

        except Exception as err:  # noqa: BLE001
            self._connection_health.consecutive_failures += 1
            self._connection_health.total_failures += 1
            self._connection_health.last_error_time = dt_util.utcnow()
            self._connection_health.last_error_type = "unknown"
            self._connection_health.last_error_msg = f"{type(err).__name__}: {err}"[:200]
            self._connection_health.partial_failures_last_poll = 0
            self._connection_health.record_poll(time.monotonic(), "unknown")
            self._maybe_open_circuit_breaker()
            raise

    def _maybe_open_circuit_breaker(self) -> None:
        """Open the circuit breaker if the failure threshold has been reached."""
        if self._connection_health.consecutive_failures >= _CB_THRESHOLD:
            self._cb_open = True
            self._cb_probe_after = time.monotonic() + _CB_PROBE_INTERVAL
            _LOGGER.warning(
                "Circuit breaker opened after %d consecutive failures — "
                "next probe in %.0fs",
                self._connection_health.consecutive_failures,
                _CB_PROBE_INTERVAL,
            )

    async def _fetch_circuit_data(
        self,
        plant_id: str,
        path: str,
        ctype: str,
        circuit: dict,
    ) -> HovalCircuitData:
        """Fetch live values and programs for a single circuit.

        This is a proper coordinator method rather than a closure to avoid the
        closure-over-loop-variable hazard and to make it independently testable.

        Failed sub-fetches (live_values, programs) are recorded in
        HovalCircuitData.failed_sub_fetches so the caller can aggregate
        partial-failure counts without relying on log inspection.

        Program cache fallback:
          If programs fetch fails but a stale cache entry exists, the stale
          data is used and the cache timestamp is NOT updated — so the next
          poll will retry the fetch.  If no cache entry exists the circuit
          simply has no program data for this poll.
        """
        raw_program = circuit.get("activeProgram")
        air_quality = circuit.get("airQuality") or {}
        circuit_data = HovalCircuitData(
            circuit_type=ctype,
            path=path,
            name=circuit.get("name") or ctype,
            operation_mode=circuit.get("operationMode"),
            active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
            target_value=circuit.get("targetValue"),
            is_air_quality_guided=bool(air_quality.get("isAirQualityGuided")),
            has_error=circuit.get("hasError", False),
            circuit_status=circuit.get("circuitStatus"),
        )

        cached_prog = self._program_cache.get(path)
        need_programs = (
            cached_prog is None
            or time.time() - cached_prog[1] > self._program_cache_ttl
        )

        live_task = self.api.get_live_values(plant_id, path, ctype)
        if need_programs:
            results = await asyncio.gather(
                live_task,
                self.api.get_programs(plant_id, path),
                return_exceptions=True,
            )
        else:
            live_result = await asyncio.gather(live_task, return_exceptions=True)
            results = [live_result[0], cached_prog[0]]

        # Live values
        if not isinstance(results[0], BaseException):
            circuit_data.live_values = {v["key"]: v["value"] for v in results[0]}
            _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
        else:
            circuit_data.failed_sub_fetches.append("live_values")
            _LOGGER.debug("Live values not available for %s: %s", path, results[0])

        # Programs — with stale-cache fallback on fetch failure
        programs_data: Any = None
        raw_prog_result = results[1]
        if not isinstance(raw_prog_result, BaseException):
            programs_data = raw_prog_result
            if need_programs:
                self._program_cache[path] = (programs_data, time.time())
        elif cached_prog is not None:
            # Fetch failed but we have a previously cached value — use it and
            # do NOT update the cache timestamp so the next poll retries.
            # Still record as a sub-fetch failure so partial_failures_last_poll
            # reflects that the programs endpoint is broken, even though stale
            # data is being served (which keeps the circuit entities populated).
            programs_data = cached_prog[0]
            circuit_data.failed_sub_fetches.append("programs_stale_cache")
            _LOGGER.debug(
                "Programs fetch failed for %s — using stale cache: %s",
                path,
                raw_prog_result,
            )
        else:
            circuit_data.failed_sub_fetches.append("programs")
            _LOGGER.debug(
                "Programs not available for %s (no cache): %s", path, raw_prog_result
            )

        if programs_data is not None:
            now = dt_util.now()
            week_name, day_name, phase_value = _resolve_active_program_value(
                programs_data, now
            )
            circuit_data.active_week_name = week_name
            circuit_data.active_day_program_name = day_name
            circuit_data.program_air_volume = phase_value
            w1 = programs_data.get("week1", {})
            w2 = programs_data.get("week2", {})
            if w1.get("name"):
                circuit_data.program_names["week1"] = w1["name"]
            if w2.get("name"):
                circuit_data.program_names["week2"] = w2["name"]

        return circuit_data

    async def _fetch_all_data(self) -> HovalData:
        """Inner fetch — called inside the asyncio.timeout guard in _async_update_data."""
        data = HovalData()

        plants = await self.api.get_plants()

        # Accumulators for partial-failure tracking across all plants.
        # Initialised here so multi-plant setups report the full poll's
        # failure counts, not just the last plant processed.
        all_partial_fail_count: int = 0
        all_partial_fail_names: list[str] = []

        for plant in plants:
            plant_id = plant.get("plantExternalId")
            if not plant_id:
                _LOGGER.debug("Skipping plant with missing plantExternalId")
                continue

            plant_name = plant.get("description", plant_id)

            plant_data = HovalPlantData(
                plant_id=plant_id,
                name=plant_name,
                is_online=plant.get("isOnline", True),
            )

            if not plant_data.is_online:
                self.api.invalidate_plant_token(plant_id)
                data.plants[plant_id] = plant_data
                continue

            try:
                circuits_raw = await self.api.get_circuits(plant_id)
            except HovalApiError as err:
                _LOGGER.error(
                    "Circuits endpoint failed for plant %s: %s — entities will go "
                    "unavailable until the cloud API recovers or the integration is "
                    "updated.",
                    plant_id,
                    err,
                )
                raise

            _non_selectable_types = {CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW}

            _LOGGER.debug(
                "Fetched %d circuits (%d supported)",
                len(circuits_raw),
                sum(
                    1
                    for c in circuits_raw
                    if c.get("type") in SUPPORTED_CIRCUIT_TYPES
                    and (c.get("selectable") or c.get("type") in _non_selectable_types)
                ),
            )

            supported_circuits: list[tuple[str, str, dict]] = []
            for circuit in circuits_raw:
                ctype = circuit.get("type", "")
                if ctype not in SUPPORTED_CIRCUIT_TYPES:
                    continue
                if not circuit.get("selectable", False) and ctype not in _non_selectable_types:
                    continue
                path = circuit["path"]
                _LOGGER.debug(
                    "Circuit %s raw: %s",
                    path,
                    {k: v for k, v in circuit.items() if k != "name"},
                )
                supported_circuits.append((path, ctype, circuit))

            # Dispatch all tasks in parallel: circuits + plant-level endpoints.
            # Using the promoted _fetch_circuit_data method eliminates the
            # closure-over-loop-variable risk that existed with the old inner
            # function (previously guarded only by a default-arg trick).
            all_tasks: list[Any] = [
                self._fetch_circuit_data(plant_id, path, ctype, circ)
                for path, ctype, circ in supported_circuits
            ]
            latest_idx = len(all_tasks)
            all_tasks.append(self.api.get_latest_event(plant_id))
            events_idx = len(all_tasks)
            all_tasks.append(self.api.get_events(plant_id))
            weather_idx = len(all_tasks)
            all_tasks.append(self.api.get_weather(plant_id))

            all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

            # ---- Track partial (sub-task) failures for this plant -------- #
            partial_fail_count = 0
            partial_fail_names: list[str] = []

            # Circuit results
            for result in all_results[:latest_idx]:
                if isinstance(result, BaseException):
                    partial_fail_count += 1
                    partial_fail_names.append("circuit_fetch")
                    _LOGGER.debug("Circuit fetch failed: %s", result)
                    continue
                # Accumulate sub-fetch failures from within the circuit
                for sub in result.failed_sub_fetches:
                    partial_fail_count += 1
                    partial_fail_names.append(f"{result.path}.{sub}")
                if result.has_error:
                    plant_data.has_error = True
                plant_data.circuits[result.path] = result

            # Plant-level endpoints
            latest_result = all_results[latest_idx]
            if not isinstance(latest_result, BaseException) and latest_result:
                plant_data.latest_event = _parse_event(latest_result)
                if _is_problem_event(plant_data.latest_event):
                    plant_data.has_error = True
                _LOGGER.debug(
                    "Latest event: type=%s active=%s desc=%s",
                    plant_data.latest_event.event_type,
                    plant_data.latest_event.is_active,
                    plant_data.latest_event.description,
                )
            elif isinstance(latest_result, BaseException):
                partial_fail_count += 1
                partial_fail_names.append("latest_event")
                _LOGGER.debug("Events endpoint not available for %s", plant_id)

            events_result = all_results[events_idx]
            if not isinstance(events_result, BaseException) and events_result:
                for ev in events_result[:10]:
                    plant_data.events.append(_parse_event(ev))
                for ev in plant_data.events:
                    if _is_problem_event(ev):
                        plant_data.has_error = True
                        break
            elif isinstance(events_result, BaseException):
                partial_fail_count += 1
                partial_fail_names.append("events")
                _LOGGER.debug("Events list not available for %s", plant_id)

            weather_result = all_results[weather_idx]
            if not isinstance(weather_result, BaseException) and weather_result:
                if isinstance(weather_result, list) and weather_result:
                    w = weather_result[0]
                    plant_data.weather = HovalWeatherData(
                        weather_type=w.get("weatherType"),
                        outside_temperature=w.get("outsideTemperature"),
                        outside_temperature_min=w.get("outsideTemperatureMin"),
                    )
            elif isinstance(weather_result, BaseException):
                partial_fail_count += 1
                partial_fail_names.append("weather")
                _LOGGER.debug("Weather not available for %s", plant_id)

            # Accumulate partial-failure health across all plants.
            # total_partial_failures is updated immediately; partial_failures_last_poll
            # and partial_failure_endpoints are set after all plants are processed
            # so they always reflect the full picture rather than just the last plant.
            self._connection_health.total_partial_failures += partial_fail_count
            if partial_fail_count:
                _LOGGER.debug(
                    "Partial failures for plant %s (%d): %s",
                    plant_id,
                    partial_fail_count,
                    ", ".join(partial_fail_names),
                )
            all_partial_fail_count += partial_fail_count
            all_partial_fail_names.extend(partial_fail_names)

            data.plants[plant_id] = plant_data

        # Commit accumulated partial-failure counts after processing all plants
        self._connection_health.partial_failures_last_poll = all_partial_fail_count
        self._connection_health.partial_failure_endpoints = (
            ", ".join(all_partial_fail_names) if all_partial_fail_names else None
        )

        # Detect new circuits for dynamic entity discovery
        current_circuits = {
            f"{pid}_{path}" for pid, plant in data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        if new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        # Clear optimistic overrides only after a SUCCESSFUL fetch
        self._mode_override.clear()

        return data
