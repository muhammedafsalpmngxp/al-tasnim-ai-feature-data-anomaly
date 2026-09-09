"""Tests for `app/sentinel/business_rules_index.py` -- the parser that expands a
finding's terse `business_rule_ref` (e.g. "§4, §7") into the actual rule title/text from
`docs/BUSINESS_RULES.md`, so a report never shows a bare section number a reader has to go
look up elsewhere. Reads the real file (no live DB or API key needed) -- this is
deliberately NOT mocked, since the whole point is that the report and the LLM prompt read
the identical source and can never drift apart.
"""
from __future__ import annotations

from app.sentinel.business_rules_index import cites, describe, referenced_sections, sections


def test_all_twelve_numbered_sections_are_found():
    secs = sections()
    assert set(secs.keys()) == {str(n) for n in range(1, 13)}


def test_section_four_is_milestone_deadlines_with_nonempty_body():
    secs = sections()
    assert secs["4"].title == "Milestone deadlines"
    assert len(secs["4"].text) > 100
    assert "pegged_date" in secs["4"].text


def test_describe_expands_a_comma_separated_ref():
    out = describe("§4, §7")
    assert "§4 Milestone deadlines" in out
    assert "§7 Lifecycle order" in out


def test_describe_expands_a_slash_separated_ref():
    """config/column_semantics.yaml's date_order entries use '§4/§5', a different
    separator than business_rules.py's '§4, §7' -- both must parse.
    """
    out = describe("§4/§5")
    assert "§4 Milestone deadlines" in out
    assert "§5" in out


def test_describe_of_none_or_empty_is_empty_string():
    assert describe(None) == ""
    assert describe("") == ""


def test_describe_passes_through_an_unknown_section_number_unchanged():
    assert describe("§99") == "§99"


def test_cites_matches_a_section_within_a_combined_ref():
    assert cites("§7, §8", "8") is True
    assert cites("§7, §8", "9") is False
    assert cites(None, "7") is False


def test_referenced_sections_deduplicates_and_orders_by_number():
    refs = ["§7, §8", "§4, §7", None, "§9", "not-a-real-ref"]
    result = referenced_sections(refs)
    assert [s.number for s in result] == ["4", "7", "8", "9"]


def test_referenced_sections_of_no_refs_is_empty():
    assert referenced_sections([None, "", None]) == []
