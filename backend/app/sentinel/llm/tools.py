"""Bounded, read-only tools for the suggestion agent (Tier 2 -- see docs/04 and the
2026-09-08 design discussion). Every tool goes through the SAME guards as the rest of the
app: `ReadOnlyGuard` validates any agent-written SQL, `Scope` restricts which tables/columns
are even visible, and PII columns are invisible to the agent, not merely excluded from its
output.

This is the first place in the codebase where an LLM decides what SQL to run. That makes
it the highest-risk surface in the feature, so every call is:
  - wrapped so it can NEVER return more than a capped number of rows, regardless of what
    the agent's own query does (`SELECT TOP N * FROM (<agent SQL>) AS _sub`)
  - time-bounded
  - logged verbatim, with its result, to an in-memory trace the caller persists
  - incapable of writing: it reuses `SourceDatabase`, whose account is verified
    `db_datareader`-only (docs/01 -- BIuser cannot INSERT/CREATE TABLE)

Nothing this module produces is a finding. It produces CANDIDATE checks that a human must
review before they can ever affect a report -- see `suggest.py`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.db.source import ReadOnlyViolation, SourceDatabase
from app.logging import get_logger
from app.sentinel.scope import Scope

log = get_logger(__name__)


@dataclass(slots=True)
class ToolCallRecord:
    """One tool invocation, kept for the audit trail (docs/04 `agent_trace`)."""

    step_no: int
    tool_name: str
    tool_input: str
    tool_output: str
    ok: bool
    elapsed_ms: int


class ExplorationTools:
    """Stateful per-session tool surface: enforces the call/row/time budget across the
    whole suggestion session, not just per call, and accumulates the trace.
    """

    def __init__(
        self,
        source: SourceDatabase,
        scope: Scope,
        *,
        max_calls: int = 20,
        max_rows_per_query: int = 50,
        max_seconds_total: int = 120,
    ) -> None:
        self.source = source
        self.scope = scope
        self.max_calls = max_calls
        self.max_rows_per_query = max_rows_per_query
        self.max_seconds_total = max_seconds_total
        self.trace: list[ToolCallRecord] = []
        self._started = time.monotonic()
        self._row_counts: dict[str, int] | None = None
        # Confirmed live 2026-09-08: a model retried one failing query verbatim 10 times
        # in a row, burning its entire tool budget without ever adapting. Tracked here so
        # a verbatim repeat gets an escalating, explicit instruction instead of the same
        # error message again -- see `run_query`.
        self._last_failed_sql: str | None = None
        self._repeat_count = 0

    # --------------------------------------------------------------------- budget
    @property
    def calls_used(self) -> int:
        return len(self.trace)

    @property
    def budget_exhausted(self) -> bool:
        if self.calls_used >= self.max_calls:
            return True
        return (time.monotonic() - self._started) > self.max_seconds_total

    def _record(self, step_no: int, name: str, tool_input: str, output: Any, ok: bool, t0: float) -> str:
        text = str(output)[:4000]
        self.trace.append(ToolCallRecord(
            step_no=step_no, tool_name=name, tool_input=tool_input[:2000],
            tool_output=text, ok=ok, elapsed_ms=int((time.monotonic() - t0) * 1000),
        ))
        return text

    # ---------------------------------------------------------------------- tools
    def list_tables(self) -> str:
        """In-scope tables with their approximate row counts."""
        t0 = time.monotonic()
        step = self.calls_used + 1
        if self._row_counts is None:
            self._row_counts = self.source.table_row_counts()
        rows = {
            t.full: self._row_counts.get(t.full, 0)
            for t in self.scope.filter_tables(self._row_counts)
        }
        out = "\n".join(f"{t}: {n:,} rows" for t, n in sorted(rows.items()))
        return self._record(step, "list_tables", "", out, True, t0)

    def describe_table(self, table: str) -> str:
        """Column list, types, null%, distinct count for one in-scope table -- with any
        PII/secret-named column removed from the result entirely (the agent never learns
        such a column exists, not merely that it may not query it).
        """
        t0 = time.monotonic()
        step = self.calls_used + 1
        try:
            schema, name = table.split(".", 1)
        except ValueError:
            return self._record(step, "describe_table", table, "error: expected 'schema.table'", False, t0)
        if not self.scope.table_allowed(schema, name):
            return self._record(
                step, "describe_table", table,
                f"error: {table} is not in scope ({self.scope.exclusion_reason(schema, name)})",
                False, t0,
            )
        cols = self.source.rows(
            """
            SELECT c.name AS column_name, ty.name AS data_type, c.is_nullable
            FROM sys.columns c
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE c.object_id = OBJECT_ID(?)
            ORDER BY c.column_id
            """,
            (table,),
        )
        visible = [c for c in cols if self.scope.column_allowed(schema, name, c["column_name"])]
        if not visible:
            return self._record(step, "describe_table", table, "error: no visible columns", False, t0)
        select_list = ", ".join(
            f"COUNT(DISTINCT [{c['column_name']}]) AS [{c['column_name']}__distinct], "
            f"SUM(CASE WHEN [{c['column_name']}] IS NULL THEN 1 ELSE 0 END) AS [{c['column_name']}__nulls]"
            for c in visible[:15]  # cap columns profiled per call -- keeps this bounded too
        )
        try:
            row = self.source.one(f"SELECT COUNT(*) AS n, {select_list} FROM [{schema}].[{name}]") or {}
        except Exception as exc:  # noqa: BLE001
            return self._record(step, "describe_table", table, f"error: {exc}", False, t0)
        n = row.get("n") or 0
        lines = [f"{table}: {n:,} rows"]
        for c in visible[:15]:
            cn = c["column_name"]
            nulls = row.get(f"{cn}__nulls") or 0
            distinct = row.get(f"{cn}__distinct") or 0
            pct = round(100.0 * nulls / n, 1) if n else 0.0
            lines.append(f"  {cn} ({c['data_type']}): {pct}% null, {distinct} distinct")
        if len(visible) > 15:
            lines.append(f"  ... {len(visible) - 15} more columns not shown (call limit)")
        return self._record(step, "describe_table", table, "\n".join(lines), True, t0)

    def _execute_capped(self, sql: str) -> list[dict[str, Any]]:
        """Shared guarded/capped execution. `ReadOnlyGuard` (via SourceDatabase.rows)
        rejects anything but a bare SELECT/WITH; the TOP-N wrap caps output regardless of
        what the inner query does. Raises `ReadOnlyViolation` or the driver's own
        exception on failure -- callers decide how to report that.

        A trailing `;` is stripped before wrapping -- confirmed live 2026-09-08: a model
        naturally writes `SELECT ...;`, and nesting that verbatim inside
        `SELECT TOP N * FROM (<sql>) AS _sub` produces a semicolon INSIDE the subquery,
        which both breaks the SQL and trips the guard's "multiple statements" check. This
        strips only a single, genuinely trailing terminator -- an injection attempt that
        does not end the string with `;` (e.g. `...; DROP TABLE x; --`) is untouched by
        `.rstrip(';')` and is still caught by the guard's keyword scan afterwards.
        """
        clean = sql.strip().rstrip(";").strip()
        wrapped = f"SELECT TOP {self.max_rows_per_query} * FROM (\n{clean}\n) AS _sub"
        return self.source.rows(wrapped)

    def run_query(self, sql: str) -> str:
        """Agent-facing tool: execute a SELECT and return a short text preview, logged."""
        t0 = time.monotonic()
        step = self.calls_used + 1
        if self.budget_exhausted:
            return self._record(step, "run_query", sql, "error: session budget exhausted", False, t0)

        # Confirmed live 2026-09-08: without this, a model that gets one query rejected
        # can retry the EXACT same text repeatedly, burning the whole session budget on
        # one dead end. An identical repeat gets an escalating, explicit instruction
        # instead of the same error a second time.
        if sql == self._last_failed_sql:
            self._repeat_count += 1
            if self._repeat_count >= 2:
                return self._record(
                    step, "run_query", sql,
                    "error: you have submitted this EXACT query 3 times and it failed "
                    "identically each time. Do not resubmit it again -- either change the "
                    "SQL (remove a trailing semicolon, fix the syntax) or move on to a "
                    "different table/hypothesis with describe_table.",
                    False, t0,
                )
        else:
            self._repeat_count = 0

        try:
            rows = self._execute_capped(sql)
        except ReadOnlyViolation as exc:
            self._last_failed_sql = sql
            return self._record(step, "run_query", sql, f"rejected: {exc}", False, t0)
        except Exception as exc:  # noqa: BLE001
            self._last_failed_sql = sql
            return self._record(step, "run_query", sql, f"error: {str(exc)[:300]}", False, t0)
        self._last_failed_sql = None
        if not rows:
            return self._record(step, "run_query", sql, "(no rows)", True, t0)
        preview = "\n".join(str(r) for r in rows[:20])
        note = f"\n... ({len(rows)} rows returned, capped at {self.max_rows_per_query})" if len(rows) > 20 else ""
        return self._record(step, "run_query", sql, preview + note, True, t0)

    def test_execute(self, sql: str) -> tuple[int, list[dict[str, Any]]] | None:
        """Independently re-run a PROPOSED check's SQL for the human-review record --
        structured (row count + sample), not the agent-facing text preview. Returns None
        on any failure (guard rejection or SQL error); the suggestion is still recorded
        with that failure noted, never silently dropped, so a human sees that the agent's
        proposed SQL did not actually re-run cleanly server-side.
        """
        try:
            rows = self._execute_capped(sql)
        except Exception as exc:  # noqa: BLE001
            log.warning("suggest.test_execute_failed", error=str(exc)[:300])
            return None
        return len(rows), rows[:10]
