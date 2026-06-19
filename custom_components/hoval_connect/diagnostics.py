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

    Structured in three top-level sections:

    config_entry
        Redacted copy of the stored config (email/password removed).

    coordinator_data
        Full coordinator snapshot: plants, circuits, live values, events,
        weather. All plant IDs, names, and source paths are redacted.

    connection_health
        API connection quality snapshot including:
        - Last success / last error timestamps and details
        - Cumulative counters since HA startup (polls, failures, auth errors)
        - Rolling 1-hour window: failure rate %, auth failure rate %,
          availability % — the three most actionable metrics for automations
        - Latency statistics: last poll, rolling average, p95
    """
    coordinator = entry.runtime_data.coordinator

    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "coordinator_data": async_redact_data(asdict(coordinator.data), REDACT_COORDINATOR),
        "connection_health": coordinator.connection_health.as_diagnostic_dict(),
    }
