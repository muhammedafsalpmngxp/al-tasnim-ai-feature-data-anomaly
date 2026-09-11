"""Bridge from the `generated_check` table (written rarely, by the compile-time agent) to
the deterministic check registry (run every time -- zero LangGraph, zero LLM, at runtime).

Nothing in this module ever writes SQL -- it only reads rows already marked
`status='active'` in the store and registers them through the exact same
`register_check(source="generated")` path the 39 column_semantics.yaml-driven checks in
generator.py already use, so a compiled check is bound by the identical mandatory
grain/baseline guard (checks/base.py) as everything else in this codebase. A row whose
grain was never confirmed by a human stays `needs_review` and is silently never registered
here -- not run, not partially run, not run against a guessed grain.

This is also the answer to "not every run needs to invoke the agent": the agent's entire
job is to populate this table, which happens only when the schema changes or a human asks
for a re-compile. Every ordinary run just calls `register_compiled_checks()` below, which
is a handful of SQLite SELECTs -- the SQL text itself is exactly as stable/reproducible
between runs as the 39 already-generated checks are.

The stored SQL contract (deliberately the smallest possible surface, so both a human
writing one by hand and an eventual LLM node can target it): a single `SELECT` that
returns exactly two columns aliased `n` (rows scanned) and `violations` (rows failing).
Every hand-written and generated check in this codebase already produces internally
this exact shape (see generator.py) -- a compiled check is not a new pattern, just a new
*source* for SQL that already has to look like this.
"""
from __future__ import annotations

from app.db.store import FindingsStore
from app.domain.models import Baseline, CheckResult, Finding, FindingClass, Grain, GrainKind, Severity
from app.logging import get_logger
from app.sentinel.checks.base import CheckContext, CheckOutcome, register_check

log = get_logger(__name__)

FAMILY = "COMPILED"


def _grain_from_row(row: dict) -> Grain:
    kind = GrainKind(row["grain_kind"])
    keys = tuple(k for k in (row["grain_keys"] or "").split(",") if k)
    if kind is GrainKind.ROW:
        return Grain.row()
    if kind is GrainKind.LATEST_PER:
        return Grain.latest_per(*keys, order_by=row["grain_order_by"] or "")
    if kind is GrainKind.SNAPSHOT_PINNED:
        return Grain.snapshot_pinned(*keys, order_by=row["grain_order_by"] or "")
    return Grain(kind=kind, keys=keys, order_by=row["grain_order_by"])


def _make_run(row: dict):
    check_id = row["check_id"]
    sql = row["sql_text"]
    table_ref = row["table_ref"]
    severity = Severity(row["severity"])
    title = row["title"]
    why = row["why_it_matters"] or title
    business_rule_ref = row["business_rule_ref"]

    def run(ctx: CheckContext) -> CheckOutcome:
        grain = _grain_from_row(row)
        baseline = row["baseline"]
        result_row = ctx.source.one(sql) or {}
        n = int(result_row.get("n") or 0)
        bad = int(result_row.get("violations") or 0)
        result = CheckResult(
            check_id=check_id, family=FAMILY, status="fail" if bad else "pass",
            rows_scanned=n, violations=bad, grain=grain.describe(), baseline=baseline,
        )
        if not bad:
            return CheckOutcome(result=result)
        finding = Finding(
            check_id=check_id, family=FAMILY, severity=severity,
            finding_class=FindingClass.DEFECT,
            title=title, entity_type="table", entity_id=table_ref, entity_label=table_ref,
            affected_count=bad, grain=grain.describe(), baseline=baseline,
            business_rule_ref=business_rule_ref, why_it_matters=why, sql_text=sql,
            evidence=[{"rows_scanned": n, "violations": bad}],
        )
        return CheckOutcome(result=result, findings=[finding])

    return run


def register_compiled_checks(store: FindingsStore) -> int:
    """Register every `status='active'` row in `generated_check`.

    Called once per run, right after `clear_generated_checks()` + `register_all_generated()`
    in orchestrator.py -- same registry lifecycle, same 'generated' source tag, so a
    compiled check is indistinguishable from a column_semantics.yaml-generated one to every
    downstream consumer (report generators, the API, `all_checks()`) except for its
    `family='COMPILED'` label, which is what keeps it in its own labelled section of the
    Findings Register rather than mixed into the 55 core checks.
    """
    count = 0
    for row in store.active_generated_checks():
        try:
            grain = _grain_from_row(row)
            baseline = Baseline(row["baseline"])
            register_check(
                id=row["check_id"], family=FAMILY, grain=grain, baseline=baseline,
                fn=_make_run(row), severity=row["severity"], title=row["title"],
                business_rule_ref=row["business_rule_ref"], source="generated",
            )
            count += 1
        except Exception as exc:  # noqa: BLE001
            # A malformed row must not take the whole run down -- log it and skip, the
            # same failure mode a bad column_semantics.yaml entry would have.
            log.error(
                "compiled.register_failed", check_id=row.get("check_id"), error=str(exc)[:300]
            )
    log.info("compiled.registered", count=count)
    return count
