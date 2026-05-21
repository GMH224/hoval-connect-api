"""Rolling-window API communication statistics for Hoval Connect.

``HovalApiStats`` is a lightweight, asyncio-safe statistics collector that
records every outbound HTTP request and its outcome.  It maintains four
monotonic-timestamp deques (calls, timeouts, errors, retries) that act as a
1-hour sliding window, so all rate metrics automatically reflect the most recent
60 minutes without any external reset.

Lifetime counters (total_*) are also kept for the full HA session lifetime.

Design notes
------------
* **Asyncio-safe** — all state is mutated only on the HA event loop thread
  (the asyncio thread that drives all coordinator and API calls).  No locks
  needed.
* **No external dependencies** — uses only stdlib (collections.deque,
  datetime, time).
* **Monotonic pruning** — window boundaries use ``time.monotonic()`` so the
  deques are pruned in O(k) where k is the number of expired entries, not O(n).
* **Wall-clock last-event times** — ``last_success_time`` and
  ``last_error_time`` are UTC ``datetime`` objects so HA's TIMESTAMP sensor
  can render them directly.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import time
from typing import Any


# Rolling window length in seconds (1 hour).
_WINDOW_SECONDS: int = 3600


class HovalApiStats:
    """Rolling-window collector for Hoval Connect API communication metrics.

    Attributes exposed for HA sensors
    ----------------------------------
    calls_last_hour         int   — HTTP requests made in last 60 min
    timeouts_last_hour      int   — requests that timed out in last 60 min
    errors_last_hour        int   — requests that ended in any error in last 60 min
    retries_last_hour       int   — retry attempts triggered in last 60 min
    failure_ratio_last_hour float — errors / calls × 100 (%) in last 60 min; 0 if no calls
    total_calls             int   — all HTTP requests since HA started
    total_timeouts          int   — all timeouts since HA started
    total_errors            int   — all errors since HA started
    total_retries           int   — all retries since HA started
    last_success_time       datetime | None — UTC time of last successful response
    last_error_time         datetime | None — UTC time of last error
    last_error_message      str | None      — description of last error
    """

    def __init__(self) -> None:
        """Initialise all counters and queues to zero / empty."""
        # Rolling event queues — each entry is a monotonic timestamp (float)
        self._calls: deque[float] = deque()
        self._timeouts: deque[float] = deque()
        self._errors: deque[float] = deque()
        self._retries: deque[float] = deque()

        # Lifetime counters (never reset, survive backoff / reconnect cycles)
        self.total_calls: int = 0
        self.total_timeouts: int = 0
        self.total_errors: int = 0
        self.total_retries: int = 0

        # Most-recent event metadata (wall-clock UTC for HA timestamp sensors)
        self.last_success_time: datetime | None = None
        self.last_error_time: datetime | None = None
        self.last_error_message: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Remove entries older than _WINDOW_SECONDS from all queues.

        Each queue is sorted by insertion time (monotonically increasing), so
        we only need to pop from the left until we reach a fresh entry.
        """
        cutoff = time.monotonic() - _WINDOW_SECONDS
        for q in (self._calls, self._timeouts, self._errors, self._retries):
            while q and q[0] < cutoff:
                q.popleft()

    # ------------------------------------------------------------------
    # Recording methods — called from api.py on every HTTP event
    # ------------------------------------------------------------------

    def record_call(self) -> None:
        """Record one outbound HTTP request (before the response is received)."""
        now = time.monotonic()
        self._calls.append(now)
        self.total_calls += 1

    def record_timeout(self) -> None:
        """Record a timeout on a single HTTP attempt.

        Called for every ``TimeoutError`` catch, including intermediate retries.
        The total count therefore reflects individual timed-out attempts, not
        just final failures — giving a more accurate picture of network quality.
        """
        now = time.monotonic()
        self._timeouts.append(now)
        self.total_timeouts += 1

    def record_retry(self) -> None:
        """Record one retry attempt (called before the sleep, after the failure)."""
        now = time.monotonic()
        self._retries.append(now)
        self.total_retries += 1

    def record_error(self, message: str) -> None:
        """Record a terminal request failure (all retries exhausted or non-retryable error).

        Args:
            message: Human-readable error description stored as ``last_error_message``.
        """
        now_mono = time.monotonic()
        self._errors.append(now_mono)
        self.total_errors += 1
        self.last_error_time = datetime.now(timezone.utc)
        self.last_error_message = message

    def record_success(self) -> None:
        """Record a successful HTTP response.

        Updates ``last_success_time`` to the current UTC wall-clock time.
        Does NOT add an entry to ``_calls`` — that is done by ``record_call``
        at the start of each request.
        """
        self.last_success_time = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Computed properties — read by HA sensor entities
    # ------------------------------------------------------------------

    @property
    def calls_last_hour(self) -> int:
        """Number of outbound HTTP requests in the rolling 60-minute window."""
        self._prune()
        return len(self._calls)

    @property
    def timeouts_last_hour(self) -> int:
        """Number of timed-out HTTP attempts in the rolling 60-minute window."""
        self._prune()
        return len(self._timeouts)

    @property
    def errors_last_hour(self) -> int:
        """Number of terminal request failures in the rolling 60-minute window."""
        self._prune()
        return len(self._errors)

    @property
    def retries_last_hour(self) -> int:
        """Number of retry attempts in the rolling 60-minute window."""
        self._prune()
        return len(self._retries)

    @property
    def failure_ratio_last_hour(self) -> float:
        """Error rate as a percentage of calls in the rolling 60-minute window.

        Returns ``0.0`` when no calls have been made in the window (avoids
        division-by-zero and prevents the sensor showing 100% on startup).
        Rounded to one decimal place.
        """
        self._prune()
        calls = len(self._calls)
        if calls == 0:
            return 0.0
        return round(len(self._errors) / calls * 100.0, 1)

    def as_dict(self) -> dict[str, Any]:
        """Return all metrics as a plain dict (used by diagnostics platform)."""
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
