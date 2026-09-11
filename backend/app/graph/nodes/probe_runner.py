"""Probe Runner node (deterministic) - executes every compiled probe. No LLM anywhere.

THE TWO-QUERY CONTRACT IS THE ECONOMY, AND THIS IS WHERE IT PAYS
----------------------------------------------------------------
SUMMARY returns one row and is cheap, so it always runs. DETAIL scans, so it runs ONLY when
SUMMARY reports something to look at. On a healthy database most probes stop after one cheap
query, which is what makes a daily run affordable against two hundred of them.

A skipped DETAIL is recorded as skipped, never as "no rows found". They mean different things
and conflating them would let an unexamined rule read as a clean one.

MEMORY IS BOUNDED BY CONSTRUCTION
---------------------------------
Rows are fetched in batches and written straight to a per-rule spool file. Only the first
`report_rows` are retained in memory, for the Word document. Peak memory is therefore one
fetch batch per concurrent probe - independent of how much is wrong with the data, which is
the one number nobody can predict in advance.

CONCURRENCY IS BOUNDED ON PURPOSE. These are scanning queries against a live production
database. An unbounded pool would cost the source system more than the report is worth, so
ANOMALY_RUN_CONCURRENCY governs it and defaults low.

A FAILING PROBE NEVER STOPS THE RUN. Its error is captured on its own result and reported.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import settings
from app.db.connection import get_connection
from app.graph.run_state import RunState
from app.observability import get_logger
from app.report import spool as spool_store
from app.rules.contract import (
    check_detail,
    check_summary,
    columns_of,
    sanity_concerns,
    summary_values,
)
from app.rules.spec import ProbeResult

log = get_logger()


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _run_summary(cur, probe, result: ProbeResult) -> bool:
    """Execute SUMMARY and fold its one row onto the result. False means it cannot be used."""
    started = time.perf_counter()
    cur.execute(probe.summary_sql)
    columns = columns_of(cur.description)
    rows = cur.fetchmany(2)
    result.summary_seconds = time.perf_counter() - started

    problems = check_summary(columns, len(rows))
    if problems:
        # At RUN time a contract violation is a hard failure with no recourse: there is no
        # author here to rewrite anything. It means the database changed under a probe that
        # compiled cleanly, which is exactly what the report's transparency section is for.
        result.ok = False
        result.error = "the stored probe no longer satisfies its contract: " + "; ".join(problems)
        return False

    values = summary_values(columns, list(rows[0]))
    result.scope_total = _as_int(values.get("scope_total"))
    result.anomaly_count = _as_int(values.get("anomaly_count"))
    result.anomaly_pct = _as_float(values.get("anomaly_pct"))
    worst = values.get("worst_severity_val")
    result.worst_severity_val = None if worst is None else _as_float(worst)
    result.concerns = sanity_concerns(values)
    return True


def _run_detail(cur, probe, result: ProbeResult, run_id: str) -> None:
    """Execute DETAIL, spooling every row and keeping only the Word cap in memory."""
    started = time.perf_counter()
    cur.execute(probe.detail_sql)
    columns = columns_of(cur.description)
    result.detail_columns = columns

    problems = check_detail(columns)
    if problems:
        result.ok = False
        result.error = "the stored probe no longer satisfies its contract: " + "; ".join(problems)
        return

    spool = spool_store.Spool(run_id, probe.rule_id, columns)
    total = 0
    truncated = False
    try:
        while True:
            batch = cur.fetchmany(settings.fetch_batch)
            if not batch:
                break
            for raw in batch:
                if total >= settings.export_max_rows:
                    truncated = True
                    break
                row = list(raw)
                spool.write(row)
                if total < settings.report_rows:
                    result.detail_rows.append([spool_store.jsonable(v) for v in row])
                total += 1
            if truncated:
                break
    finally:
        spool.close()

    result.detail_total = total
    result.spool_path = spool.path
    result.spool_truncated = truncated
    # The in-memory list was cut short whenever the spool holds more than it does.
    result.detail_truncated = total > len(result.detail_rows)
    result.detail_seconds = time.perf_counter() - started

    # SUMMARY and DETAIL are two queries and can disagree. Comparable only when nothing was
    # capped - otherwise every large rule would manufacture a false disagreement.
    if not truncated and total != result.anomaly_count:
        result.concerns.append(
            f"SUMMARY reports {result.anomaly_count} anomalies but DETAIL returned {total} "
            f"rows. The two queries disagree - they must apply the SAME condition at the SAME "
            f"grain."
        )


def _run_one(probe, run_id: str) -> ProbeResult:
    result = ProbeResult(rule_id=probe.rule_id)
    conn = None
    try:
        # The longer DETAIL deadline governs the connection: a detail query legitimately scans,
        # and inheriting the one-row SUMMARY deadline turns every large rule into a timeout.
        conn = get_connection(timeout=settings.detail_timeout)
        cur = conn.cursor()

        if not _run_summary(cur, probe, result):
            log.warning("probe: %s FAILED - %s", probe.rule_id, result.error[:160])
            return result

        if result.anomaly_count <= 0:
            result.detail_skipped = True
            # A probe that examined NOTHING is not clean, and the line an operator watches
            # during a run must not say it is. The scorer already treats these as coverage
            # gaps; a log that called them clean would be the one place the system contradicted
            # itself, and it is the place a person is most likely to read.
            if result.scope_total <= 0:
                log.warning(
                    "probe: %s examined NO rows - a coverage gap, NOT a clean result",
                    probe.rule_id,
                )
            else:
                log.info(
                    "probe: %s clean - %s row(s) examined, nothing flagged (detail skipped)",
                    probe.rule_id, f"{result.scope_total:,}",
                )
            return result

        _run_detail(cur, probe, result, run_id)
        if result.ok:
            log.info(
                "probe: %s found %s of %s (%.2f%%) in %.1fs",
                probe.rule_id, f"{result.anomaly_count:,}", f"{result.scope_total:,}",
                result.anomaly_pct, result.summary_seconds + result.detail_seconds,
            )
        else:
            log.warning("probe: %s FAILED - %s", probe.rule_id, result.error[:160])
        return result

    except Exception as exc:  # noqa: BLE001 - one probe must never end the run
        result.ok = False
        result.error = str(exc)
        log.warning("probe: %s FAILED - %s", probe.rule_id, result.error[:200])
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def probe_runner_node(state: RunState) -> dict:
    probes = state.get("probes") or []
    run_id = state.get("run_id", "")
    if not probes:
        log.warning("probe: nothing to run")
        return {"results": [], "executed": 0, "failed": 0}

    started = time.perf_counter()
    workers = max(1, min(settings.run_concurrency, len(probes)))
    log.info("probe: running %d probe(s) with %d worker(s)", len(probes), workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: _run_one(p, run_id), probes))

    failed = sum(1 for r in results if not r.ok)
    with_findings = sum(1 for r in results if r.has_findings)
    log.info(
        "probe: done in %.1fs - %d probe(s), %d with findings, %d failed",
        time.perf_counter() - started, len(results), with_findings, failed,
    )
    return {"results": results, "executed": len(results), "failed": failed}
