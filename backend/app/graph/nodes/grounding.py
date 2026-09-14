"""Grounding node (LLM, CHEAP tier) - maps one rule's business language onto real tables.

TWO JOBS, AND THE SECOND IS THE REASON IT IS AN AGENT AT ALL:

1. It decides which tables the SQL Author is shown. On this database the full reference block
   is ~18,400 tokens; a grounded, pruned block is a small fraction of that, on every author
   call and every retry. The cheap model pays for itself several times over within one rule.

2. It decides whether this database can express the rule AT ALL. That is what makes a rule
   file portable: a rule written for another system is retired as `not_applicable`, with a
   stated reason, instead of producing a probe that runs and quietly measures nothing.

Deliberately the ONLY node on the cheap tier. Naming tables in an index is a recognition task;
writing and judging SQL is not, and both of those keep the main model.

A FAILURE HERE IS NEVER FATAL. Grounding is an optimisation plus a portability check, so any
problem - a malformed reply, a provider error, nothing resolvable named - falls back to the
full schema and continues. Losing the prune costs tokens; refusing to compile costs the rule.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.context import build_context, table_index
from app.graph.nodes._common import rule_brief
from app.graph.prompts import GROUNDING_SYSTEM
from app.graph.state import CompileState
from app.llm import chat, chat_structured
from app.observability import get_logger
from app.utils import extract_json

log = get_logger()

_UNREADABLE: dict = {"__unreadable__": True}


class GroundingResult(BaseModel):
    """Which tables a rule concerns, as a schema the provider must satisfy.

    Mirrors the JSON the prompt already asks for, so prompt and parsing cannot drift. Note
    `applicable` defaults to TRUE: only an EXPLICIT false retires a rule, and a model that
    simply omitted the field must never be read as saying "this database cannot express it".
    """

    tables: list[str] = Field(
        default_factory=list,
        description="every table needed, schema-qualified, exactly as the index spells it",
    )
    applicable: bool = Field(
        default=True,
        description="false ONLY when this database cannot express the rule at all",
    )
    reason: str = Field(default="", description="the missing concept, when not applicable")
    notes: str = Field(
        default="", description="how the rule's business language maps onto those tables"
    )


def _fallback(state: CompileState, why: str) -> dict:
    """Compile with the full reference block. Never blocks the rule."""
    log.warning("ground: %s - using the full schema", why)
    schema_block, hint_block, tables = build_context(
        state.get("schema", ""), state.get("value_hints", ""), state.get("numeric_hints", ""), [],
    )
    return {
        "applicable": True,
        "tables": tables,
        "schema_block": schema_block,
        "hint_block": hint_block,
        "grounding_note": "",
    }


def grounding_node(state: CompileState) -> dict:
    index = table_index(state.get("schema", ""), state.get("numeric_hints", ""))
    if not index.strip():
        return _fallback(state, "the schema block is empty")

    user = "\n\n".join(
        [
            "TABLE INDEX (every table in scope, with its column names):\n" + index,
            "THE RULE TO GROUND:\n" + rule_brief(dict(state)),
            "Which of these tables does this rule concern? Reply with the JSON.",
        ]
    )

    spent = state.get("llm_calls", 0) + 1

    # Structured first, for the same reason as the verifier: an unparseable reply here costs
    # the schema prune and sends the author the WHOLE database, which on a wide schema is the
    # difference between a focused question and an unusable one. The text path stays as a
    # fallback so a provider without structured output still grounds exactly as before.
    data: dict | None = None
    try:
        grounded = chat_structured(
            GROUNDING_SYSTEM, user, GroundingResult, temperature=0.0, fast=True
        )
        if grounded is not None:
            data = grounded.model_dump()
    except Exception as exc:  # noqa: BLE001 - a provider failure must not lose the rule
        return {**_fallback(state, f"the model call failed ({exc})"), "llm_calls": spent}

    if data is None:
        try:
            raw = chat(GROUNDING_SYSTEM, user, temperature=0.0, fast=True)
        except Exception as exc:  # noqa: BLE001 - a provider failure must not lose the rule
            return {**_fallback(state, f"the model call failed ({exc})"), "llm_calls": spent}
        parsed = extract_json(raw, default=_UNREADABLE)
        data = parsed if isinstance(parsed, dict) and parsed is not _UNREADABLE else None

    if data is None:
        return {**_fallback(state, "the reply could not be parsed"), "llm_calls": spent}

    # Only an EXPLICIT false retires a rule. A missing or malformed `applicable` key means the
    # model did not answer the question, and reading that silence as "this rule is impossible"
    # would delete a check nobody asked to delete.
    if data.get("applicable") is False:
        reason = str(data.get("reason") or "").strip() or "no reason given"
        log.info("ground: %s is NOT APPLICABLE here - %s", state.get("rule_id", ""), reason)
        return {
            "applicable": False,
            "not_applicable_reason": reason,
            "status": "not_applicable",
            "llm_calls": spent,
        }

    named = data.get("tables")
    named = [str(t) for t in named] if isinstance(named, list) else []
    schema_block, hint_block, tables = build_context(
        state.get("schema", ""), state.get("value_hints", ""),
        state.get("numeric_hints", ""), named,
    )
    note = str(data.get("notes") or "").strip()
    log.info(
        "ground: %s -> %d table(s): %s",
        state.get("rule_id", ""), len(tables), ", ".join(tables[:8]) or "(none resolved)",
    )
    return {
        "applicable": True,
        "tables": tables,
        "schema_block": schema_block,
        "hint_block": hint_block,
        "grounding_note": note,
        "llm_calls": spent,
    }
