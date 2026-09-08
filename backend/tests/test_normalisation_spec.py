"""Load config/*.yaml exactly as the app does and assert the contract holds.

No live DB needed -- this only parses the YAML the business will actually review.
"""
from __future__ import annotations

import pytest

from app.domain.models import Baseline
from app.sentinel.normalise.spec import ConfigError, load_spec


@pytest.fixture(scope="module")
def spec():
    return load_spec()


def test_spec_loads_without_error(spec) -> None:
    assert spec.snapshots
    assert spec.dedup
    assert spec.placeholder_dates is not None


def test_engineering_task_plan_snapshot_rule_present(spec) -> None:
    rule = spec.snapshot_for("core.engineering_task_plan")
    assert rule is not None
    assert rule.timestamp_column == "Time_Stamp"
    assert rule.partition_by == ["project_id"]


def test_well_progress_dedup_is_keep_latest(spec) -> None:
    rule = spec.dedup_for("well.well_progress")
    assert rule is not None
    assert rule.mode == "keep_latest"
    assert rule.keys == ["well_id", "week_number"]
    assert rule.compare_columns  # a business decision, not left to a heuristic


def test_task_daily_dedup_is_exact_only_not_keep_latest(spec) -> None:
    """This table is a daily log -- one row per day worked is correct history.
    Only byte-identical duplicates may be collapsed automatically.
    """
    rule = spec.dedup_for("well.task_daily")
    assert rule is not None
    assert rule.mode == "exact_only"
    assert rule.keys == ["well_id", "task_code", "ActionOn"]


def test_activity_task_plan_dedup_is_disabled_pending_decision(spec) -> None:
    disabled = [r for r in spec.dedup if r.table == "dbo.activity_task_plan"]
    assert disabled and not disabled[0].enabled
    # dedup_for() must not return a disabled rule -- a disabled rule must not run silently.
    assert spec.dedup_for("dbo.activity_task_plan") is None


def test_committed_columns_are_display_only_in_semantics(spec) -> None:
    sem = spec.semantics_for("well.task_daily")
    assert sem is not None
    cstart = sem.column("committed_start")
    cend = sem.column("committed_end")
    assert cstart is not None and cend is not None
    assert cstart.role == "display_only"
    assert cend.role == "display_only"
    # The role maps to the disabled baseline -- a check cannot accidentally use it.
    assert cstart.baseline is Baseline.COMMITTED
    assert Baseline.COMMITTED.enabled is False


def test_target_columns_are_the_authoritative_baseline(spec) -> None:
    sem = spec.semantics_for("well.task_daily")
    tstart = sem.column("target_start")
    assert tstart is not None
    assert tstart.authoritative is True
    assert tstart.baseline is Baseline.TARGET


def test_p6_columns_are_not_authoritative(spec) -> None:
    sem = spec.semantics_for("well.task_daily")
    p6 = sem.column("startDate")
    assert p6 is not None
    assert p6.authoritative is False
    assert p6.baseline is Baseline.P6


def test_unused_columns_generate_no_invariants(spec) -> None:
    """well_master.status_id has a declared FK and is 100% NULL -- it must not be paired
    with anything by the invariant generator (Phase 2), or it produces false checks the
    way the naive name-based prototype did (docs/01e Section 3).
    """
    sem = spec.semantics_for("well.well_master")
    status = sem.column("status_id")
    assert status is not None
    assert status.all_null is True
    assert status.generates_invariants is False


def test_task_daily_grain_is_latest_per_not_row(spec) -> None:
    """well.task_daily is a daily log at 3.03 rows per task. Declaring grain=row here would
    silently reintroduce the 15x overstatement documented in docs/01c.
    """
    sem = spec.semantics_for("well.task_daily")
    assert sem.grain.keys == ("well_id", "task_code")


def test_engineering_task_plan_grain_is_snapshot_pinned(spec) -> None:
    sem = spec.semantics_for("core.engineering_task_plan")
    assert sem.grain.keys == ("project_id", "code")


def test_pii_columns_cover_contact_and_email(spec) -> None:
    assert spec.is_pii("dbo", "employee_contact") or spec.is_pii(
        "dbo.employee_contact", "any_column"
    ) or any("employee_contact" in p for p in spec.pii_columns)
    assert spec.is_pii("ref.employee", "email")
    assert not spec.is_pii("ref.employee", "emp_name")


def test_bad_dedup_mode_is_rejected() -> None:
    from app.sentinel.normalise.spec import DedupRule

    with pytest.raises(ConfigError, match="mode must be"):
        DedupRule(table="t", keys=["a"], order_by="a DESC", mode="bogus", check_id="X")


def test_dedup_requires_at_least_one_key() -> None:
    from app.sentinel.normalise.spec import DedupRule

    with pytest.raises(ConfigError, match="at least one key"):
        DedupRule(table="t", keys=[], order_by="a DESC", mode="keep_latest", check_id="X")
