"""Tests for select.py's program display-name resolution.

Independent audit finding (2026-09, "more" report, finding #10):
resolve_program_display_names() was extracted from HovalProgramSelect as a
standalone pure function specifically so this disambiguation algorithm is
directly unit-testable without needing to construct a full entity/
coordinator/config-entry stack.
"""

from __future__ import annotations

from custom_components.hoval_connect.select import (
    DEFAULT_NAMES,
    resolve_program_display_names,
)


class TestResolveProgramDisplayNames:
    def test_no_custom_names_uses_defaults(self):
        result = resolve_program_display_names({})
        assert result["week1"] == DEFAULT_NAMES["week1"]
        assert result["week2"] == DEFAULT_NAMES["week2"]

    def test_custom_unique_names_pass_through_unchanged(self):
        result = resolve_program_display_names({"week1": "Winter", "week2": "Summer"})
        assert result["week1"] == "Winter"
        assert result["week2"] == "Summer"

    def test_duplicate_custom_names_are_disambiguated(self):
        """The core regression case: both weeks named identically."""
        result = resolve_program_display_names({"week1": "Standard", "week2": "Standard"})
        assert result["week1"] != result["week2"]
        assert "week1" in result["week1"]
        assert "week2" in result["week2"]

    def test_disambiguated_names_still_contain_the_original_name(self):
        result = resolve_program_display_names({"week1": "Standard", "week2": "Standard"})
        assert result["week1"].startswith("Standard")
        assert result["week2"].startswith("Standard")

    def test_duplicate_with_a_default_name_is_also_disambiguated(self):
        """A custom name that happens to collide with another key's
        DEFAULT (unrenamed) name must be caught too, not just two
        explicitly-renamed keys colliding with each other.
        """
        result = resolve_program_display_names({"week1": DEFAULT_NAMES["week2"]})
        assert result["week1"] != result["week2"]

    def test_all_keys_present_in_result(self):
        from custom_components.hoval_connect.select import API_PROGRAMS

        result = resolve_program_display_names({"week1": "Custom"})
        assert set(result.keys()) == set(API_PROGRAMS)

    def test_non_colliding_names_are_unaffected_by_an_unrelated_collision(self):
        """Only the ACTUALLY colliding pair should be disambiguated —
        every other key's name stays exactly as it was.
        """
        result = resolve_program_display_names({"week1": "Same", "week2": "Same"})
        assert result["ecoMode"] == DEFAULT_NAMES["ecoMode"]
        assert result["standby"] == DEFAULT_NAMES["standby"]
        assert result["constant"] == DEFAULT_NAMES["constant"]

    def test_three_way_collision_all_disambiguated(self):
        result = resolve_program_display_names({"week1": "X", "week2": "X", "ecoMode": "X"})
        names = {result["week1"], result["week2"], result["ecoMode"]}
        assert len(names) == 3  # all three distinct after disambiguation

    def test_result_names_are_unique_even_with_full_collision(self):
        """Sanity invariant: however the input is shaped, the OUTPUT must
        never contain a duplicate display name — that's the entire point.
        """
        result = resolve_program_display_names(
            {key: "SameName" for key in ("week1", "week2", "ecoMode", "standby", "constant")}
        )
        assert len(set(result.values())) == len(result)

    def test_never_produces_an_entry_for_manual_or_externalconstant(self):
        """Independent audit finding (2026-09, fourth round, HVC-015): the
        API permits activeProgram values ("manual", "externalConstant")
        that were never part of the selectable API_PROGRAMS list.
        select.py's current_option relies on checking membership in this
        function's OUTPUT (`circuit.active_program not in display_names`)
        to decide whether to report None instead of an unselectable raw
        value — this confirms the foundation that check depends on: no
        input can ever make either of those keys appear in the result,
        regardless of what program_names contains.
        """
        result = resolve_program_display_names(
            {"manual": "Some Name", "externalConstant": "Another Name"}
        )
        assert "manual" not in result
        assert "externalConstant" not in result
