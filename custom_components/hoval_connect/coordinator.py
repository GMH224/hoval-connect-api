"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import (
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_WW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEALTH_STORAGE_KEY,
    HEALTH_STORAGE_VERSION,
    PROGRAM_CACHE_TTL,
    SUPPORTED_CIRCUIT_TYPES,
)

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

_LOGGER = logging.getLogger(__name__)

# v1 API returns different activeProgram values than v3.
# Normalize so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",  # time program active (v1 doesn't say which week)
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _resolve_active_program_value(
    programs: dict[str, Any] | None, now: datetime, active_program: str | None = None
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day program name, and air volume.

    Returns (week_name, day_program_name, current_phase_value).
    active_program is used to pick week1 vs week2; defaults to week1.

    programs must be a dict.  Non-programmable circuits (e.g. BL/boiler) may
    yield None (HTTP 204) or an empty JSON array [] from the programs endpoint
    after Hoval's May 2026 API change.  Any non-dict value is treated as
    "no programs available" and all three return values will be None.
    This guard prevents AttributeError crashes that silently dropped the BL
    circuit from plant_data.circuits.
    """
    if not isinstance(programs, dict):
        return None, None, None
    day_programs = programs.get("dayPrograms", {})
    day_configs = day_programs.get("dayConfigurations", [])
    if not day_configs:
        return None, None, None

    # Build lookup: id -> day config
    config_by_id: dict[int, dict] = {d["id"]: d for d in day_configs}

    # Determine which week is active based on the circuit's active_program field.
    # Defaulting to week1 was wrong for week2 users: they got the wrong day
    # program name, wrong active_week_name, and the wrong program_air_volume
    # (which feeds into the fan speed fallback chain in resolve_fan_speed).
    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key, {})
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


# ---------------------------------------------------------------------------
# Rolling-window sizing
# ---------------------------------------------------------------------------
# Timestamps are kept for up to _HEALTH_HISTORY_SIZE poll cycles.
# At the fastest polling interval (30 s) this covers 90 minutes — enough to
# compute accurate 1-hour rates even after a burst of rapid polls.
_HEALTH_HISTORY_SIZE = 180   # ~90 min at 30 s polling / ~3 h at 60 s
_LATENCY_HISTORY_SIZE = 60   # p95 needs enough samples to be meaningful

# Exponential-moving-average decay factor: 10 % weight to each new sample.
# α = 0.1 means the EMA takes ~22 samples to reflect a step-change by 90 %,
# making it smooth for dashboards while still responding to sustained shifts.
_EMA_ALPHA = 0.1

# How long to wait after a successful poll before flushing to HA storage.
# Using async_delay_save means rapid polls only trigger one I/O per window.
_HEALTH_SAVE_DELAY_S = 30.0


# ---------------------------------------------------------------------------
# Per-circuit reliability tracker
# ---------------------------------------------------------------------------

@dataclass
class HovalCircuitHealth:
    """Per-circuit API reliability tracker.

    Tracks whether the *live-values* fetch for a specific circuit is healthy.
    Programs fetch failures are NOT counted here — they use a stale cache and
    are far less impactful on entity availability.

    Persisted fields (total_polls, total_failures) accumulate across HA
    restarts and are saved / restored by HovalConnectionHealth's persistence
    helpers. Rolling deques are intentionally ephemeral.
    """

    # Cumulative counters — persisted across restarts
    total_polls: int = 0
    total_failures: int = 0

    # Session-only state
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        self._poll_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._failure_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)

    # ------------------------------------------------------------------
    # Recording methods — called from _fetch_circuit
    # ------------------------------------------------------------------

    def record_success(self, ts: datetime) -> None:
        """Record a successful live-values fetch."""
        self.total_polls += 1
        self.consecutive_failures = 0
        self.last_success = ts
        self._poll_times.append(ts)

    def record_failure(self, ts: datetime, error: str) -> None:
        """Record a failed live-values fetch."""
        self.total_polls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_failure = ts
        self.last_error = error[:200]
        self._poll_times.append(ts)
        self._failure_times.append(ts)

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def failure_rate_1h(self) -> float | None:
        """Percentage of live-values polls that failed in the last hour."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=3600)
        polls = sum(1 for t in self._poll_times if t >= cutoff)
        if polls == 0:
            return None
        failures = sum(1 for t in self._failure_times if t >= cutoff)
        return round(failures / polls * 100, 1)

    @property
    def availability_1h(self) -> float | None:
        """1-hour availability (100 % − failure rate)."""
        rate = self.failure_rate_1h
        return None if rate is None else round(100.0 - rate, 1)

    # ------------------------------------------------------------------
    # Serialisation helpers — called by HovalConnectionHealth
    # ------------------------------------------------------------------

    def to_store_dict(self) -> dict:
        return {
            "total_polls": self.total_polls,
            "total_failures": self.total_failures,
        }

    def restore_from_store(self, data: dict) -> None:
        try:
            self.total_polls = int(data.get("total_polls", 0))
        except (TypeError, ValueError):
            pass
        try:
            self.total_failures = int(data.get("total_failures", 0))
        except (TypeError, ValueError):
            pass

    def as_diagnostic_dict(self) -> dict:
        return {
            "total_polls": self.total_polls,
            "total_failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "failure_rate_1h_pct": self.failure_rate_1h,
            "availability_1h_pct": self.availability_1h,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Plant-level (coordinator) health tracker
# ---------------------------------------------------------------------------

# Canonical error-type strings used in last_error_type and error_counts.
# Using a frozen set of literals (not an Enum) keeps the code simple and the
# diagnostics JSON human-readable without importing enum everywhere.
ERROR_TYPE_TIMEOUT = "timeout"
ERROR_TYPE_AUTH = "auth"
ERROR_TYPE_CIRCUIT_LIST = "circuit_list"
ERROR_TYPE_API = "api"
ERROR_TYPE_UNKNOWN = "unknown"

_ALL_ERROR_TYPES = (
    ERROR_TYPE_TIMEOUT,
    ERROR_TYPE_AUTH,
    ERROR_TYPE_CIRCUIT_LIST,
    ERROR_TYPE_API,
    ERROR_TYPE_UNKNOWN,
)


@dataclass
class HovalConnectionHealth:
    """Tracks API connection health metrics across coordinator polls.

    Scalar dataclass fields are persisted across HA restarts via HA's Store
    helper; rolling deques are intentionally ephemeral (they reset on restart,
    which is correct — 1-hour rates should reflect the current session).

    Use as_diagnostic_dict() to get a complete, JSON-safe snapshot.
    Use to_store_dict() / restore_from_store() for persistence.
    """

    # --- Timestamps (session-only) ---
    last_success: datetime | None = None
    last_error_time: datetime | None = None

    # --- Error details (session-only) ---
    last_error_msg: str | None = None
    # One of ERROR_TYPE_* constants for precise categorisation.
    # "circuit_list" distinguishes a circuits-endpoint failure from a generic
    # API error, because it's the most impactful single-endpoint failure.
    last_error_type: str | None = None

    # --- Cumulative counters (persisted across restarts) ---
    consecutive_failures: int = 0
    total_polls: int = 0
    total_failures: int = 0
    auth_failures: int = 0

    # --- Per-error-type counts (persisted) ---
    # Keys are ERROR_TYPE_* constants; values are cumulative counts.
    error_counts: dict[str, int] = field(default_factory=dict)

    # --- EMA latency (persisted — the EMA carries meaningful signal across restarts) ---
    # Initialised to None until the first successful poll.
    ema_latency_ms: float | None = None

    # --- Last successful poll latency (session-only) ---
    poll_latency_ms: float | None = None

    def __post_init__(self) -> None:
        """Initialise rolling-history containers and per-circuit tracker.

        These are plain instance attributes (not dataclass fields) so they
        are invisible to asdict() and don't interfere with serialisation.
        """
        self._poll_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._failure_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._auth_failure_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._latency_samples: deque[float] = deque(maxlen=_LATENCY_HISTORY_SIZE)
        # Per-circuit health — keyed by circuit path string
        self._circuit_health: dict[str, HovalCircuitHealth] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def _count_in_window(self, bucket: deque[datetime], window_s: int) -> int:
        """Count timestamps that fall within the last window_s seconds."""
        cutoff = self._utcnow() - timedelta(seconds=window_s)
        return sum(1 for t in bucket if t >= cutoff)

    # ------------------------------------------------------------------
    # Circuit health accessors
    # ------------------------------------------------------------------

    def get_circuit_health(self, path: str) -> HovalCircuitHealth:
        """Return (creating if necessary) the HovalCircuitHealth for a path."""
        if path not in self._circuit_health:
            self._circuit_health[path] = HovalCircuitHealth()
        return self._circuit_health[path]

    # ------------------------------------------------------------------
    # EMA update
    # ------------------------------------------------------------------

    def update_ema(self, ms: float) -> None:
        """Update the exponential moving average latency with a new sample.

        α = 0.1 gives an ~22-sample half-life, which balances responsiveness
        to sustained degradation with resistance to one-off spikes.
        """
        if self.ema_latency_ms is None:
            self.ema_latency_ms = round(ms, 1)
        else:
            self.ema_latency_ms = round(
                _EMA_ALPHA * ms + (1.0 - _EMA_ALPHA) * self.ema_latency_ms, 1
            )

    # ------------------------------------------------------------------
    # Error recording helper
    # ------------------------------------------------------------------

    def _record_error(
        self,
        ts: datetime,
        error_type: str,
        msg: str,
        *,
        is_auth: bool = False,
    ) -> None:
        """Centralised error recording — updates all relevant counters at once."""
        self._failure_times.append(ts)
        if is_auth:
            self._auth_failure_times.append(ts)
            self.auth_failures += 1
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error_time = ts
        self.last_error_type = error_type
        self.last_error_msg = msg[:200]
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1

    # ------------------------------------------------------------------
    # Computed properties — 1-hour rolling window
    # ------------------------------------------------------------------

    @property
    def failure_rate_1h(self) -> float | None:
        """Percentage of coordinator polls that failed in the last hour.

        Returns None until at least one poll is recorded in the window so
        that sensors show unavailable rather than a misleading 0 %.
        """
        polls = self._count_in_window(self._poll_times, 3600)
        if polls == 0:
            return None
        failures = self._count_in_window(self._failure_times, 3600)
        return round(failures / polls * 100, 1)

    @property
    def auth_failure_rate_1h(self) -> float | None:
        """Auth failures as a percentage of all polls in the last hour."""
        polls = self._count_in_window(self._poll_times, 3600)
        if polls == 0:
            return None
        auth_f = self._count_in_window(self._auth_failure_times, 3600)
        return round(auth_f / polls * 100, 1)

    @property
    def availability_1h(self) -> float | None:
        """API availability over the last hour (100 % − failure_rate_1h)."""
        rate = self.failure_rate_1h
        return None if rate is None else round(100.0 - rate, 1)

    # ------------------------------------------------------------------
    # Computed properties — latency statistics
    # ------------------------------------------------------------------

    @property
    def avg_latency_ms(self) -> float | None:
        """Arithmetic mean latency across the last _LATENCY_HISTORY_SIZE polls."""
        if not self._latency_samples:
            return None
        return round(sum(self._latency_samples) / len(self._latency_samples), 1)

    @property
    def p95_latency_ms(self) -> float | None:
        """95th-percentile latency across the last _LATENCY_HISTORY_SIZE polls.

        More sensitive to tail latency than the mean; a rising p95 reliably
        predicts imminent coordinator timeouts before the mean shifts.
        """
        samples = sorted(self._latency_samples)
        if not samples:
            return None
        idx = max(0, int(len(samples) * 0.95) - 1)
        return round(samples[idx], 1)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_store_dict(self) -> dict:
        """Return a JSON-safe dict of the fields that should survive restarts.

        Rolling deques are intentionally excluded — 1-hour rates should
        reflect the current HA session, not a mix of sessions.
        """
        return {
            "total_polls": self.total_polls,
            "total_failures": self.total_failures,
            "auth_failures": self.auth_failures,
            "error_counts": dict(self.error_counts),
            "ema_latency_ms": self.ema_latency_ms,
            "circuits": {
                path: ch.to_store_dict()
                for path, ch in self._circuit_health.items()
            },
        }

    def restore_from_store(self, data: dict) -> None:
        """Restore persisted counters from a Store snapshot.

        Only counters that are safe to accumulate across restarts are
        restored. Session-only fields (consecutive_failures, last_success,
        last_error_*, rolling deques) are intentionally left at their
        zero/None defaults.

        All integer conversions are wrapped in try/except so a corrupted
        storage file does not crash the integration on startup.
        """
        for attr in ("total_polls", "total_failures", "auth_failures"):
            try:
                setattr(self, attr, int(data.get(attr, 0)))
            except (TypeError, ValueError):
                pass
        self.error_counts = {
            k: int(v)
            for k, v in data.get("error_counts", {}).items()
            if k in _ALL_ERROR_TYPES and isinstance(v, (int, float))
        }
        ema = data.get("ema_latency_ms")
        if isinstance(ema, (int, float)) and ema > 0:
            self.ema_latency_ms = float(ema)
        for path, ch_data in data.get("circuits", {}).items():
            if isinstance(ch_data, dict):
                ch = self.get_circuit_health(path)
                ch.restore_from_store(ch_data)

    # ------------------------------------------------------------------
    # Diagnostics serialisation
    # ------------------------------------------------------------------

    def as_diagnostic_dict(self) -> dict:
        """Return a complete, JSON-safe snapshot for the HA diagnostics export.

        Structured into logical groups so the diagnostics page is readable
        without any extra formatting. All timestamps are ISO-8601 UTC strings.
        """
        polls_1h = self._count_in_window(self._poll_times, 3600)
        failures_1h = self._count_in_window(self._failure_times, 3600)
        auth_failures_1h = self._count_in_window(self._auth_failure_times, 3600)

        return {
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": {
                "time": self.last_error_time.isoformat() if self.last_error_time else None,
                "type": self.last_error_type,
                "message": self.last_error_msg,
            },
            "counters_since_startup": {
                "total_polls": self.total_polls,
                "total_failures": self.total_failures,
                "auth_failures": self.auth_failures,
                "consecutive_failures": self.consecutive_failures,
                "overall_failure_rate_pct": (
                    round(self.total_failures / self.total_polls * 100, 1)
                    if self.total_polls
                    else None
                ),
                "error_counts": dict(self.error_counts),
            },
            "rolling_1h_window": {
                "polls": polls_1h,
                "failures": failures_1h,
                "auth_failures": auth_failures_1h,
                "failure_rate_pct": self.failure_rate_1h,
                "auth_failure_rate_pct": self.auth_failure_rate_1h,
                "availability_pct": self.availability_1h,
            },
            "latency_ms": {
                "last": self.poll_latency_ms,
                "avg": self.avg_latency_ms,
                "p95": self.p95_latency_ms,
                "ema": self.ema_latency_ms,
                "sample_count": len(self._latency_samples),
            },
            "circuits": {
                path: ch.as_diagnostic_dict()
                for path, ch in sorted(self._circuit_health.items())
            },
        }


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
    # Per-circuit reliability metrics populated each poll from HovalCircuitHealth.
    # Exposed as diagnostic sensors so users can identify a flaky individual circuit
    # without inspecting the full diagnostics export.
    circuit_failure_rate_1h: float | None = None
    circuit_availability_1h: float | None = None
    circuit_consecutive_failures: int = 0


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


class _CircuitListError(Exception):
    """Raised when the circuits-list endpoint fails.

    Wraps HovalApiError so _async_update_data can distinguish this specific
    failure and record it under ERROR_TYPE_CIRCUIT_LIST rather than the
    generic ERROR_TYPE_API bucket.
    """


class HovalDataCoordinator(DataUpdateCoordinator[HovalData]):
    """Coordinator to fetch data from Hoval Connect API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: HovalConnectApi,
        health_store: Store,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self._health_store = health_store
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

    @property
    def connection_health(self) -> HovalConnectionHealth:
        """Return the current API connection health snapshot."""
        return self._connection_health

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
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside control_lock
        so concurrent control actions don't race each other.

        The coordinator refresh is deliberately scheduled as a fire-and-forget
        background task OUTSIDE the lock for two reasons:
        - The lock is released quickly (only held during the API round-trip),
          so a second control action can proceed without waiting for the full
          data refresh.
        - The calling entity method returns to HA promptly, keeping the UI
          responsive even when the Hoval cloud is slow.

        A 2 s settle delay runs inside the background task (not here) so the
        API has time to commit the change before we fetch fresh state, without
        blocking the caller.

        If the background refresh fails (transient timeout), it is silently
        discarded — the coordinator will retry on its normal poll schedule and
        entities remain at their optimistic state until then.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        # Schedule refresh as background task — do not await it here.
        # This keeps the caller (entity action method) fast and prevents the
        # lock from being starved during a slow/timeout refresh.
        async def _do_refresh() -> None:
            # Brief pause so the API has time to commit the change before we
            # fetch fresh state.  Moved inside the task so the entity action
            # method returns to HA immediately instead of blocking for 2 s.
            await asyncio.sleep(2)
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

        Health counters are updated before exceptions are re-raised so that
        connection-health sensors always reflect the latest failure state even
        when HA marks the coordinator as unavailable.

        Error types are now finer-grained:
        - ERROR_TYPE_TIMEOUT   — the 90 s overall coordinator timeout fired
        - ERROR_TYPE_AUTH      — HovalAuthError (credentials problem)
        - ERROR_TYPE_CIRCUIT_LIST — get_circuits() specifically failed; the most
                                    impactful single-endpoint failure
        - ERROR_TYPE_API       — any other HovalApiError
        - ERROR_TYPE_UNKNOWN   — unexpected exception (bug or API schema change)
        """
        _start = time.monotonic()
        self._connection_health.total_polls += 1
        _poll_ts = dt_util.utcnow()
        self._connection_health._poll_times.append(_poll_ts)

        try:
            async with asyncio.timeout(90):
                result = await self._fetch_all_data()

            elapsed_ms = round((time.monotonic() - _start) * 1000, 0)
            self._connection_health.last_success = dt_util.utcnow()
            self._connection_health.consecutive_failures = 0
            self._connection_health.poll_latency_ms = elapsed_ms
            self._connection_health._latency_samples.append(elapsed_ms)
            self._connection_health.update_ema(elapsed_ms)
            _LOGGER.debug(
                "Poll succeeded in %.0f ms (ema=%.0f ms, total_polls=%d)",
                elapsed_ms,
                self._connection_health.ema_latency_ms or 0,
                self._connection_health.total_polls,
            )
            # Persist updated counters with a debounced delay to avoid I/O on
            # every poll cycle.  If HA is shut down before the delay fires,
            # async_unload_entry does an immediate save.
            self._health_store.async_delay_save(
                self._connection_health.to_store_dict, _HEALTH_SAVE_DELAY_S
            )
            return result

        except TimeoutError as err:
            _ts = dt_util.utcnow()
            self._connection_health._record_error(
                _ts, ERROR_TYPE_TIMEOUT, "Poll timeout after 90 s"
            )
            _LOGGER.warning(
                "Poll timed out (consecutive=%d, total_failures=%d)",
                self._connection_health.consecutive_failures,
                self._connection_health.total_failures,
            )
            raise UpdateFailed(
                "Hoval API refresh timed out after 90 s — cloud may be unresponsive. "
                "HA will retry automatically."
            ) from err

        except HovalAuthError as err:
            _ts = dt_util.utcnow()
            self._connection_health._record_error(
                _ts, ERROR_TYPE_AUTH, f"Auth error: {err}", is_auth=True
            )
            _LOGGER.warning(
                "Auth failure (consecutive=%d, auth_failures=%d): %s",
                self._connection_health.consecutive_failures,
                self._connection_health.auth_failures,
                err,
            )
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err

        except _CircuitListError as err:
            # Raised by _fetch_all_data when get_circuits() specifically fails.
            # Categorised separately so error_counts distinguishes this from a
            # generic API failure on a less-critical endpoint.
            _ts = dt_util.utcnow()
            self._connection_health._record_error(
                _ts, ERROR_TYPE_CIRCUIT_LIST, str(err)[:200]
            )
            _LOGGER.warning(
                "Circuit list fetch failed (consecutive=%d): %s",
                self._connection_health.consecutive_failures,
                err,
            )
            raise UpdateFailed(f"Circuit list unavailable: {err}") from err

        except HovalApiError as err:
            _ts = dt_util.utcnow()
            self._connection_health._record_error(
                _ts, ERROR_TYPE_API, str(err)[:200]
            )
            _LOGGER.warning(
                "API error during poll (consecutive=%d, total_failures=%d): %s",
                self._connection_health.consecutive_failures,
                self._connection_health.total_failures,
                err,
            )
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

        except Exception as err:  # noqa: BLE001
            _ts = dt_util.utcnow()
            self._connection_health._record_error(
                _ts, ERROR_TYPE_UNKNOWN, f"{type(err).__name__}: {err}"
            )
            raise

    async def async_save_health(self) -> None:
        """Force an immediate health snapshot save to HA storage.

        Called from async_unload_entry so counters are not lost on a clean
        shutdown even if the debounced save hasn't fired yet.
        """
        await self._health_store.async_save(self._connection_health.to_store_dict())

    async def _fetch_all_data(self) -> HovalData:
        """Inner fetch — called inside the asyncio.timeout guard.

        HovalAuthError and HovalApiError propagate up to _async_update_data
        which converts them to ConfigEntryAuthFailed / UpdateFailed respectively.

        _mode_override is cleared only at the END of a successful fetch so
        that optimistic entity state survives failed/timed-out refreshes.
        """
        data = HovalData()

        plants = await self.api.get_plants()

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

            # Skip all API calls when plant is offline
            if not plant_data.is_online:
                # Invalidate cached PAT so we get a fresh token when back
                self.api.invalidate_plant_token(plant_id)
                data.plants[plant_id] = plant_data
                continue

            # Fetch circuits. A persistent failure here is the most common
            # symptom of an upstream API change (the v1 endpoint removal in
            # April 2026 was masked for days because we used to swallow this
            # error). Log loudly and let DataUpdateCoordinator surface the
            # failure to the user as `unavailable` entities.
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
                raise _CircuitListError(str(err)) from err

            # BL/WW circuits have selectable=False but still provide live values
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

            # Build list of supported circuits
            supported_circuits: list[tuple[str, str, dict]] = []
            for circuit in circuits_raw:
                ctype = circuit.get("type", "")
                if ctype not in SUPPORTED_CIRCUIT_TYPES:
                    continue
                if not circuit.get("selectable", False) and ctype not in _non_selectable_types:
                    continue
                path = circuit.get("path")
                if not path:
                    _LOGGER.warning(
                        "Skipping circuit with missing 'path' field for plant %s: %s",
                        plant_id,
                        {k: v for k, v in circuit.items() if k != "name"},
                    )
                    continue
                _LOGGER.debug(
                    "Circuit %s raw: %s",
                    path,
                    {k: v for k, v in circuit.items() if k != "name"},
                )
                supported_circuits.append((path, ctype, circuit))

            # Fetch live values + programs for all circuits in parallel
            async def _fetch_circuit(
                path: str,
                ctype: str,
                circuit: dict,
                _plant_id: str = plant_id,
            ) -> HovalCircuitData:
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

                # Check program cache
                cached_prog = self._program_cache.get(path)
                need_programs = (
                    cached_prog is None
                    or time.time() - cached_prog[1] > self._program_cache_ttl
                )

                # Fetch live values (always) + programs (only if cache expired)
                live_task = self.api.get_live_values(_plant_id, path, ctype)
                if need_programs:
                    prog_task = self.api.get_programs(_plant_id, path)
                    results = await asyncio.gather(
                        live_task,
                        prog_task,
                        return_exceptions=True,
                    )
                else:
                    live_result = await asyncio.gather(
                        live_task,
                        return_exceptions=True,
                    )
                    results = [live_result[0], cached_prog[0]]

                if not isinstance(results[0], BaseException):
                    lv_raw = results[0]
                    # Normalise paginated wrapper → plain list (Hoval May 2026 change).
                    if isinstance(lv_raw, dict):
                        lv_raw = lv_raw.get("content", [])
                    # Defensive: api.get_live_values() always returns a list, but
                    # guard against any future regression that returns None / other.
                    if not isinstance(lv_raw, list):
                        _LOGGER.warning(
                            "Live-values for %s returned unexpected type %s; treating as empty",
                            path, type(lv_raw).__name__,
                        )
                        lv_raw = []
                    circuit_data.live_values = {
                        v["key"]: v["value"]
                        for v in lv_raw
                        if isinstance(v, dict) and "key" in v and "value" in v
                    }
                    _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
                    # Initialise ch before recording so the variable is always
                    # bound even if an unlikely exception occurs in the block above.
                    ch = self._connection_health.get_circuit_health(path)
                    ch.record_success(dt_util.utcnow())
                else:
                    _LOGGER.debug("Live values not available for %s: %s", path, results[0])
                    ch = self._connection_health.get_circuit_health(path)
                    ch.record_failure(dt_util.utcnow(), str(results[0]))

                # Propagate per-circuit reliability metrics into the data object so
                # circuit-level sensors can read them without touching the coordinator.
                circuit_data.circuit_failure_rate_1h = ch.failure_rate_1h
                circuit_data.circuit_availability_1h = ch.availability_1h
                circuit_data.circuit_consecutive_failures = ch.consecutive_failures

                programs = results[1]
                # Guard: only process programs when the API returned a proper dict.
                # Non-programmable circuits (BL, operationMode=None) may return
                # HTTP 204 → None or HTTP 200 with body [] after Hoval's May 2026
                # change.  [] is not None and not an exception, so a weaker guard
                # would enter this block and crash on [].get("dayPrograms", {}).
                # Non-dict values are never cached.  See CLAUDE.md for full history.
                if isinstance(programs, dict):
                    if need_programs:
                        self._program_cache[path] = (programs, time.time())
                    now = dt_util.now()
                    week_name, day_name, phase_value = _resolve_active_program_value(
                        programs, now, circuit_data.active_program
                    )
                    circuit_data.active_week_name = week_name
                    circuit_data.active_day_program_name = day_name
                    circuit_data.program_air_volume = phase_value
                    # Extract user-defined program names
                    w1 = programs.get("week1", {})
                    w2 = programs.get("week2", {})
                    if w1.get("name"):
                        circuit_data.program_names["week1"] = w1["name"]
                    if w2.get("name"):
                        circuit_data.program_names["week2"] = w2["name"]
                elif isinstance(programs, BaseException):
                    _LOGGER.debug("Programs not available for %s: %s", path, programs)
                else:
                    # None (HTTP 204 / empty body) or unexpected type such as []
                    # (empty JSON array).  Log the type so future API surprises are
                    # visible in the HA log at debug level.
                    _LOGGER.debug(
                        "Programs endpoint for %s returned %s (type=%s); "
                        "circuit has no programs — skipping program processing",
                        path,
                        repr(programs),
                        type(programs).__name__,
                    )

                return circuit_data

            # Run circuits, events, and weather all in parallel
            all_tasks = [
                _fetch_circuit(path, ctype, circ) for path, ctype, circ in supported_circuits
            ]
            # Append plant-level tasks (events + weather)
            latest_idx = len(all_tasks)
            all_tasks.append(self.api.get_latest_event(plant_id))
            events_idx = len(all_tasks)
            all_tasks.append(self.api.get_events(plant_id))
            weather_idx = len(all_tasks)
            all_tasks.append(self.api.get_weather(plant_id))

            all_results = await asyncio.gather(
                *all_tasks,
                return_exceptions=True,
            )

            # Process circuit results
            for result in all_results[:latest_idx]:
                if isinstance(result, BaseException):
                    _LOGGER.debug("Circuit fetch failed: %s", result)
                    continue
                if result.has_error:
                    plant_data.has_error = True
                plant_data.circuits[result.path] = result

            # Process latest event
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
                _LOGGER.debug("Events endpoint not available for %s", plant_id)

            # Process events list
            events_result = all_results[events_idx]
            if not isinstance(events_result, BaseException) and events_result:
                for ev in events_result[:10]:
                    plant_data.events.append(_parse_event(ev))
                for ev in plant_data.events:
                    if _is_problem_event(ev):
                        plant_data.has_error = True
                        break
            elif isinstance(events_result, BaseException):
                _LOGGER.debug("Events list not available for %s", plant_id)

            # Process weather forecast
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
                _LOGGER.debug("Weather not available for %s", plant_id)

            data.plants[plant_id] = plant_data

        # Detect new circuits for dynamic entity discovery.
        # Fire on any newly seen circuit, including the first one. Skipping the
        # initial set (when `_known_circuits` was still empty) used to leave
        # circuits stranded if the very first refresh came back without them
        # — async_setup_entry's _add_new() ran against an empty circuits dict
        # and the dispatcher then suppressed the catch-up signal. Each platform
        # already deduplicates via its `known` set, so firing on the first
        # discovery is a no-op when entities are already present.
        current_circuits = {
            f"{pid}_{path}" for pid, plant in data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        if new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        # Clear optimistic overrides only after a SUCCESSFUL fetch so that if
        # the refresh fails (API timeout, transient error), entities continue to
        # show their optimistic state rather than snapping back to stale data
        # mid-cycle.  Fresh coordinator data takes over on the next good refresh.
        self._mode_override.clear()

        return data
