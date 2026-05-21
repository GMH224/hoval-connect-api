"""Rolling-window API communication statistics for Hoval Connect.

This module is intentionally free of Home Assistant imports so it can be unit-
tested in isolation and imported early in the startup sequence without pulling
in HA framework machinery.

Design
------
``HovalApiStats`` maintains four ``collections.deque`` structures, one per event
type (calls, timeouts, errors, retries).  Each entry is a ``time.monotonic()``
timestamp so the 1-hour sliding window can be pruned in O(k) time (k = expired
entries) without a background timer.

All mutation happens on the HA event-loop thread (the same thread that drives
all coordinator and API calls), so no locks are needed.

Wall-clock timestamps (``last_success_time``, ``last_error_time``) are stored as
timezone-aware UTC ``datetime`` objects so the TIMESTAMP sensor device class can
render them directly without any additional parsing.

Usage
-----
Create one instance per integration entry and pass it to ``HovalConnectApi``::

    stats = HovalApiStats()
    api   = HovalConnectApi(session, email, password, stats=stats)

The ``HovalConnectApi`` calls the ``record_*`` methods at every HTTP event.
Sensor entities read the computed properties (e.g. ``stats.calls_last_hour``).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import time
from typing import Any


# Length of the sliding measurement window in seconds (1 hour).
STATS_WINDOW_SECONDS: int = 3600


class HovalApiStats:
    """Rolling-window collector for Hoval Connect API communication metrics.

    All *_last_hour properties reflect activity in the most recent 60 minutes.
    All total_* attributes accumulate for the lifetime of the HA session (i.e.
    they reset when HA restarts, not when the Hoval cloud recovers).

    Properties (used by sensor entities)
    -------------------------------------
    calls_last_hour         int   — HTTP requests made in the last 60 min
    timeouts_last_hour      int   — requests that timed out in the last 60 min
    errors_last_hour        int   — terminal failures in the last 60 min
    retries_last_hour       int   — retry attempts in the last 60 min
    failure_ratio_last_hour float — errors ÷ calls × 100 (%), 0.0 when no calls

    Lifetime counters (never reset within a session)
    -------------------------------------------------
    total_calls    int
    total_timeouts int
    total_errors   int
    total_retries  int

    Last-event metadata
    -------------------
    last_success_time   datetime | None  — UTC time of last successful response
    last_error_time     datetime | None  — UTC time of last terminal error
    last_error_message  str | None       — description of most recent error
    """

    def __init__(self) -> None:
        """Initialise all counters and event queues to their zero/empty state."""
        # Sliding-window event queues — each entry is a monotonic clock value.
        self._calls: deque[float] = deque()
        self._timeouts: deque[float] = deque()
        self._errors: deque[float] = deque()
        self._retries: deque[float] = deque()

        # Lifetime counters — accumulate for the full HA session.
        self.total_calls: int = 0
        self.total_timeouts: int = 0
        self.total_errors: int = 0
        self.total_retries: int = 0

        # Most-recent event metadata — wall-clock UTC for TIMESTAMP sensors.
        self.last_success_time: datetime | None = None
        self.last_error_time: datetime | None = None
        self.last_error_message: str | None = None

        # Current poll interval in seconds — written by the coordinator after
        # every successful data fetch so the diagnostic sensor stays current.
        # Initialised to 0; the coordinator overwrites it on the first poll.
        self._poll_interval_seconds: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Discard entries older than STATS_WINDOW_SECONDS from all queues.

        Each queue is insertion-ordered (monotonically increasing), so we only
        need to pop from the left until we reach a fresh entry.  Called lazily
        at the start of every computed property read.
        """
        cutoff = time.monotonic() - STATS_WINDOW_SECONDS
        for q in (self._calls, self._timeouts, self._errors, self._retries):
            while q and q[0] < cutoff:
                q.popleft()

    # ------------------------------------------------------------------
    # Recording methods — called from api.py on every HTTP event
    # ------------------------------------------------------------------

    def record_call(self) -> None:
        """Record one outbound HTTP request attempt.

        Called at the start of every attempt inside the retry loop in
        ``_request``, ``_get_id_token``, and ``_get_plant_access_token``.
        """
        now = time.monotonic()
        self._calls.append(now)
        self.total_calls += 1

    def record_timeout(self) -> None:
        """Record a single timed-out HTTP attempt.

        Called on every ``TimeoutError`` catch, including intermediate retries,
        so the count reflects individual timed-out attempts rather than just
        final failures.  This gives a more accurate picture of network quality.
        """
        now = time.monotonic()
        self._timeouts.append(now)
        self.total_timeouts += 1

    def record_retry(self) -> None:
        """Record one retry attempt, called just before the back-off sleep."""
        now = time.monotonic()
        self._retries.append(now)
        self.total_retries += 1

    def record_error(self, message: str) -> None:
        """Record a terminal request failure (all retries exhausted or non-retryable).

        Args:
            message: Human-readable description stored as ``last_error_message``.
        """
        now_mono = time.monotonic()
        self._errors.append(now_mono)
        self.total_errors += 1
        self.last_error_time = datetime.now(timezone.utc)
        self.last_error_message = message

    def record_success(self) -> None:
        """Record a successful HTTP response.

        Only updates ``last_success_time``; does not add to ``_calls`` because
        ``record_call`` is already called at the start of each attempt.
        """
        self.last_success_time = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Computed properties — read by sensor entities
    # ------------------------------------------------------------------

    @property
    def calls_last_hour(self) -> int:
        """Number of outbound HTTP request attempts in the last 60 minutes."""
        self._prune()
        return len(self._calls)

    @property
    def timeouts_last_hour(self) -> int:
        """Number of timed-out HTTP attempts in the last 60 minutes."""
        self._prune()
        return len(self._timeouts)

    @property
    def errors_last_hour(self) -> int:
        """Number of terminal request failures in the last 60 minutes."""
        self._prune()
        return len(self._errors)

    @property
    def retries_last_hour(self) -> int:
        """Number of retry attempts in the last 60 minutes."""
        self._prune()
        return len(self._retries)

    @property
    def failure_ratio_last_hour(self) -> float:
        """Error rate as a percentage of calls in the last 60 minutes.

        Returns ``0.0`` when no calls have been made in the window (avoids
        division-by-zero and prevents the sensor showing 100% on first startup).
        Value is rounded to one decimal place.
        """
        self._prune()
        calls = len(self._calls)
        if calls == 0:
            return 0.0
        return round(len(self._errors) / calls * 100.0, 1)

    def as_dict(self) -> dict[str, Any]:
        """Return all metrics as a plain dict for the HA diagnostics platform."""
        return {
            "calls_last_hour": self.calls_last_hour,
            "timeouts_last_hour": self.timeouts_last_hour,
            "errors_last_hour": self.errors_last_hour,
            "retries_last_hour": self.retries_last_hour,
            "failure_ratio_last_hour": self.failure_ratio_last_hour,
            "total_calls": self.total_calls,
            "total_timeouts": self.total_timeouts,
            "total_errors": self.total_errors,
            "total_retries": self.total_retries,
            "last_success_time": (
                self.last_success_time.isoformat() if self.last_success_time else None
            ),
            "last_error_time": (
                self.last_error_time.isoformat() if self.last_error_time else None
            ),
            "last_error_message": self.last_error_message,
        }
