"""HTTP endpoints. Every read here goes through `FindingsStore`, exactly like
`app/cli.py`'s `show`/`report` commands -- this module adds no new persistence logic.
The one write path (`POST /api/runs`) starts the same `Orchestrator.run()` the CLI's
`run` command calls, via `JobManager` (`app/api/jobs.py`).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import get_settings
from app.db.store import FindingsStore
from app.sentinel.checks import business_rules  # noqa: F401 -- registers hand-written checks
from app.sentinel.checks.base import all_checks

from .jobs import ConcurrencyLimitError, get_job_manager
from .schemas import (
    CheckResultOut,
    FindingOut,
    IncidentOut,
    JobOut,
    RunOut,
    StartRunRequest,
)

router = APIRouter(prefix="/api")


def _store() -> FindingsStore:
    return FindingsStore()


def _run_out(row: dict[str, Any], store: FindingsStore) -> RunOut:
    return RunOut(
        **{k: row.get(k) for k in (
            "run_id", "started_at", "finished_at", "status", "triggered_by", "db_name",
            "as_of_date", "rows_scanned", "checks_run", "checks_passed", "checks_skipped",
            "findings_total", "llm_model", "llm_tokens", "error_text",
        )},
        has_excel=bool(row.get("xlsx_path")),
        has_word=bool(row.get("docx_path")),
        severity_counts=store.severity_counts(row["run_id"]),
        class_counts=store.class_counts(row["run_id"]),
    )


def _finding_out(row: dict[str, Any]) -> FindingOut:
    data = dict(row)
    raw_evidence = data.get("evidence")
    if isinstance(raw_evidence, str) and raw_evidence:
        try:
            data["evidence"] = json.loads(raw_evidence)
        except (TypeError, ValueError):
            data["evidence"] = []
    elif raw_evidence is None:
        data["evidence"] = []
    data["sampled"] = bool(data.get("sampled"))
    return FindingOut(**data)


def _incident_out(row: dict[str, Any]) -> IncidentOut:
    data = dict(row)
    raw_ids = data.get("finding_ids")
    if isinstance(raw_ids, str) and raw_ids:
        try:
            data["finding_ids"] = json.loads(raw_ids)
        except (TypeError, ValueError):
            data["finding_ids"] = []
    elif raw_ids is None:
        data["finding_ids"] = []
    return IncidentOut(**data)


# --------------------------------------------------------------------------- health
@router.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "db_name": s.db_name, "llm_model": s.openai_model}


# ------------------------------------------------------------------------------ runs
@router.get("/runs", response_model=list[RunOut])
def list_runs(limit: int = Query(50, ge=1, le=200)) -> list[RunOut]:
    store = _store()
    try:
        return [_run_out(r, store) for r in store.list_runs(limit=limit)]
    finally:
        store.close()


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str) -> RunOut:
    store = _store()
    try:
        row = store.get_run(run_id)
        if not row:
            raise HTTPException(404, f"run {run_id} not found")
        return _run_out(row, store)
    finally:
        store.close()


@router.get("/runs/{run_id}/findings", response_model=list[FindingOut])
def get_findings(
    run_id: str,
    family: str | None = None,
    severity: str | None = None,
    finding_class: str | None = None,
    actionable_only: bool = False,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> list[FindingOut]:
    store = _store()
    try:
        if not store.get_run(run_id):
            raise HTTPException(404, f"run {run_id} not found")
        rows = store.findings(
            run_id, family=family, severity=severity, finding_class=finding_class,
            actionable_only=actionable_only, limit=limit, offset=offset,
        )
        return [_finding_out(r) for r in rows]
    finally:
        store.close()


@router.get("/runs/{run_id}/incidents", response_model=list[IncidentOut])
def get_incidents(run_id: str) -> list[IncidentOut]:
    store = _store()
    try:
        if not store.get_run(run_id):
            raise HTTPException(404, f"run {run_id} not found")
        return [_incident_out(r) for r in store.incidents(run_id)]
    finally:
        store.close()


@router.get("/runs/{run_id}/checks", response_model=list[CheckResultOut])
def get_check_results(run_id: str) -> list[CheckResultOut]:
    store = _store()
    try:
        if not store.get_run(run_id):
            raise HTTPException(404, f"run {run_id} not found")
        return [CheckResultOut(**r) for r in store.check_results(run_id)]
    finally:
        store.close()


@router.get("/runs/{run_id}/normalisation")
def get_normalisation_actions(run_id: str) -> list[dict[str, Any]]:
    store = _store()
    try:
        if not store.get_run(run_id):
            raise HTTPException(404, f"run {run_id} not found")
        return store.normalisation_actions(run_id)
    finally:
        store.close()


@router.get("/runs/{run_id}/report/excel")
def download_excel(run_id: str) -> FileResponse:
    store = _store()
    try:
        row = store.get_run(run_id)
    finally:
        store.close()
    if not row or not row.get("xlsx_path"):
        raise HTTPException(404, "no Excel report for this run")
    return FileResponse(
        row["xlsx_path"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Data_Anomaly_Report_{run_id}.xlsx",
    )


@router.get("/runs/{run_id}/report/word")
def download_word(run_id: str) -> FileResponse:
    store = _store()
    try:
        row = store.get_run(run_id)
    finally:
        store.close()
    if not row or not row.get("docx_path"):
        raise HTTPException(404, "no Word report for this run")
    return FileResponse(
        row["docx_path"],
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Data_Anomaly_Report_{run_id}.docx",
    )


# ------------------------------------------------------------------------- checks
@router.get("/checks")
def list_registered_checks() -> list[dict[str, Any]]:
    """The ~13 hand-written checks (business_rules.py). The ~39 generated invariant
    checks only exist once a run has introspected the live schema, so they are not
    listed here -- see `GET /api/runs/{run_id}/checks` for what actually ran, generated
    checks included.
    """
    return [
        {"id": c.id, "family": c.family, "title": c.title, "severity": c.severity,
         "business_rule_ref": c.business_rule_ref, "grain": c.grain.describe(),
         "baseline": c.baseline.value}
        for c in all_checks()
    ]


# ------------------------------------------------------------------------------- jobs
@router.post("/runs", response_model=JobOut, status_code=202)
def start_run(body: StartRunRequest) -> JobOut:
    mgr = get_job_manager()
    try:
        job = mgr.start_run(
            tables=body.tables, triggered_by=body.triggered_by,
            enrich=body.enrich, generate_reports=body.generate_reports,
        )
    except ConcurrencyLimitError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JobOut(**job.as_dict())


@router.get("/jobs", response_model=list[JobOut])
def list_jobs() -> list[JobOut]:
    return [JobOut(**j.as_dict()) for j in get_job_manager().list_jobs()]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> JobOut:
    job = get_job_manager().get(job_id)
    if not job:
        raise HTTPException(404, f"job {job_id} not found")
    return JobOut(**job.as_dict())
