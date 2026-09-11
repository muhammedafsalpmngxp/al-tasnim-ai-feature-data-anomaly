"""Catalog Writer node (deterministic) - turns the finished state into one CompiledProbe.

It does NOT write the file. The driver collects every probe and saves the catalog once, so a
compile that is interrupted half way cannot leave a partially rewritten catalog behind.

A RULE THAT FAILED IS STILL RECORDED, WITH ITS ERROR. It would be easy to drop it and write a
tidy catalog of the probes that worked, and it would be the single worst thing this file could
do: the operator would believe a check is running when it is not. A failed probe is stored with
status `failed` and the reason, so the report can list it in its transparency section and
somebody can go and fix the rule.
"""
from __future__ import annotations

import datetime as dt

from app.graph.state import CompileState
from app.observability import get_logger
from app.rules.spec import CompiledProbe

log = get_logger()


def _failure_reason(state: CompileState) -> str:
    """The most specific explanation available, in the order the pipeline would have hit it."""
    for key in ("exec_error", "validation_error", "contract_error", "verify_feedback"):
        value = (state.get(key) or "").strip()
        if value:
            return f"{key}: {value}"
    return "the rule could not be compiled into a usable probe"


def _status(state: CompileState) -> tuple[str, str]:
    """(status, error). Decided from the state alone, never from how the graph got here."""
    if state.get("applicable") is False:
        return "not_applicable", state.get("not_applicable_reason", "")
    if not state.get("summary_sql", "").strip() or not state.get("detail_sql", "").strip():
        return "failed", _failure_reason(state)
    # Any unresolved mechanical fault means the last attempt never reached a working state.
    if state.get("validation_error") or state.get("exec_error") or state.get("contract_error"):
        return "failed", _failure_reason(state)
    # A probe the reviewer rejected and that ran out of rewrites is NOT quietly accepted.
    # Storing it as active would mean shipping a query a reviewer said measures the wrong
    # thing, and the report would present its number as a finding.
    if state.get("verify_ok") is False:
        return "failed", _failure_reason(state)
    return "active", ""


def catalog_writer_node(state: CompileState) -> dict:
    status, error = _status(state)
    rule_id = state.get("rule_id", "")

    probe = CompiledProbe(
        rule_id=rule_id,
        summary_sql=state.get("summary_sql", ""),
        detail_sql=state.get("detail_sql", ""),
        status=status,
        rule_hash=state.get("rule_hash", ""),
        structure_fingerprint=state.get("structure_fingerprint", ""),
        compiled_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        tables=tuple(state.get("tables", []) or ()),
        grounding_note=state.get("grounding_note", ""),
        verifier_note=state.get("verifier_note", ""),
        threshold_note=state.get("threshold_note", ""),
        error=error,
        llm_calls=state.get("llm_calls", 0),
    )

    if status == "active":
        log.info("catalog: %s compiled OK (%d llm call(s))", rule_id, probe.llm_calls)
    elif status == "not_applicable":
        log.info("catalog: %s not applicable to this database - %s", rule_id, error[:120])
    else:
        log.warning("catalog: %s FAILED - %s", rule_id, error[:160])

    return {"status": status, "error": error, "probe": probe}
