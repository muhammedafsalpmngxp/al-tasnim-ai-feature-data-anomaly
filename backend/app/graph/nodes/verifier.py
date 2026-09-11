"""Rule Verifier node (LLM, MAIN tier) - the last gate before a probe is stored.

WHY THIS IS WORTH A REASONING-TIER CALL WHEN EVERYTHING BEFORE IT WAS FREE
--------------------------------------------------------------------------
Every earlier gate answers a mechanical question: is it read-only, does it parse, does it
return the right columns, do the counts hold together. None of them can answer the only
question that actually matters - does this query detect the anomaly the rule DESCRIBES? A
probe can be safe, well-formed, contract-perfect and measure entirely the wrong thing, and it
would then run unattended for months reporting a confident number about nothing.

WHAT IT IS SHOWN, AND WHAT IT IS NOT
------------------------------------
The rule's prose, the pruned schema, the measured hints, both queries, the single summary row,
and at most `sample_rows` detail rows. Never more. The summary row is one row by contract, so
it is always safe to show and always carries the true counts; the detail sample exists only to
judge whether the right KIND of record came back. The prompt says so explicitly, because a
model shown twenty-five rows will otherwise reason about the population from them.

IT FAILS CLOSED. An unreadable verdict is a rejection, not an approval. Defaulting the other
way let a malformed reply - a trailing comma is enough - silently turn a rejection into an
approval, with nothing in the log to say it had happened.

GENERIC PROBES NEVER REACH THIS NODE. Their SQL is rendered deterministically from the
schema's own declarations, so there is no business judgement in it for a reviewer to add; the
graph routes them straight to the catalog. That is what keeps two hundred structural probes
genuinely free.
"""
from __future__ import annotations

from app.config import settings
from app.graph.nodes._common import numbered, probe_sql, rows_to_table, rule_brief, summary_line
from app.graph.prompts import RULE_VERIFIER_SYSTEM
from app.graph.state import CompileState
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_json, truncate

log = get_logger()

# extract_json hands back this exact object when it cannot parse a verdict, so a parse failure
# stays distinguishable from a genuine {"ok": true}.
_UNREADABLE: dict = {"__unreadable__": True}

# Feedback is re-sent on every rewrite, so it needs a bound - but a hard slice is dangerous:
# a cut landing inside the instruction leaves the author acting on a fragment. truncate() cuts
# at a sentence or line boundary, and this budget is generous enough to rarely bind.
MAX_FEEDBACK_CHARS = 1200


def verifier_node(state: CompileState) -> dict:
    rule_id = state.get("rule_id", "")

    parts: list[str] = []
    # The schema block is the largest and most repetitive part of the prompt, and it is
    # byte-identical to the one the author saw, so it goes FIRST where a provider can
    # prefix-cache it. Everything below varies per attempt and would defeat that cache above.
    if state.get("schema_block"):
        parts.append(
            "DATABASE SCHEMA (the ONLY tables and columns that exist - judge the SQL against "
            "this, and never name anything absent from it):\n" + state["schema_block"]
        )
    if state.get("hint_block"):
        parts.append("MEASURED HINTS:\n" + state["hint_block"])

    parts.append("THE RULE THIS PROBE IS MEANT TO IMPLEMENT:\n" + rule_brief(dict(state)))
    parts.append(
        "THE PROBE AS WRITTEN:\n"
        + probe_sql(state.get("summary_sql", ""), state.get("detail_sql", ""))
    )
    parts.append(
        "WHAT IT RETURNED WHEN RUN.\n"
        "Summary row (one row by contract - these counts are the truth):\n"
        + summary_line(state.get("summary_columns", []), state.get("summary_row", []))
        + f"\n\nDetail sample (at most {settings.sample_rows} rows, for judging the SHAPE of "
          "the result - never count or rank from it):\n"
        + rows_to_table(state.get("detail_columns", []), state.get("detail_rows", []))
    )

    if state.get("concerns"):
        parts.append(
            "DETERMINISTIC CONCERNS TO ADJUDICATE. Each is derived from the database's own "
            "foreign keys, grain markers and measured scales - these are facts about the "
            "query, not opinions. They are conservative and can flag something legitimate, so "
            "decide each on its merits and reject only where it genuinely affects the result:\n"
            + numbered(state["concerns"])
        )
    if state.get("advice"):
        parts.append(
            "NON-BLOCKING NOTES (the probe is usable without these; do not reject over them):\n"
            + numbered(state["advice"])
        )

    parts.append(
        "Does this probe correctly detect the anomaly the rule describes? Reply with the JSON "
        "verdict."
    )

    spent = state.get("llm_calls", 0) + 1
    try:
        raw = chat(RULE_VERIFIER_SYSTEM, "\n\n".join(parts), temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        # A provider failure is not evidence the probe is good. Fail closed, exactly as an
        # unreadable verdict does - but say plainly that the review did not happen.
        log.warning("verify: the review call failed [%s] - %s", rule_id, exc)
        return _reject(
            state,
            "The automated review could not be completed. Re-check the probe against the "
            "schema and the business rules, and correct anything that looks wrong.",
            spent,
        )

    data = extract_json(raw, default=_UNREADABLE)
    if data is _UNREADABLE or not isinstance(data, dict) or "ok" not in data:
        log.warning(
            "verify: verdict unreadable [%s] (%d chars) - failing closed, not approving",
            rule_id, len(raw or ""),
        )
        return _reject(
            state,
            "The automated review returned nothing usable. Re-check the probe against the "
            "schema and the business rules, and correct anything that looks wrong.",
            spent,
        )

    note = str(data.get("note") or "").strip()
    threshold_note = str(data.get("threshold_note") or "").strip()

    if bool(data.get("ok")):
        log.info("verify: ok [%s] %s", rule_id, note[:120])
        return {
            "verify_ok": True,
            # Cleared HERE, not in the author. That is what lets the instruction survive a
            # rewrite that tripped the validator or the database on its way back: it is only
            # genuinely spent once the rewrite has been judged, and approved.
            "verify_feedback": "",
            "verifier_note": note,
            "threshold_note": threshold_note,
            "llm_calls": spent,
        }

    feedback = str(data.get("feedback") or "").strip()
    if not feedback:
        # A rejection with no instruction is unusable: the author would rewrite blind and is
        # most likely to re-emit the same thing. Give it the one thing it can act on.
        feedback = (
            "The review rejected this probe without naming a fix. Re-read the rule's 'How to "
            "detect' and 'Do NOT flag' sections and make the probe match them exactly."
        )
    log.info("verify: rejected [%s] -> %s", rule_id, feedback[:160])
    return _reject(state, feedback, spent, note=note)


def _reject(state: CompileState, feedback: str, spent: int, note: str = "") -> dict:
    """Record the rejection and fund the rewrite.

    `retry_count` is reset because this rejection discards a probe that was mechanically fine:
    the work has to be redone, so it must be funded. Leaving it alone meant a couple of
    unrelated syntax errors earlier in the rule could leave a correctly identified defect with
    no attempts left to fix it - the reviewer would be right, and immediately overruled.

    That is not unbounded: verify_retry_count still caps how many times this path can be taken
    at all, so the total work per rule stays bounded by verify_retries x max_sql_retries.
    """
    return {
        "verify_ok": False,
        "verify_feedback": truncate(feedback, MAX_FEEDBACK_CHARS),
        "verifier_note": note,
        "verify_retry_count": state.get("verify_retry_count", 0) + 1,
        "retry_count": 0,
        "llm_calls": spent,
    }
