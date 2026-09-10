"""LLM enrichment -- narrate every finding, correlate into incidents, write an executive
summary. Reads/writes only through `FindingsStore`; never touches the source database.

Degrades cleanly at every step: if the client is disabled (no key, or DQ_LLM_ENABLED=false),
`enrich_run` returns immediately with `skipped=True` and the deterministic findings are
left exactly as they were -- the report still builds correctly (docs/02 §7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db.store import FindingsStore
from app.logging import get_logger
from app.sentinel.llm.cache import narration_key, prompt_fingerprint
from app.sentinel.llm.client import LLMClient, Role
from app.sentinel.llm.prompts import (
    correlate_system_prompt,
    correlate_user_prompt,
    narrate_system_prompt,
    narrate_user_prompt,
    summary_system_prompt,
    summary_user_prompt,
)
from app.sentinel.llm.schemas import CORRELATE_SCHEMA, NARRATE_SCHEMA, SUMMARY_SCHEMA
from app.sentinel.llm.validate import validate_correlation, validate_narration

log = get_logger(__name__)


@dataclass(slots=True)
class EnrichmentOutcome:
    skipped: bool = False
    skip_reason: str = ""
    findings_narrated: int = 0
    findings_reused: int = 0
    findings_rejected: int = 0
    incidents_written: int = 0
    incidents_rejected: int = 0
    summary: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def enrich_run(store: FindingsStore, run_id: str, client: LLMClient | None = None) -> EnrichmentOutcome:
    client = client or LLMClient()
    if not client.enabled:
        log.info("enrich.skipped", reason=client.disabled_reason)
        return EnrichmentOutcome(skipped=True, skip_reason=client.disabled_reason or "disabled")

    findings = store.findings(run_id, limit=2000)
    outcome = EnrichmentOutcome()

    # Narration is the long pole of a run (~4 of ~6 minutes, ~95% of the tokens) and most
    # findings recur unchanged between runs. Reuse is keyed on the finding's own measured
    # data plus the model and prompt, so anything that moved gets written fresh -- see
    # llm/cache.py for the cases.
    narrate_system = narrate_system_prompt()
    prompt_fp = prompt_fingerprint(narrate_system)
    narrate_model = client.model_for(Role.NARRATE)
    log.info(
        "enrich.started", run_id=run_id, findings_to_narrate=len(findings),
        model=narrate_model, prompt=prompt_fp,
    )

    # ---------------------------------------------------------------------- 1. narrate
    for i, f in enumerate(findings, start=1):
        key = narration_key(f, model=narrate_model, prompt_fp=prompt_fp)
        if cached := store.find_cached_narration(key):
            store.update_finding_narration(
                f["finding_id"],
                explanation=cached["llm_explanation"],
                root_cause=cached["llm_root_cause"],
                remediation=cached["llm_remediation"],
                narration_key=key,
            )
            outcome.findings_reused += 1
            log.info(
                "enrich.reused", progress=f"{i}/{len(findings)}",
                check_id=f["check_id"], tokens_saved_est=4000,
            )
            continue

        result = client.structured(
            Role.NARRATE,
            system=narrate_system,
            user=narrate_user_prompt(f),
            json_schema=NARRATE_SCHEMA,
            schema_name="finding_narration",
        )
        if not result.ok or result.data is None:
            log.warning("enrich.narrate_failed", check_id=f["check_id"], error=result.error)
            continue
        v = validate_narration(f, result.data)
        if not v.ok:
            outcome.findings_rejected += 1
            log.warning("enrich.narration_rejected", check_id=f["check_id"], reason=v.reason)
            continue
        store.update_finding_narration(
            f["finding_id"],
            explanation=result.data["explanation"],
            root_cause=result.data["root_cause"],
            remediation=result.data["remediation"],
            narration_key=key,
        )
        outcome.findings_narrated += 1
        # Per-finding, because narration is the long pole of a run (~4 of ~6 minutes)
        # and without this the terminal sits silent through all of it.
        log.info(
            "enrich.narrated",
            progress=f"{i}/{len(findings)}",
            check_id=f["check_id"],
            model=result.model,
            tokens=result.usage.total,
        )

    # -------------------------------------------------------------------- 2. correlate
    incidents: list[dict] = []
    by_id = {f["check_id"]: f for f in findings}
    if len(findings) >= 2:
        result = client.structured(
            Role.CORRELATE,
            system=correlate_system_prompt(),
            user=correlate_user_prompt(findings),
            json_schema=CORRELATE_SCHEMA,
            schema_name="incidents",
        )
        if result.ok and result.data:
            for incident in result.data.get("incidents", []):
                v = validate_correlation(by_id, incident)
                if not v.ok:
                    outcome.incidents_rejected += 1
                    log.warning("enrich.incident_rejected", reason=v.reason)
                    continue
                store.add_incident(
                    run_id,
                    title=incident["title"],
                    root_cause=incident["root_cause"],
                    severity=_incident_severity(by_id, incident["finding_ids"]),
                    finding_ids=incident["finding_ids"],
                    narrative=incident["narrative"],
                )
                incidents.append(incident)
                outcome.incidents_written += 1
        elif not result.ok:
            log.warning("enrich.correlate_failed", error=result.error)

    # ---------------------------------------------------------------------- 3. summary
    severity_counts = store.severity_counts(run_id)
    class_counts = store.class_counts(run_id)
    top = store.findings(run_id, actionable_only=True, limit=10)
    result = client.structured(
        Role.SUMMARY,
        system=summary_system_prompt(),
        user=summary_user_prompt(severity_counts, class_counts, top, incidents),
        json_schema=SUMMARY_SCHEMA,
        schema_name="executive_summary",
    )
    if result.ok and result.data:
        outcome.summary = result.data
    else:
        log.warning("enrich.summary_failed", error=result.error)

    outcome.usage = client.usage_summary()
    store.set_run_llm_usage(run_id, **outcome.usage)
    log.info(
        "enrich.complete", run_id=run_id, narrated=outcome.findings_narrated,
        reused=outcome.findings_reused, rejected=outcome.findings_rejected,
        incidents=outcome.incidents_written,
        tokens=outcome.usage.get("total_tokens", 0),
    )
    return outcome


def _incident_severity(by_id: dict[str, dict], finding_ids: list[str]) -> str:
    order = ["critical", "high", "medium", "low", "review", "info"]
    sevs = [by_id[fid]["severity"] for fid in finding_ids if fid in by_id]
    for s in order:
        if s in sevs:
            return s
    return "medium"
