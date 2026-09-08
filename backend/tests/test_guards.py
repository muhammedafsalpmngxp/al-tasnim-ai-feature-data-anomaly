"""Regression tests for the guards that stop the report being wrong.

Each test here corresponds to a mistake that was actually made — by me, twice, and by an
external tool. They exist so those mistakes cannot recur silently.
See docs/01c and docs/01d.
"""
from __future__ import annotations

import pytest

from app.db.source import ReadOnlyGuard, ReadOnlyViolation
from app.domain.models import (
    Baseline,
    FindingClass,
    Grain,
    GrainKind,
    Severity,
)


# --------------------------------------------------------------------------------------
# Guard 1 — the read-only statement guard
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "  \n SELECT a FROM t",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT * FROM t WHERE name = 'DROP TABLE x'",      # keyword inside a literal
        "SELECT 1 -- ; DROP TABLE t",                        # keyword inside a comment
        "SELECT 1 /* UPDATE t SET a=1 */",                   # keyword in a block comment
        "SELECT 1;",                                         # a single trailing semicolon
        "SELECT name FROM t WHERE note = 'it''s an INSERT'",  # escaped quote in a literal
    ],
)
def test_guard_allows_reads(sql: str) -> None:
    ReadOnlyGuard.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM well.well_master",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "CREATE TABLE t (a int)",
        "ALTER TABLE t ADD b int",
        "MERGE t USING s ON 1=1",
        "GRANT SELECT ON t TO u",
        "EXEC sp_who",
        "EXECUTE sp_who",
        "BACKUP DATABASE d TO DISK='x'",
        "SELECT 1; DROP TABLE t",                 # statement stacking
        "SELECT 1; SELECT 2",                     # two statements, both reads
        "SELECT * INTO #tmp FROM t",              # writes to a temp table
        "",
        "   ",
    ],
)
def test_guard_blocks_writes(sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyGuard.validate(sql)


def test_guard_rejects_statement_not_starting_with_select() -> None:
    with pytest.raises(ReadOnlyViolation, match="must begin with SELECT or WITH"):
        ReadOnlyGuard.validate("DECLARE @x int; SELECT @x")


# --------------------------------------------------------------------------------------
# Guard 2 — Baseline.COMMITTED is disabled
#
# The business confirmed committed_start/committed_end have no established meaning, so they
# are display-only. A check may not use them as a yardstick.
# --------------------------------------------------------------------------------------
def test_committed_baseline_is_disabled() -> None:
    assert Baseline.COMMITTED.enabled is False
    assert Baseline.COMMITTED.disabled_reason is not None
    assert "TARGET" in Baseline.COMMITTED.disabled_reason


def test_target_is_the_authoritative_baseline() -> None:
    assert Baseline.TARGET.enabled is True
    assert Baseline.TARGET.disabled_reason is None


def test_p6_baseline_is_available_but_is_not_the_default() -> None:
    """P6 is usable — `duration` genuinely describes the P6 span — but it is not TARGET.

    Measured: using P6 as the delay baseline reports 5.5x more late tasks (23,628 vs
    4,316 over the same 83,076 finished tasks).
    """
    assert Baseline.P6.enabled is True
    assert Baseline.P6 is not Baseline.TARGET


# --------------------------------------------------------------------------------------
# Guard 3 — Grain must be declared explicitly
#
# well.task_daily is a daily log at 3.03 rows per task. Counting per row overstated one
# finding 15x (42,831 rows vs 2,804 tasks).
# --------------------------------------------------------------------------------------
def test_latest_per_requires_keys_and_order() -> None:
    with pytest.raises(ValueError, match="at least one key"):
        Grain.latest_per(order_by="ActionOn DESC")
    with pytest.raises(ValueError, match="order_by"):
        Grain.latest_per("well_id", order_by="")


def test_latest_per_describes_itself_for_the_report() -> None:
    g = Grain.latest_per("well_id", "task_code", order_by="ActionOn DESC, id DESC")
    assert g.kind is GrainKind.LATEST_PER
    assert g.keys == ("well_id", "task_code")
    # Every finding prints its grain, so a reader can see what was counted.
    assert "well_id, task_code" in g.describe()
    assert "ActionOn DESC" in g.describe()


def test_row_and_aggregate_grains_need_no_keys() -> None:
    assert Grain.row().describe() == "row"
    assert Grain.aggregate().describe() == "aggregate"


def test_grain_is_frozen() -> None:
    """A check must not be able to mutate its declared grain after registration."""
    g = Grain.row()
    with pytest.raises((AttributeError, TypeError)):
        g.kind = GrainKind.AGGREGATE  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Guard 4 — PENDING and DESIGN are not actionable
#
# Of 1,278 "missing" schedule dates, 916 are wells correctly awaiting a future milestone
# and only 362 are genuinely overdue. A flat severity list is what let a non-issue sit at
# the top of the external report.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls, actionable",
    [
        (FindingClass.VIOLATION, True),
        (FindingClass.DEFECT, True),
        (FindingClass.GAP, True),
        (FindingClass.PENDING, False),
        (FindingClass.DESIGN, False),
        (FindingClass.RISK, False),
        (FindingClass.REVIEW, False),
    ],
)
def test_finding_class_actionability(cls: FindingClass, actionable: bool) -> None:
    assert cls.is_actionable is actionable


def test_pending_and_design_still_exist_as_classes() -> None:
    """They must be reportable, not discarded — a reader has to see they were cleared."""
    assert FindingClass("pending") is FindingClass.PENDING
    assert FindingClass("design") is FindingClass.DESIGN


# --------------------------------------------------------------------------------------
# Guard 5 — severity ordering, so the report ranks correctly
# --------------------------------------------------------------------------------------
def test_severity_orders_critical_first_and_info_last() -> None:
    ordered = sorted(Severity, key=lambda s: s.rank)
    assert ordered[0] is Severity.CRITICAL
    assert ordered[-1] is Severity.INFO
    # An outlier is a question, not a defect: REVIEW must never outrank a real problem.
    assert Severity.REVIEW.rank > Severity.MEDIUM.rank
    assert Severity.REVIEW.rank > Severity.HIGH.rank
