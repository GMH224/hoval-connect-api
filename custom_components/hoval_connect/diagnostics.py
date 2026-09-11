"""Diagnostics support for Hoval Connect."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HovalConnectConfigEntry

REDACT_CONFIG = {"password", "email"}
# Independent audit finding (2026-09, "more" report, finding #9): these key
# names are only ever redacted by async_redact_data() when they appear as
# DICTIONARY KEYS inside the structure it's given — it does not rename or
# obscure a dict key that merely contains an identifier as its own literal
# value (e.g. coordinator.data.plants is `dict[plant_id_string,
# HovalPlantData]` — the plant ID is the *key* of that dict, not a field
# named "plant_id" inside a value, so async_redact_data alone never
# touches it, no matter what's in this set). This set still correctly
# redacts the *field-level* occurrences (HovalPlantData.plant_id,
# HovalCircuitData.name, etc.) — kept for that purpose — but the real
# structural fix is building an explicit, pre-anonymised representation
# in _anonymise_coordinator_data() below rather than relying on this set
# alone. "source_path" (in the previous version of this set) was dead
# weight: a leftover from HovalEventData, which v1.0.0 removed entirely —
# the field that actually needed redacting was HovalCircuitData.path,
# which is now redacted explicitly below instead.
REDACT_COORDINATOR = {
    "token",
    "id_token",
    "plant_access_token",
    "plant_id",
    "plantExternalId",
    "name",
    "description",
}


def _redact_identifiers_in_text(text: str | None, identifiers: list[str]) -> str | None:
    """Replace any known plant ID / circuit path substring inside free text.

    Independent audit finding (2026-09, "more" report, finding #9):
    connection_health's last-error message is a free-form string that can
    embed a circuit path or plant ID directly (several _LOGGER call sites
    in coordinator.py/api.py format one into their message). Previously
    this whole section bypassed async_redact_data() entirely.
    async_redact_data() itself cannot help here regardless — it redacts
    dict keys/values, not substrings inside a larger string — so this
    does a plain substring replacement using this specific installation's
    actual identifiers instead.
    """
    if not text:
        return text
    for identifier in identifiers:
        if identifier:
            text = text.replace(identifier, "**REDACTED**")
    return text


def _anonymise_coordinator_data(data: Any) -> dict[str, Any]:
    """Build an explicit diagnostics-safe snapshot of coordinator.data.

    Independent audit finding (2026-09, "more" report, finding #9):
    `async_redact_data(asdict(coordinator.data), REDACT_COORDINATOR)` alone
    left three things unredacted despite this module's own docstring
    claiming otherwise: plant IDs used as `plants` dict KEYS, circuit
    paths used as `circuits` dict KEYS, and `HovalCircuitData.path` (a
    field — the old REDACT_COORDINATOR set redacted a stale, no-longer-
    existent "source_path" key instead of the real "path" field). All
    three are handled explicitly here: both mapping levels are rebuilt
    with indexed placeholder keys ("plant_1", "circuit_1", ...) instead of
    the real identifiers, and `path` is overwritten after the fact. Field
    values that were already correctly redacted by REDACT_COORDINATOR
    (plant_id, name, description, tokens) keep going through
    async_redact_data as before — this only fixes what that call could
    never have covered by construction.
    """
    result: dict[str, Any] = {"plants": {}}
    for plant_index, plant in enumerate(data.plants.values(), start=1):
        plant_dict = async_redact_data(asdict(plant), REDACT_COORDINATOR)
        circuits: dict[str, Any] = {}
        for circuit_index, circuit in enumerate(plant.circuits.values(), start=1):
            circuit_dict = async_redact_data(asdict(circuit), REDACT_COORDINATOR)
            circuit_dict["path"] = "**REDACTED**"
            circuits[f"circuit_{circuit_index}"] = circuit_dict
        plant_dict["circuits"] = circuits
        result["plants"][f"plant_{plant_index}"] = plant_dict
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HovalConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Structured in three top-level sections:

    config_entry
        Redacted copy of the stored config (email/password removed).

    coordinator_data
        Full coordinator snapshot: plants and circuits. Plant IDs and
        circuit paths are replaced with indexed placeholders as mapping
        keys (plant_1, circuit_1, ...) in addition to the usual field-level
        redaction (name, description, tokens) — see
        _anonymise_coordinator_data()'s docstring for why both are needed.

    connection_health
        API connection quality snapshot including:
        - Last success / last error timestamps and details (the error
          message text has any of this installation's own plant IDs or
          circuit paths substituted out — see
          _redact_identifiers_in_text()'s docstring)
        - Cumulative counters since HA startup (polls, failures, auth errors)
        - Rolling 1-hour window: failure rate %, auth failure rate %,
          availability % — the three most actionable metrics for automations
        - Latency statistics: last poll, rolling average, p95
    """
    coordinator = entry.runtime_data.coordinator

    identifiers = list(coordinator.data.plants.keys()) + [
        path for plant in coordinator.data.plants.values() for path in plant.circuits
    ]

    connection_health = coordinator.connection_health.as_diagnostic_dict()
    last_error = connection_health.get("last_error")
    if isinstance(last_error, dict) and isinstance(last_error.get("message"), str):
        last_error["message"] = _redact_identifiers_in_text(last_error["message"], identifiers)

    return {
        "config_entry": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "coordinator_data": _anonymise_coordinator_data(coordinator.data),
        "connection_health": connection_health,
    }
