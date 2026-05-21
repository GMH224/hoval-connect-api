"""Diagnostics support for Hoval Connect.

The diagnostics payload includes:

* ``config_entry``       — integration configuration (credentials redacted).
* ``coordinator_data``   — latest polled data snapshot (plant IDs and names
                           redacted for privacy).
* ``api_stats``          — rolling-window and lifetime API communication metrics
                           from ``HovalApiStats``.  Useful for diagnosing
                           connectivity issues without needing HA logs.
* ``coordinator_health`` — current poll interval (reflects adaptive backoff)
                           and consecutive failure count.
"""

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

    Includes coordinator data, API statistics, and current health metrics.
    Sensitive fields (credentials, plant IDs, names) are redacted.
    """
    coordinator = entry.runtime_data.coordinator
    stats = entry.runtime_data.stats

    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "coordinator_data": async_redact_data(asdict(coordinator.data), REDACT_COORDINATOR),
        "api_stats": stats.as_dict(),
        "coordinator_health": {
            "consecutive_failures": coordinator._consecutive_failures,
            "current_poll_interval_seconds": int(coordinator.update_interval.total_seconds()),
            "base_poll_interval_seconds": int(coordinator._base_update_interval.total_seconds()),
        },
    }
