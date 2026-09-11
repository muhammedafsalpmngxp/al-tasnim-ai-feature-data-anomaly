"""Tests for the generated_check table (store.py) and its bridge to the deterministic
check registry (checks/compiled.py) -- the seam that lets the agentic layer contribute a
SQL string RARELY while every ordinary run stays zero-LangGraph, zero-LLM, deterministic.

Proven here with hand-written SQL, no agent/LLM involved yet: that is deliberate -- this
is the step that de-risks the whole integration before any LangGraph node exists.
"""
from __future__ import annotations

import pytest

from app.db.store import FindingsStore
from app.domain.models import Baseline
from app.sentinel.checks.base import (
    CheckContext,
    _clear_registry_for_tests,
    all_checks,
    get_check,
    registry_size,
)
from app.sentinel.checks.compiled import register_compiled_checks


@pytest.fixture(autouse=True)
def _isolated_registry():
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


class FakeSource:
    """Returns a fixed {n, violations} row regardless of the SQL text -- these tests are
    about the bridge's plumbing (store -> registry -> Finding), not about SQL Server.
    """

    def __init__(self, n: int, violations: int):
        self._row = {"n": n, "violations": violations}

    def one(self, sql, params=()):
        self.last_sql = sql
        return self._row


def _ctx(source) -> CheckContext:
    return CheckContext(
        source=source, scope=None, spec=None, sources={}, as_of_date="2026-09-10"  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- store layer
def test_only_active_rows_are_returned_by_active_generated_checks(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-1", table_ref="well.task_daily", title="t1",
        sql_text="SELECT 1 AS n, 0 AS violations", status="needs_review",
    )
    store.upsert_generated_check(
        check_id="CMP-2", table_ref="well.task_daily", title="t2",
        sql_text="SELECT 1 AS n, 0 AS violations", status="active",
    )
    store.upsert_generated_check(
        check_id="CMP-3", table_ref="well.task_daily", title="t3",
        sql_text="SELECT 1 AS n, 0 AS violations", status="disabled",
    )
    active = store.active_generated_checks()
    assert [r["check_id"] for r in active] == ["CMP-2"]
    store.close()


def test_upsert_overwrites_in_place_not_duplicates(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-1", table_ref="t", title="v1", sql_text="SELECT 1 AS n, 0 AS violations",
    )
    store.upsert_generated_check(
        check_id="CMP-1", table_ref="t", title="v2", sql_text="SELECT 2 AS n, 1 AS violations",
        status="active",
    )
    rows = store.generated_checks()
    assert len(rows) == 1
    assert rows[0]["title"] == "v2"
    assert rows[0]["status"] == "active"
    store.close()


def test_set_generated_check_status_rejects_unknown_status(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-1", table_ref="t", title="t", sql_text="SELECT 1 AS n, 0 AS violations",
    )
    with pytest.raises(ValueError, match="invalid generated_check status"):
        store.set_generated_check_status("CMP-1", "bogus")
    store.close()


# ------------------------------------------------------------------------------ the bridge
def test_needs_review_rows_are_never_registered(tmp_store_path):
    """A table whose grain was measured but not confirmed by a human must stay
    needs_review forever, not just until the next run -- this is what makes that true.
    """
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-UNCONFIRMED", table_ref="wbs.WBS_master", title="unconfirmed",
        sql_text="SELECT 1 AS n, 0 AS violations", status="needs_review",
    )
    count = register_compiled_checks(store)
    assert count == 0
    assert registry_size() == 0
    assert get_check("CMP-UNCONFIRMED") is None
    store.close()


def test_a_hand_written_sql_string_registers_and_runs_end_to_end(tmp_store_path):
    """The exact scenario the user asked to prove first, with zero LLM cost: write one
    SQL string by hand, mark it active, and confirm it flows all the way through to a
    Finding exactly like a column_semantics.yaml-generated check would.
    """
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-FUTURE-WELLDATE", table_ref="well.well_master",
        title="well_master.spud_date not in the future",
        sql_text=(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN spud_date > CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS violations "
            "FROM well.well_master"
        ),
        severity="high", grain_kind="row", baseline="actual",
        why_it_matters="spud_date records what already happened; it cannot be in the future.",
        status="active", created_by="human",
    )
    count = register_compiled_checks(store)
    assert count == 1
    entry = get_check("CMP-FUTURE-WELLDATE")
    assert entry is not None
    assert entry.family == "COMPILED"
    assert entry.source == "generated"
    assert entry.baseline is Baseline.ACTUAL

    outcome = entry.run(_ctx(FakeSource(n=500, violations=3)))
    assert outcome.result.status == "fail"
    assert outcome.result.violations == 3
    assert outcome.result.family == "COMPILED"
    assert len(outcome.findings) == 1
    f = outcome.findings[0]
    assert f.affected_count == 3
    assert f.family == "COMPILED"
    assert "spud_date" in f.why_it_matters
    store.close()


def test_a_passing_compiled_check_produces_no_finding(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-CLEAN", table_ref="well.well_master", title="clean check",
        sql_text="SELECT 100 AS n, 0 AS violations", status="active",
    )
    register_compiled_checks(store)
    entry = get_check("CMP-CLEAN")
    outcome = entry.run(_ctx(FakeSource(n=100, violations=0)))
    assert outcome.result.status == "pass"
    assert outcome.findings == []
    store.close()


def test_a_malformed_row_is_skipped_not_fatal_to_the_whole_batch(tmp_store_path):
    """One bad row (e.g. a baseline value that no longer exists in the Baseline enum)
    must not take down every other compiled check in the same run -- the same failure
    mode a single bad column_semantics.yaml entry would have, not a run-ending crash.
    """
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-BAD", table_ref="t", title="bad", sql_text="SELECT 1 AS n, 0 AS violations",
        baseline="not_a_real_baseline", status="active",
    )
    store.upsert_generated_check(
        check_id="CMP-GOOD", table_ref="t", title="good", sql_text="SELECT 1 AS n, 0 AS violations",
        status="active",
    )
    count = register_compiled_checks(store)
    assert count == 1
    assert get_check("CMP-BAD") is None
    assert get_check("CMP-GOOD") is not None
    store.close()


def test_compiled_checks_share_the_generated_source_tag_and_clear_together(tmp_store_path):
    """clear_generated_checks() (called at the top of every run's checks phase) must clear
    compiled checks along with the column_semantics.yaml-generated ones -- otherwise a
    disabled/removed compiled check would keep running forever.
    """
    from app.sentinel.checks.base import clear_generated_checks

    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-1", table_ref="t", title="t", sql_text="SELECT 1 AS n, 0 AS violations",
        status="active",
    )
    register_compiled_checks(store)
    assert registry_size() == 1
    clear_generated_checks()
    assert registry_size() == 0
    store.close()


def test_latest_per_grain_round_trips_through_the_store(tmp_store_path):
    store = FindingsStore(path=tmp_store_path)
    store.upsert_generated_check(
        check_id="CMP-LATEST", table_ref="well.task_daily", title="latest per task",
        sql_text="SELECT 1 AS n, 0 AS violations",
        grain_kind="latest_per", grain_keys="well_id,task_code", grain_order_by="time_stamp",
        status="active",
    )
    register_compiled_checks(store)
    entry = get_check("CMP-LATEST")
    assert entry.grain.keys == ("well_id", "task_code")
    assert entry.grain.order_by == "time_stamp"
    store.close()
