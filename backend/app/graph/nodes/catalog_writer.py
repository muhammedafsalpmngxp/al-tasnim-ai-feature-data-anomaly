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
from app.rules.contract import summary_values
from app.rules.spec import CompiledProbe

log = get_logger()


def _failure_reason(state: CompileState) -> str:
    """The most specific explanation available, in the order the pipeline would have hit it."""
    for key in ("exec_error", "validation_error", "contract_error", "verify_feedback"):
        value = (state.get(key) or "").strip()
        if value:
            return f"{key}: {value}"
    return "the rule could not be compiled into a usable probe"


def _examined_nothing(state: CompileState) -> bool:
    """True when the probe's own SUMMARY reported a scope of zero at compile time.

    Read from what the database actually returned, not from anyone's opinion about it.
    """
    columns = state.get("summary_columns") or []
    row = state.get("summary_row") or []
    if not columns or not row:
        return False
    values = summary_values(columns, row)
    try:
        return int(values.get("scope_total") or 0) <= 0
    except (TypeError, ValueError):
        return False


def _status(state: CompileState) -> tuple[str, str]:
    """(status, error). Decided from the state alone, never from how the graph got here."""
    if state.get("applicable") is False:
        return "not_applicable", state.get("not_applicable_reason", "")

    # A PROBE THAT EXAMINED NOTHING IS NEVER STORED AS ACTIVE. This is a deterministic
    # backstop, and it exists because the alternative was observed in practice: asked to
    # review a probe whose scope was zero, the reviewer approved it and explained that the
    # rule "cannot be implemented against the provided schema" - a correct diagnosis, but the
    # verdict it produced meant the probe was stored active and then reported "0 of 0, clean"
    # on every run thereafter. A check that proves nothing while reading as a pass is the
    # exact failure this whole engine exists to prevent, so it cannot be left to a model's
    # judgement: the query ran, the database said it matched no rows, and that is decidable
    # here without asking anyone.
    if _examined_nothing(state):
        reason = (
            "the compiled probe examined 0 records, so it can prove nothing about its "
            "subject. This is normally a join that matches nothing, a filter on a value that "
            "does not exist, or a rule whose concepts this database does not record."
        )
        note = (state.get("verifier_note") or "").strip()
        if note:
            reason += f" The reviewer's assessment: {note}"
        return "not_applicable", reason
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
    from app.db.introspect import probe_fingerprint
    from app.rules.semantics import semantic_fingerprint

    status, error = _status(state)
    rule_id = state.get("rule_id", "")
    tables = tuple(state.get("tables", []) or ())

    # The MEASURED MEANING of this probe's tables, recorded from the very hints the author and
    # the reviewer were shown. Taken from the FULL artefacts in state, never from the pruned
    # blocks: the pruned copies are cut down per rule, so hashing them would make the
    # fingerprint depend on how much context a retry happened to be given.
    meaning = semantic_fingerprint(
        tables,
        state.get("schema", ""),
        state.get("value_hints", ""),
        state.get("numeric_hints", ""),
    )

    probe = CompiledProbe(
        rule_id=rule_id,
        summary_sql=state.get("summary_sql", ""),
        detail_sql=state.get("detail_sql", ""),
        status=status,
        # Carried from the rule, not guessed from the id: this is what lets a reader of the
        # catalog (or of the report) tell an agent-written check from a templated one.
        source=state.get("source", "declared"),
        rule_hash=state.get("rule_hash", ""),
        structure_fingerprint=state.get("structure_fingerprint", ""),
        # What this probe's OWN tables looked like when it was written, so a later change to an
        # unrelated table does not drag it into a recompile it does not need.
        table_fingerprint=probe_fingerprint(tables, state.get("table_signatures") or {}),
        # What those tables MEANT when it was written - scales, bounds, coded values. Structure
        # alone cannot see a 0-100 column become 0-1, and that change turns a correct probe
        # into one that silently reports zero anomalies.
        semantic_fingerprint=meaning,
        compiled_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        tables=tables,
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
