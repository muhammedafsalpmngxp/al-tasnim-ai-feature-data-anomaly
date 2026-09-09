"""Regression tests for the server-side §5 guard in `_handle_proposal` -- found live
2026-09-08: a session proposed "rig-on date earlier than expected" with
severity_guess='high', even though its own hypothesis correctly called it an
acceleration. A prompt instruction is not enforcement; this must be refused in code
regardless of what the model's prose says, and a refusal must not count toward
`proposals_made`. No live API or database needed -- exercises `_handle_proposal` directly.
"""
from __future__ import annotations

from app.db.store import FindingsStore
from app.sentinel.llm.suggest import _handle_proposal
from tests.test_suggest_tools import FakeScope, FakeSource
from app.sentinel.llm.tools import ExplorationTools


def _tools() -> ExplorationTools:
    return ExplorationTools(FakeSource(canned_rows=[{"well_id": 1}]), FakeScope())


def test_an_early_variance_proposal_is_refused_not_recorded(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    args = {
        "title": "Rig-on Date Earlier than Expected Date",
        "family": "SCHED", "severity_guess": "high",
        "hypothesis": (
            "Several wells have a rig-on date that is earlier than the expected date, "
            "indicating an acceleration in the schedule."
        ),
        "sql_text": "SELECT well_id FROM well.well_master WHERE rig_on_date < ex_rig_on_date",
        "table_ref": "well.well_master",
    }
    recorded, message = _handle_proposal(store, _tools(), args)
    assert recorded is False
    assert "REFUSED" in message
    assert store.suggestions() == []  # nothing was persisted
    store.close()


def test_refusal_matches_regardless_of_which_marker_phrase_is_used(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    phrasings = [
        "this well finished ahead of schedule",
        "results show an acceleration of the timeline",
        "completed sooner than planned",
        "the actual date is before its target date",
    ]
    for hypothesis in phrasings:
        args = {
            "title": "x", "family": "SCHED", "severity_guess": "low",
            "hypothesis": hypothesis, "sql_text": "SELECT 1", "table_ref": "well.well_master",
        }
        recorded, message = _handle_proposal(store, _tools(), args)
        assert recorded is False, f"should have refused: {hypothesis!r}"
        assert "REFUSED" in message
    store.close()


def test_a_genuinely_late_variance_proposal_is_not_refused(tmp_store_path):
    """The guard must not be so broad it blocks legitimate LATE-schedule proposals --
    only the early/accelerated direction is refused."""
    store = FindingsStore(path=tmp_store_path)
    args = {
        "title": "Rig-on Date Later than Expected Date",
        "family": "SCHED", "severity_guess": "high",
        "hypothesis": "Several wells have a rig-on date after the expected date, a real delay.",
        "sql_text": "SELECT well_id FROM well.well_master WHERE rig_on_date > ex_rig_on_date",
        "table_ref": "well.well_master",
    }
    recorded, message = _handle_proposal(store, _tools(), args)
    assert recorded is True
    assert "REFUSED" not in message
    assert len(store.suggestions()) == 1
    store.close()


def test_missing_required_field_is_also_not_recorded(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    recorded, message = _handle_proposal(store, _tools(), {"title": "x"})
    assert recorded is False
    assert "missing required field" in message
    store.close()
