"""Tests for the Tier 2 suggestion agent's tool surface (`app/sentinel/llm/tools.py`) --
no live database or API key needed. Uses a fake source that replicates the ONE property
that actually matters for safety: it runs every statement through the real
`ReadOnlyGuard`, exactly like `SourceDatabase.rows()` does, so a test proving a malicious
agent-supplied query is rejected here proves the same thing the real class guarantees.
"""
from __future__ import annotations

import pytest

from app.db.source import ReadOnlyGuard, ReadOnlyViolation
from app.sentinel.llm.tools import ExplorationTools


class FakeSource:
    """Stands in for SourceDatabase: same guard behaviour, canned data, no network."""

    def __init__(self, canned_rows: list[dict] | None = None, table_counts: dict | None = None):
        self._canned = canned_rows if canned_rows is not None else [{"n": 1}]
        self._counts = table_counts or {
            "well.well_master": 814, "well.task_daily": 107484, "ref.employee": 18476,
        }
        self.queries_seen: list[str] = []

    def rows(self, sql, params=()):
        ReadOnlyGuard.validate(sql)  # the same guard the real class applies
        self.queries_seen.append(sql)
        return self._canned

    def one(self, sql, params=()):
        rs = self.rows(sql, params)
        return rs[0] if rs else None

    def table_row_counts(self):
        return dict(self._counts)


class FakeScope:
    """Minimal Scope stand-in: everything in-scope except a deliberately excluded table
    and a deliberately PII-shaped column, to prove both exclusions are enforced.
    """

    def table_allowed(self, schema, table):
        return f"{schema}.{table}" != "test.excluded"

    def exclusion_reason(self, schema, table):
        return "test fixture: excluded on purpose"

    def column_allowed(self, schema, table, column):
        return "password" not in column.lower()

    def filter_tables(self, row_counts):
        from app.sentinel.scope import TableRef
        return [
            TableRef(*k.split(".", 1)) for k in row_counts
            if self.table_allowed(*k.split(".", 1))
        ]


@pytest.fixture()
def tools() -> ExplorationTools:
    return ExplorationTools(FakeSource(), FakeScope(), max_calls=5, max_rows_per_query=10,
                             max_seconds_total=60)


# --------------------------------------------------------------------------- list_tables
def test_list_tables_returns_in_scope_tables_with_counts(tools: ExplorationTools):
    out = tools.list_tables()
    assert "well.well_master: 814 rows" in out
    assert "well.task_daily: 107,484 rows" in out


def test_list_tables_records_a_trace_entry(tools: ExplorationTools):
    tools.list_tables()
    assert len(tools.trace) == 1
    assert tools.trace[0].tool_name == "list_tables"
    assert tools.trace[0].ok is True


# ------------------------------------------------------------------------ describe_table
def test_describe_table_rejects_out_of_scope_table():
    src = FakeSource(table_counts={"test.excluded": 5})
    t = ExplorationTools(src, FakeScope())
    out = t.describe_table("test.excluded")
    assert "not in scope" in out
    assert t.trace[-1].ok is False


def test_describe_table_rejects_malformed_reference(tools: ExplorationTools):
    out = tools.describe_table("not_a_schema_dot_table")
    assert "error" in out


# ---------------------------------------------------------------------------- run_query
def test_run_query_wraps_with_a_row_cap():
    src = FakeSource()
    t = ExplorationTools(src, FakeScope(), max_rows_per_query=25)
    t.run_query("SELECT * FROM well.well_master")
    assert len(src.queries_seen) == 1
    assert "TOP 25" in src.queries_seen[0]


def test_run_query_rejects_a_write_statement_via_the_real_guard(tools: ExplorationTools):
    out = tools.run_query("DELETE FROM well.well_master")
    assert "rejected" in out
    assert tools.trace[-1].ok is False


def test_run_query_rejects_an_injection_attempt_smuggled_inside_the_wrap(tools: ExplorationTools):
    """The wrap is `SELECT TOP N * FROM (<agent sql>) AS _sub` -- an agent-supplied string
    trying to break out of that subquery with a semicolon and a write statement must
    still be caught, because ReadOnlyGuard scans the WHOLE wrapped text for forbidden
    keywords, not just the outer statement shape.
    """
    malicious = "SELECT 1); DROP TABLE well.well_master; --"
    out = tools.run_query(malicious)
    assert "rejected" in out
    assert "DROP" in out.upper() or "rejected" in out


def test_run_query_reports_no_rows_cleanly():
    t = ExplorationTools(FakeSource(canned_rows=[]), FakeScope())
    out = t.run_query("SELECT * FROM well.well_master WHERE 1=0")
    assert out == "(no rows)"


# ------------------------------------------------------------------------- test_execute
def test_test_execute_returns_row_count_and_sample():
    rows = [{"well_id": i} for i in range(3)]
    t = ExplorationTools(FakeSource(canned_rows=rows), FakeScope())
    result = t.test_execute("SELECT well_id FROM well.well_master")
    assert result is not None
    count, sample = result
    assert count == 3
    assert sample == rows[:10]


def test_test_execute_returns_none_on_guard_rejection():
    t = ExplorationTools(FakeSource(), FakeScope())
    result = t.test_execute("DROP TABLE well.well_master")
    assert result is None


# ---------------------------------------------------------------------------- budgeting
def test_budget_exhausted_after_max_calls():
    t = ExplorationTools(FakeSource(), FakeScope(), max_calls=2)
    t.list_tables()
    assert not t.budget_exhausted
    t.list_tables()
    assert t.budget_exhausted


def test_run_query_refuses_once_budget_is_exhausted():
    t = ExplorationTools(FakeSource(), FakeScope(), max_calls=1)
    t.list_tables()  # uses the one call in the budget
    out = t.run_query("SELECT 1")
    assert "budget exhausted" in out


def test_budget_exhausted_by_elapsed_time():
    t = ExplorationTools(FakeSource(), FakeScope(), max_calls=100, max_seconds_total=60)
    assert not t.budget_exhausted
    t._started -= 61  # simulate 61 elapsed seconds without an actual sleep
    assert t.budget_exhausted


# --------------------------------------------------------------------------- regression
def test_a_trailing_semicolon_does_not_break_the_wrap():
    """Found live 2026-09-08: a model naturally writes 'SELECT ...;'. Nesting that
    verbatim inside 'SELECT TOP N * FROM (<sql>) AS _sub' put a semicolon INSIDE the
    subquery, which the guard's 'multiple statements' check then rejected -- a query with
    exactly one legitimate trailing terminator must succeed.
    """
    src = FakeSource()
    t = ExplorationTools(src, FakeScope())
    out = t.run_query("SELECT well_id FROM well.well_master WHERE rig_id IS NOT NULL;")
    assert "rejected" not in out
    assert "error" not in out
    assert ";" not in src.queries_seen[0]  # the terminator was stripped before wrapping


def test_an_embedded_semicolon_injection_is_still_rejected_after_the_strip_fix():
    """The trailing-semicolon strip must not become a bypass: an attempt that does NOT
    end with a bare semicolon (real SQL injection ends with more text) is untouched by
    rstrip(';') and is still caught by the guard's keyword scan.
    """
    t = ExplorationTools(FakeSource(), FakeScope())
    out = t.run_query("SELECT 1); DROP TABLE well.well_master; --")
    assert "rejected" in out


def test_identical_repeated_failing_query_gets_an_escalating_message_not_the_same_error():
    """Found live 2026-09-08: a model retried one failing query verbatim 10 times,
    consuming its entire session budget on one dead end. The third identical attempt
    must get an explicit instruction to stop repeating, not the same raw error.
    """
    class AlwaysRejects(FakeSource):
        def rows(self, sql, params=()):
            raise ReadOnlyViolation("multiple statements are not allowed")

    t = ExplorationTools(AlwaysRejects(), FakeScope(), max_calls=10)
    bad_sql = "SELECT 1 FROM x WHERE y;"
    first = t.run_query(bad_sql)
    second = t.run_query(bad_sql)
    third = t.run_query(bad_sql)
    assert "rejected" in first
    assert "rejected" in second
    assert "3 times" in third
    assert "Do not resubmit" in third


def test_changing_the_query_after_a_failure_resets_the_repeat_counter():
    t = ExplorationTools(FakeSource(), FakeScope())
    t.run_query("DELETE FROM x")  # fails
    out = t.run_query("SELECT 1 FROM well.well_master")  # different, should succeed cleanly
    assert "rejected" not in out
    assert "3 times" not in out
