"""Sanity Gate node (deterministic) - judges what the probe actually returned.

THE WHOLE PURPOSE OF THIS NODE IS THE SPLIT IT MAKES. Two kinds of finding arrive here and
they must not be treated alike:

  HARD      the result cannot be used at all - a required alias is missing, or SUMMARY returned
            other than exactly one row. The report would have nowhere to put the output. This
            is sent back to the author like a syntax error, against MAX_SQL_RETRIES.

  ADVISORY  the result is usable but the NUMBERS look like a mistake: nothing was examined,
            every row was flagged, the two queries disagree, a 0-1 column was compared against
            100. Each is usually a bug and occasionally legitimate - which is exactly why it is
            handed to the Verifier to ADJUDICATE rather than enforced here.

Collapsing the two would break the engine in one direction or the other. Enforce the advisory
checks and a legitimate probe that genuinely flags every row can never be compiled. Demote the
hard ones and a probe missing entity_key is stored, only to produce findings nobody can trace
back to a record.

THE MOST IMPORTANT CHECK IN THE FILE is the one for scope_total = 0. A probe that examined
nothing returns zero anomalies, which reads exactly like a clean bill of health. Nothing
downstream can tell those apart, so it is caught here and stated in the strongest terms.
"""
from __future__ import annotations

from app.graph.sqlcheck import check_probe
from app.graph.state import CompileState
from app.observability import get_logger
from app.rules.contract import (
    check_detail,
    check_summary,
    columns_of,
    detail_advice,
    sanity_concerns,
    summary_values,
)

log = get_logger()


def sanity_gate_node(state: CompileState) -> dict:
    rule_id = state.get("rule_id", "")
    summary_columns = state.get("summary_columns", []) or []
    detail_columns = state.get("detail_columns", []) or []
    detail_rows = state.get("detail_rows", []) or []

    # ── HARD: the contract, checked against what the driver actually handed back ──
    problems = check_summary(summary_columns, state.get("summary_row_count", 0))
    problems += check_detail(detail_columns)
    if problems:
        joined = "\n".join(f"- {p}" for p in problems)
        log.warning("contract failed [%s]: %d problem(s)", rule_id, len(problems))
        return {
            "contract_error": joined,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    # ── ADVISORY: everything the Verifier will adjudicate ──
    values = summary_values(summary_columns, state.get("summary_row", []) or [])
    # The detail row count is only comparable with anomaly_count when the sample was NOT cut
    # short. Passing a truncated count would manufacture a disagreement on every large rule.
    detail_count = None if state.get("detail_truncated") else len(detail_rows)
    concerns = sanity_concerns(values, detail_count)
    concerns += check_probe(
        state.get("summary_sql", ""),
        state.get("detail_sql", ""),
        state.get("schema_block", ""),
        state.get("numeric_hints", ""),
    )
    advice = detail_advice(detail_columns)

    unique = list(dict.fromkeys(concerns))
    if unique:
        log.info("sanity: %d concern(s) for the reviewer to adjudicate [%s]", len(unique), rule_id)
        for text in unique:
            log.info("sanity: - %s", text[:160])
    else:
        log.info("contract ok [%s]: %s", rule_id, _counts(values))

    return {"contract_error": "", "concerns": unique, "advice": advice}


def _counts(values: dict) -> str:
    scope = values.get("scope_total")
    count = values.get("anomaly_count")
    return f"scope {scope}, anomalies {count}"
