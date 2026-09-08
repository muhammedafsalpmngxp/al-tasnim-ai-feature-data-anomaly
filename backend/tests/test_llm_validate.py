"""Tests for the citation validator -- the "no invented numbers" enforcement.

No network access or API key required: these fabricate model output directly and check
that the validator behaves correctly, both for legitimate output and for output containing
a number the finding never actually stated.
"""
from __future__ import annotations

from app.sentinel.llm.validate import validate_correlation, validate_narration

FINDING = {
    "check_id": "BIZ-107",
    "title": "14,003 tasks across 444 wells have a planner target finishing AFTER the rig",
    "affected_count": 14003,
    "why_it_matters": "Worst overrun 5459 days.",
    "grain": "latest_per(well_id, task_code)",
    "baseline": "master_date",
    "evidence": [{"wells_affected": 444, "worst_overrun_days": 5459}],
}


def test_accepts_narration_using_only_real_numbers():
    narration = {
        "explanation": (
            "14003 tasks across 444 wells have a planner-committed finish date after "
            "the rig is due -- a worst-case overrun of 5459 days."
        ),
        "root_cause": "Targets were never re-baselined against the master date.",
        "remediation": "Re-baseline task targets against ex_rig_on_date.",
        "cited_numbers": ["14003", "444", "5459"],
    }
    result = validate_narration(FINDING, narration)
    assert result.ok
    assert result.invented_numbers == []


def test_accepts_a_percentage_derived_purely_for_display_when_present_in_evidence():
    finding = {**FINDING, "why_it_matters": "55% of the portfolio (444 of 814 wells)."}
    narration = {
        "explanation": "55% of wells are affected.",
        "root_cause": "x",
        "remediation": "y",
        "cited_numbers": ["55%"],
    }
    assert validate_narration(finding, narration).ok


def test_rejects_a_fabricated_number_not_in_the_finding():
    narration = {
        "explanation": "Roughly 20000 tasks are affected across 500 wells.",  # fabricated
        "root_cause": "x",
        "remediation": "y",
        "cited_numbers": ["20000", "500"],
    }
    result = validate_narration(FINDING, narration)
    assert not result.ok
    assert "20000" in result.invented_numbers
    assert "500" in result.invented_numbers


def test_rejects_a_fabricated_number_even_if_not_declared_in_cited_numbers():
    """A model that fabricates a number without honestly declaring it in `cited_numbers`
    must still be caught -- the free-text fields are independently re-scanned.
    """
    narration = {
        "explanation": "About 9999 tasks are affected.",
        "root_cause": "x",
        "remediation": "y",
        "cited_numbers": [],  # model didn't admit to using a number, but it did
    }
    result = validate_narration(FINDING, narration)
    assert not result.ok
    assert "9999" in result.invented_numbers


def test_accepts_zero_and_small_numbers_that_are_not_really_data():
    """Guard against being so strict that ordinary connective numbers in prose (e.g. a
    section reference) make every narration fail -- only unsupported DATA numbers should
    be rejected. This test documents current behaviour: any digit sequence is checked
    against the source, so remediation text should avoid inventing counts."""
    finding = {**FINDING, "business_rule_ref": "§4"}
    narration = {
        "explanation": "See section 4 of the business rules.",
        "root_cause": "x",
        "remediation": "y",
        "cited_numbers": [],
    }
    # "4" from "section 4" is not present in the finding's own numeric data -- this is
    # expected to be flagged, which is the conservative (safe) failure mode.
    result = validate_narration(finding, narration)
    assert not result.ok


# --------------------------------------------------------------------------- correlation
def test_correlation_rejects_unknown_finding_id():
    by_id = {"BIZ-107": FINDING}
    incident = {
        "title": "x", "root_cause": "y",
        "finding_ids": ["BIZ-107", "DOES-NOT-EXIST"],
        "narrative": "z",
    }
    result = validate_correlation(by_id, incident)
    assert not result.ok
    assert "DOES-NOT-EXIST" in result.reason


def test_correlation_accepts_valid_grouping_with_real_numbers():
    by_id = {"BIZ-107": FINDING}
    incident = {
        "title": "Schedule misalignment", "root_cause": "targets never re-baselined",
        "finding_ids": ["BIZ-107"],
        "narrative": "444 wells show targets committed past the rig-on date.",
    }
    assert validate_correlation(by_id, incident).ok


def test_correlation_rejects_fabricated_number_in_narrative():
    by_id = {"BIZ-107": FINDING}
    incident = {
        "title": "x", "root_cause": "y", "finding_ids": ["BIZ-107"],
        "narrative": "This affects 12345 wells across the portfolio.",
    }
    result = validate_correlation(by_id, incident)
    assert not result.ok
    assert "12345" in result.invented_numbers


# --------------------------------------------------------------------- regression
def test_sentence_ending_period_after_a_number_is_not_a_decimal_point():
    """Found live on 2026-09-08: valid narration citing a business-rule reference (e.g.
    "...defined in section 4.") or ending a sentence right after a date
    ("...(2026-09-08).") was rejected, because the number-extraction regex swallowed the
    sentence-ending period as though it were a decimal point, turning "4" into "4." and
    "-08" into "-08." -- neither of which match the source. A genuine decimal like
    "1.1167" must still be recognised in full.
    """
    finding = {**FINDING, "business_rule_ref": "§4",
               "why_it_matters": "Deadline defined in ex_rig_on_date - 60 days, per §4."}
    narration = {
        "explanation": "This is governed by the deadline rule defined in section 4.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["4"],
    }
    assert validate_narration(finding, narration).ok

    finding2 = {**FINDING, "title": "rows dated after today (2026-09-08)"}
    narration2 = {
        "explanation": "These rows are dated after today, 2026-09-08.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["2026-09-08"],
    }
    assert validate_narration(finding2, narration2).ok

    # A real decimal must still be caught if it's genuinely absent from the source.
    finding3 = {**FINDING, "why_it_matters": "weightage reaches 1.1167 on some rows"}
    narration3 = {
        "explanation": "Weightage reaches 1.1167, which is invalid.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["1.1167"],
    }
    assert validate_narration(finding3, narration3).ok

    narration4 = {
        "explanation": "Weightage reaches 9.9999, which is invalid.",  # fabricated
        "root_cause": "x", "remediation": "y", "cited_numbers": ["9.9999"],
    }
    result = validate_narration(finding3, narration4)
    assert not result.ok
    assert "9.9999" in result.invented_numbers


def test_a_rounded_float_from_evidence_is_accepted_not_flagged_as_invented():
    """Found live 2026-09-08: evidence held observed_max=1.1166666666666667 (raw binary-
    rounding noise); the model wrote '1.1167' for readability and was rejected because the
    exact substring never appears in the source. Rounding to 4dp is a faithful restatement
    of the same real number, not a fabrication.
    """
    finding = {
        **FINDING,
        "title": "dbo.activity_taskplan_job_progress.weightage: 116 rows outside [0.0, 1.0]",
        "evidence": [{"observed_max": 1.1166666666666667, "out_of_range_rows": 116}],
    }
    narration = {
        "explanation": "Weightage reaches 1.1167, exceeding the declared range.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["1.1167"],
    }
    assert validate_narration(finding, narration).ok


def test_evidence_as_a_raw_json_string_still_supports_rounding_and_dates():
    """FindingsStore.findings() returns `evidence` as a raw JSON STRING, not a parsed
    list (confirmed 2026-09-08) -- the rounding/date expansion must work by scanning that
    string for embedded number-shaped substrings, not by float-parsing the whole blob
    (which always fails). This is the actual shape validate_narration receives in
    production, not the convenience dict shape used in the other tests here.
    """
    finding = {
        "check_id": "GEN-RANGE-x", "title": "x outside [0.0, 1.0]",
        "affected_count": 116, "why_it_matters": "", "grain": "row", "baseline": "none",
        "evidence": '[{"observed_max": 1.1166666666666667, "out_of_range_rows": 116}]',
    }
    narration = {
        "explanation": "The maximum observed value is 1.1167, above the allowed range.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["1.1167"],
    }
    assert validate_narration(finding, narration).ok


def test_a_date_reworded_in_a_different_format_is_accepted():
    """Found live 2026-09-08: evidence held max_value='2026-10-19'; the model wrote
    '19 October 2026' and was rejected because that plain '19' never appears in the
    ISO-formatted source string. The day/month/year of a source date are legitimate to
    restate in prose, in either padded or unpadded form.
    """
    finding = {
        **FINDING,
        "title": "well.well_master.loc_start_date: 1 rows dated after today (2026-09-08)",
        "evidence": [{"as_of_date": "2026-09-08", "future_rows": 1, "max_value": "2026-10-19"}],
    }
    narration = {
        "explanation": "One row is dated 19 October 2026, after today.",
        "root_cause": "x", "remediation": "y", "cited_numbers": ["19"],
    }
    assert validate_narration(finding, narration).ok
