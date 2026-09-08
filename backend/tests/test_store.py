"""FindingsStore tests against an in-memory / temp-file SQLite DB. No live DB needed."""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.db.store import SCHEMA_VERSION, FindingsStore
from app.domain.models import (
    CheckResult,
    Finding,
    FindingClass,
    NormalisationAction,
    NormalisationKind,
    Run,
    RunStatus,
    Severity,
)


def _run(run_id: str = "run_test") -> Run:
    return Run(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        db_name="AlTasnimBI",
        as_of_date=date(2026, 9, 7),
    )


def _finding(check_id="BIZ-101", severity=Severity.HIGH, cls=FindingClass.VIOLATION,
             well_id=None, entity_id=None) -> Finding:
    return Finding(
        check_id=check_id,
        family=check_id.split("-")[0],
        severity=severity,
        finding_class=cls,
        title=f"test finding {check_id}",
        entity_type="well" if well_id else "table",
        entity_id=entity_id,
        well_id=well_id,
        affected_count=1,
        grain="row",
        baseline="target",
    )


def test_migrate_is_idempotent(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    assert store.migrate() == SCHEMA_VERSION
    assert store.migrate() == SCHEMA_VERSION  # calling twice must not error or duplicate rows
    store.close()


def test_create_and_finish_run(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run = _run()
    store.create_run(run)
    fetched = store.get_run(run.run_id)
    assert fetched is not None
    assert fetched["status"] == "running"

    store.finish_run(run.run_id, RunStatus.COMPLETED, rows_scanned=1000, checks_run=5)
    fetched = store.get_run(run.run_id)
    assert fetched["status"] == "completed"
    assert fetched["rows_scanned"] == 1000
    store.close()


def test_add_and_query_findings(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run = _run()
    store.create_run(run)

    findings = [
        _finding("BIZ-101", Severity.HIGH, FindingClass.VIOLATION, well_id=101, entity_id="101"),
        _finding("BIZ-102", Severity.CRITICAL, FindingClass.DEFECT, well_id=102, entity_id="102"),
        _finding("CMP-003", Severity.LOW, FindingClass.PENDING, well_id=101, entity_id="101b"),
    ]
    n = store.add_findings(run.run_id, findings)
    assert n == 3

    all_findings = store.findings(run.run_id)
    assert len(all_findings) == 3
    # Critical must sort before high, high before low (per Severity.rank).
    assert all_findings[0]["severity"] == "critical"

    actionable = store.findings(run.run_id, actionable_only=True)
    assert len(actionable) == 2  # VIOLATION + DEFECT, not PENDING

    by_well = store.findings(run.run_id, well_id=101)
    assert len(by_well) == 2

    sev = store.severity_counts(run.run_id)
    assert sev == {"high": 1, "critical": 1, "low": 1}

    cls = store.class_counts(run.run_id)
    assert cls["pending"] == 1
    assert cls["violation"] == 1
    store.close()


def test_finding_lifecycle_new_then_recurring(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)

    run1 = _run("run_1")
    store.create_run(run1)
    store.add_findings(run1.run_id, [_finding("BIZ-101", entity_id="well_36754")])
    store.finish_run(run1.run_id, RunStatus.COMPLETED)
    lc1 = store.mark_finding_lifecycle(run1.run_id)
    assert lc1["new"] == 1
    assert lc1["recurring"] == 0

    run2 = _run("run_2")
    store.create_run(run2)
    store.add_findings(
        run2.run_id,
        [
            _finding("BIZ-101", entity_id="well_36754"),  # same finding again
            _finding("BIZ-102", entity_id="well_99999"),  # a new one
        ],
    )
    store.finish_run(run2.run_id, RunStatus.COMPLETED)
    lc2 = store.mark_finding_lifecycle(run2.run_id)
    assert lc2["new"] == 1
    assert lc2["recurring"] == 1
    assert lc2["resolved"] == 0  # BIZ-101/well_36754 is still present in run2
    store.close()


def test_finding_lifecycle_resolved(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run1 = _run("run_1")
    store.create_run(run1)
    store.add_findings(run1.run_id, [_finding("BIZ-101", entity_id="well_1")])
    store.finish_run(run1.run_id, RunStatus.COMPLETED)
    store.mark_finding_lifecycle(run1.run_id)

    run2 = _run("run_2")
    store.create_run(run2)
    # BIZ-101/well_1 does not appear in run2 -> it was fixed.
    store.add_findings(run2.run_id, [_finding("CMP-003", entity_id="well_2")])
    store.finish_run(run2.run_id, RunStatus.COMPLETED)
    lc2 = store.mark_finding_lifecycle(run2.run_id)
    assert lc2["resolved"] == 1
    store.close()


def test_well_scorecard_orders_by_actionable_then_count(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run = _run()
    store.create_run(run)
    store.add_findings(
        run.run_id,
        [
            _finding("BIZ-101", Severity.CRITICAL, FindingClass.VIOLATION, well_id=1, entity_id="a"),
            _finding("BIZ-102", Severity.CRITICAL, FindingClass.VIOLATION, well_id=1, entity_id="b"),
            _finding("CMP-003", Severity.LOW, FindingClass.PENDING, well_id=2, entity_id="c"),
        ],
    )
    rows = store.well_scorecard(run.run_id)
    assert rows[0]["well_id"] == 1
    assert rows[0]["actionable"] == 2
    assert rows[0]["critical"] == 2
    store.close()


def test_normalisation_actions_round_trip(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run = _run()
    store.create_run(run)
    action = NormalisationAction(
        kind=NormalisationKind.DEDUP,
        target="well.well_progress",
        rows_affected=98771,
        rows_total=99589,
        check_id="DUP-001",
        detail={"keys": ["well_id", "week_number"]},
    )
    store.add_normalisation_actions(run.run_id, [action])
    actions = store.normalisation_actions(run.run_id)
    assert len(actions) == 1
    assert actions[0]["rows_affected"] == 98771
    assert actions[0]["pct"] == round(100.0 * 98771 / 99589, 2)
    store.close()


def test_check_results_round_trip(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run = _run()
    store.create_run(run)
    store.add_check_results(
        run.run_id,
        [
            CheckResult(check_id="TMP-01", family="TMP", status="pass", rows_scanned=814),
            CheckResult(check_id="TMP-02", family="TMP", status="fail", violations=45),
        ],
    )
    results = store.check_results(run.run_id)
    assert {r["check_id"] for r in results} == {"TMP-01", "TMP-02"}
    store.close()


def test_metric_baseline_excludes_current_run(tmp_store_path) -> None:
    store = FindingsStore(path=tmp_store_path)
    run1 = _run("run_1")
    store.create_run(run1)
    store.finish_run(run1.run_id, RunStatus.COMPLETED)
    store.add_metrics(run1.run_id, [("well.well_master", "status_id", "null_pct", 100.0)])

    run2 = _run("run_2")
    store.create_run(run2)
    baseline = store.baseline_metric("well.well_master", "status_id", "null_pct", run2.run_id)
    assert baseline == 100.0
    store.close()
