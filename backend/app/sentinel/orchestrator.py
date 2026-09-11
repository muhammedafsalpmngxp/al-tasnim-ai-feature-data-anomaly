"""Run lifecycle and phase sequencing.

Phase 1 implements phases 0 (normalise) and 5 (persist). Phases 1-4 and 6 are registered
here with explicit `skipped` status so a run always reports what it did *and did not* do —
a report that silently omits a phase is how "did you check X?" happens.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.config import Settings, get_settings
from app.db.source import SourceDatabase
from app.db.store import FindingsStore
from app.domain.models import CheckResult, Run, RunStatus, utcnow
from app.logging import get_logger
from app.sentinel.checks import business_rules  # noqa: F401 -- registers hand-written checks
from app.sentinel.checks.base import CheckContext, all_checks, clear_generated_checks
from app.sentinel.checks.compiled import register_compiled_checks
from app.sentinel.checks.generator import register_all_generated
from app.sentinel.llm.client import LLMClient
from app.sentinel.llm.enrich import enrich_run
from app.sentinel.normalise.normaliser import Normaliser
from app.sentinel.normalise.spec import NormalisationSpec, load_spec
from app.reporting.excel import build_excel_report
from app.reporting.word import build_word_report
from app.sentinel.schema_snapshot import SchemaSnapshotter
from app.sentinel.scope import Scope, TableRef

log = get_logger(__name__)

ProgressFn = Callable[[str, str, dict[str, Any]], None]

PHASES = (
    ("normalise", "Phase 0 — normalise"),
    ("checks", "Phase 1 — deterministic checks"),
    ("stats", "Phase 2 — statistics"),
    ("verify", "Phase 3 — verify"),
    ("llm", "Phase 4 — LLM enrichment"),
    ("persist", "Phase 5 — persist"),
    ("reports", "Phase 6 — reports"),
)

# Phases not yet built. Named so a run reports them as skipped rather than silently absent.
NOT_YET_IMPLEMENTED = {
    "stats": "Phase 7 of the build plan",
    "verify": "Phase 4 of the build plan",
}


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    normalisation: dict[str, Any] = field(default_factory=dict)
    findings_written: int = 0
    actions_written: int = 0
    lifecycle: dict[str, int] = field(default_factory=dict)
    phases_skipped: list[str] = field(default_factory=list)
    schema_drift: dict[str, Any] = field(default_factory=dict)
    checks_run: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    checks_skipped_individually: int = 0
    checks_findings: int = 0
    llm_enabled: bool = False
    llm_skip_reason: str = ""
    llm_findings_narrated: int = 0
    llm_findings_reused: int = 0
    llm_incidents: int = 0
    llm_usage: dict[str, Any] = field(default_factory=dict)
    xlsx_path: str | None = None
    docx_path: str | None = None
    error: str | None = None
    seconds: float = 0.0


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        source: SourceDatabase | None = None,
        store: FindingsStore | None = None,
        spec: NormalisationSpec | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.s = settings or get_settings()
        self.source = source or SourceDatabase(self.s)
        self.store = store or FindingsStore(self.s)
        self.spec = spec or load_spec()
        self.scope = Scope(self.s)
        self._progress = progress or (lambda phase, state, data: None)

    def _emit(self, phase: str, state: str, **data: Any) -> None:
        self._progress(phase, state, data)
        log.info(f"phase.{state}", phase=phase, **data)

    # ------------------------------------------------------------------------ run
    def run(
        self,
        *,
        tables: list[str] | None = None,
        triggered_by: str | None = None,
        dry_run: bool = False,
        enrich: bool = True,
        generate_reports: bool = True,
    ) -> RunOutcome:
        started = utcnow()
        t0 = datetime.now()

        # The account must be a reader. If someone points this at a privileged login the
        # core safety assumption is void and the run must not proceed.
        info = self.source.assert_read_only_account()

        run = Run(
            run_id=self.store.new_run_id(),
            started_at=started,
            triggered_by=triggered_by or "cli",
            db_name=info.get("db_name"),
            server_now=info.get("server_now"),
            as_of_date=info.get("server_today"),
            scope=self.scope.describe(),
        )
        outcome = RunOutcome(run_id=run.run_id, status=RunStatus.RUNNING)
        if not dry_run:
            self.store.create_run(run)
        log.info(
            "run.started",
            run_id=run.run_id,
            database=run.db_name,
            as_of=str(run.as_of_date),
            dry_run=dry_run,
        )

        check_results: list[CheckResult] = []
        try:
            # ------------------------------------------- Phase 0a schema discovery
            # Runs BEFORE normalisation and is fully dynamic: it introspects whatever
            # tables/columns exist right now (via Scope, which is config not code), so a
            # table or column added to the source database appears here with zero code
            # changes, and is reported as drift the first time it is seen.
            self._emit("schema", "started")
            snapshotter = SchemaSnapshotter(self.source, self.scope)
            snapshots = snapshotter.capture()
            diff = snapshotter.diff_against_baseline(self.store, run.run_id, snapshots)
            drift_findings = snapshotter.findings_for(diff)
            # Coverage facts the check layer structurally cannot report: a table that is
            # empty, and a table holding data that no declared check opens. Free -- the
            # row counts come from the capture above.
            drift_findings.extend(
                snapshotter.coverage_findings(
                    snapshots, self.spec.tables_touched | set(self.spec.tables)
                )
            )
            drift_findings.extend(
                snapshotter.dead_column_findings(
                    snapshots, max_rows=self.s.dq_large_table_row_limit
                )
            )
            outcome.schema_drift = {
                "tables_scanned": len(snapshots),
                "baseline_run_id": diff.baseline_run_id,
                "tables_added": diff.tables_added,
                "tables_removed": diff.tables_removed,
                "columns_added": diff.columns_added,
                "columns_removed": diff.columns_removed,
                "columns_changed": diff.columns_changed,
            }
            self._emit(
                "schema", "completed",
                tables_scanned=len(snapshots),
                drift_findings=len(drift_findings),
            )

            # ---------------------------------------------------- Phase 0b normalise
            self._emit("normalise", "started")
            normaliser = Normaliser(
                self.source, self.spec, self.scope, as_of=str(run.as_of_date)
            )
            refs = [TableRef.parse(t) for t in tables] if tables else None
            normaliser.build_all(refs)
            normaliser.findings.extend(drift_findings)
            summary = normaliser.summary()
            outcome.normalisation = summary
            run.snapshot_pinned = summary.get("snapshots_pinned", {})
            self._emit("normalise", "completed", **{
                k: v for k, v in summary.items() if k != "snapshots_pinned"
            })

            # ------------------------------------------------------ Phase 1 checks
            self._emit("checks", "started")
            clear_generated_checks()  # rebuild fresh each run -- see base.py docstring
            register_all_generated(
                self.spec,
                live_columns=lambda table: normaliser.columns_of(TableRef.parse(table)),
            )
            register_compiled_checks(self.store)  # the agentic layer's ONLY runtime touch
            check_ctx = CheckContext(
                source=self.source, scope=self.scope, spec=self.spec,
                sources=normaliser.sources, as_of_date=str(run.as_of_date),
            )
            check_findings: list = []
            for registered in all_checks():
                out = registered.run(check_ctx)
                check_results.append(out.result)
                check_findings.extend(out.findings)
                # One line per check, not just the aggregate: a check that quietly
                # errored or skipped is a hole in this run's coverage, and the aggregate
                # count alone doesn't say WHICH check went missing.
                logger = log.warning if out.result.status in ("error", "skipped") else log.info
                logger(
                    f"check.{out.result.status}",
                    check_id=out.result.check_id,
                    family=out.result.family,
                    violations=out.result.violations,
                    rows_scanned=out.result.rows_scanned,
                    findings=len(out.findings),
                    **({"reason": out.result.skip_reason} if out.result.skip_reason else {}),
                    **({"error": out.result.error_text} if out.result.error_text else {}),
                )
                if out.result.status == "pass":
                    outcome.checks_passed += 1
                elif out.result.status == "fail":
                    outcome.checks_failed += 1
                elif out.result.status == "skipped":
                    outcome.checks_skipped_individually += 1
            outcome.checks_run = len(all_checks())
            outcome.checks_findings = len(check_findings)
            normaliser.findings.extend(check_findings)
            self._emit(
                "checks", "completed",
                checks_run=outcome.checks_run, passed=outcome.checks_passed,
                failed=outcome.checks_failed,
                skipped=outcome.checks_skipped_individually,
                findings=outcome.checks_findings,
            )

            # --------------------------------------------- Phases 2-4: not yet built
            for phase, note in NOT_YET_IMPLEMENTED.items():
                if phase == "reports":
                    continue
                outcome.phases_skipped.append(phase)
                check_results.append(
                    CheckResult(
                        check_id=f"PHASE-{phase.upper()}",
                        family="PHASE",
                        status="skipped",
                        skip_reason=f"not implemented yet — {note}",
                    )
                )
                self._emit(phase, "skipped", reason=note)

            # ------------------------------------------------------ Phase 5 persist
            self._emit("persist", "started")
            if not dry_run:
                snapshotter.persist(self.store, run.run_id, snapshots)
                outcome.actions_written = self.store.add_normalisation_actions(
                    run.run_id, normaliser.actions
                )
                outcome.findings_written = self.store.add_findings(
                    run.run_id, normaliser.findings
                )
                self.store.add_check_results(run.run_id, check_results)
                self.store.add_metrics(
                    run.run_id,
                    [
                        (name, "", "rows_raw", float(src.rows_raw))
                        for name, src in normaliser.sources.items()
                    ]
                    + [
                        (name, "", "rows_effective", float(src.rows_effective))
                        for name, src in normaliser.sources.items()
                    ],
                )
                outcome.lifecycle = self.store.mark_finding_lifecycle(run.run_id)
            else:
                outcome.findings_written = len(normaliser.findings)
                outcome.actions_written = len(normaliser.actions)
            self._emit(
                "persist",
                "completed",
                findings=outcome.findings_written,
                actions=outcome.actions_written,
            )

            # Populate the run row's stats NOW, before enrichment/reports render --
            # otherwise the reports render against a run row that still shows
            # finished_at=None, rows_scanned=0, findings_total=0 (status stays RUNNING
            # here; the real COMPLETED/finished_at timestamp is set once more at the
            # very end, after reports actually finish building).
            if not dry_run:
                self.store.finish_run(
                    run.run_id,
                    RunStatus.RUNNING,
                    rows_scanned=summary.get("rows_effective", 0),
                    checks_run=outcome.checks_run,
                    checks_passed=outcome.checks_passed,
                    checks_skipped=len(outcome.phases_skipped) + outcome.checks_skipped_individually,
                    snapshot_pinned=run.snapshot_pinned,
                )

            # ------------------------------------------------------ Phase 4 LLM enrich
            if dry_run:
                outcome.phases_skipped.append("llm")
                self._emit("llm", "skipped", reason="dry-run: nothing persisted to enrich")
            elif not enrich:
                outcome.phases_skipped.append("llm")
                self._emit("llm", "skipped", reason="enrich=False")
            else:
                self._emit("llm", "started")
                enrich_outcome = enrich_run(self.store, run.run_id, LLMClient(self.s))
                outcome.llm_enabled = not enrich_outcome.skipped
                outcome.llm_skip_reason = enrich_outcome.skip_reason
                outcome.llm_findings_narrated = enrich_outcome.findings_narrated
                outcome.llm_findings_reused = enrich_outcome.findings_reused
                outcome.llm_incidents = enrich_outcome.incidents_written
                outcome.llm_usage = enrich_outcome.usage
                if enrich_outcome.summary:
                    self.store.set_run_summary(run.run_id, enrich_outcome.summary)
                if enrich_outcome.skipped:
                    outcome.phases_skipped.append("llm")
                self._emit(
                    "llm", "completed" if not enrich_outcome.skipped else "skipped",
                    narrated=enrich_outcome.findings_narrated,
                    incidents=enrich_outcome.incidents_written,
                    reason=enrich_outcome.skip_reason,
                )

            # ------------------------------------------------------- Phase 6 reports
            if dry_run:
                outcome.phases_skipped.append("reports")
                self._emit("reports", "skipped", reason="dry-run: nothing persisted to report on")
            elif not generate_reports:
                outcome.phases_skipped.append("reports")
                self._emit("reports", "skipped", reason="generate_reports=False")
            else:
                self._emit("reports", "started")
                out_dir = self.s.output_dir / run.run_id
                xlsx_path = build_excel_report(self.store, run.run_id, out_dir / "Data_Anomaly_Report.xlsx")
                docx_path = build_word_report(self.store, run.run_id, out_dir / "Data_Anomaly_Report.docx")
                outcome.xlsx_path = str(xlsx_path)
                outcome.docx_path = str(docx_path)
                self.store.set_run_report_paths(
                    run.run_id, xlsx_path=outcome.xlsx_path, docx_path=outcome.docx_path
                )
                self._emit("reports", "completed", xlsx=outcome.xlsx_path, docx=outcome.docx_path)

            outcome.status = RunStatus.COMPLETED
            if not dry_run:
                self.store.finish_run(
                    run.run_id,
                    RunStatus.COMPLETED,
                    rows_scanned=summary.get("rows_effective", 0),
                    checks_run=outcome.checks_run,
                    checks_passed=outcome.checks_passed,
                    checks_skipped=len(outcome.phases_skipped) + outcome.checks_skipped_individually,
                    snapshot_pinned=run.snapshot_pinned,
                )

        except Exception as exc:  # noqa: BLE001
            outcome.status = RunStatus.FAILED
            outcome.error = f"{type(exc).__name__}: {exc}"
            log.error("run.failed", run_id=run.run_id, error=outcome.error,
                      traceback=traceback.format_exc()[-2000:])
            if not dry_run:
                self.store.finish_run(
                    run.run_id, RunStatus.FAILED, error_text=outcome.error
                )
            raise
        finally:
            outcome.seconds = round((datetime.now() - t0).total_seconds(), 1)
            log.info(
                "run.finished",
                run_id=run.run_id,
                status=outcome.status.value,
                seconds=outcome.seconds,
                queries=self.source.stats.queries,
                query_seconds=round(self.source.stats.millis / 1000, 1),
            )
        return outcome

    def close(self) -> None:
        self.source.close()
        self.store.close()
