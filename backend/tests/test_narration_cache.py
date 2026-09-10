"""Tests for cross-run narration reuse (`app/sentinel/llm/cache.py` + the store lookup).

The data this runs against is LIVE, so the cache has to be right about change, not just
about sameness. Each test below is one of those cases:

    unchanged finding          -> reuse
    partly fixed (33 -> 23)    -> re-narrate, because the prose quotes the number
    same count, new evidence   -> re-narrate
    different model            -> re-narrate
    edited business rules      -> re-narrate
    narration that was rejected-> never served as a hit

No live database or API key needed.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db.store import FindingsStore
from app.domain.models import Finding, FindingClass, Run, Severity
from app.sentinel.llm.cache import narration_key, prompt_fingerprint

MODEL = "gpt-4o-mini"
FP = "promptfp00000001"


def _finding_row(**overrides):
    """Shaped like a row `FindingsStore.findings()` returns."""
    row = {
        "check_id": "BIZ-101", "severity": "high", "finding_class": "violation",
        "title": "33 wells: rig is off but pegging was never issued",
        "affected_count": 33, "grain": "row", "baseline": "none",
        "business_rule_ref": "§7", "owner": "PDO",
        "why_it_matters": "Pegging is a PDO prerequisite.",
        "entity_id": "well.well_master", "well_id": None,
        "evidence": '[{"wells": 33}]',
    }
    row.update(overrides)
    return row


def _key(row, *, model=MODEL, fp=FP):
    return narration_key(row, model=model, prompt_fp=fp)


# ------------------------------------------------------------------ key behaviour
def test_an_unchanged_finding_produces_the_same_key():
    assert _key(_finding_row()) == _key(_finding_row())


def test_a_partly_fixed_anomaly_produces_a_different_key():
    """33 wells fixed down to 23: the narration quotes "33", so it MUST be rewritten.
    This is the case that makes a naive check_id-only cache wrong on live data."""
    before = _finding_row(affected_count=33, title="33 wells: rig is off ...",
                          evidence='[{"wells": 33}]')
    after = _finding_row(affected_count=23, title="23 wells: rig is off ...",
                         evidence='[{"wells": 23}]')
    assert _key(before) != _key(after)


def test_same_count_but_different_evidence_produces_a_different_key():
    """The headline number can hold while the detail underneath changes -- different
    wells, a new worst-case. The evidence is in the key, so this re-narrates."""
    a = _finding_row(evidence='[{"wells": 33, "worst_days": 120}]')
    b = _finding_row(evidence='[{"wells": 33, "worst_days": 400}]')
    assert _key(a) != _key(b)


def test_changing_the_model_produces_a_different_key():
    """Switching OPENAI_MODEL must not leave the report half-written by the old model."""
    assert _key(_finding_row(), model="gpt-4o-mini") != _key(_finding_row(), model="gpt-5-nano")


def test_changing_the_business_rules_produces_a_different_key():
    """The prompt fingerprint covers BUSINESS_RULES.md, so editing a rule re-narrates
    everything rather than silently keeping prose written under the old rule."""
    assert _key(_finding_row(), fp="aaaa") != _key(_finding_row(), fp="bbbb")


def test_prompt_fingerprint_tracks_the_prompt_text():
    assert prompt_fingerprint("rules v1") != prompt_fingerprint("rules v2")
    assert prompt_fingerprint("rules v1") == prompt_fingerprint("rules v1")


@pytest.mark.parametrize(
    "field,value",
    [("severity", "critical"), ("title", "different"), ("business_rule_ref", "§4"),
     ("owner", "AlTasnim"), ("why_it_matters", "other"), ("entity_id", "other"),
     ("grain", "aggregate"), ("finding_class", "defect")],
)
def test_every_narration_input_is_part_of_the_key(field, value):
    assert _key(_finding_row()) != _key(_finding_row(**{field: value}))


# --------------------------------------------------------------- store round-trip
def _seed(store: FindingsStore, run_id: str, **overrides) -> int:
    store.create_run(Run(run_id=run_id, started_at=datetime.now(timezone.utc),
                         as_of_date=date(2026, 9, 9)))
    f = Finding(
        check_id="BIZ-101", family="BIZ", severity=Severity.HIGH,
        finding_class=FindingClass.VIOLATION, title="t", entity_type="table",
        affected_count=33, grain="row", baseline="none",
    )
    store.add_findings(run_id, [f])
    return store.findings(run_id)[0]["finding_id"]


def test_a_stored_narration_is_found_again_by_its_key(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    fid = _seed(store, "run_a")
    store.update_finding_narration(
        fid, explanation="E", root_cause="C", remediation="R", narration_key="KEY1",
    )
    hit = store.find_cached_narration("KEY1")
    assert hit == {"llm_explanation": "E", "llm_root_cause": "C", "llm_remediation": "R"}
    store.close()


def test_an_unknown_key_is_a_miss(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    _seed(store, "run_a")
    assert store.find_cached_narration("NOPE") is None
    store.close()


def test_a_finding_with_no_narration_is_never_served_as_a_hit(tmp_store_path):
    """A narration rejected by the citation validator leaves llm_explanation NULL. If
    that row could answer a cache lookup, one rejection would become permanent."""
    store = FindingsStore(path=tmp_store_path)
    fid = _seed(store, "run_a")
    store._cn.execute(
        "UPDATE finding SET narration_key='KEY2' WHERE finding_id=?", (fid,)
    )
    store._cn.commit()
    assert store.find_cached_narration("KEY2") is None
    store.close()


def test_the_newest_narration_wins_when_a_key_repeats(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    first = _seed(store, "run_a")
    store.update_finding_narration(first, explanation="OLD", root_cause="c",
                                   remediation="r", narration_key="KEY3")
    second = _seed(store, "run_b")
    store.update_finding_narration(second, explanation="NEW", root_cause="c",
                                   remediation="r", narration_key="KEY3")
    assert store.find_cached_narration("KEY3")["llm_explanation"] == "NEW"
    store.close()


def test_writing_narration_without_a_key_does_not_erase_an_existing_one(tmp_store_path):
    """`update_finding_narration` is also called on paths that don't pass a key --
    COALESCE keeps whatever was there rather than nulling the row out of the cache."""
    store = FindingsStore(path=tmp_store_path)
    fid = _seed(store, "run_a")
    store.update_finding_narration(fid, explanation="E", root_cause="C",
                                   remediation="R", narration_key="KEY4")
    store.update_finding_narration(fid, explanation="E2", root_cause="C2", remediation="R2")
    assert store.find_cached_narration("KEY4")["llm_explanation"] == "E2"
    store.close()
