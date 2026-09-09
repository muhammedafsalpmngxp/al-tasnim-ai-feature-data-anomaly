"""Hand-written business-rule checks -- meaning, not coverage.

These encode specific sections of `docs/BUSINESS_RULES.md` that no generic invariant can
express: a 60/90-day deadline window, "rig off but tasks still open", a WBS weightage that
must sum to 100%. Each one is reviewed, cites the section it implements, and was verified
against the live database during discovery (`docs/01-DISCOVERY-FINDINGS.md`,
`docs/01c-BASELINE-AND-GRAIN-CORRECTION.md`) -- the numbers cited in comments below are
that evidence, NOT a threshold baked into the SQL. Every query here recomputes its own
count from the live database on every run; nothing is cached or hardcoded as a result.

This is a representative subset (~13 checks across BIZ/CON/WBS/RES), not the full ~50 in
docs/03-ANOMALY-TAXONOMY.md -- building and individually verifying all of them at once
would not be reviewable. The remainder is a documented next increment, not a silent gap:
see the family list at the bottom of this file.

PENDING vs GAP (docs/01d Section 3): a milestone whose deadline has not yet passed is NOT
a finding -- it is normal, and is counted separately as `Finding.finding_class = PENDING`
so a reader can see it was checked and cleared, never lumped into "1,278 missing" the way
an external report did.
"""
from __future__ import annotations

from app.domain.models import Baseline, Finding, FindingClass, Severity, CheckResult
from app.sentinel.checks.base import CheckContext, CheckOutcome, check
from app.sentinel.normalise.spec import load_spec

# Loaded once at import time -- grain declarations are structural config, not run-time
# data, so it is safe (and correct) to read them here for use as decorator arguments.
_SPEC = load_spec()
_WELL_MASTER_GRAIN = _SPEC.semantics_for("well.well_master").grain
_TASK_DAILY_GRAIN = _SPEC.semantics_for("well.task_daily").grain
_WELL_PROGRESS_GRAIN = _SPEC.semantics_for("well.well_progress").grain
_ETP_GRAIN = _SPEC.semantics_for("core.engineering_task_plan").grain


def _need(ctx: CheckContext, *tables: str) -> dict[str, object] | None:
    """Return {table: NormalisedSource} or None (skip) if any is missing this run."""
    out = {}
    for t in tables:
        src = ctx.normalised(t)
        if src is None:
            return None
        out[t] = src
    return out


def _skip(check_id: str, family: str, reason: str) -> CheckOutcome:
    return CheckOutcome(
        result=CheckResult(check_id=check_id, family=family, status="skipped", skip_reason=reason)
    )


# ============================================================================= BIZ family
@check(
    id="BIZ-101", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.NONE,
    severity="high", business_rule_ref="§7",
    title="Drilling complete (rig off) but pegging sheet never issued",
)
def biz_101_rig_off_no_pegging(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.well_master")
    if src is None:
        return _skip("BIZ-101", "BIZ", "well.well_master not normalised this run")
    wm = src["well.well_master"]
    row = ctx.source.one(f"""
        SELECT COUNT(*) AS n FROM {wm.subquery()}
        WHERE rig_off_date IS NOT NULL AND pegged_date IS NULL
    """) or {}
    n = int(row.get("n") or 0)
    result = CheckResult(check_id="BIZ-101", family="BIZ", status="fail" if n else "pass",
                          rows_scanned=wm.rows_effective, violations=n,
                          grain=wm.grain.describe(), baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="BIZ-101", family="BIZ", severity=Severity.HIGH,
        finding_class=FindingClass.VIOLATION, owner="PDO",
        title=f"{n} wells: rig is off (drilling complete) but pegging was never issued",
        entity_type="table", entity_id="well.well_master", entity_label="well.well_master",
        affected_count=n, grain=wm.grain.describe(), baseline="none", business_rule_ref="§7",
        why_it_matters=(
            "Pegging (PDO's responsibility, §1) must precede Location Construction, which "
            "must finish before the rig comes on. A well cannot legitimately reach rig-off "
            "with no pegging sheet on record -- this is a data-entry gap upstream, not a "
            "site condition."
        ),
        evidence=[{"metric": "rig_off_date set, pegged_date NULL", "count": n}],
    )
    return CheckOutcome(result=result, findings=[finding])


@check(
    id="BIZ-102", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.NONE,
    severity="high", business_rule_ref="§7",
    title="Drilling complete (rig off) but FLAF never issued",
)
def biz_102_rig_off_no_flaf(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.well_master")
    if src is None:
        return _skip("BIZ-102", "BIZ", "well.well_master not normalised this run")
    wm = src["well.well_master"]
    row = ctx.source.one(f"""
        SELECT COUNT(*) AS n FROM {wm.subquery()}
        WHERE rig_off_date IS NOT NULL AND flaf_issue_date IS NULL
    """) or {}
    n = int(row.get("n") or 0)
    result = CheckResult(check_id="BIZ-102", family="BIZ", status="fail" if n else "pass",
                          rows_scanned=wm.rows_effective, violations=n,
                          grain=wm.grain.describe(), baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="BIZ-102", family="BIZ", severity=Severity.HIGH,
        finding_class=FindingClass.VIOLATION, owner="PDO",
        title=f"{n} wells: rig is off but FLAF was never issued",
        entity_type="table", entity_id="well.well_master", entity_label="well.well_master",
        affected_count=n, grain=wm.grain.describe(), baseline="none", business_rule_ref="§7",
        why_it_matters=(
            "FLAF (PDO, §1) must precede Flowline Construction, which must finish before "
            "rig-on. Rig-off with no FLAF on record is the same class of gap as BIZ-101."
        ),
        evidence=[{"metric": "rig_off_date set, flaf_issue_date NULL", "count": n}],
    )
    return CheckOutcome(result=result, findings=[finding])


@check(
    id="BIZ-105", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.NONE,
    severity="critical", business_rule_ref="§7, §8",
    title="Well completed (hook-up done) but pegging was never issued",
)
def biz_105_completed_no_pegging(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.well_master")
    if src is None:
        return _skip("BIZ-105", "BIZ", "well.well_master not normalised this run")
    wm = src["well.well_master"]
    row = ctx.source.one(f"""
        SELECT COUNT(*) AS n FROM {wm.subquery()}
        WHERE eng_completion_date IS NOT NULL AND pegged_date IS NULL
    """) or {}
    n = int(row.get("n") or 0)
    result = CheckResult(check_id="BIZ-105", family="BIZ", status="fail" if n else "pass",
                          rows_scanned=wm.rows_effective, violations=n,
                          grain=wm.grain.describe(), baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="BIZ-105", family="BIZ", severity=Severity.CRITICAL,
        finding_class=FindingClass.VIOLATION, owner="PDO",
        title=f"{n} wells marked COMPLETE (§8) with no pegging sheet ever recorded",
        entity_type="table", entity_id="well.well_master", entity_label="well.well_master",
        affected_count=n, grain=wm.grain.describe(), baseline="none",
        business_rule_ref="§7, §8",
        why_it_matters=(
            "§8: a well is completed when hook-up is done. §7: pegging is the FIRST step "
            "in the lifecycle. A completed well with no pegging sheet on record means "
            "either the pegging record was lost, or the completion date is on the wrong "
            "well -- either way this is CRITICAL, because it breaks the audit trail for "
            "an otherwise-finished well."
        ),
        evidence=[{"metric": "eng_completion_date set, pegged_date NULL", "count": n}],
    )
    return CheckOutcome(result=result, findings=[finding])


@check(
    id="BIZ-107", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.MASTER_DATE,
    severity="critical", business_rule_ref="§4, §7",
    title="Task committed to finish after the well's own master rig-on date",
)
def biz_107_target_past_master_date(ctx: CheckContext) -> CheckOutcome:
    """The single largest finding in discovery (docs/01c Section 4.1): 39,991 tasks across
    449 of 814 wells (55%) had a planner target_end after their well's ex_rig_on_date --
    construction committed to finish after the rig is contractually due. Measured against
    TARGET (the authoritative planner baseline, §5/§7 correction), never against P6.
    """
    src = _need(ctx, "well.task_daily", "well.well_master")
    if src is None:
        return _skip("BIZ-107", "BIZ", "task_daily or well_master not normalised this run")
    td, wm = src["well.task_daily"], src["well.well_master"]
    row = ctx.source.one(f"""
        SELECT COUNT(*) AS tasks_compared,
               SUM(CASE WHEN t.target_end > m.ex_rig_on_date THEN 1 ELSE 0 END) AS violations,
               COUNT(DISTINCT CASE WHEN t.target_end > m.ex_rig_on_date
                                    THEN t.well_id END) AS wells_affected,
               MAX(DATEDIFF(day, m.ex_rig_on_date, t.target_end)) AS worst_overrun_days
        FROM {td.at_grain('t')}
        JOIN {wm.subquery('m')} ON m.well_id = t.well_id
        WHERE t.target_end IS NOT NULL AND m.ex_rig_on_date IS NOT NULL
    """) or {}
    n = int(row.get("violations") or 0)
    result = CheckResult(check_id="BIZ-107", family="BIZ", status="fail" if n else "pass",
                          rows_scanned=int(row.get("tasks_compared") or 0), violations=n,
                          grain=td.grain.describe(), baseline=Baseline.MASTER_DATE.value)
    if not n:
        return CheckOutcome(result=result)
    wells = int(row.get("wells_affected") or 0)
    finding = Finding(
        check_id="BIZ-107", family="BIZ", severity=Severity.CRITICAL,
        finding_class=FindingClass.VIOLATION,
        title=(
            f"{n:,} tasks across {wells} wells have a planner target finishing AFTER "
            "the rig is contractually due (ex_rig_on_date)"
        ),
        entity_type="table", entity_id="well.task_daily", entity_label="well.task_daily",
        affected_count=n, grain=td.grain.describe(), baseline=Baseline.MASTER_DATE.value,
        business_rule_ref="§4, §7",
        why_it_matters=(
            "ex_rig_on_date is the master date the whole schedule is measured against "
            "(§2): all construction must finish in time for the rig. A planner target "
            "past that date means the schedule was never re-baselined against reality, "
            "not merely that a well is running late -- this affects the schedule's "
            "internal consistency for over half the portfolio."
        ),
        evidence=[{
            "wells_affected": wells, "tasks_affected": n,
            "worst_overrun_days": row.get("worst_overrun_days"),
        }],
    )
    return CheckOutcome(result=result, findings=[finding])


@check(
    id="BIZ-108", family="BIZ", grain=_TASK_DAILY_GRAIN, baseline=Baseline.NONE,
    severity="high", business_rule_ref="§7, §8",
    title="Rig is off but the well's own task list still has open tasks",
)
def biz_108_rig_off_tasks_open(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.task_daily", "well.well_master")
    if src is None:
        return _skip("BIZ-108", "BIZ", "task_daily or well_master not normalised this run")
    td, wm = src["well.task_daily"], src["well.well_master"]
    row = ctx.source.one(f"""
        WITH t AS (
            SELECT well_id, COUNT(*) AS tasks,
                   SUM(CASE WHEN ISNULL(completed,0)=1 THEN 1 ELSE 0 END) AS done
            FROM {td.at_grain()} GROUP BY well_id
        )
        SELECT COUNT(DISTINCT t.well_id) AS wells,
               SUM(t.tasks - t.done) AS open_tasks
        FROM {wm.subquery('m')} JOIN t ON t.well_id = m.well_id
        WHERE m.rig_off_date IS NOT NULL AND t.done < t.tasks
    """) or {}
    wells = int(row.get("wells") or 0)
    open_tasks = int(row.get("open_tasks") or 0)
    result = CheckResult(check_id="BIZ-108", family="BIZ", status="fail" if wells else "pass",
                          rows_scanned=td.rows_effective, violations=wells,
                          grain=td.grain.describe(), baseline=Baseline.NONE.value)
    if not wells:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="BIZ-108", family="BIZ", severity=Severity.HIGH,
        finding_class=FindingClass.GAP,
        title=f"{wells} wells with the rig off still carry {open_tasks:,} open tasks",
        entity_type="table", entity_id="well.task_daily", entity_label="well.task_daily",
        affected_count=wells, grain=td.grain.describe(), baseline="none",
        business_rule_ref="§7, §8",
        why_it_matters=(
            "Drilling (rig on -> rig off) is PDO's activity (§7); construction tasks "
            "should already be closed by then. Open tasks after rig-off mean either the "
            "task-completion flag is not being maintained, or real work is genuinely "
            "outstanding after drilling started -- both need follow-up."
        ),
        evidence=[{"wells_affected": wells, "open_tasks": open_tasks}],
    )
    return CheckOutcome(result=result, findings=[finding])


@check(
    id="BIZ-109", family="BIZ", grain=_TASK_DAILY_GRAIN, baseline=Baseline.NONE,
    severity="high", business_rule_ref=None,
    title="Wells with many completed tasks but no rig-on date recorded",
)
def biz_109_completed_tasks_no_rig_on(ctx: CheckContext) -> CheckOutcome:
    """Proposed by the Tier 2 suggestion agent (suggestion #4, 2026-09-08), reviewed and
    rewritten here. The agent's own SQL queried well.task_daily directly instead of
    collapsing to its declared grain (latest_per well_id/task_code) first -- since a task
    averages 3.03 daily-log rows (config/column_semantics.yaml), that would count the same
    signed-off task multiple times. Rewritten to use `td.at_grain()`, same as BIZ-108.
    """
    src = _need(ctx, "well.task_daily", "well.well_master")
    if src is None:
        return _skip("BIZ-109", "BIZ", "task_daily or well_master not normalised this run")
    td, wm = src["well.task_daily"], src["well.well_master"]
    row = ctx.source.one(f"""
        WITH t AS (
            SELECT well_id,
                   SUM(CASE WHEN ISNULL(completed,0)=1 THEN 1 ELSE 0 END) AS completed_tasks
            FROM {td.at_grain()} GROUP BY well_id
        )
        SELECT COUNT(DISTINCT t.well_id) AS wells, SUM(t.completed_tasks) AS completed_tasks
        FROM {wm.subquery('m')} JOIN t ON t.well_id = m.well_id
        WHERE m.rig_on_date IS NULL AND t.completed_tasks > 10
    """) or {}
    wells = int(row.get("wells") or 0)
    completed_tasks = int(row.get("completed_tasks") or 0)
    result = CheckResult(check_id="BIZ-109", family="BIZ", status="fail" if wells else "pass",
                          rows_scanned=td.rows_effective, violations=wells,
                          grain=td.grain.describe(), baseline=Baseline.NONE.value)
    if not wells:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="BIZ-109", family="BIZ", severity=Severity.HIGH,
        finding_class=FindingClass.GAP,
        title=f"{wells} wells have over 10 completed tasks but no rig-on date recorded",
        entity_type="table", entity_id="well.well_master", entity_label="well.well_master",
        affected_count=wells, grain=td.grain.describe(), baseline="none",
        why_it_matters=(
            "rig_on_date is an authoritative, site-recorded actual (config/column_semantics.yaml), "
            "not a planning figure. A well with more than 10 signed-off tasks but no rig-on date "
            "means either the date was never captured or task sign-off is happening before "
            "drilling has a recorded start -- both need follow-up."
        ),
        evidence=[{"wells_affected": wells, "completed_tasks": completed_tasks}],
    )
    return CheckOutcome(result=result, findings=[finding])


def _milestone_deadline_check(
    check_id: str, actual_col: str, deadline_expr: str, owner: str,
    business_rule_ref: str, label: str,
):
    """Shared shape for the §4 milestone checks: split into LATE (actual recorded, past
    deadline), GAP (no actual, deadline has passed) and PENDING (no actual, deadline not
    yet passed -- normal, reported as PENDING per docs/01d, never lumped in with the gap).
    """

    def run(ctx: CheckContext) -> CheckOutcome:
        src = _need(ctx, "well.well_master")
        if src is None:
            return _skip(check_id, "BIZ", "well.well_master not normalised this run")
        wm = src["well.well_master"]
        row = ctx.source.one(f"""
            SELECT
              SUM(CASE WHEN {actual_col} IS NOT NULL AND {actual_col} > ({deadline_expr})
                       THEN 1 ELSE 0 END) AS late,
              SUM(CASE WHEN {actual_col} IS NULL
                            AND CAST('{ctx.as_of_date}' AS date) > ({deadline_expr})
                       THEN 1 ELSE 0 END) AS gap,
              SUM(CASE WHEN {actual_col} IS NULL
                            AND CAST('{ctx.as_of_date}' AS date) <= ({deadline_expr})
                       THEN 1 ELSE 0 END) AS pending,
              COUNT(*) AS total
            FROM {wm.subquery()} WHERE ex_rig_on_date IS NOT NULL
        """) or {}
        late = int(row.get("late") or 0)
        gap = int(row.get("gap") or 0)
        pending = int(row.get("pending") or 0)
        bad = late + gap
        result = CheckResult(
            check_id=check_id, family="BIZ", status="fail" if bad else "pass",
            rows_scanned=int(row.get("total") or 0), violations=bad,
            grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
        )
        findings: list[Finding] = []
        if late:
            findings.append(Finding(
                check_id=check_id, family="BIZ", severity=Severity.HIGH,
                finding_class=FindingClass.VIOLATION, owner=owner,
                title=f"{label}: {late} wells issued LATE (past the §4 deadline)",
                entity_type="table", entity_id="well.well_master",
                entity_label="well.well_master", affected_count=late,
                grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
                business_rule_ref=business_rule_ref,
                why_it_matters=f"{label} exists but was recorded after its §4 deadline.",
                evidence=[{"bucket": "late", "count": late}],
            ))
        if gap:
            findings.append(Finding(
                check_id=check_id, family="BIZ", severity=Severity.HIGH,
                finding_class=FindingClass.GAP, owner=owner,
                title=f"{label}: {gap} wells MISSING and past the §4 deadline",
                entity_type="table", entity_id="well.well_master",
                entity_label="well.well_master", affected_count=gap,
                grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
                business_rule_ref=business_rule_ref,
                why_it_matters=(
                    f"{label} has never been recorded, and the §4 deadline for it has "
                    "already passed as of this run's own live date -- this is the "
                    "actionable figure, not the raw NULL count."
                ),
                evidence=[{"bucket": "gap", "count": gap}],
            ))
        if pending:
            findings.append(Finding(
                check_id=check_id, family="BIZ", severity=Severity.INFO,
                finding_class=FindingClass.PENDING, owner=owner,
                title=f"{label}: {pending} wells correctly awaiting a future deadline",
                entity_type="table", entity_id="well.well_master",
                entity_label="well.well_master", affected_count=pending,
                grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
                business_rule_ref=business_rule_ref,
                why_it_matters=(
                    "Not yet due -- correct NULL, not a gap. Reported so a reader can see "
                    "it was checked and cleared (docs/01d Section 3)."
                ),
                evidence=[{"bucket": "pending", "count": pending}],
            ))
        return CheckOutcome(result=result, findings=findings)

    return run


check(
    id="BIZ-205", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.ACTUAL,
    severity="high", business_rule_ref="§4",
    title="Pegging sheet vs its 60-day deadline (ex_rig_on_date - 60 days)",
)(_milestone_deadline_check(
    "BIZ-205", "pegged_date", "DATEADD(day, -60, ex_rig_on_date)", "PDO", "§4",
    "Pegging sheet",
))

check(
    id="BIZ-206", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.ACTUAL,
    severity="high", business_rule_ref="§4",
    title="FLAF vs its 90-day deadline (ex_rig_on_date - 90 days)",
)(_milestone_deadline_check(
    "BIZ-206", "flaf_issue_date", "DATEADD(day, -90, ex_rig_on_date)", "PDO", "§4",
    "FLAF",
))


@check(
    id="BIZ-204", family="BIZ", grain=_WELL_MASTER_GRAIN, baseline=Baseline.ACTUAL,
    severity="high", business_rule_ref="§4",
    title="Hook-up vs its deadline (rig_off_date + 2 days, actual takes precedence)",
)
def biz_204_hookup_deadline(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.well_master")
    if src is None:
        return _skip("BIZ-204", "BIZ", "well.well_master not normalised this run")
    wm = src["well.well_master"]
    row = ctx.source.one(f"""
        SELECT
          SUM(CASE WHEN eng_completion_date IS NOT NULL
                        AND eng_completion_date > DATEADD(day,2,rig_off_date)
                   THEN 1 ELSE 0 END) AS late,
          SUM(CASE WHEN eng_completion_date IS NULL
                        AND CAST('{ctx.as_of_date}' AS date) > DATEADD(day,2,rig_off_date)
                   THEN 1 ELSE 0 END) AS gap,
          SUM(CASE WHEN eng_completion_date IS NULL
                        AND CAST('{ctx.as_of_date}' AS date) <= DATEADD(day,2,rig_off_date)
                   THEN 1 ELSE 0 END) AS pending,
          COUNT(*) AS total
        FROM {wm.subquery()} WHERE rig_off_date IS NOT NULL
    """) or {}
    late, gap, pending = (int(row.get(k) or 0) for k in ("late", "gap", "pending"))
    bad = late + gap
    result = CheckResult(check_id="BIZ-204", family="BIZ", status="fail" if bad else "pass",
                          rows_scanned=int(row.get("total") or 0), violations=bad,
                          grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value)
    findings: list[Finding] = []
    if late:
        findings.append(Finding(
            check_id="BIZ-204", family="BIZ", severity=Severity.HIGH,
            finding_class=FindingClass.VIOLATION, owner="AlTasnim",
            title=f"Hook-up: {late} wells completed LATE (past rig_off + 2 days)",
            entity_type="table", entity_id="well.well_master",
            entity_label="well.well_master", affected_count=late,
            grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
            business_rule_ref="§4",
            why_it_matters="Hook-up (AlTasnim, §1) recorded after its §4 deadline.",
            evidence=[{"bucket": "late", "count": late}],
        ))
    if gap:
        findings.append(Finding(
            check_id="BIZ-204", family="BIZ", severity=Severity.HIGH,
            finding_class=FindingClass.GAP, owner="AlTasnim",
            title=f"Hook-up: {gap} wells MISSING and past the rig_off+2 deadline",
            entity_type="table", entity_id="well.well_master",
            entity_label="well.well_master", affected_count=gap,
            grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
            business_rule_ref="§4",
            why_it_matters="No completion recorded, and the actual-date deadline has passed.",
            evidence=[{"bucket": "gap", "count": gap}],
        ))
    if pending:
        findings.append(Finding(
            check_id="BIZ-204", family="BIZ", severity=Severity.INFO,
            finding_class=FindingClass.PENDING, owner="AlTasnim",
            title=f"Hook-up: {pending} wells correctly within their completion window",
            entity_type="table", entity_id="well.well_master",
            entity_label="well.well_master", affected_count=pending,
            grain=wm.grain.describe(), baseline=Baseline.ACTUAL.value,
            business_rule_ref="§4", why_it_matters="Not yet due -- correct, not a gap.",
            evidence=[{"bucket": "pending", "count": pending}],
        ))
    return CheckOutcome(result=result, findings=findings)


# ============================================================================= RES family
@check(
    id="RES-101", family="RES", grain=_WELL_MASTER_GRAIN, baseline=Baseline.NONE,
    severity="critical", business_rule_ref="not in BUSINESS_RULES.md -- physical impossibility",
    title="A rig is scheduled on two wells at overlapping times",
)
def res_101_rig_double_booking(ctx: CheckContext) -> CheckOutcome:
    src = _need(ctx, "well.well_master")
    if src is None:
        return _skip("RES-101", "RES", "well.well_master not normalised this run")
    wm = src["well.well_master"]
    row = ctx.source.one(f"""
        WITH d AS (
            SELECT well_id, rig_id, rig_on_date, rig_off_date FROM {wm.subquery()}
            WHERE rig_id IS NOT NULL AND rig_on_date IS NOT NULL AND rig_off_date IS NOT NULL
              AND rig_on_date <= rig_off_date
        )
        SELECT COUNT(*) AS overlapping_pairs, COUNT(DISTINCT a.rig_id) AS rigs_affected
        FROM d a JOIN d b ON a.rig_id = b.rig_id AND a.well_id < b.well_id
        WHERE a.rig_on_date <= b.rig_off_date AND b.rig_on_date <= a.rig_off_date
    """) or {}
    n = int(row.get("overlapping_pairs") or 0)
    result = CheckResult(check_id="RES-101", family="RES", status="fail" if n else "pass",
                          rows_scanned=wm.rows_effective, violations=n,
                          grain=wm.grain.describe(), baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    rigs = int(row.get("rigs_affected") or 0)
    finding = Finding(
        check_id="RES-101", family="RES", severity=Severity.CRITICAL,
        finding_class=FindingClass.DEFECT,
        title=f"{n} overlapping rig assignments across {rigs} rigs -- physically impossible",
        entity_type="table", entity_id="well.well_master", entity_label="well.well_master",
        affected_count=n, grain=wm.grain.describe(), baseline="none",
        why_it_matters=(
            "A rig cannot be on two wells at once. Every overlapping pair means at least "
            "one of the two wells has an incorrect rig_on_date, rig_off_date, or rig_id."
        ),
        evidence=[{"overlapping_pairs": n, "rigs_affected": rigs}],
    )
    return CheckOutcome(result=result, findings=[finding])


# ============================================================================= CON family
def _progress_vs_completion(check_id: str, progress_col: str, gate_col: str,
                             gate_label: str, business_rule_ref: str):
    """Shared shape for CON-007/CON-009: a lifecycle GATE is met (well completed / rig
    off) but the corresponding PROGRESS measure has not reached 100%. Uses the LATEST
    row per well (not the table's declared per-(well,week) grain) because the question
    here is "what is this well's current state", which needs one row per well, not one
    row per week -- a deliberately different collapse from the table's default grain,
    computed explicitly in this check rather than assumed.
    """

    def run(ctx: CheckContext) -> CheckOutcome:
        src = _need(ctx, "well.well_progress", "well.well_master")
        if src is None:
            return _skip(check_id, "CON", "well_progress or well_master not normalised")
        wp, wm = src["well.well_progress"], src["well.well_master"]
        row = ctx.source.one(f"""
            WITH latest AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY well_id
                          ORDER BY week_number DESC, progress_id DESC) AS __rn
                FROM {wp.subquery()} WHERE week_number IS NOT NULL
            )
            SELECT COUNT(*) AS wells,
                   SUM(CASE WHEN m.{gate_col} IS NOT NULL AND l.{progress_col} < 1
                            THEN 1 ELSE 0 END) AS violations
            FROM latest l JOIN {wm.subquery('m')} ON m.well_id = l.well_id
            WHERE l.__rn = 1
        """) or {}
        n = int(row.get("violations") or 0)
        result = CheckResult(
            check_id=check_id, family="CON", status="fail" if n else "pass",
            rows_scanned=int(row.get("wells") or 0), violations=n,
            grain="latest_per(well_id ORDER BY week_number DESC, progress_id DESC)",
            baseline=Baseline.NONE.value,
        )
        if not n:
            return CheckOutcome(result=result)
        finding = Finding(
            check_id=check_id, family="CON", severity=Severity.HIGH,
            finding_class=FindingClass.DEFECT,
            title=f"{n} wells: {gate_label} but {progress_col} is under 100%",
            entity_type="table", entity_id="well.well_progress",
            entity_label="well.well_progress", affected_count=n,
            grain="latest_per(well_id)", baseline="none",
            business_rule_ref=business_rule_ref,
            why_it_matters=(
                f"{gate_label} is the business definition of that milestone; a progress "
                f"figure still under 100% at that point means the two are computed by "
                "different, disagreeing pipelines -- any dashboard reading progress "
                "alone will under-report completion."
            ),
            evidence=[{"gate": gate_label, "progress_column": progress_col, "count": n}],
        )
        return CheckOutcome(result=result, findings=[finding])

    return run


check(
    id="CON-007", family="CON", grain=_WELL_PROGRESS_GRAIN, baseline=Baseline.NONE,
    severity="high", business_rule_ref="§8",
    title="Well completed (§8) but overall_progress has not reached 100%",
)(_progress_vs_completion(
    "CON-007", "overall_progress", "eng_completion_date", "well completed (§8)", "§8",
))

check(
    id="CON-009", family="CON", grain=_WELL_PROGRESS_GRAIN, baseline=Baseline.NONE,
    severity="medium", business_rule_ref="§7",
    title="Rig off but construction_progress has not reached 100%",
)(_progress_vs_completion(
    "CON-009", "construction_progress", "rig_off_date", "rig is off", "§7",
))


# ============================================================================= WBS family
@check(
    id="WBS-101", family="WBS", grain=_ETP_GRAIN, baseline=Baseline.NONE,
    severity="critical", business_rule_ref="§9",
    title="WBS weightage does not sum to 100% for a project",
)
def wbs_101_weightage_not_100(ctx: CheckContext) -> CheckOutcome:
    """§9: each WBS carries a PMS weight; the project's WBS weights are a share of the
    whole. Evaluated on the SNAPSHOT-PINNED source (Grain.snapshot_pinned, declared in
    config) so this never double-counts across core.engineering_task_plan's 80+ daily
    snapshots (docs/01d Section 2.1) -- pinning is handled once, centrally, by the
    Normaliser; this check only ever sees one snapshot per project.
    """
    src = _need(ctx, "core.engineering_task_plan")
    if src is None:
        return _skip("WBS-101", "WBS", "core.engineering_task_plan not normalised this run")
    etp = src["core.engineering_task_plan"]
    rows = ctx.source.rows(f"""
        SELECT project_id, COUNT(DISTINCT code) AS wbs_codes,
               CAST(SUM(w) AS decimal(14,4)) AS sum_weightage
        FROM (SELECT DISTINCT project_id, code, weightage AS w
              FROM {etp.subquery()} WHERE type = 'W') x
        GROUP BY project_id
    """)
    total = len(rows)
    bad = [r for r in rows if r["sum_weightage"] is None or abs(float(r["sum_weightage"]) - 100.0) > 0.5]
    n = len(bad)
    result = CheckResult(check_id="WBS-101", family="WBS", status="fail" if n else "pass",
                          rows_scanned=total, violations=n,
                          grain=etp.grain.describe(), baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    findings = [
        Finding(
            check_id="WBS-101", family="WBS", severity=Severity.CRITICAL,
            finding_class=FindingClass.VIOLATION,
            title=f"Project {r['project_id']}: WBS weightage sums to "
                  f"{r['sum_weightage']} across {r['wbs_codes']} codes, not 100",
            entity_type="project", entity_id=str(r["project_id"]),
            entity_label=str(r["project_id"]), affected_count=1,
            grain=etp.grain.describe(), baseline="none", business_rule_ref="§9",
            why_it_matters=(
                "§9 requires each WBS's PMS weight to be a share of the project total, "
                "so the project's WBS weights must sum to 100. A different total means "
                "the reported overall progress percentage for this project is arithmetically "
                "wrong, not merely imprecise."
            ),
            evidence=[{"project_id": str(r["project_id"]), "wbs_codes": r["wbs_codes"],
                       "sum_weightage": r["sum_weightage"]}],
        )
        for r in bad
    ]
    return CheckOutcome(result=result, findings=findings)


# ----------------------------------------------------------------------------- not built
# Remaining families from docs/03-ANOMALY-TAXONOMY.md -- next increment, not a silent gap:
#   MDM-* (master-data conflicts: norms/UOM/code disagreement between mapping_master and
#          activity_master_mapping) -- these are exactly the ones the agentic layer
#          (docs/04) is designed to investigate beyond a flat percentage, so building the
#          deterministic MDM-001/002 counts alongside Phase 4a is the natural pairing.
#   REF-*  (mapping-chain coverage per discipline, WBS phantom-key detection)
#   DUP-*/PLC-*  beyond what Layer 0 normalisation already reports as findings
#   OUT-*  (statistical outliers) -- Phase 2 "stats", not this file
