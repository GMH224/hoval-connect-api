"""Tests for the Hoval Connect diagnostics module.

Independent audit finding (2026-09, "more" report, finding #9): the
previous version of this file only checked that certain key NAMES were
present in the redaction sets — it never actually called
async_get_config_entry_diagnostics() and inspected the real output. That
gap is exactly how the underlying bug (plant IDs and circuit paths used as
DICTIONARY KEYS, which async_redact_data() never touches, plus the entire
connection_health section bypassing redaction altogether) went unnoticed:
the sets themselves looked reasonable, but the code built around them
didn't actually apply them to everything the module's own docstring
claimed. This file now drives the real function end-to-end with a real
(not mocked) `async_redact_data` implementation, matching Home Assistant's
actual documented behavior, so a future regression here would be caught by
running the suite rather than only by another manual audit.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

# Mock homeassistant modules
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)
sys.modules.setdefault("aiohttp", ha_mock)
# Deliberately NOT stubbing "voluptuous" here (unlike the file this
# replaces): diagnostics.py itself never imports it, and — found while
# fixing this exact issue — sys.modules.setdefault("voluptuous", ha_mock)
# would silently poison it for every OTHER test file's later `import
# voluptuous as vol` in the same pytest session whenever this file
# happens to be collected first (setdefault only sets when the key is
# absent, and nothing before this file was guaranteed to have already
# imported the real package). config_flow.py needs the REAL voluptuous
# to build its options-flow schema correctly; a stubbed one made its
# schema markers stop comparing equal to plain strings, which looked
# like an unrelated failure in a completely different test file.


def _real_async_redact_data(data: Any, to_redact: set[str]) -> Any:
    """A faithful, real stand-in for homeassistant.components.diagnostics.async_redact_data.

    Recursively walks dicts/lists; for any dict key matching `to_redact`,
    replaces its VALUE with a placeholder, leaving the key itself (and
    every other key/value) untouched. This is what real HA does — it does
    NOT rename or obscure a dict key just because that key's own string
    happens to be a sensitive identifier, which is precisely the gap this
    test file exists to catch.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key in to_redact:
                result[key] = "**REDACTED**"
            else:
                result[key] = _real_async_redact_data(value, to_redact)
        return result
    if isinstance(data, list):
        return [_real_async_redact_data(item, to_redact) for item in data]
    return data


_diagnostics_module = MagicMock()
_diagnostics_module.async_redact_data = _real_async_redact_data
sys.modules["homeassistant.components.diagnostics"] = _diagnostics_module

from custom_components.hoval_connect.diagnostics import (  # noqa: E402
    REDACT_CONFIG,
    REDACT_COORDINATOR,
    _anonymise_coordinator_data,
    _redact_identifiers_in_text,
    async_get_config_entry_diagnostics,
)

# ---------------------------------------------------------------------------
# Minimal stand-ins for the real dataclasses, avoiding a full coordinator import
# ---------------------------------------------------------------------------


@dataclass
class _FakeCircuit:
    circuit_type: str
    path: str
    name: str
    operation_mode: str | None = None
    active_program: str | None = None
    target_value: float | None = None
    has_error: bool = False


@dataclass
class _FakePlant:
    plant_id: str
    name: str
    is_online: bool = True
    has_error: bool = False
    circuits: dict[str, _FakeCircuit] = field(default_factory=dict)


@dataclass
class _FakeData:
    plants: dict[str, _FakePlant] = field(default_factory=dict)


class _FakeConnectionHealth:
    def __init__(self, error_message: str | None = None) -> None:
        self._error_message = error_message

    def as_diagnostic_dict(self) -> dict[str, Any]:
        return {
            "last_success": None,
            "last_successful_contact_at": None,
            "last_error": {"time": None, "type": "api", "message": self._error_message},
            "counters_since_startup": {"total_polls": 1},
        }


class _FakeCoordinator:
    def __init__(self, data: _FakeData, error_message: str | None = None) -> None:
        self.data = data
        self.connection_health = _FakeConnectionHealth(error_message)


class _FakeRuntimeData:
    def __init__(self, coordinator: _FakeCoordinator) -> None:
        self.coordinator = coordinator


class _FakeEntry:
    def __init__(self, coordinator: _FakeCoordinator, data: dict[str, Any]) -> None:
        self.runtime_data = _FakeRuntimeData(coordinator)
        self.data = data


def _make_entry(plant_id="604961729200133", circuit_path="1.1.0", error_message=None):
    circuit = _FakeCircuit(circuit_type="HK", path=circuit_path, name="Bodenheizung")
    plant = _FakePlant(plant_id=plant_id, name="My House", circuits={circuit_path: circuit})
    data = _FakeData(plants={plant_id: plant})
    coordinator = _FakeCoordinator(data, error_message=error_message)
    return _FakeEntry(coordinator, {"email": "user@example.com", "password": "hunter2"})


# ---------------------------------------------------------------------------
# Behavioral tests — the real gap the previous version of this file missed
# ---------------------------------------------------------------------------


class TestDiagnosticsActuallyRedactsMappingKeys:
    """Independent audit finding (2026-09, "more" report, finding #9)."""

    async def test_plant_id_does_not_appear_as_a_dict_key(self):
        entry = _make_entry(plant_id="604961729200133")
        result = await async_get_config_entry_diagnostics(None, entry)

        assert "604961729200133" not in result["coordinator_data"]["plants"]
        assert set(result["coordinator_data"]["plants"].keys()) == {"plant_1"}

    async def test_circuit_path_does_not_appear_as_a_dict_key(self):
        entry = _make_entry(circuit_path="1.1.0")
        result = await async_get_config_entry_diagnostics(None, entry)

        plant = result["coordinator_data"]["plants"]["plant_1"]
        assert "1.1.0" not in plant["circuits"]
        assert set(plant["circuits"].keys()) == {"circuit_1"}

    async def test_circuit_path_field_is_redacted(self):
        """HovalCircuitData.path — a FIELD, not just a dict key — must also
        not leak its real value into the exported diagnostics.
        """
        entry = _make_entry(circuit_path="1.1.0")
        result = await async_get_config_entry_diagnostics(None, entry)

        circuit = result["coordinator_data"]["plants"]["plant_1"]["circuits"]["circuit_1"]
        assert circuit["path"] != "1.1.0"

    async def test_plant_id_field_is_redacted(self):
        entry = _make_entry(plant_id="604961729200133")
        result = await async_get_config_entry_diagnostics(None, entry)

        plant = result["coordinator_data"]["plants"]["plant_1"]
        assert plant["plant_id"] != "604961729200133"

    async def test_name_and_description_fields_are_redacted(self):
        entry = _make_entry()
        result = await async_get_config_entry_diagnostics(None, entry)

        plant = result["coordinator_data"]["plants"]["plant_1"]
        assert plant["name"] != "My House"
        circuit = plant["circuits"]["circuit_1"]
        assert circuit["name"] != "Bodenheizung"

    async def test_non_identifying_fields_survive_unredacted(self):
        """Sanity check: redaction shouldn't be so aggressive that ordinary,
        non-identifying diagnostic fields disappear too.
        """
        entry = _make_entry()
        result = await async_get_config_entry_diagnostics(None, entry)

        circuit = result["coordinator_data"]["plants"]["plant_1"]["circuits"]["circuit_1"]
        assert circuit["circuit_type"] == "HK"
        assert circuit["has_error"] is False


class TestConnectionHealthRedaction:
    """Independent audit finding (2026-09, "more" report, finding #9): the
    connection_health section used to bypass redaction entirely.
    """

    async def test_error_message_with_embedded_plant_id_is_redacted(self):
        entry = _make_entry(
            plant_id="604961729200133",
            error_message="Circuits endpoint failed for plant 604961729200133: timeout",
        )
        result = await async_get_config_entry_diagnostics(None, entry)

        message = result["connection_health"]["last_error"]["message"]
        assert "604961729200133" not in message

    async def test_error_message_with_embedded_circuit_path_is_redacted(self):
        entry = _make_entry(
            circuit_path="1.1.0",
            error_message="Circuit settings not available for 1.1.0: down",
        )
        result = await async_get_config_entry_diagnostics(None, entry)

        message = result["connection_health"]["last_error"]["message"]
        assert "1.1.0" not in message

    async def test_error_message_without_identifiers_survives_unmodified(self):
        entry = _make_entry(error_message="Request timeout after 2 attempts")
        result = await async_get_config_entry_diagnostics(None, entry)

        assert result["connection_health"]["last_error"]["message"] == (
            "Request timeout after 2 attempts"
        )

    async def test_no_error_message_does_not_crash(self):
        entry = _make_entry(error_message=None)
        result = await async_get_config_entry_diagnostics(None, entry)

        assert result["connection_health"]["last_error"]["message"] is None

    async def test_other_connection_health_fields_still_present(self):
        """Redaction must not swallow the rest of the connection_health
        section — only the message text is touched.
        """
        entry = _make_entry()
        result = await async_get_config_entry_diagnostics(None, entry)

        assert "counters_since_startup" in result["connection_health"]


class TestConfigEntryRedaction:
    async def test_password_and_email_redacted(self):
        entry = _make_entry()
        result = await async_get_config_entry_diagnostics(None, entry)

        assert result["config_entry"]["password"] == "**REDACTED**"
        assert result["config_entry"]["email"] == "**REDACTED**"


class TestRedactIdentifiersInText:
    """Unit tests for the standalone helper, independent of the full flow."""

    def test_redacts_single_identifier(self):
        assert (
            _redact_identifiers_in_text("plant 123 failed", ["123"]) == "plant **REDACTED** failed"
        )

    def test_redacts_multiple_identifiers(self):
        text = "circuit 1.1.0 on plant 999 failed"
        result = _redact_identifiers_in_text(text, ["999", "1.1.0"])
        assert "999" not in result
        assert "1.1.0" not in result

    def test_none_input_returns_none(self):
        assert _redact_identifiers_in_text(None, ["123"]) is None

    def test_empty_string_returns_empty_string(self):
        assert _redact_identifiers_in_text("", ["123"]) == ""

    def test_no_matching_identifiers_leaves_text_unchanged(self):
        assert _redact_identifiers_in_text("generic error", ["123"]) == "generic error"


class TestAnonymiseCoordinatorData:
    """Unit tests for the standalone helper, independent of the full flow."""

    def test_multiple_plants_get_distinct_indices(self):
        data = _FakeData(
            plants={
                "p1": _FakePlant(plant_id="p1", name="A"),
                "p2": _FakePlant(plant_id="p2", name="B"),
            }
        )
        result = _anonymise_coordinator_data(data)
        assert set(result["plants"].keys()) == {"plant_1", "plant_2"}

    def test_multiple_circuits_get_distinct_indices(self):
        plant = _FakePlant(
            plant_id="p1",
            name="A",
            circuits={
                "c1": _FakeCircuit(circuit_type="HK", path="c1", name="Heating"),
                "c2": _FakeCircuit(circuit_type="WW", path="c2", name="Hot water"),
            },
        )
        data = _FakeData(plants={"p1": plant})
        result = _anonymise_coordinator_data(data)
        circuits = result["plants"]["plant_1"]["circuits"]
        assert set(circuits.keys()) == {"circuit_1", "circuit_2"}

    def test_empty_plants_produces_empty_result(self):
        result = _anonymise_coordinator_data(_FakeData(plants={}))
        assert result == {"plants": {}}


class TestRedactionSets:
    """Retained from the previous version: the sets themselves are still a
    real, if incomplete-by-construction, part of the fix (field-level
    redaction). Kept as a quick sanity guard, now alongside the behavioral
    tests above rather than instead of them.
    """

    def test_config_redacts_credentials(self):
        assert "password" in REDACT_CONFIG
        assert "email" in REDACT_CONFIG

    def test_coordinator_redacts_tokens(self):
        assert "token" in REDACT_COORDINATOR
        assert "id_token" in REDACT_COORDINATOR
        assert "plant_access_token" in REDACT_COORDINATOR

    def test_coordinator_redacts_plant_ids(self):
        assert "plant_id" in REDACT_COORDINATOR
        assert "plantExternalId" in REDACT_COORDINATOR

    def test_coordinator_redacts_pii(self):
        assert "name" in REDACT_COORDINATOR
        assert "description" in REDACT_COORDINATOR
