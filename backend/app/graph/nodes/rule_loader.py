"""Rule Loader node (deterministic) - prepares one rule for compilation.

Almost everything it needs was already put into the state by the driver. What this node adds
is the DECISION about how this particular rule should be compiled, which is worth having as a
visible node rather than buried in a routing function: reading the graph should tell you that
a pinned rule skips the author, without having to trace a lambda.
"""
from __future__ import annotations

from app.graph.context import build_context
from app.graph.state import CompileState
from app.observability import get_logger

log = get_logger()


def rule_loader_node(state: CompileState) -> dict:
    rule_id = state.get("rule_id", "")
    mode = state.get("sql_mode", "authored")
    source = state.get("source", "declared")

    log.info(
        "RULE: %s [%s] %s/%s - %s",
        rule_id, state.get("severity", ""), source, mode, state.get("title", ""),
    )

    out: dict = {
        "summary_sql": state.get("seed_summary_sql", ""),
        "detail_sql": state.get("seed_detail_sql", ""),
        "tried_sql": [],
        "retry_count": 0,
        "verify_retry_count": 0,
        "llm_calls": 0,
        "concerns": [],
        "advice": [],
        "validation_error": "",
        "exec_error": "",
        "contract_error": "",
        "applicable": True,
        "status": "active",
    }

    # A GENERIC probe is rendered directly from the schema, so its tables are already known
    # exactly. Grounding could only guess at what is already certain, so it is skipped and the
    # context is built from the rendered SQL's own parameters instead. This is also what keeps
    # the generic families genuinely free: no LLM call is made for them anywhere.
    if source == "generic":
        tables = [
            value for key, value in (state.get("params") or {}).items()
            if key.endswith("table")
        ]
        schema_block, hint_block, resolved = build_context(
            state.get("schema", ""), state.get("value_hints", ""),
            state.get("numeric_hints", ""), tables,
        )
        out.update(
            schema_block=schema_block, hint_block=hint_block, tables=resolved,
            grounding_note="rendered directly from the schema; no grounding required",
        )
    return out
