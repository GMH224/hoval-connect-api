"""Diagnostics support for Hoval Connect."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HovalConnectConfigEntry

REDACT_CONFIG = {"password", "email"}
REDACT_COORDINATOR = {
    "token",
    "id_token",
    "plant_access_token",
    "plant_id",
    "plantExternalId",
    "name",
    "description",
    "source_path",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HovalConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Includes the full coordinator data snapshot (plant/circuit/event/weather)
    plus a connection_health section with all telemetry fields.  The raw
    _poll_records rolling window is omitted (it contains only monotonic
    timestamps which are meaningless outside the running process); derived
    metrics (failure rates, P95 latency) are included instead.
    """
    coordinator = entry.runtime_data.coordinator
    h = coordinator.connection_health

    connection_health: dict[str, Any] = {
        # Timestamps (ISO-8601 strings for readability in the diagnostics JSON)
        "last_success": h.last_success.isoformat() if h.last_success else None,
        "last_error_time": h.last_error_time.isoformat() if h.last_error_time else None,
        # Last error details
        "last_error_msg": h.last_error_msg,
        "last_error_type": h.last_error_type,
        # Counters
        "consecutive_failures": h.consecutive_failures,
        "total_failures": h.total_failures,
        "total_polls": h.total_polls,
        "auth_failures": h.auth_failures,
        # Partial / sub-task failures
        "partial_failures_last_poll": h.partial_failures_last_poll,
        "total_partial_failures": h.total_partial_failures,
        "partial_failure_endpoints": h.partial_failure_endpoints,
        # Performance
        "poll_latency_ms": h.poll_latency_ms,
        "p95_latency_ms_1h": h.p95_latency_ms_1h,
        # Rolling-window rates (computed on read from _poll_records)
        "failure_rate_pct_1h": h.failure_rate_pct_1h,
        "auth_failure_rate_pct_1h": h.auth_failure_rate_pct_1h,
        # Circuit-breaker state
        "circuit_breaker_open": coordinator.circuit_breaker_open,
        # Window sample size (useful for gauging how much history is behind rates)
        "poll_records_in_1h_window": len(h._window()),
    }

    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "coordinator_data": async_redact_data(asdict(coordinator.data), REDACT_COORDINATOR),
        "connection_health": connection_health,
    }
