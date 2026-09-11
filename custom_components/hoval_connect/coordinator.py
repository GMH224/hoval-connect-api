"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
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
    CIRCUIT_SETTINGS_CACHE_TTL,
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_WW,
    DOMAIN,
    HEALTH_CHECK_INTERVAL,
    PROGRAM_CACHE_TTL,
    SUPPORTED_CIRCUIT_TYPES,
    SUPPORTS_PROGRAMS,
    SUPPORTS_WEATHER_IMPACT,
    clamp_weather_impact_outside_temperature,
    clamp_weather_impact_solar_radiation,
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


async def resolve_resume_program(
    api: HovalConnectApi, plant_id: str, circuit_path: str, cached_circuit: HovalCircuitData | None
) -> str:
    """Resolve which weekly program a "resume the schedule" action should activate.

    Independent audit finding (2026-09, HVC-ICS-008 + "more" report finding
    #8): api.reset_circuit() defaults to program="week1", and every
    "resume" caller — fan.py's "Resume time program" turn-on option,
    climate.py's AUTO mode, water_heater.py's heat_pump reset — called it
    without supplying the circuit's actual currently-active weekly program.
    A circuit running on week2 would silently get switched to week1 by
    "resume", which is a semantic mismatch: resuming the existing schedule
    should not itself change which schedule is active.

    The first fix for this (HVC-ICS-008) read `active_program` from the
    coordinator's last-known snapshot — correct in spirit, but that
    snapshot is only refreshed at startup or after some write, never on
    the routine 30-minute health check. If the schedule was changed
    externally (the Hoval app, another client) since the last refresh, the
    cached value could itself be stale and "resume" could reactivate the
    WRONG schedule with just as much confidence as before.

    Fixed properly: this is a write-triggered action, so — consistent with
    this integration's actual design principle (no data fetched on a
    schedule; writes may fetch what they specifically need) — it now does
    one fresh, targeted GET /circuits call immediately before resolving,
    rather than trusting a snapshot that could be stale by an unbounded
    amount. Falls back to the cached value (and then to the "week1"
    default) only if that fresh fetch itself fails, so a transient network
    problem degrades to the old (still-correct-most-of-the-time) behavior
    rather than blocking the resume action entirely.
    """
    fresh_program: str | None = None
    try:
        circuits_raw = await api.get_circuits(plant_id)
    except HovalApiError:
        circuits_raw = None
    if isinstance(circuits_raw, list):
        for raw_circuit in circuits_raw:
            if isinstance(raw_circuit, dict) and raw_circuit.get("path") == circuit_path:
                raw_program = raw_circuit.get("activeProgram")
                fresh_program = _V1_PROGRAM_MAP.get(raw_program, raw_program)
                break

    if fresh_program == "week2":
        return "week2"
    if fresh_program is not None:
        # A genuine fresh read that isn't week2 (week1 already, constant,
        # ecoMode, standby, ...) is authoritative — don't second-guess it
        # against a possibly-older cached value.
        return "week1"

    # Fresh fetch failed, or didn't find this circuit — fall back to the
    # last-known cached value rather than blocking the resume action.
    if cached_circuit is not None and cached_circuit.active_program == "week2":
        return "week2"
    return "week1"


def _resolve_active_program_value(
    programs: dict[str, Any] | None, now: datetime, active_program: str | None = None
) -> tuple[str | None, str | None, float | None]:
    """DEPRECATED — retained only as a historical reference, unused since v1.0.0.

    Used to resolve the currently active week/day-program name and phase
    value for informational sensors (active_week_name, active_day_program_name,
    program_air_volume on HovalCircuitData). Those sensors were pure telemetry
    with no write dependency and were removed in v1.0.0 along with the rest of
    sensor.py, once the user's separate CAN-bus HACS integration became the
    source of truth for that kind of display-only data (see
    docs/audit-v1.0.0.md). Nothing calls this function anymore; kept
    temporarily in case a future release needs to resurrect one of those
    sensors, and deletable outright once nobody has.
    """
    if not isinstance(programs, dict):
        return None, None, None
    day_programs = programs.get("dayPrograms")
    if not isinstance(day_programs, dict):
        return None, None, None
    day_configs = day_programs.get("dayConfigurations")
    if not isinstance(day_configs, list) or not day_configs:
        return None, None, None

    # Build lookup: id -> day config. Entries that are not dicts or lack an
    # "id" are skipped instead of raising — audit finding F1 (v0.21.1): a
    # KeyError here used to propagate out of _fetch_circuit and silently drop
    # the whole circuit (including its already-fetched live values).
    config_by_id: dict[Any, dict] = {
        d["id"]: d for d in day_configs if isinstance(d, dict) and "id" in d
    }

    # Determine which week is active based on the circuit's active_program field.
    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key)
    if not isinstance(week, dict):
        # Week entry missing or wrong shape — no week/day info resolvable.
        return None, None, None
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds")
    if not isinstance(day_program_ids, list):
        day_program_ids = []

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")

    # Find active phase based on current time. Malformed phases (non-dict,
    # missing/non-dict start or end, non-numeric times) are skipped, not fatal.
    current_minutes = now.hour * 60 + now.minute
    phases = day_config.get("phases")
    if not isinstance(phases, list):
        phases = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        start = phase.get("start")
        end = phase.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        try:
            start_min = int(start["hours"]) * 60 + int(start["minutes"])
            end_min = int(end["hours"]) * 60 + int(end["minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        if start_min <= current_minutes < end_min:
            return week_name, day_name, phase.get("value")

    return week_name, day_name, None


# ---------------------------------------------------------------------------
# Rolling-window sizing
# ---------------------------------------------------------------------------
# Timestamps are kept for up to _HEALTH_HISTORY_SIZE update cycles. v1.0.0
# changed what "update cycle" means: previously a poll every 30-60s (this
# constant's original sizing target); now a lightweight health check every
# HEALTH_CHECK_INTERVAL (30 min) plus occasional full refreshes around
# startup/writes. 180 is oversized for that cadence (it covers days, not
# minutes) but that's harmless — it only bounds memory, and the 1-hour
# rolling-window metrics below now simply reflect however few checks
# actually happened in the last hour (typically 1-2), which is an accepted,
# inherent consequence of checking less often, not a bug.
_HEALTH_HISTORY_SIZE = 180
# Circuit types that are not user-selectable but still expose live values.
_NON_SELECTABLE_TYPES = frozenset({CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW})
_LATENCY_HISTORY_SIZE = 60  # p95 needs enough samples to be meaningful


def _coerce_finite_number(value: Any) -> float | None:
    """Coerce an API numeric field to a finite float, or None if it isn't one.

    Independent audit finding (2026-09, fourth round, HVC-008): `targetValue`
    and `actualValue` used to be copied straight from the API response with
    no validation at all. Schema drift or a malformed response (a string,
    NaN, +-infinity — Python's json module parses `NaN`/`Infinity` from a
    response body by default, this isn't a hypothetical) could then reach
    entity properties that assume a clean number, e.g. fan.py's
    `int(float(val))` — `int(float("nan"))` raises `ValueError`, taking
    that entity's whole percentage property down with it.

    Accepts int/float (rejecting non-finite values) and numeric strings
    (a defensive nicety, not because the API is known to send numbers as
    strings); anything else — the wrong type, or a non-finite value —
    becomes None, which every consumer already treats as "no data" rather
    than a value to render.
    """
    if isinstance(value, bool):
        return None  # bool is a subclass of int; explicitly excluded
    if isinstance(value, (int, float)):
        return float(value) if isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if isfinite(parsed) else None
    return None


def _is_circuit_selectable(circuit: dict) -> bool:
    """Return whether a circuit is selectable, per the v3 contract.

    Independent audit finding (2026-09, "more" report, finding #1):
    docs/openapi-v3.json's CircuitV3DTO declares `isSelectable` as a
    REQUIRED field and `selectable` as merely optional (verified directly
    against the schema's own `required` list, not assumed) — yet this
    coordinator filtered on `selectable` alone. A valid v3 response that
    omits the optional legacy field (while still including the guaranteed
    `isSelectable`) would silently make an otherwise-selectable HV/HK
    circuit disappear from plant_data.circuits entirely, with no error.

    Prefers `isSelectable` when present; falls back to the legacy
    `selectable` field only if `isSelectable` is absent, so a response that
    only ever sends the older field (as every response captured so far
    has) keeps working exactly as before.
    """
    is_selectable = circuit.get("isSelectable")
    if is_selectable is None:
        is_selectable = circuit.get("selectable", False)
    return bool(is_selectable)


# Exponential-moving-average decay factor: 10 % weight to each new sample.
# α = 0.1 means the EMA takes ~22 samples to reflect a step-change by 90 %,
# making it smooth for dashboards while still responding to sustained shifts.
_EMA_ALPHA = 0.1

# How long to wait after a successful poll before flushing to HA storage.
# Using async_delay_save means rapid polls only trigger one I/O per window.
_HEALTH_SAVE_DELAY_S = 30.0

# Maximum lifetime of an optimistic mode override (seconds).
# Overrides are normally cleared at the end of the next successful poll, but if
# polls keep failing an override could otherwise persist indefinitely and show
# a state that was never confirmed by the device. This TTL bounds that window so
# a stale optimistic value cannot mask reality forever.
_MODE_OVERRIDE_TTL_S = 120.0


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

    # --- v1.0.0: last successful contact of ANY kind (persisted) ---
    # Updated by BOTH a successful scheduled health check and a successful
    # write — whichever happened more recently. Drives the "Cloud API
    # problem" diagnostic binary sensor (see binary_sensor.py): that sensor
    # turns on once this is more than CLOUD_API_PROBLEM_THRESHOLD stale.
    last_successful_contact_at: datetime | None = None

    def __post_init__(self) -> None:
        """Initialise rolling-history containers.

        These are plain instance attributes (not dataclass fields) so they
        are invisible to asdict() and don't interfere with serialisation.
        """
        self._poll_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._failure_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._auth_failure_times: deque[datetime] = deque(maxlen=_HEALTH_HISTORY_SIZE)
        self._latency_samples: deque[float] = deque(maxlen=_LATENCY_HISTORY_SIZE)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    def _count_in_window(self, bucket: deque[datetime], window_s: int) -> int:
        """Count timestamps that fall within the last window_s seconds."""
        cutoff = self._utcnow() - timedelta(seconds=window_s)
        return sum(1 for t in bucket if t >= cutoff)

    # ------------------------------------------------------------------
    # Contact tracking (v1.0.0)
    # ------------------------------------------------------------------

    def record_successful_contact(self, ts: datetime) -> None:
        """Record that the cloud API responded successfully, right now.

        Called from BOTH a successful health check (via record_poll_success)
        and immediately after a successful write (async_control_and_refresh /
        async_set_weather_impact) — whichever happens is equally good
        evidence the API is reachable.
        """
        self.last_successful_contact_at = ts

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
    # Poll recording — the coordinator's ONLY write interface (audit F9)
    # ------------------------------------------------------------------
    # These three methods are the complete public recording API. The
    # coordinator must not touch _poll_times/_failure_times/_latency_samples
    # directly; keeping mutation behind named methods makes the counter
    # semantics auditable in one place.

    def record_poll_attempt(self, ts: datetime) -> None:
        """Record the start of a coordinator poll cycle (outcome not yet known)."""
        self.total_polls += 1
        self._poll_times.append(ts)

    def record_poll_success(self, ts: datetime, latency_ms: float) -> None:
        """Record a successful poll: reset failure streak, update latency stats."""
        self.last_success = ts
        self.consecutive_failures = 0
        self.poll_latency_ms = latency_ms
        self._latency_samples.append(latency_ms)
        self.update_ema(latency_ms)
        self.record_successful_contact(ts)

    def record_error(
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
            "last_successful_contact_at": (
                self.last_successful_contact_at.isoformat()
                if self.last_successful_contact_at
                else None
            ),
        }

    def restore_from_store(self, data: dict) -> None:
        """Restore persisted counters from a Store snapshot.

        Only counters that are safe to accumulate across restarts are
        restored. Session-only fields (consecutive_failures, last_success,
        last_error_*, rolling deques) are intentionally left at their
        zero/None defaults.

        All conversions are defensive so a corrupted storage file does not
        crash the integration on startup — treating persisted data as
        untrusted input throughout (independent audit finding, 2026-09,
        fourth round, HVC-012, plus the broader "persistence robustness"
        review in the same report): a corrupted or hand-edited store could
        contain non-finite numbers (Python's own json module parses `NaN`/
        `Infinity` from a response or file by default — not hypothetical),
        strings, or plausible-looking but nonsensical negative counters.
        """
        for attr in ("total_polls", "total_failures", "auth_failures"):
            raw = data.get(attr, 0)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
                continue
            if raw < 0:
                continue
            setattr(self, attr, int(raw))

        # HVC-012: was a dict comprehension with an unguarded int(v) inside
        # it — a single corrupted entry (NaN, +-infinity; the isinstance
        # check let both through, since NaN/inf ARE floats) raised
        # ValueError/OverflowError partway through, which is not caught
        # anywhere outside this method and would crash the whole
        # integration's setup, not just this one counter. Each entry is
        # now validated (finite, non-negative) before conversion, and a
        # bad entry is skipped rather than aborting the whole dict.
        error_counts: dict[str, int] = {}
        for k, v in data.get("error_counts", {}).items():
            if k not in _ALL_ERROR_TYPES:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v):
                continue
            if v < 0:
                continue
            error_counts[k] = int(v)
        self.error_counts = error_counts

        ema = data.get("ema_latency_ms")
        # `ema > 0` alone let +infinity through (inf > 0 is True in Python) —
        # isfinite() added, same class of gap as error_counts above.
        if (
            not isinstance(ema, bool)
            and isinstance(ema, (int, float))
            and isfinite(ema)
            and ema > 0
        ):
            self.ema_latency_ms = float(ema)
        raw_contact = data.get("last_successful_contact_at")
        if isinstance(raw_contact, str):
            with contextlib.suppress(ValueError):
                parsed_contact = datetime.fromisoformat(raw_contact)
                # Independent audit finding (2026-09, fourth round, HVC-009):
                # datetime.fromisoformat() happily returns a timezone-naive
                # datetime for a string with no offset — `last_successful_
                # contact_at` is always written from dt_util.utcnow() (aware)
                # under normal operation, so this only matters for a
                # corrupted/hand-edited/foreign-origin storage file, but if
                # it happens, the later `dt_util.utcnow() - last` in
                # binary_sensor.py's cloud_api_problem sensor raises
                # TypeError (Python refuses to subtract a naive datetime
                # from an aware one), breaking that diagnostic entity.
                # Naive timestamps are assumed UTC (matching what this
                # integration always writes) rather than the local zone.
                if parsed_contact.tzinfo is None:
                    parsed_contact = parsed_contact.replace(tzinfo=UTC)
                self.last_successful_contact_at = parsed_contact

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
            "last_successful_contact_at": (
                self.last_successful_contact_at.isoformat()
                if self.last_successful_contact_at
                else None
            ),
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
        }


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
    # Independent audit finding (2026-09, HVC-003): the circuits-list
    # response already includes `actualValue` and `temporaryChange`
    # (confirmed against docs/openapi-v3.json's CircuitV3DTO) — a free,
    # zero-extra-call source of *some* real current-state data for
    # climate/fan/water_heater, restoring part of what was lost when
    # get_live_values() was removed. actual_value mirrors target_value's
    # per-circuit-type meaning (HV: air-volume %; HK: temperature °C).
    # temporary_change_active is True whenever the API reports a
    # party/away temporary override in effect (the object being non-null
    # IS the "active" signal — see TemporaryChangeV3DTO), replacing the
    # old (now permanently-false) `live_values.get("temporaryChangeActive")
    # == "true"` check.
    actual_value: float | None = None
    temporary_change_active: bool = False
    is_air_quality_guided: bool = False
    has_error: bool = False
    circuit_status: str | None = None
    # v1.0.0: no longer populated (get_live_values() is no longer called —
    # see docs/audit-v1.0.0.md). Left in place, always empty, so climate.py /
    # fan.py / water_heater.py's existing `.live_values.get(...)` calls keep
    # working unchanged (they degrade gracefully to None on a missing key)
    # rather than needing every read site touched for this release.
    live_values: dict[str, str] = field(default_factory=dict)
    # User-defined program names: API key → display name (e.g. "week1" → "Normal")
    program_names: dict[str, str] = field(default_factory=dict)
    # "Weather based control" Eco<->Comfort weighting (CircuitSettingsDTO.weatherImpact).
    # weather_impact_supported distinguishes "fetched, but the API reported this
    # specific field as null" (supported=True, value=None) from "we never
    # queried this circuit for settings, or the endpoint isn't available for its
    # type/firmware" (supported=False) — number entities use this to decide
    # availability rather than just checking the value for None.
    weather_impact_supported: bool = False
    weather_impact_outside_temperature: int | None = None
    weather_impact_solar_radiation: float | None = None


@dataclass
class HovalPlantData:
    """Parsed data for a single plant."""

    plant_id: str
    name: str
    is_online: bool = True
    # v1.0.0: derived from `any(c.has_error for c in circuits.values())` —
    # the per-circuit `hasError` flag already comes free with the circuits
    # list (get_circuits()), which this integration still fetches for
    # control purposes. Previously derived from event-history telemetry
    # (get_latest_event/get_events), which is no longer fetched at all.
    has_error: bool = False
    circuits: dict[str, HovalCircuitData] = field(default_factory=dict)


@dataclass
class HovalData:
    """Top-level data returned by the coordinator."""

    plants: dict[str, HovalPlantData] = field(default_factory=dict)


def _extract_weather_impact(settings: dict) -> tuple[Any, Any]:
    """Safely pull (outsideTemperature, solarRadiation) out of a settings dict.

    Independent audit finding (2026-09, "more" report, finding #5): the
    write-merge path in async_set_weather_impact() used to do
    `settings.get("weatherImpact") or {}` — the exact same anti-pattern
    already fixed for the READ path elsewhere in this file (see the
    "weatherImpact" in settings checks in _fetch_all_data). A truthy
    non-dict `weatherImpact` value (a string, list, or number) passes
    through `or {}` unchanged, and the following `.get(...)` call raises
    AttributeError — which, since this is called from a fire-and-forget
    background task (number.py's debounced write), would be an invisible
    unhandled exception rather than a visible HomeAssistantError.
    """
    weather_impact = settings.get("weatherImpact")
    if not isinstance(weather_impact, dict):
        return None, None
    return weather_impact.get("outsideTemperature"), weather_impact.get("solarRadiation")


def _clamp_if_valid_number(value: Any, clamp_fn) -> Any:
    """Clamp a value if it's usable; otherwise treat it as missing.

    Independent audit finding (2026-09, fourth round, HVC-019): the
    sibling value in a weather-impact PATCH (see
    resolve_weather_impact_update below) is sourced from an optimistic
    override, the settings cache, or last-parsed circuit data — none of
    which are re-validated before being forwarded, unlike the value the
    user is actually changing (which IS clamped). A corrupted source
    (a stale/malformed cache entry, for instance) could otherwise put an
    out-of-range or non-finite value straight into the outgoing PATCH even
    though the field the user actually asked to change was itself valid.
    Used for the sibling specifically: applies the same clamp function
    already used for the primary value, and degrades a genuinely
    unusable value (clamp_fn raises ValueError for non-finite input) to
    None rather than propagating the exception — there's no better
    fallback available this deep in a write path already in progress, and
    None is what every consumer already treats as "no data" anyway.
    """
    if value is None:
        return None
    try:
        return clamp_fn(value)
    except (ValueError, TypeError):
        return None


def resolve_weather_impact_update(
    current_outside_temperature: int | None,
    current_solar_radiation: float | None,
    *,
    outside_temperature: float | None = None,
    solar_radiation: float | None = None,
) -> tuple[int | None, float | None]:
    """Resolve the full (outside_temperature, solar_radiation) pair to PATCH.

    The cloud's PATCH .../settings endpoint is not confirmed to be a JSON-merge
    patch (see api.update_circuit_settings docstring), so every request must
    carry both fields. The number entity for one slider only knows the value
    the user just dragged; the sibling field's *current* value (from cache or
    a still-fresh optimistic override) must be threaded through unchanged so
    it isn't silently cleared.

    Exactly one of outside_temperature / solar_radiation is expected to be
    provided by a caller — the field being changed. Values that are provided
    are clamped into the API's valid band; the other (sibling) field is now
    ALSO re-validated (independent audit finding, 2026-09, fourth round,
    HVC-019) via _clamp_if_valid_number rather than forwarded completely
    as-is, in case its source (cache/override/circuit data) was itself
    corrupted.

    Pure helper (no HA imports) so it is directly unit-testable.
    """
    resolved_outside = (
        clamp_weather_impact_outside_temperature(outside_temperature)
        if outside_temperature is not None
        else _clamp_if_valid_number(
            current_outside_temperature, clamp_weather_impact_outside_temperature
        )
    )
    resolved_solar = (
        clamp_weather_impact_solar_radiation(solar_radiation)
        if solar_radiation is not None
        else _clamp_if_valid_number(current_solar_radiation, clamp_weather_impact_solar_radiation)
    )
    return resolved_outside, resolved_solar


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
        config_entry: ConfigEntry,
        api: HovalConnectApi,
        health_store: Store,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=HEALTH_CHECK_INTERVAL,
        )
        self.api = api
        self._health_store = health_store
        self.control_lock = asyncio.Lock()
        # Optimistic mode override per circuit (set by control actions, cleared
        # on next successful poll OR when it exceeds _MODE_OVERRIDE_TTL_S).
        # Value: (operation_mode_string, monotonic_timestamp).
        #
        # Keyed by (plant_id, circuit_path), NOT circuit_path alone
        # (independent audit finding, 2026-09, HVC-001): circuit paths are
        # only guaranteed unique WITHIN a plant — the API's own URL scheme
        # (/v3/plants/{plantExternalId}/circuits/{circuitPath}/...) treats
        # (plantExternalId, circuitPath) as the real identity, and so do
        # entity unique_ids elsewhere in this integration. A single-plant
        # account (the common case, and the only one tested live) can never
        # hit this, but nothing here guarantees Hoval's account model always
        # stays that way — a plant split (e.g. heating and a separate future
        # AC/ventilation plant) would silently let two plants' circuits that
        # happen to share a path read and overwrite each other's cached
        # programs/settings/mode overrides. Fixed by keying every one of
        # these dicts on the same (plant_id, circuit_path) tuple.
        self._mode_override: dict[tuple[str, str], tuple[str, float]] = {}
        # Program cache: key=(plant_id, circuit_path), value=(programs_data, timestamp)
        self._program_cache: dict[tuple[str, str], tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Circuit settings cache (weather-based control weighting):
        # key=(plant_id, circuit_path), value=(settings_dict, timestamp)
        self._settings_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        self._settings_cache_ttl = CIRCUIT_SETTINGS_CACHE_TTL.total_seconds()
        # Optimistic weather-impact override per circuit, same shape/keying as
        # _mode_override: set immediately after a successful control action so
        # the slider UI doesn't wait for the next poll. Unlike _mode_override,
        # it is NOT cleared at the end of a successful poll — it expires only
        # via _MODE_OVERRIDE_TTL_S in get_weather_impact_override(). This is
        # deliberate: circuit settings are cache-tiered (CIRCUIT_SETTINGS_
        # CACHE_TTL), so a successful poll does not necessarily re-fetch them;
        # the TTL plus the _settings_cache update in async_set_weather_impact
        # keep entity state consistent in the meantime.
        # (Audit finding F4, v0.21.1 — the previous comment claimed poll-based
        # clearing that the code never implemented.)
        # value=({"outsideTemperature": int|None, "solarRadiation": float|None}, monotonic_ts)
        self._weather_impact_override: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()
        # API connection health — persists across poll cycles
        self._connection_health = HovalConnectionHealth()
        # v1.0.0: dispatch flags for _async_update_data (see its docstring).
        # The full (telemetry-trimmed) circuit/program/settings fetch only
        # runs once at startup and again after an explicit write; every other
        # scheduled tick (every HEALTH_CHECK_INTERVAL) runs the minimal
        # _health_check() instead. See docs/audit-v1.0.0.md.
        #
        # _pending_full_refresh_since is a monotonic timestamp, not a bare
        # bool (independent audit finding, 2026-09 — HVC-002): a bool cannot
        # tell "a second write asked for a refresh while the first write's
        # refresh was already in flight" from "nobody asked again", so the
        # first refresh's completion would unconditionally clear the second
        # write's still-outstanding request, silently downgrading the next
        # tick to a health-check-only cycle and leaving the second write's
        # effect unconfirmed for up to HEALTH_CHECK_INTERVAL. Storing *when*
        # the request was made lets _async_update_data compare "was this
        # refresh's request the most recent one, or did a newer one arrive
        # while I was fetching" before deciding whether it's safe to clear.
        self._did_initial_discovery = False
        self._pending_full_refresh_since: float | None = None
        # Independent audit finding (2026-09, "more" report, finding #3):
        # consecutive count of get_plants() coming back empty while plants
        # were previously known. A single anomalous/transient empty
        # response must not be treated as authoritative evidence the
        # account now has zero plants — see _guard_against_empty_plants()
        # for the full rationale. Reset to 0 by any non-empty response.
        self._consecutive_empty_plants = 0
        # Independent audit finding (2026-09, "more" report, finding #7):
        # each write schedules a fire-and-forget `hass.async_create_task(
        # _do_refresh())` with no reference kept anywhere. A write followed
        # quickly by a config-entry reload/unload could leave that task
        # asleep (it waits 2s before doing anything) past the point where
        # async_unload_entry() has already closed the API session, so it
        # would wake up and try to use an already-closed requests.Session.
        # Tracked here so async_shutdown() (called from async_unload_entry
        # BEFORE the session closes) can cancel any still-pending ones.
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def connection_health(self) -> HovalConnectionHealth:
        """Return the current API connection health snapshot."""
        return self._connection_health

    def create_tracked_task(self, coro) -> asyncio.Task:
        """Schedule a background task and track it for shutdown cancellation.

        Public (renamed from the original _create_tracked_task, independent
        audit finding, 2026-09, fourth round, HVC-003) specifically so
        entity platforms can use it too, not just the coordinator's own
        post-write refresh tasks. Returns the created task so a caller that
        also needs its own local reference (e.g. fan.py/number.py's
        debounce cancel-on-new-value logic) can keep one, in addition to
        this shared tracking.

        HVC-003: fan.py's and number.py's debounced control-write tasks
        used to bypass this tracking entirely, calling
        `hass.async_create_task()` directly. Home Assistant only cancels
        those via each entity's own `async_will_remove_from_hass()`, which
        runs during platform unload — which happens AFTER
        `async_unload_entry()` had already called `async_shutdown()` and
        closed the API session. A debounce task still asleep through its
        1.5s window at reload time could wake up and try to use an
        already-closed requests.Session. Routing these through this same
        tracked-task mechanism means `async_shutdown()` (still called
        before the session closes) now cancels AND awaits them too, not
        just the coordinator's own tasks.
        """
        task = self.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_shutdown(self) -> None:
        """Cancel any outstanding post-write refresh AND entity control tasks.

        Independent audit finding (2026-09, "more" report, finding #7, and
        fourth round, HVC-003): must be called from async_unload_entry()
        BEFORE the API session is closed — otherwise a task that was
        sleeping through its settle/debounce delay could wake up afterwards
        and try to use an already-closed requests.Session. Covers both the
        coordinator's own post-write refresh tasks AND every entity's
        debounced control-write task, now that both go through
        create_tracked_task().
        """
        tasks = list(self._background_tasks)
        if not tasks:
            return
        _LOGGER.debug("Cancelling %d pending background task(s)", len(tasks))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Require this many consecutive empty get_plants() responses (while
    # plants were previously known) before actually accepting a wipe to
    # zero plants. 2 means the anomaly must survive across at least one
    # full retry cycle — whichever comes first, the next scheduled health
    # check or the next write-triggered refresh — not just one blip.
    _EMPTY_PLANTS_CONFIRMATION_THRESHOLD = 2

    def _guard_against_empty_plants(self, plants_raw: list) -> bool:
        """Return True if an empty get_plants() response should be trusted.

        Independent audit finding (2026-09, "more" report, finding #3): both
        _health_check() and _fetch_all_data() used to rebuild the plant
        dict from whatever get_plants() returned, unconditionally — so one
        anomalous-but-well-formed empty response (a transient backend
        hiccup, a proxy returning `[]` instead of an error, etc.) was
        treated as authoritative evidence the account now has zero plants,
        silently wiping every entity while the poll itself was recorded as
        a *successful* contact. This is a fail-open-into-a-destructive-state
        problem, not merely a display glitch.

        Only distrusts an empty response when plants were PREVIOUSLY known
        (self.data already has at least one plant) — a genuinely new
        account, or a legitimate first-ever fetch, has nothing to lose by
        accepting `[]` immediately. Once distrusted, requires
        _EMPTY_PLANTS_CONFIRMATION_THRESHOLD consecutive empty responses
        (via either code path — a health check or a full refresh both call
        this) before actually accepting the wipe, so a real "the account
        genuinely now has zero plants" transition still eventually takes
        effect rather than being permanently refused.
        """
        if plants_raw:
            self._consecutive_empty_plants = 0
            return True
        if not (self.data and self.data.plants):
            return True  # nothing previously known — an empty result is unremarkable
        self._consecutive_empty_plants += 1
        if self._consecutive_empty_plants >= self._EMPTY_PLANTS_CONFIRMATION_THRESHOLD:
            _LOGGER.warning(
                "get_plants() returned empty %d times in a row despite %d "
                "previously known plant(s); accepting this as a genuine "
                "topology change rather than a transient anomaly.",
                self._consecutive_empty_plants,
                len(self.data.plants),
            )
            return True
        _LOGGER.warning(
            "get_plants() returned an empty list while %d plant(s) were "
            "previously known (%d/%d confirmations so far) — treating this "
            "as a likely transient/anomalous response and keeping the "
            "existing topology rather than wiping it.",
            len(self.data.plants),
            self._consecutive_empty_plants,
            self._EMPTY_PLANTS_CONFIRMATION_THRESHOLD,
        )
        return False

    def set_mode_override(self, plant_id: str, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[(plant_id, circuit_path)] = (mode, time.monotonic())

    def get_mode_override(self, plant_id: str, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit, if still fresh.

        Returns None once the override exceeds _MODE_OVERRIDE_TTL_S so a stale
        optimistic value cannot mask the real device state indefinitely when
        polls are failing.
        """
        key = (plant_id, circuit_path)
        entry = self._mode_override.get(key)
        if entry is None:
            return None
        mode, ts = entry
        if time.monotonic() - ts > _MODE_OVERRIDE_TTL_S:
            self._mode_override.pop(key, None)
            return None
        return mode

    def get_weather_impact_override(
        self, plant_id: str, circuit_path: str
    ) -> dict[str, Any] | None:
        """Get the optimistic weather-impact override for a circuit, if still fresh.

        Expiry is TTL-only: returns None once the override exceeds
        _MODE_OVERRIDE_TTL_S so a stale optimistic value cannot mask the real
        device state indefinitely. Unlike mode overrides, weather-impact
        overrides are intentionally NOT cleared on successful polls (see the
        _weather_impact_override comment in __init__ for the rationale).
        """
        key = (plant_id, circuit_path)
        entry = self._weather_impact_override.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > _MODE_OVERRIDE_TTL_S:
            self._weather_impact_override.pop(key, None)
            return None
        return value

    async def async_set_weather_impact(
        self,
        plant_id: str,
        circuit_path: str,
        *,
        outside_temperature: float | None = None,
        solar_radiation: float | None = None,
    ) -> None:
        """Set one or both weather-impact weighting values for a circuit.

        Resolves the full (outsideTemperature, solarRadiation) pair before
        calling the API — see resolve_weather_impact_update() and
        api.update_circuit_settings() for why both fields must always be sent
        together. "Current" values are read from (in priority order): a still
        fresh optimistic override; a still-fresh settings cache entry; a
        FRESH GET if the cache is stale or missing; falling back to the
        last-parsed circuit data only if that fresh GET itself fails.

        Independent audit finding (2026-09, "more" report, finding #4): the
        settings cache used to be read here with NO freshness check at
        all — a cache entry from hours ago was still preferred over a fresh
        read. Traced through the report's own example scenario carefully:
        simply falling through to `circuit.weather_impact_*` (the report's
        "simple" suggested fix) would NOT actually have fixed it, since
        that field is populated from the exact same cache in lockstep and
        would be equally stale in the same scenario. Only a genuine fresh
        GET closes the real gap — a sibling value changed by some other
        client (the app, another HA instance) between this cache entry's
        age and now could otherwise be silently reverted by this PATCH,
        since both fields are always sent together (see
        api.update_circuit_settings's docstring for why).
        """
        async with self.control_lock:
            key = (plant_id, circuit_path)
            current = self.get_weather_impact_override(plant_id, circuit_path)
            cached = self._settings_cache.get(key)
            cache_is_fresh = cached is not None and (
                time.time() - cached[1] <= self._settings_cache_ttl
            )
            # Look up the circuit within its OWN plant only — not by
            # scanning every plant for a matching path (independent audit
            # finding, 2026-09, HVC-001): two plants could share a circuit
            # path, and plant_id is already known here, so there is no
            # reason to risk resolving the wrong plant's circuit.
            plant = self.data.plants.get(plant_id) if self.data else None
            circuit = plant.circuits.get(circuit_path) if plant is not None else None

            if current is not None:
                current_outside = current.get("outsideTemperature")
                current_solar = current.get("solarRadiation")
            elif cache_is_fresh:
                current_outside, current_solar = _extract_weather_impact(cached[0])
            else:
                # Cache missing or stale — get a genuinely fresh read before
                # merging, rather than trusting old data (finding #4) or
                # skipping straight to potentially-equally-stale circuit
                # data. Only falls back to circuit/cached/None if this
                # itself fails, so a transient GET failure still lets the
                # write proceed on a best-effort basis instead of blocking
                # the user's action entirely.
                try:
                    fresh_settings = await self.api.get_circuit_settings(plant_id, circuit_path)
                except HovalApiError:
                    _LOGGER.debug(
                        "Fresh settings GET failed while resolving weather-impact "
                        "write for %s; falling back to last-known data.",
                        circuit_path,
                    )
                    fresh_settings = None
                if isinstance(fresh_settings, dict):
                    self._settings_cache[key] = (fresh_settings, time.time())
                    current_outside, current_solar = _extract_weather_impact(fresh_settings)
                elif cached is not None:
                    current_outside, current_solar = _extract_weather_impact(cached[0])
                elif circuit is not None:
                    current_outside = circuit.weather_impact_outside_temperature
                    current_solar = circuit.weather_impact_solar_radiation
                else:
                    current_outside = None
                    current_solar = None

            try:
                resolved_outside, resolved_solar = resolve_weather_impact_update(
                    current_outside,
                    current_solar,
                    outside_temperature=outside_temperature,
                    solar_radiation=solar_radiation,
                )
            except ValueError as err:
                # Independent audit finding (2026-09, HVC-ICS-004): the
                # clamp helpers now reject non-finite input (NaN, +-inf)
                # instead of silently turning it into a valid-looking
                # boundary value. Re-raised as HovalApiError specifically
                # so number.py's existing `except HovalApiError` handling
                # converts it to a visible HomeAssistantError — without
                # this, a ValueError here would propagate uncaught through
                # a fire-and-forget background task (the exact "invisible
                # failure" pattern audit finding F5 already fixed
                # elsewhere in this codebase).
                raise HovalApiError(str(err)) from err

            await self.api.update_circuit_settings(
                plant_id,
                circuit_path,
                outside_temperature=resolved_outside,
                solar_radiation=resolved_solar,
            )
            # The write itself succeeding is direct, immediate evidence the
            # cloud is reachable — record it now rather than waiting for the
            # background refresh below (which could itself time out even
            # though this write just worked).
            self._connection_health.record_successful_contact(dt_util.utcnow())
            # Notify listeners (independent audit finding, 2026-09):
            # HovalCloudApiProblem reads this timestamp live, but as a
            # CoordinatorEntity it only re-renders when the coordinator
            # notifies its listeners — which otherwise only happens after a
            # full _async_update_data() cycle completes. Without this call,
            # a write clearing the "cloud problem" state would not actually
            # be reflected in the entity/history/automations until the
            # following background refresh succeeds — and if that refresh
            # itself times out, the sensor could keep showing "problem" for
            # up to HEALTH_CHECK_INTERVAL despite the write just proving the
            # cloud was reachable. async_update_listeners() only notifies —
            # it does not touch self.data or trigger a new fetch.
            self.async_update_listeners()

            merged = {"outsideTemperature": resolved_outside, "solarRadiation": resolved_solar}
            now_mono = time.monotonic()
            self._weather_impact_override[key] = (merged, now_mono)
            # Independent bug report (2026-09, verified live on v0.24.1):
            # deliberately does NOT also optimistically refresh
            # _settings_cache the way it used to. Pre-populating the cache
            # with our own just-written guess made the following
            # need_settings check see a "fresh" cache and skip the real
            # verification GET entirely — for up to CIRCUIT_SETTINGS_
            # CACHE_TTL (10 minutes) — even though a background refresh runs
            # just 2 seconds after every write specifically to confirm it.
            # A write that returned success but silently didn't take effect
            # on the device (accepted by the API, then overridden/ignored/
            # reverted) could then mask the true value far longer than the
            # override's own 120s TTL, since the settings poll that's
            # supposed to reconcile it never actually re-fetched. The
            # override above already covers the UI during the gap between
            # this write and that verification fetch landing; leaving
            # _settings_cache alone lets that fetch do its real job.

        # Schedule refresh as background task — do not await it here, matching
        # async_control_and_refresh's rationale: keep the calling entity method
        # fast and don't starve control_lock during a slow/timeout refresh.
        # v1.0.0: _pending_full_refresh_since tells _async_update_data to do a
        # real (telemetry-trimmed) circuit/settings resync instead of the
        # minimal scheduled health check, so this write's effect actually
        # shows up. It's set to a fresh timestamp on EVERY call (not just
        # when None) so a second write's request always registers as newer
        # than whatever an in-flight refresh captured — see the field's
        # comment in __init__ (HVC-002) and _async_update_data's docstring.
        async def _do_refresh() -> None:
            await asyncio.sleep(2)
            try:
                self._pending_full_refresh_since = time.monotonic()
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        self.create_tracked_task(_do_refresh())

    async def async_control_and_refresh(
        self,
        coro: Any,
        *,
        plant_id: str,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside control_lock
        so concurrent control actions don't race each other.

        `plant_id` is required (independent audit finding, 2026-09, HVC-001):
        the optimistic mode override is keyed on (plant_id, circuit_path),
        not circuit_path alone, since circuit paths are only unique within a
        plant. Made keyword-only together with circuit_path/mode_override so
        every call site names its arguments — this signature changed from
        `(coro, circuit_path, mode_override)`, and requiring keywords makes
        any call site that wasn't updated fail loudly (TypeError) instead of
        silently passing plant_id positionally into the wrong parameter.

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
            self.set_mode_override(plant_id, circuit_path, mode_override)
            # See the equivalent comment in async_set_weather_impact: the
            # write succeeding is itself proof of cloud contact.
            self._connection_health.record_successful_contact(dt_util.utcnow())
            # See the equivalent comment in async_set_weather_impact for why
            # this is needed (independent audit finding, 2026-09): without
            # it, the cloud-problem sensor and the optimistic mode override
            # just set above would not actually reach the entity until the
            # next full coordinator update.
            self.async_update_listeners()

        # Schedule refresh as background task — do not await it here.
        # This keeps the caller (entity action method) fast and prevents the
        # lock from being starved during a slow/timeout refresh.
        async def _do_refresh() -> None:
            # Brief pause so the API has time to commit the change before we
            # fetch fresh state.  Moved inside the task so the entity action
            # method returns to HA immediately instead of blocking for 2 s.
            await asyncio.sleep(2)
            try:
                # v1.0.0: force a real (telemetry-trimmed) resync rather than
                # the minimal scheduled health check — see docstring above.
                # Always a fresh timestamp, not just when unset — see the
                # field's comment in __init__ (HVC-002).
                self._pending_full_refresh_since = time.monotonic()
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        self.create_tracked_task(_do_refresh())

    async def _async_update_data(self) -> HovalData:
        """Dispatch to either a full resync or the minimal health check.

        v1.0.0: this integration no longer polls telemetry on a schedule.
        _fetch_all_data() (circuits + programs + settings, no live-values/
        events/weather — see its docstring) now runs only:
          - once, on the very first call (startup discovery), and
          - again whenever a write set self._pending_full_refresh_since
            before calling async_request_refresh() (see
            async_control_and_refresh / async_set_weather_impact).
        Every other call — i.e. every regular HEALTH_CHECK_INTERVAL tick —
        runs _health_check() instead: one auth call, one GET /api/my-plants,
        nothing circuit-specific. See docs/audit-v1.0.0.md.

        Health counters are updated before exceptions are re-raised so that
        connection-health sensors always reflect the latest failure state even
        when HA marks the coordinator as unavailable.

        Lost-update race fix (independent audit finding, 2026-09, HVC-002):
        a second write's refresh request, made while an earlier write's
        refresh is already fetching, must not be silently dropped when that
        earlier refresh finishes. This is handled by capturing the
        *timestamp* of the request this specific call is serving
        (`refresh_requested_at`) before the (possibly slow) fetch starts,
        and only clearing `_pending_full_refresh_since` afterwards if
        nothing newer arrived while we were fetching. If a second write's
        `_do_refresh()` set a later timestamp during that window, this
        call's completion leaves it in place, so the NEXT tick still does a
        full resync instead of downgrading to a health check.

        Error types:
        - ERROR_TYPE_TIMEOUT   — the 90 s overall timeout fired
        - ERROR_TYPE_AUTH      — HovalAuthError (credentials problem)
        - ERROR_TYPE_CIRCUIT_LIST — get_circuits() specifically failed (only
                                    possible during a full resync, never
                                    during a plain health check)
        - ERROR_TYPE_API       — any other HovalApiError
        - ERROR_TYPE_UNKNOWN   — unexpected exception (bug or API schema change)
        """
        _start = time.monotonic()
        self._connection_health.record_poll_attempt(dt_util.utcnow())

        refresh_requested_at = self._pending_full_refresh_since
        do_full_refresh = not self._did_initial_discovery or refresh_requested_at is not None

        try:
            async with asyncio.timeout(90):
                if do_full_refresh:
                    result = await self._fetch_all_data(fetch_started_at=_start)
                    self._did_initial_discovery = True
                    # Only clear if no NEWER request arrived while this fetch
                    # was in flight — see the docstring above and HVC-002.
                    if self._pending_full_refresh_since == refresh_requested_at:
                        self._pending_full_refresh_since = None
                else:
                    result = await self._health_check()

            elapsed_ms = round((time.monotonic() - _start) * 1000, 0)
            self._connection_health.record_poll_success(dt_util.utcnow(), elapsed_ms)
            _LOGGER.debug(
                "%s succeeded in %.0f ms (ema=%.0f ms, total_polls=%d)",
                "Full refresh" if do_full_refresh else "Health check",
                elapsed_ms,
                self._connection_health.ema_latency_ms or 0,
                self._connection_health.total_polls,
            )
            # Persist updated counters with a debounced delay to avoid I/O on
            # every update cycle.  If HA is shut down before the delay fires,
            # async_unload_entry does an immediate save.
            self._health_store.async_delay_save(
                self._connection_health.to_store_dict, _HEALTH_SAVE_DELAY_S
            )
            return result

        except TimeoutError as err:
            _ts = dt_util.utcnow()
            self._connection_health.record_error(_ts, ERROR_TYPE_TIMEOUT, "Poll timeout after 90 s")
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
            self._connection_health.record_error(
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
            self._connection_health.record_error(_ts, ERROR_TYPE_CIRCUIT_LIST, str(err)[:200])
            _LOGGER.warning(
                "Circuit list fetch failed (consecutive=%d): %s",
                self._connection_health.consecutive_failures,
                err,
            )
            raise UpdateFailed(f"Circuit list unavailable: {err}") from err

        except HovalApiError as err:
            _ts = dt_util.utcnow()
            self._connection_health.record_error(_ts, ERROR_TYPE_API, str(err)[:200])
            _LOGGER.warning(
                "API error during poll (consecutive=%d, total_failures=%d): %s",
                self._connection_health.consecutive_failures,
                self._connection_health.total_failures,
                err,
            )
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

        except Exception as err:  # noqa: BLE001
            _ts = dt_util.utcnow()
            self._connection_health.record_error(
                _ts, ERROR_TYPE_UNKNOWN, f"{type(err).__name__}: {err}"
            )
            raise

    async def _health_check(self) -> HovalData:
        """The ONLY thing that runs on a recurring schedule (v1.0.0).

        Deliberately minimal: one auth call (implicit inside get_plants(),
        via token caching/refresh), one GET /api/my-plants. Does NOT touch
        circuits, programs, settings, or anything telemetry-related — those
        are populated once at startup and refreshed only after an actual
        write (see _async_update_data's docstring). This is the design the
        user explicitly asked for: minimal standing cloud traffic, with just
        enough of a heartbeat to know whether the API is still reachable at
        all. See docs/audit-v1.0.0.md.

        Reuses each plant's existing circuits dict unchanged; only refreshes
        is_online (from the response) and has_error (recomputed from those
        unchanged circuits, in case optimistic overrides changed it since) —
        UNLESS a topology change is detected (see below), in which case this
        upgrades itself to a real discovery fetch for that one cycle.

        Independent audit finding (2026-09, HVC-ICS-001): a plant that was
        offline during the very first discovery, or a plant that appears
        for the first time after startup (e.g. Hoval splitting an account
        into multiple plants — a scenario the user specifically flagged as
        plausible), would otherwise NEVER get its circuits discovered.
        Neither this method nor a health-check-only _async_update_data
        cycle ever calls get_circuits() or fires SIGNAL_NEW_CIRCUITS — only
        _fetch_all_data() does — and nothing was watching for exactly the
        two conditions that make a fresh discovery necessary: a plant with
        no prior circuits at all, or a plant that just came online.
        Detecting that here and immediately running a full (telemetry-
        trimmed) discovery fetch instead of the usual minimal check closes
        that gap the moment it's next observable, rather than depending on
        an unrelated write to some other, already-known circuit to
        incidentally trigger one — which might never happen, e.g. if this
        is the only plant on the account and it has no working entities to
        write to yet.
        """
        plants_raw = await self.api.get_plants()

        # Independent audit finding (2026-09, "more" report, finding #3):
        # an empty response must not be trusted blindly — see
        # _guard_against_empty_plants()'s docstring. Checked before
        # anything else so a distrusted empty response short-circuits
        # straight to "keep the existing snapshot", without running the
        # topology-change detection below against nothing.
        if not self._guard_against_empty_plants(plants_raw):
            return self.data

        existing = self.data.plants if self.data else {}

        topology_changed = False
        for raw in plants_raw:
            if not isinstance(raw, dict):
                continue
            plant_id = raw.get("plantExternalId")
            if not plant_id:
                continue
            prior = existing.get(plant_id)
            now_online = raw.get("isOnline", True)
            if prior is None or (not prior.is_online and now_online):
                topology_changed = True
                break

        if topology_changed:
            _LOGGER.info(
                "Detected a new or newly-online plant during a scheduled "
                "health check; running a full discovery fetch instead of "
                "the usual minimal check so its circuits/entities appear "
                "without waiting for an unrelated write or a restart."
            )
            return await self._fetch_all_data()

        plants: dict[str, HovalPlantData] = {}
        for raw in plants_raw:
            if not isinstance(raw, dict):
                continue
            plant_id = raw.get("plantExternalId")
            if not plant_id:
                continue
            # Independent audit finding (2026-09, fourth round, HVC-022):
            # same class of gap as HVC-002 (duplicate circuit paths), one
            # level up — nothing detected two plants sharing an ID. Kept
            # deterministic (first-seen wins) and visible (logged), rather
            # than silently overwriting one plant's metadata with another's.
            if plant_id in plants:
                _LOGGER.warning(
                    "get_plants() reported two plants with the same ID %r; "
                    "keeping the first one seen and discarding the duplicate. "
                    "This indicates an upstream data problem, not normal operation.",
                    plant_id,
                )
                continue
            prior = existing.get(plant_id)
            circuits = prior.circuits if prior is not None else {}
            plants[plant_id] = HovalPlantData(
                plant_id=plant_id,
                name=raw.get("description") or (prior.name if prior is not None else plant_id),
                is_online=raw.get("isOnline", True),
                has_error=any(c.has_error for c in circuits.values()),
                circuits=circuits,
            )
        return HovalData(plants=plants)

    async def async_save_health(self) -> None:
        """Force an immediate health snapshot save to HA storage.

        Called from async_unload_entry so counters are not lost on a clean
        shutdown even if the debounced save hasn't fired yet.
        """
        await self._health_store.async_save(self._connection_health.to_store_dict())

    async def _fetch_all_data(self, *, fetch_started_at: float | None = None) -> HovalData:
        """Full (telemetry-trimmed) resync — called only at startup and after writes.

        v1.0.0: this is no longer the scheduled polling method (that's
        _health_check() now — see _async_update_data's docstring). It still
        fetches everything needed for CONTROL: circuits list, programs (for
        program_names), and circuit settings (for weatherImpact) — but never
        live-values, events, or weather, which were pure telemetry with no
        write dependency and are no longer fetched at all. See
        docs/audit-v1.0.0.md.

        HovalAuthError and HovalApiError propagate up to _async_update_data
        which converts them to ConfigEntryAuthFailed / UpdateFailed respectively.

        `fetch_started_at` (a time.monotonic() timestamp captured by the
        caller before this coroutine starts awaiting anything) bounds which
        optimistic mode overrides are safe to clear at the end — see the
        comment above self._mode_override.clear() below and HVC-002 in
        docs/audit-v1.0.0.md. Defaults to "now" for any direct/test caller
        that doesn't pass one, which preserves the pre-fix (whole-dict-clear)
        behavior for those call sites.
        """
        if fetch_started_at is None:
            fetch_started_at = time.monotonic()
        data = HovalData()

        plants = await self.api.get_plants()

        # Independent audit finding (2026-09, "more" report, finding #3):
        # same guard as _health_check() — an anomalous-but-well-formed
        # empty response must not silently wipe every known plant just
        # because this happened to be the refresh that observed it. See
        # _guard_against_empty_plants()'s docstring for the full rationale.
        if not self._guard_against_empty_plants(plants):
            return self.data

        for plant in plants:
            plant_id = plant.get("plantExternalId")
            if not plant_id:
                _LOGGER.debug("Skipping plant with missing plantExternalId")
                continue
            # Independent audit finding (2026-09, fourth round, HVC-022):
            # same class of gap as HVC-002 (duplicate circuit paths), one
            # level up. Checked here, before any circuit/program/settings
            # calls are made for this plant, so a duplicate doesn't even
            # cost the wasted API calls its processing would otherwise
            # trigger — first-seen wins, and the duplicate is skipped
            # entirely rather than silently overwriting the first one's
            # data later.
            if plant_id in data.plants:
                _LOGGER.warning(
                    "get_plants() reported two plants with the same ID %r; "
                    "keeping the first one seen and discarding the duplicate. "
                    "This indicates an upstream data problem, not normal operation.",
                    plant_id,
                )
                continue

            # `or plant_id` (not `.get(key, default)`), deliberately: dict.get's
            # default only applies when the KEY is absent, not when its value
            # is explicitly null — a "description": null response would
            # otherwise produce a HovalPlantData.name of None, violating the
            # dataclass's declared `name: str`. Matches the equivalent line
            # in _health_check() (independent audit finding, 2026-09,
            # "additional observations" — the two functions had drifted).
            plant_name = plant.get("description") or plant_id

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

            # Independent audit finding (2026-09, fourth round, HVC-001):
            # every element of circuits_raw is assumed to be a dict. The API
            # client validates the outer response is a list (or a dict with
            # a list "content" key), but never validated that each ELEMENT
            # of that list is itself a dict. A response like [{"type":
            # "HK", ...}, null] would raise AttributeError on c.get(...)
            # below — BEFORE _fetch_circuit() ever runs, so it happens
            # outside the per-circuit asyncio.gather(..., return_exceptions
            # =True) isolation that's supposed to protect exactly this kind
            # of malformed-input scenario. One bad element could abort the
            # entire refresh (all circuits, all plants), not just itself.
            valid_circuits_raw = [c for c in circuits_raw if isinstance(c, dict)]
            if len(valid_circuits_raw) != len(circuits_raw):
                _LOGGER.warning(
                    "Circuits response for plant %s contained %d non-dict element(s); "
                    "skipping them and continuing with the rest.",
                    plant_id,
                    len(circuits_raw) - len(valid_circuits_raw),
                )

            _LOGGER.debug(
                "Fetched %d circuits (%d supported)",
                len(valid_circuits_raw),
                sum(
                    1
                    for c in valid_circuits_raw
                    if c.get("type") in SUPPORTED_CIRCUIT_TYPES
                    and (_is_circuit_selectable(c) or c.get("type") in _NON_SELECTABLE_TYPES)
                ),
            )

            # Build list of supported circuits
            supported_circuits: list[tuple[str, str, dict]] = []
            for circuit in valid_circuits_raw:
                ctype = circuit.get("type", "")
                if ctype not in SUPPORTED_CIRCUIT_TYPES:
                    continue
                if not _is_circuit_selectable(circuit) and ctype not in _NON_SELECTABLE_TYPES:
                    continue
                path = circuit.get("path")
                if not path:
                    _LOGGER.warning(
                        "Skipping circuit with missing 'path' field for plant %s: %s",
                        plant_id,
                        {k: v for k, v in circuit.items() if k != "name"},
                    )
                    continue
                if _LOGGER.isEnabledFor(logging.DEBUG):
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
                # Independent audit finding (2026-09, HVC-ICS-005): explicit
                # isinstance check, not just `or {}`. A truthy NON-dict value
                # (a string, list, or number) would pass through `or {}`
                # unchanged, and the `.get()` call two lines below would then
                # raise AttributeError — which, since this whole function
                # runs under asyncio.gather(..., return_exceptions=True) in
                # _fetch_all_data(), would silently drop this ENTIRE circuit
                # from plant_data.circuits, not just degrade this one
                # optional field. Malformed optional metadata must degrade
                # that feature, never delete the circuit.
                air_quality = circuit.get("airQuality")
                if not isinstance(air_quality, dict):
                    air_quality = {}
                circuit_data = HovalCircuitData(
                    circuit_type=ctype,
                    path=path,
                    name=circuit.get("name") or ctype,
                    operation_mode=circuit.get("operationMode"),
                    active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
                    target_value=_coerce_finite_number(circuit.get("targetValue")),
                    # Free (already part of this same circuits-list response,
                    # no extra call) — see HovalCircuitData's field comments
                    # and HVC-003 in docs/audit-v1.0.0.md.
                    actual_value=_coerce_finite_number(circuit.get("actualValue")),
                    temporary_change_active=circuit.get("temporaryChange") is not None,
                    is_air_quality_guided=bool(air_quality.get("isAirQualityGuided")),
                    has_error=circuit.get("hasError", False),
                    circuit_status=circuit.get("circuitStatus"),
                )

                # Check program cache. Only fetched for circuit types confirmed
                # to expose a time-program endpoint (see SUPPORTS_PROGRAMS) —
                # BL (boiler) returns HTTP 417 on every call, so skipping it
                # avoids a predictable failure on every refresh cycle, the
                # same reasoning already applied to weatherImpact below.
                # Keyed by (plant_id, path) — see HVC-001 in docs/audit-v1.0.0.md.
                cached_prog = self._program_cache.get((_plant_id, path))
                need_programs = ctype in SUPPORTS_PROGRAMS and (
                    cached_prog is None or time.time() - cached_prog[1] > self._program_cache_ttl
                )

                # Check circuit-settings (weather impact) cache. Only fetched for
                # circuit types confirmed to support it (see SUPPORTS_WEATHER_IMPACT)
                # so unsupported types never take an extra, likely-erroring round trip.
                cached_settings = self._settings_cache.get((_plant_id, path))
                need_settings = ctype in SUPPORTS_WEATHER_IMPACT and (
                    cached_settings is None
                    or time.time() - cached_settings[1] > self._settings_cache_ttl
                )

                # Fetch programs/settings (only if their respective cache is
                # stale). v1.0.0: live-values are no longer fetched here at
                # all (see docs/audit-v1.0.0.md) — this integration no longer
                # polls telemetry; a separate CAN-bus HACS integration covers
                # it. Tasks are gathered by name (not position) so
                # adding/omitting any of them can't silently shift which
                # result maps to which variable.
                tasks: dict[str, Any] = {}
                if need_programs:
                    tasks["programs"] = self.api.get_programs(_plant_id, path)
                if need_settings:
                    tasks["settings"] = self.api.get_circuit_settings(_plant_id, path)

                gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
                results: dict[str, Any] = dict(zip(tasks.keys(), gathered, strict=True))
                # Guarded (unlike a plain `if not need_programs`): for a circuit
                # type outside SUPPORTS_PROGRAMS, need_programs is always False
                # and cached_prog is always None (nothing is ever cached for
                # it), so the old unconditional `cached_prog[0]` would raise
                # TypeError on the very first poll of such a circuit.
                if not need_programs and cached_prog is not None:
                    results["programs"] = cached_prog[0]
                if not need_settings and cached_settings is not None:
                    results["settings"] = cached_settings[0]

                programs = results.get("programs")
                # Guard: only process programs when the API returned a proper dict.
                # Non-programmable circuits (BL, operationMode=None) may return
                # HTTP 204 → None or HTTP 200 with body [] after Hoval's May 2026
                # change.  [] is not None and not an exception, so a weaker guard
                # would enter this block and crash on [].get("dayPrograms", {}).
                # Non-dict values are never cached.  See CLAUDE.md for full history.
                if isinstance(programs, dict):
                    if need_programs:
                        self._program_cache[(_plant_id, path)] = (programs, time.time())
                    # Isolation barrier (audit finding F1, v0.21.1): any residual
                    # exception in this block must degrade the program *fields*
                    # only — never propagate out of _fetch_circuit, which would
                    # discard the whole circuit via gather(return_exceptions=True).
                    try:
                        # Extract user-defined program names — the only thing
                        # still read from `programs` since v1.0.0 removed the
                        # active-week/day/phase-value resolution (pure
                        # telemetry, no write dependency; see
                        # docs/audit-v1.0.0.md). select.py uses these names to
                        # show "Winter"/"Sommer" instead of raw "week1"/"week2".
                        #
                        # Independent audit finding (2026-09, fourth round,
                        # HVC-016): `w1.get("name")` was only checked for
                        # truthiness, not for being a string — a malformed
                        # response with "name": ["a", "list"] or {"nested":
                        # "obj"} is truthy and would pass through unchanged.
                        # select.py's resolve_program_display_names() later
                        # does `counts[name] = counts.get(name, 0) + 1`,
                        # which requires `name` to be hashable — a list or
                        # dict there raises TypeError, taking down that
                        # entity's entire options/state computation. Now
                        # requires an actual (non-empty, after stripping
                        # whitespace) string.
                        w1 = programs.get("week1")
                        w2 = programs.get("week2")
                        if isinstance(w1, dict):
                            w1_name = w1.get("name")
                            if isinstance(w1_name, str) and w1_name.strip():
                                circuit_data.program_names["week1"] = w1_name.strip()
                        if isinstance(w2, dict):
                            w2_name = w2.get("name")
                            if isinstance(w2_name, str) and w2_name.strip():
                                circuit_data.program_names["week2"] = w2_name.strip()
                    except Exception:  # noqa: BLE001 — see isolation note above
                        _LOGGER.warning(
                            "Program data for circuit %s could not be parsed; "
                            "program name will be unknown this cycle",
                            path,
                            exc_info=True,
                        )
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

                # --- Weather-based control weighting (weatherImpact), HK only ---
                if ctype in SUPPORTS_WEATHER_IMPACT:
                    settings = results.get("settings")
                    if isinstance(settings, dict):
                        if need_settings:
                            self._settings_cache[(_plant_id, path)] = (settings, time.time())
                        # `"weatherImpact" in settings` (key presence), not just
                        # `.get(...)`, deliberately: Hoval's cloud has been
                        # observed (forensic crawl, 2026-09) to drop the
                        # `weatherImpact` key from this response entirely for
                        # every circuit tested, returning only `circuitName`.
                        # A missing key means the cloud no longer offers the
                        # feature for this circuit right now and the number
                        # entities should report unavailable, which is
                        # different from a key present-but-null (both
                        # sub-fields legitimately unset), which should still
                        # show the sliders with an unknown value.
                        if "weatherImpact" in settings:
                            # Independent audit finding (2026-09, HVC-ICS-005):
                            # explicit isinstance check, not just `or {}` — see
                            # the identical fix/rationale on airQuality above.
                            weather_impact = settings["weatherImpact"]
                            if not isinstance(weather_impact, dict):
                                weather_impact = {}
                            circuit_data.weather_impact_supported = True
                            circuit_data.weather_impact_outside_temperature = weather_impact.get(
                                "outsideTemperature"
                            )
                            circuit_data.weather_impact_solar_radiation = weather_impact.get(
                                "solarRadiation"
                            )
                        else:
                            _LOGGER.debug(
                                "Circuit settings for %s do not include "
                                "'weatherImpact' (cloud may have removed/moved "
                                "the feature); weather-impact controls will be "
                                "unavailable for this circuit until it "
                                "reappears.",
                                path,
                            )
                        # Independent bug report (2026-09, verified live on
                        # v0.24.1, confirmed still present pre-fix in
                        # v1.0.0): reconcile the optimistic override against
                        # this GENUINE fresh poll result (need_settings=True
                        # means an actual API call happened this cycle, not
                        # a cache hit). Previously the override was cleared
                        # ONLY by its own 120s TTL, never by a real poll —
                        # so a write that returned success but silently
                        # didn't take effect on the device (accepted by the
                        # API, then overridden/ignored/reverted) could mask
                        # the true value indefinitely: any consumer
                        # comparing entity state to a target before deciding
                        # whether to (re)send would wrongly conclude no
                        # write was needed. Guarded the same way as
                        # _mode_override in HVC-002: only clear an override
                        # OLDER than this fetch started, so a write landing
                        # for THIS circuit while this exact fetch was
                        # already in flight is not wiped by a snapshot that
                        # could not possibly reflect it.
                        if need_settings:
                            override_key = (_plant_id, path)
                            existing_override = self._weather_impact_override.get(override_key)
                            if (
                                existing_override is not None
                                and existing_override[1] < fetch_started_at
                            ):
                                self._weather_impact_override.pop(override_key, None)
                    elif isinstance(settings, BaseException):
                        _LOGGER.debug("Circuit settings not available for %s: %s", path, settings)
                        # Fall back to a still-fresh cached value (if any) rather
                        # than flipping the number entities unavailable on a
                        # single transient failure. Same key-presence check as
                        # above applies to the cached value.
                        if cached_settings is not None and "weatherImpact" in cached_settings[0]:
                            weather_impact = cached_settings[0]["weatherImpact"]
                            if not isinstance(weather_impact, dict):
                                weather_impact = {}
                            circuit_data.weather_impact_supported = True
                            circuit_data.weather_impact_outside_temperature = weather_impact.get(
                                "outsideTemperature"
                            )
                            circuit_data.weather_impact_solar_radiation = weather_impact.get(
                                "solarRadiation"
                            )
                    # else: settings is None (no cache and not fetched this cycle
                    # for a type that supports it, e.g. first poll ordering edge
                    # case) — leave weather_impact_supported at its False default.

                    # An optimistic override from a very recent slider drag takes
                    # priority over whatever the poll just fetched, so the UI
                    # doesn't flicker back to a pre-update value while the cloud
                    # is still settling the change.
                    override = self.get_weather_impact_override(_plant_id, path)
                    if override is not None:
                        circuit_data.weather_impact_supported = True
                        circuit_data.weather_impact_outside_temperature = override.get(
                            "outsideTemperature"
                        )
                        circuit_data.weather_impact_solar_radiation = override.get("solarRadiation")

                return circuit_data

            # Run circuits in parallel.
            all_tasks = [
                _fetch_circuit(path, ctype, circ) for path, ctype, circ in supported_circuits
            ]
            all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

            # Process circuit results. has_error is derived entirely from the
            # per-circuit `hasError` flag (free — it's already part of the
            # circuits-list response) since v1.0.0 removed event-history
            # telemetry (get_latest_event/get_events) entirely; see
            # docs/audit-v1.0.0.md. This is a narrower definition than
            # before (any circuit reporting hasError=true, vs. any active
            # blocking/locking/warning event in the plant's recent history)
            # but needs no extra API call and stays accurate for as long as
            # the circuits list itself is fresh.
            for result in all_results:
                if isinstance(result, BaseException):
                    _LOGGER.debug("Circuit fetch failed: %s", result)
                    continue
                # Independent audit finding (2026-09, fourth round, HVC-002):
                # nothing enforced that circuit paths are unique within a
                # plant. Two entries sharing a path (a genuine upstream data
                # error, not something ever observed live) would silently
                # overwrite each other here with no warning — the physical/
                # logical identity of one circuit could vanish in favor of
                # the other's state, and since entity unique_ids are built
                # from (plant_id, path), the platform setup would also
                # receive duplicate identities. Deterministic and visible:
                # first-seen wins (checked and skipped before contributing
                # anything, including has_error, to plant_data), logged
                # loudly so a real occurrence is never silent.
                if result.path in plant_data.circuits:
                    _LOGGER.warning(
                        "Plant %s reported two circuits with the same path %r; "
                        "keeping the first one seen and discarding the duplicate. "
                        "This indicates an upstream data problem, not normal operation.",
                        plant_id,
                        result.path,
                    )
                    continue
                if result.has_error:
                    plant_data.has_error = True
                plant_data.circuits[result.path] = result

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

        # Clear optimistic MODE overrides only after a SUCCESSFUL fetch so that
        # if the refresh fails (API timeout, transient error), entities continue
        # to show their optimistic state rather than snapping back to stale data
        # mid-cycle.  Fresh coordinator data takes over on the next good refresh.
        # Weather-impact overrides are deliberately not cleared here — they
        # expire via TTL only (see __init__ comment on _weather_impact_override).
        #
        # Only clear overrides set BEFORE this fetch started (independent
        # audit finding, 2026-09, HVC-002): an unconditional .clear() here
        # would also wipe an override for a DIFFERENT circuit set by a write
        # that landed WHILE this fetch was already in flight. This fetch's
        # `data` snapshot was taken before that write happened, so `data`
        # itself doesn't reflect it either — clearing that override would
        # make the entity flash back to stale (pre-write) state until the
        # next successful refresh, rather than keeping the correct
        # optimistic value in the meantime. Keeping any override timestamped
        # at or after fetch_started_at leaves it in place for exactly that
        # case; it will be cleared by whichever refresh's fetch actually
        # starts after that write.
        self._mode_override = {
            path: entry
            for path, entry in self._mode_override.items()
            if entry[1] >= fetch_started_at
        }

        return data
