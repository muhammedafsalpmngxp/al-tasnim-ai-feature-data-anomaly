"""Tests for the suggestion store CRUD and the approved-suggestion boilerplate generator.
No live database or API key needed.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from app.db.store import FindingsStore
from app.sentinel.llm.suggest import generate_boilerplate


def _sample_suggestion(store: FindingsStore) -> int:
    return store.add_suggestion(
        title="Wells with a rig assigned but no cluster_code",
        family="REF", severity_guess="medium",
        hypothesis="12 wells have rig_id set but cluster_code NULL, verified with run_query.",
        sql_text="SELECT well_id FROM well.well_master WHERE rig_id IS NOT NULL AND cluster_code IS NULL",
        table_ref="well.well_master", test_row_count=12,
        test_sample=[{"well_id": 101}, {"well_id": 202}],
    )


# ------------------------------------------------------------------------------- store
def test_add_and_fetch_suggestion(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    sug = store.get_suggestion(sid)
    assert sug is not None
    assert sug["status"] == "pending"
    assert sug["test_row_count"] == 12
    assert sug["test_sample"] == [{"well_id": 101}, {"well_id": 202}]
    store.close()


def test_suggestions_list_and_status_filter(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    a = _sample_suggestion(store)
    b = _sample_suggestion(store)
    store.review_suggestion(a, status="approved", note="looks right")
    assert {s["suggestion_id"] for s in store.suggestions()} == {a, b}
    pending = store.suggestions(status="pending")
    assert [s["suggestion_id"] for s in pending] == [b]
    approved = store.suggestions(status="approved")
    assert approved[0]["review_note"] == "looks right"
    store.close()


def test_review_suggestion_rejects_invalid_status(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    try:
        store.review_suggestion(sid, status="maybe")
        assert False, "should have raised"
    except ValueError:
        pass
    store.close()


def test_approve_records_boilerplate_path(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    store.review_suggestion(sid, status="approved", boilerplate_path="/tmp/x.py.suggested")
    sug = store.get_suggestion(sid)
    assert sug["boilerplate_path"] == "/tmp/x.py.suggested"
    assert sug["status"] == "approved"
    store.close()


# --------------------------------------------------------------------------- agent_trace
@dataclass(slots=True)
class _FakeRecord:
    step_no: int
    tool_name: str
    tool_input: str
    tool_output: str
    ok: bool
    elapsed_ms: int


def test_agent_trace_roundtrip(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    records = [
        _FakeRecord(1, "list_tables", "", "well.well_master: 814 rows", True, 12),
        _FakeRecord(2, "run_query", "SELECT 1", "1", True, 8),
    ]
    n = store.add_agent_trace("suggest_abc123", records)
    assert n == 2
    trace = store.agent_trace("suggest_abc123")
    assert len(trace) == 2
    assert trace[0]["tool_name"] == "list_tables"
    assert trace[1]["step_no"] == 2
    assert bool(trace[0]["ok"]) is True
    store.close()


def test_agent_trace_isolated_per_session(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.add_agent_trace("session_a", [_FakeRecord(1, "list_tables", "", "x", True, 1)])
    store.add_agent_trace("session_b", [_FakeRecord(1, "list_tables", "", "y", True, 1)])
    assert len(store.agent_trace("session_a")) == 1
    assert len(store.agent_trace("session_b")) == 1
    assert store.agent_trace("session_a")[0]["tool_output"] == "x"
    store.close()


# --------------------------------------------------------------------------- boilerplate
def test_boilerplate_is_syntactically_valid_python(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    sug = store.get_suggestion(sid)
    code = generate_boilerplate(sug)
    ast.parse(code)  # raises SyntaxError if the template is broken
    store.close()


def test_boilerplate_contains_the_check_id_and_hypothesis(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    sug = store.get_suggestion(sid)
    code = generate_boilerplate(sug)
    assert f"SUG-{sid:04d}" in code
    assert "12 wells have rig_id set" in code
    assert sug["sql_text"] in code


def test_boilerplate_carries_review_todos_so_it_cannot_look_pre_approved(tmp_store_path):
    """The generated file must not read as finished, reviewed code -- it must visibly
    demand a human decide the grain and baseline (docs/01c: guessing either one wrong is
    exactly the class of mistake this whole project exists to prevent).
    """
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    sug = store.get_suggestion(sid)
    code = generate_boilerplate(sug)
    assert "TODO" in code
    assert "NOT YET REVIEWED" in code
    assert "grain" in code.lower() and "baseline" in code.lower()


def test_boilerplate_uses_the_real_check_decorator_pattern(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    sid = _sample_suggestion(store)
    sug = store.get_suggestion(sid)
    code = generate_boilerplate(sug)
    assert "@check(" in code
    assert "from app.sentinel.checks.base import" in code
    assert "CheckOutcome" in code
