"""Anomaly Scout node (LLM, MAIN tier) - the one agent that proposes work rather than doing it.

ONE CALL PER DISCOVERY RUN, and it is the only LLM call in this graph. Everything before it
measures and everything after it filters, so the model is asked exactly one question and nothing
else depends on asking it twice.

THE MAIN TIER, NOT THE FAST ONE. Grounding is a lookup and runs on the cheap model; this is a
judgement about what matters to a business, made once, whose output a person then spends time
reading. The few cents saved by demoting it would be repaid many times over in proposals not
worth the reading.

WHAT IT IS AND IS NOT ALLOWED TO PRODUCE
----------------------------------------
It writes PROSE - a business description of a defect. It never writes SQL, is never shown SQL,
and its proposals are never executed. The ordinary compile graph writes the SQL later, if and
only if a person accepts the proposal, and that SQL is then checked by the same validator,
executor, contract checks and reviewer as every hand-written rule. So the machine's judgement
enters the catalog through exactly one door, and a human holds it.

STRUCTURED OUTPUT, WITH A TEXT FALLBACK - for the same reason the Verifier uses it. A proposal
that arrives wrapped in a code fence is a proposal lost to a formatting slip, and this call is
expensive enough that losing it to punctuation would be absurd.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.discover_state import DiscoverState
from app.graph.prompts import scout_system
from app.llm import chat, chat_structured
from app.observability import get_logger
from app.utils import extract_json

log = get_logger()


class Proposal(BaseModel):
    """One proposed anomaly. Every field is prose; none of it is SQL."""

    title: str = Field(description="One line, in business words. What is wrong, not how to find it.")
    what_is_wrong: str = Field(description="One or two sentences describing the defect itself.")
    why_it_matters: str = Field(description="The consequence to the business.")
    how_to_detect: str = Field(description="The logic in words. Never SQL.")
    do_not_flag: str = Field(description="Cases that are legitimate and must be excluded.")
    category: str = Field(default="Uncategorised")
    severity: str = Field(default="medium", description="critical, high, medium or low")
    entity: str = Field(default="row", description="well, task, project, activity, employee, row")
    evidence: str = Field(description="The measured numbers, quoted from the observation.")
    observation_id: str = Field(description="The OBS-nnn this is grounded in. Required.")
    confidence: str = Field(default="medium", description="high, medium or low")


class ScoutResult(BaseModel):
    proposals: list[Proposal] = Field(default_factory=list)


def _observation_block(observations: list[dict]) -> str:
    return "\n".join(f"{o['id']}  {o['fact']}" for o in observations)


def _covered_block(covered: list[dict]) -> str:
    lines = []
    for rule in covered:
        tags = f" [{rule['tags']}]" if rule.get("tags") else ""
        lines.append(f"- {rule['rule_id']}  {rule['title']} ({rule.get('category', '')}){tags}")
    return "\n".join(lines)


def _rejected_block(rejected: list[dict]) -> str:
    lines = []
    for rule in rejected:
        why = f" - refused because: {rule['reason']}" if rule.get("reason") else ""
        lines.append(f"- {rule['title']}{why}")
    return "\n".join(lines)


def _not_expressible_block(covered: list[dict]) -> str:
    """Concepts this database was already PROVEN not to record.

    Taken from rules that compiled as not applicable. The engine has already paid to discover
    that this schema holds no work-breakdown table; proposing five more rules that depend on one
    would only reproduce that finding at the cost of five decisions.
    """
    lines = [
        f"- {r['title']}" for r in covered
        if (r.get("compiled_status") or "") == "not_applicable"
    ]
    return "\n".join(lines)


def scout_node(state: DiscoverState) -> dict:
    observations = state.get("observations") or []
    if not observations:
        return {"proposals": [], "scout_error": ""}

    parts = [
        "OBSERVATIONS - measured facts about this database. Every proposal must cite one:\n"
        + _observation_block(observations),
        "SCHEMA - every table and column that exists:\n" + (state.get("schema_block") or ""),
    ]

    covered = state.get("covered") or []
    if covered:
        parts.append(
            "ALREADY COVERED - checks that exist. Do NOT propose any of these, however "
            "differently you would word them:\n" + _covered_block(covered)
        )
    not_expressible = _not_expressible_block(covered)
    if not_expressible:
        parts.append(
            "CANNOT BE EXPRESSED HERE - this database was already proven not to record what "
            "these need. Anything depending on the same missing concepts will fail the same "
            "way, so do not propose it:\n" + not_expressible
        )
    rejected = state.get("rejected") or []
    if rejected:
        parts.append(
            "ALREADY REFUSED - a person considered these and said no. Do NOT propose them "
            "again:\n" + _rejected_block(rejected)
        )

    limit = int(state.get("max_proposals") or 8)
    parts.append(
        f"Propose at most {limit} genuinely new checks now, each citing an OBS-nnn. "
        "If nothing here is worth a person's time, propose none - that is a correct answer."
    )

    system = scout_system(limit)
    user = "\n\n".join(p for p in parts if p)
    spent = state.get("llm_calls", 0) + 1

    data = None
    try:
        # NAMED EXPLICITLY. scout_system() formats a template, so its prompt is a new
        # string every call and app.llm's id-based labelling cannot recognise it - without
        # this the Scout's cost lands in the usage table under the anonymous "agent".
        result = chat_structured(system, user, ScoutResult, temperature=0.2, label="scout")
        if result is not None:
            data = result.model_dump()
    except Exception as exc:  # noqa: BLE001 - a provider failure is not a finding
        log.warning("scout: the call failed (%s)", exc)
        return {"proposals": [], "llm_calls": spent, "scout_error": (
            f"The Scout could not be run: {exc}"
        )}

    if data is None:
        try:
            raw = chat(system, user, temperature=0.2, label="scout")
        except Exception as exc:  # noqa: BLE001
            log.warning("scout: the call failed (%s)", exc)
            return {"proposals": [], "llm_calls": spent, "scout_error": (
                f"The Scout could not be run: {exc}"
            )}
        parsed = extract_json(raw, default=None)
        if isinstance(parsed, dict):
            data = parsed
        elif isinstance(parsed, list):
            data = {"proposals": parsed}

    if not isinstance(data, dict):
        log.warning("scout: the reply could not be read")
        return {"proposals": [], "llm_calls": spent, "scout_error": (
            "The Scout's reply could not be read. Try again."
        )}

    proposals = [p for p in (data.get("proposals") or []) if isinstance(p, dict)]
    # Truncated rather than rejected: a model that returns eleven when asked for eight has done
    # the work, and throwing all of it away to punish the count would be perverse.
    if len(proposals) > limit:
        log.info("scout: %d proposal(s) returned, keeping the first %d", len(proposals), limit)
        proposals = proposals[:limit]

    log.info("scout: %d proposal(s) before filtering", len(proposals))
    return {"proposals": proposals, "llm_calls": spent, "scout_error": ""}
