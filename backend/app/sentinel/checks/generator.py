"""Generated invariants -- coverage, not meaning.

Every check in this module is BUILT, not written: the generator walks the live column
list of every in-scope table (via `Normaliser.columns_of`, which queries `sys.columns` on
each run -- a column added yesterday is included today with zero code changes) and cross-
references `config/column_semantics.yaml` for the parts that cannot be inferred safely:

  F1/F2  date_order   -- explicit `before <= after` pairs declared in the YAML (docs/01e:
                         inferring pairs from column NAMES mispaired two 100%-NULL columns
                         in the discovery prototype; every pair here was reviewed)
  F3     future dates -- any column with role: actual + a date/datetime type: an actual
                         date after the run's live as_of_date is impossible
  F4     range        -- any column with a declared `value_range: [lo, hi]`
  F5     minimum       -- any column with a declared `minimum` (e.g. quantities >= 0)

F6 (orphan `_id` where no FK is declared) and F7 (content contradicts type/name) are NOT
built here -- see docs/03-ANOMALY-TAXONOMY.md Section 11 and docs/04 Section 2: F7 is
semantic-anomaly territory that belongs to the LLM/agentic layer (Phase 4), and F6 needs a
declared reference target per column to avoid a naming-convention guess, which is future
config work, not a gap papered over with an inferred heuristic.

Every generated check is registered through the SAME `register_check()` path (and the
SAME grain/baseline validation) as the hand-written checks in `business_rules.py` -- there
is no separate, looser mechanism for generated code.
"""
from __future__ import annotations

import re

from app.domain.models import Baseline, Finding, FindingClass, Grain, Severity
from app.logging import get_logger
from app.sentinel.checks.base import CheckContext, CheckOutcome, register_check
from app.sentinel.normalise.spec import NormalisationSpec

log = get_logger(__name__)

DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _q(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"refusing to interpolate non-identifier column name: {name!r}")
    return f"[{name}]"


# ------------------------------------------------------------------------ F1/F2 date order
def register_date_order_checks(spec: NormalisationSpec) -> int:
    """Register one check per `date_order` rule declared in column_semantics.yaml."""
    count = 0
    for table, sem in spec.tables.items():
        for rule in sem.date_order:
            b_col, a_col = _q(rule.before), _q(rule.after)

            def make_run(table=table, rule=rule, b_col=b_col, a_col=a_col):
                def run(ctx: CheckContext) -> CheckOutcome:
                    from app.domain.models import CheckResult

                    src = ctx.normalised(table)
                    if src is None:
                        return CheckOutcome(
                            result=CheckResult(
                                check_id=rule.check_id, family="TMP", status="skipped",
                                skip_reason=f"{table} not normalised this run",
                            )
                        )
                    sql = f"""
                        SELECT COUNT(*) AS n,
                               SUM(CASE WHEN {b_col} > {a_col} THEN 1 ELSE 0 END) AS violations,
                               MIN(DATEDIFF(day, {a_col}, {b_col})) AS worst_days
                        FROM {src.at_grain()}
                        WHERE {b_col} IS NOT NULL AND {a_col} IS NOT NULL
                    """
                    row = ctx.source.one(sql) or {}
                    n = int(row.get("n") or 0)
                    bad = int(row.get("violations") or 0)
                    result = CheckResult(
                        check_id=rule.check_id, family="TMP",
                        status="fail" if bad else "pass",
                        rows_scanned=n, violations=bad,
                        grain=src.grain.describe(), baseline=Baseline.NONE.value,
                    )
                    if not bad:
                        return CheckOutcome(result=result)
                    finding = Finding(
                        check_id=rule.check_id, family="TMP", severity=rule.severity,
                        finding_class=FindingClass.DEFECT,
                        title=f"{table}: {rule.before} after {rule.after} on {bad:,} rows",
                        entity_type="table", entity_id=table, entity_label=table,
                        affected_count=bad, grain=src.grain.describe(), baseline="none",
                        business_rule_ref=rule.business_rule_ref,
                        why_it_matters=(
                            rule.note
                            or f"{rule.before} should never be later than {rule.after}; "
                               f"{bad:,} of {n:,} comparable rows violate that."
                        ),
                        evidence=[{
                            "before_column": rule.before, "after_column": rule.after,
                            "rows_compared": n, "violations": bad,
                            "worst_days_inverted": row.get("worst_days"),
                        }],
                    )
                    return CheckOutcome(result=result, findings=[finding])

                return run

            register_check(
                id=rule.check_id, family="TMP", grain=Grain.row(), baseline=Baseline.NONE,
                fn=make_run(), severity=rule.severity.value,
                title=f"{table}: {rule.before} <= {rule.after}",
                business_rule_ref=rule.business_rule_ref, source="generated",
            )
            count += 1
    return count


# ------------------------------------------------------------------------- F3 future dates
def register_future_date_checks(spec: NormalisationSpec, live_columns) -> int:
    """One check per (table, actual-role date column) currently in scope.

    `live_columns(table)` returns the CURRENT sys.columns list -- so a new column with
    role: actual declared later, or an existing one, is picked up without touching this
    function; only column_semantics.yaml needs the role entry.
    """
    count = 0
    for table, sem in spec.tables.items():
        cols = live_columns(table)
        if not cols:
            continue
        type_of = {c["column_name"].lower(): c["data_type"] for c in cols}
        for cname, csem in sem.columns.items():
            if csem.role != "actual" or csem.all_null:
                continue
            dtype = type_of.get(cname.lower())
            if dtype not in DATE_TYPES:
                continue
            check_id = f"GEN-FUTURE-{table}-{cname}"
            col = _q(cname)

            def make_run(table=table, cname=cname, col=col, check_id=check_id):
                def run(ctx: CheckContext) -> CheckOutcome:
                    from app.domain.models import CheckResult

                    src = ctx.normalised(table)
                    if src is None:
                        return CheckOutcome(
                            result=CheckResult(
                                check_id=check_id, family="VAL", status="skipped",
                                skip_reason=f"{table} not normalised this run",
                            )
                        )
                    sql = f"""
                        SELECT COUNT(*) AS n,
                               SUM(CASE WHEN {col} > CAST('{ctx.as_of_date}' AS date)
                                        THEN 1 ELSE 0 END) AS future_rows,
                               CONVERT(varchar(30), MAX({col}), 121) AS max_value
                        FROM {src.at_grain()}
                        WHERE {col} IS NOT NULL
                    """
                    # CONVERT to varchar server-side, not a raw fetch of MAX(): some
                    # columns here (e.g. core.engineering_task_plan.actual_end) are
                    # `datetimeoffset` (ODBC type -155), which some driver versions
                    # cannot decode back to Python without an explicit converter
                    # registered. Converting to text in SQL Server sidesteps that
                    # entirely -- the comparison in the WHERE/CASE above still runs
                    # server-side against the native type either way.
                    row = ctx.source.one(sql) or {}
                    n = int(row.get("n") or 0)
                    bad = int(row.get("future_rows") or 0)
                    result = CheckResult(
                        check_id=check_id, family="VAL", status="fail" if bad else "pass",
                        rows_scanned=n, violations=bad, grain=src.grain.describe(),
                        baseline=Baseline.ACTUAL.value,
                    )
                    if not bad:
                        return CheckOutcome(result=result)
                    finding = Finding(
                        check_id=check_id, family="VAL", severity=Severity.HIGH,
                        finding_class=FindingClass.DEFECT,
                        title=f"{table}.{cname}: {bad:,} rows dated after today ({ctx.as_of_date})",
                        entity_type="column", entity_id=f"{table}.{cname}",
                        entity_label=f"{table}.{cname}",
                        affected_count=bad, grain=src.grain.describe(),
                        baseline=Baseline.ACTUAL.value,
                        why_it_matters=(
                            f"{cname} records what actually happened on site -- it cannot "
                            f"be later than the run's own as-of date ({ctx.as_of_date}), "
                            "read live from the database, not from a cached calendar."
                        ),
                        evidence=[{
                            "column": cname, "as_of_date": ctx.as_of_date,
                            "future_rows": bad, "max_value": str(row.get("max_value")),
                        }],
                    )
                    return CheckOutcome(result=result, findings=[finding])

                return run

            register_check(
                id=check_id, family="VAL", grain=sem.grain, baseline=Baseline.ACTUAL,
                fn=make_run(), severity="high",
                title=f"{table}.{cname} not in the future", source="generated",
            )
            count += 1
    return count


# ------------------------------------------------------------------------------- F4 range
def register_range_checks(spec: NormalisationSpec, live_columns) -> int:
    count = 0
    for table, sem in spec.tables.items():
        cols = {c["column_name"].lower() for c in live_columns(table)}
        for cname, csem in sem.columns.items():
            if csem.all_null or csem.value_range is None:
                continue
            if cname.lower() not in cols:
                continue  # declared but the column no longer exists on this run
            lo, hi = csem.value_range
            check_id = f"GEN-RANGE-{table}-{cname}"
            col = _q(cname)

            def make_run(table=table, cname=cname, col=col, lo=lo, hi=hi, check_id=check_id):
                def run(ctx: CheckContext) -> CheckOutcome:
                    from app.domain.models import CheckResult

                    src = ctx.normalised(table)
                    if src is None:
                        return CheckOutcome(
                            result=CheckResult(
                                check_id=check_id, family="VAL", status="skipped",
                                skip_reason=f"{table} not normalised this run",
                            )
                        )
                    # TRY_CAST, not a bare comparison: several columns here are
                    # declared numeric semantically but stored as nvarchar in the
                    # source (docs/01: buffer_status, some job_progress columns) --
                    # a plain `{col} < {lo}` triggers SQL Server's implicit nvarchar
                    # -> numeric conversion and can throw "arithmetic overflow" on a
                    # value that doesn't parse cleanly. Non-numeric content is itself
                    # an anomaly (VAL-12 in docs/03) and is reported as its own
                    # finding below, not swallowed as a crash.
                    sql = f"""
                        SELECT COUNT(*) AS n,
                               SUM(CASE WHEN v IS NULL THEN 1 ELSE 0 END) AS non_numeric,
                               SUM(CASE WHEN v IS NOT NULL AND (v < {lo} OR v > {hi})
                                        THEN 1 ELSE 0 END) AS out_of_range,
                               MIN(v) AS min_v, MAX(v) AS max_v
                        FROM (SELECT TRY_CAST({col} AS float) AS v
                              FROM {src.at_grain()} WHERE {col} IS NOT NULL) x
                    """
                    row = ctx.source.one(sql) or {}
                    n = int(row.get("n") or 0)
                    bad = int(row.get("out_of_range") or 0)
                    non_numeric = int(row.get("non_numeric") or 0)
                    result = CheckResult(
                        check_id=check_id, family="VAL",
                        status="fail" if (bad or non_numeric) else "pass",
                        rows_scanned=n, violations=bad + non_numeric,
                        grain=src.grain.describe(), baseline=Baseline.NONE.value,
                    )
                    findings: list[Finding] = []
                    if bad:
                        findings.append(Finding(
                            check_id=check_id, family="VAL", severity=Severity.MEDIUM,
                            finding_class=FindingClass.DEFECT,
                            title=f"{table}.{cname}: {bad:,} rows outside [{lo}, {hi}]",
                            entity_type="column", entity_id=f"{table}.{cname}",
                            entity_label=f"{table}.{cname}",
                            affected_count=bad, grain=src.grain.describe(), baseline="none",
                            why_it_matters=(
                                f"{cname} is declared to hold values in [{lo}, {hi}]; "
                                f"observed range is [{row.get('min_v')}, {row.get('max_v')}]."
                            ),
                            evidence=[{
                                "column": cname, "declared_range": [lo, hi],
                                "observed_min": row.get("min_v"), "observed_max": row.get("max_v"),
                                "out_of_range_rows": bad,
                            }],
                        ))
                    if non_numeric:
                        findings.append(Finding(
                            check_id=f"{check_id}-NONNUMERIC", family="VAL",
                            severity=Severity.MEDIUM, finding_class=FindingClass.DEFECT,
                            title=(
                                f"{table}.{cname}: {non_numeric:,} rows are non-numeric "
                                "text in a column declared to hold a number"
                            ),
                            entity_type="column", entity_id=f"{table}.{cname}",
                            entity_label=f"{table}.{cname}",
                            affected_count=non_numeric, grain=src.grain.describe(),
                            baseline="none",
                            why_it_matters=(
                                f"{cname} is stored as text and {non_numeric:,} non-NULL "
                                "values do not parse as a number at all -- these were "
                                "excluded from the range check above because there is "
                                "nothing numeric to compare."
                            ),
                            evidence=[{"column": cname, "non_numeric_rows": non_numeric}],
                        ))
                    return CheckOutcome(result=result, findings=findings)

                return run

            register_check(
                id=check_id, family="VAL", grain=sem.grain, baseline=Baseline.NONE,
                fn=make_run(), severity="medium",
                title=f"{table}.{cname} in [{lo}, {hi}]", source="generated",
            )
            count += 1
    return count


# --------------------------------------------------------------------------- F5 minimum
def register_minimum_checks(spec: NormalisationSpec, live_columns) -> int:
    count = 0
    for table, sem in spec.tables.items():
        cols = {c["column_name"].lower() for c in live_columns(table)}
        for cname, csem in sem.columns.items():
            if csem.all_null or csem.minimum is None:
                continue
            if cname.lower() not in cols:
                continue
            lo = csem.minimum
            check_id = f"GEN-MIN-{table}-{cname}"
            col = _q(cname)

            def make_run(table=table, cname=cname, col=col, lo=lo, check_id=check_id):
                def run(ctx: CheckContext) -> CheckOutcome:
                    from app.domain.models import CheckResult

                    src = ctx.normalised(table)
                    if src is None:
                        return CheckOutcome(
                            result=CheckResult(
                                check_id=check_id, family="VAL", status="skipped",
                                skip_reason=f"{table} not normalised this run",
                            )
                        )
                    # TRY_CAST for the same reason as register_range_checks above.
                    sql = f"""
                        SELECT COUNT(*) AS n,
                               SUM(CASE WHEN v IS NULL THEN 1 ELSE 0 END) AS non_numeric,
                               SUM(CASE WHEN v IS NOT NULL AND v < {lo}
                                        THEN 1 ELSE 0 END) AS below_min,
                               MIN(v) AS min_v
                        FROM (SELECT TRY_CAST({col} AS float) AS v
                              FROM {src.at_grain()} WHERE {col} IS NOT NULL) x
                    """
                    row = ctx.source.one(sql) or {}
                    n = int(row.get("n") or 0)
                    bad = int(row.get("below_min") or 0)
                    non_numeric = int(row.get("non_numeric") or 0)
                    result = CheckResult(
                        check_id=check_id, family="VAL",
                        status="fail" if (bad or non_numeric) else "pass",
                        rows_scanned=n, violations=bad + non_numeric,
                        grain=src.grain.describe(), baseline=Baseline.NONE.value,
                    )
                    findings: list[Finding] = []
                    if bad:
                        findings.append(Finding(
                            check_id=check_id, family="VAL", severity=Severity.MEDIUM,
                            finding_class=FindingClass.DEFECT,
                            title=f"{table}.{cname}: {bad:,} rows below the declared minimum {lo}",
                            entity_type="column", entity_id=f"{table}.{cname}",
                            entity_label=f"{table}.{cname}",
                            affected_count=bad, grain=src.grain.describe(), baseline="none",
                            why_it_matters=(
                                f"{cname} is declared to never go below {lo}; observed "
                                f"minimum is {row.get('min_v')}."
                            ),
                            evidence=[{
                                "column": cname, "declared_minimum": lo,
                                "observed_minimum": row.get("min_v"), "below_min_rows": bad,
                            }],
                        ))
                    if non_numeric:
                        findings.append(Finding(
                            check_id=f"{check_id}-NONNUMERIC", family="VAL",
                            severity=Severity.MEDIUM, finding_class=FindingClass.DEFECT,
                            title=(
                                f"{table}.{cname}: {non_numeric:,} rows are non-numeric "
                                "text in a column declared to hold a number"
                            ),
                            entity_type="column", entity_id=f"{table}.{cname}",
                            entity_label=f"{table}.{cname}",
                            affected_count=non_numeric, grain=src.grain.describe(),
                            baseline="none",
                            why_it_matters=(
                                f"{cname} is stored as text and {non_numeric:,} non-NULL "
                                "values do not parse as a number at all."
                            ),
                            evidence=[{"column": cname, "non_numeric_rows": non_numeric}],
                        ))
                    return CheckOutcome(result=result, findings=findings)

                return run

            register_check(
                id=check_id, family="VAL", grain=sem.grain, baseline=Baseline.NONE,
                fn=make_run(), severity="medium",
                title=f"{table}.{cname} >= {lo}", source="generated",
            )
            count += 1
    return count


def register_all_generated(spec: NormalisationSpec, live_columns) -> dict[str, int]:
    """Call once, before a run's Phase 1. Returns a per-family count for logging/CLI."""
    counts = {
        "date_order": register_date_order_checks(spec),
        "future_date": register_future_date_checks(spec, live_columns),
        "range": register_range_checks(spec, live_columns),
        "minimum": register_minimum_checks(spec, live_columns),
    }
    log.info("generator.registered", **counts, total=sum(counts.values()))
    return counts
