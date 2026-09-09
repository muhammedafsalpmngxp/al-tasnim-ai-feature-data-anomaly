"""Tests for the pure request/response translation in `app/api/routes.py`.

`FindingsStore` rows come back with `evidence` / `finding_ids` as raw JSON TEXT (SQLite
has no array/object column type) -- confirmed the hard way during the LLM citation-
validator work earlier in this project. `_finding_out`/`_incident_out` are what decode
those columns for the API response; this is the one piece of real logic in routes.py
that isn't just a passthrough to an already-tested store method, so it's what gets
tested here. No live database needed.
"""
from __future__ import annotations

from app.api.routes import _finding_out, _incident_out


def _finding_row(**overrides):
    row = {
        "finding_id": 1, "run_id": "run_x", "check_id": "BIZ-101", "family": "BIZ",
        "severity": "high", "finding_class": "violation", "title": "t",
        "entity_type": "well", "entity_id": "36754", "entity_label": "36754",
        "well_id": 36754, "affected_count": 3, "grain": "row", "baseline": "none",
        "business_rule_ref": "§7", "owner": None, "why_it_matters": "because",
        "evidence": '[{"count": 3}]', "sampled": 0,
        "llm_explanation": None, "llm_root_cause": None, "llm_remediation": None,
        "status": "new",
    }
    row.update(overrides)
    return row


def test_evidence_json_string_is_decoded_to_a_list():
    out = _finding_out(_finding_row())
    assert out.evidence == [{"count": 3}]


def test_null_evidence_becomes_an_empty_list():
    out = _finding_out(_finding_row(evidence=None))
    assert out.evidence == []


def test_malformed_evidence_json_does_not_crash_the_response():
    out = _finding_out(_finding_row(evidence="{not valid json"))
    assert out.evidence == []


def test_sampled_integer_flag_becomes_a_real_bool():
    out = _finding_out(_finding_row(sampled=1))
    assert out.sampled is True


def _incident_row(**overrides):
    row = {
        "incident_id": 1, "run_id": "run_x", "title": "t", "root_cause": "rc",
        "severity": "high", "finding_ids": '["BIZ-101", "BIZ-102"]', "llm_narrative": "n",
    }
    row.update(overrides)
    return row


def test_incident_finding_ids_json_string_is_decoded_to_a_list():
    out = _incident_out(_incident_row())
    assert out.finding_ids == ["BIZ-101", "BIZ-102"]


def test_incident_null_finding_ids_becomes_an_empty_list():
    out = _incident_out(_incident_row(finding_ids=None))
    assert out.finding_ids == []
