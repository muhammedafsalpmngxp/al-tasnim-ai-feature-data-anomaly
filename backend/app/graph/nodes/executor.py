"""Executor node (deterministic) - runs the probe pair read-only, capped, timed and logged.

WHY BOTH HALVES RUN AT COMPILE TIME, EVEN WHEN NOTHING IS ANOMALOUS
--------------------------------------------------------------------
A detection RUN skips the detail query whenever the summary reports zero anomalies - that is
the economy the whole two-query contract exists for. Compiling is the opposite job: it decides
whether the SQL is RIGHT, and a query that is never executed is never checked. A probe whose
detail query is missing an alias, or names a dropped column, would be stored looking healthy
and fail months later in an unattended run, which is exactly the silent failure this engine is
built to prevent.

A query returning no rows still reports its column names through the driver, so the contract
can be checked in full against a clean database.

WHAT BOUNDS THE COST
--------------------
At most `sample_rows + 1` detail rows are ever fetched. The extra row is how truncation is
detected without counting. Nothing here accumulates a real result set: compiling never needs
the findings, only proof that the query can produce them.
"""
from __future__ import annotations

import time
from typing import Any

from app.config import settings
from app.db.connection import get_connection
from app.graph.state import CompileState
from app.observability import get_logger

log = get_logger()


def _columns(cursor) -> list[str]:
    return [str(d[0]) for d in cursor.description] if cursor.description else []


def executor_node(state: CompileState) -> dict:
    rule_id = state.get("rule_id", "")
    summary_sql = state.get("summary_sql", "")
    detail_sql = state.get("detail_sql", "")
    conn = None
    start = time.perf_counter()

    try:
        # The longer DETAIL deadline governs the whole connection: a detail query legitimately
        # scans, and inheriting the one-row SUMMARY deadline would report every large rule as a
        # timeout rather than as the working probe it is.
        #
        # A SMOKE TEST IS THE EXCEPTION, and takes the SHORT deadline. It runs no detail, so the
        # long one would only mean waiting ten minutes to learn that an aggregate is slow -
        # which is not what a smoke test is asking. It asks whether the SQL is VALID, and an
        # invalid statement is rejected when it is parsed, in milliseconds.
        smoke = bool(state.get("smoke_test"))
        conn = get_connection(
            timeout=settings.query_timeout if smoke else settings.detail_timeout
        )
        cur = conn.cursor()

        cur.execute(summary_sql)
        summary_columns = _columns(cur)
        # One more than the contract allows, so "returned more than one row" is detectable
        # here rather than being silently read as the first row.
        summary_fetched = [list(r) for r in cur.fetchmany(2)]
        summary_row: list[Any] = summary_fetched[0] if summary_fetched else []

        if not (detail_sql or "").strip():
            elapsed = time.perf_counter() - start
            log.info(
                "exec ok [%s]: summary %d row(s), detail not run in %.2fs",
                rule_id, len(summary_fetched), elapsed,
            )
            return {
                "exec_error": "",
                "summary_columns": summary_columns,
                "summary_row": summary_row,
                "summary_row_count": len(summary_fetched),
                "detail_columns": [],
                "detail_rows": [],
                "detail_truncated": False,
                "detail_skipped": True,
            }

        cur.execute(detail_sql)
        detail_columns = _columns(cur)
        fetched = [list(r) for r in cur.fetchmany(settings.sample_rows + 1)]
        truncated = len(fetched) > settings.sample_rows
        detail_rows = fetched[: settings.sample_rows]

        elapsed = time.perf_counter() - start
        log.info(
            "exec ok [%s]: summary %d row(s), detail sample %d row(s)%s in %.2fs",
            rule_id, len(summary_fetched), len(detail_rows),
            " (truncated)" if truncated else "", elapsed,
        )
        return {
            "exec_error": "",
            "summary_columns": summary_columns,
            "summary_row": summary_row,
            "summary_row_count": len(summary_fetched),
            "detail_columns": detail_columns,
            "detail_rows": detail_rows,
            "detail_truncated": truncated,
            "detail_skipped": False,
        }

    except Exception as exc:  # noqa: BLE001 - the error is fed back for self-correction
        elapsed = time.perf_counter() - start
        message = str(exc)
        log.warning("exec error [%s] in %.2fs: %s", rule_id, elapsed, message)
        return {
            "exec_error": message,
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
