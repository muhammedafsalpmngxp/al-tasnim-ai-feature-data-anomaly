"""Response models. These describe what `FindingsStore` already returns -- no new data
shapes are invented here, this only documents them for the OpenAPI schema (/docs) and
gives the frontend a typed contract.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class RunOut(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None = None
    status: str
    triggered_by: str | None = None
    db_name: str | None = None
    as_of_date: str | None = None
    rows_scanned: int = 0
    checks_run: int = 0
    checks_passed: int = 0
    checks_skipped: int = 0
    findings_total: int = 0
    llm_model: str | None = None
    llm_tokens: int = 0
    has_excel: bool = False
    has_word: bool = False
    error_text: str | None = None
    severity_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}


class FindingOut(BaseModel):
    finding_id: int
    run_id: str
    check_id: str
    family: str
    severity: str
    finding_class: str
    title: str
    entity_type: str
    entity_id: str | None = None
    entity_label: str | None = None
    well_id: int | None = None
    affected_count: int = 0
    grain: str = ""
    baseline: str = ""
    business_rule_ref: str | None = None
    owner: str | None = None
    why_it_matters: str | None = None
    evidence: list[dict[str, Any]] = []
    sampled: bool = False
    llm_explanation: str | None = None
    llm_root_cause: str | None = None
    llm_remediation: str | None = None
    status: str = "new"


class IncidentOut(BaseModel):
    incident_id: int
    run_id: str
    title: str
    root_cause: str | None = None
    severity: str
    finding_ids: list[str] = []
    llm_narrative: str | None = None


class CheckResultOut(BaseModel):
    check_id: str
    family: str
    status: str
    rows_scanned: int = 0
    violations: int = 0
    duration_ms: int = 0
    grain: str = ""
    baseline: str = ""
    skip_reason: str | None = None
    error_text: str | None = None


class JobOut(BaseModel):
    job_id: str
    status: str
    run_id: str | None = None
    phase: str | None = None
    phase_state: str | None = None
    phase_detail: dict[str, Any] = {}
    error: str | None = None
    started_at: float
    finished_at: float | None = None
    elapsed_seconds: float = 0.0


class StartRunRequest(BaseModel):
    tables: list[str] | None = None
    triggered_by: str = "ui"
    enrich: bool = True
    generate_reports: bool = True
