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

EVERY PROBE REACHES THIS NODE, INCLUDING THE FAMILY-EXPANDED STRUCTURAL ONES. An earlier
design rendered those deterministically from the schema's own declarations and routed them
straight to the catalog as "generic" probes, on the argument that a foreign-key check carries
no business judgement for a reviewer to add. It does carry one. Which of two candidate keys is
the real parent, whether a NULL is a violation or the documented way to say "not applicable",
and whether an orphan rate of 4% is a defect or just how this table has always been loaded are
all judgements, and getting them wrong produces a confident number about nothing - exactly the
failure the rest of this docstring describes. So the bypass was removed and the graph has no
edge that skips verification; `state["source"]` still records declared vs expanded for the
report, but it changes no routing.

The cost of that is real and deliberate: a compile pays author and verifier calls for every
expanded probe, which on a 77-table database is most of them. It is paid once per schema
change, not per run.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import settings
from app.graph.nodes._common import numbered, probe_sql, rows_to_table, rule_brief, summary_line
from app.graph.prompts import verifier_system
from app.graph.state import CompileState
from app.llm import chat, chat_structured
from app.observability import get_logger
from app.utils import extract_json, truncate

log = get_logger()

# extract_json hands back this exact object when it cannot parse a verdict, so a parse failure
# stays distinguishable from a genuine {"ok": true}.
_UNREADABLE: dict = {"__unreadable__": True}


class VerifierVerdict(BaseModel):
    """The reviewer's verdict, as a schema the provider must satisfy.

    Field names and meanings are IDENTICAL to the JSON the prompt already asks for, so the
    prompt, this schema and the parsing below cannot drift apart. Every field has a default:
    a reviewer that approves has nothing to say in `feedback`, and requiring it would force the
    model to invent an objection in order to answer at all.
    """

    ok: bool = Field(description="true only when the probe correctly detects the anomaly")
    not_applicable: bool = Field(
        default=False,
        description="true when this database cannot express the rule at all",
    )
    reason: str = Field(default="", description="why it is not applicable, when it is not")
    feedback: str = Field(
        default="",
        description="on a rejection, the concrete change the SQL author must make",
    )
    threshold_note: str = Field(
        default="", description="where an auto-derived threshold came from"
    )
    note: str = Field(default="", description="one sentence summarising the verdict")

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

    # WHAT THIS REVIEWER ALREADY DEMANDED, so it cannot contradict itself.
    #
    # Observed on DQ-D03: the first rejection said "replace progress >= 1.0 with progress =
    # 1.0"; the author complied; the second rejection said "use >= 1.0 rather than = 1.0". Both
    # retries were spent obeying opposite instructions and a correct probe was recorded as
    # failed. A reviewer that cannot see its own previous verdict has no way to notice it is
    # reversing itself - so it is shown, with an explicit instruction about what to do when it
    # now disagrees with its earlier self.
    if state.get("feedback_history"):
        parts.append(
            "YOU ALREADY REJECTED AN EARLIER VERSION OF THIS PROBE, SAYING:\n"
            + numbered(state["feedback_history"])
            + "\n\nThe author has acted on that. Do NOT now ask for the opposite of what you "
              "asked for before - if your earlier instruction was wrong, say so plainly in the "
              "feedback and give the corrected instruction ONCE. If the author did what you "
              "asked and the probe is now sound, APPROVE it."
        )

    parts.append(
        "Does this probe correctly detect the anomaly the rule describes? Reply with the JSON "
        "verdict."
    )

    spent = state.get("llm_calls", 0) + 1
    user = "\n\n".join(parts)

    # STRUCTURED FIRST. The provider constrains its own decoding to the schema, so a verdict
    # cannot arrive wrapped in a code fence or prefaced with a sentence of commentary - the
    # shapes that made a parse fail and, because this node fails closed, spent a rewrite cycle
    # on a formatting slip rather than a real objection.
    #
    # The text path below is kept as a fallback, not as the normal route: a provider that
    # cannot do structured output (an older local model) still gets reviewed exactly as before.
    data: dict | None = None
    # The reference material is cut to THIS rule, so the prompt carries the business
    # definitions it needs and not the whole file. See prompts._for_rule().
    rule_text = " ".join(str(state.get(k) or "") for k in
                         ("title", "category", "entity", "method", "body"))
    tags = tuple(state.get("tags") or ())
    system = verifier_system(rule_text, tags)

    try:
        verdict = chat_structured(
            system, user, VerifierVerdict, temperature=0.0
        )
        if verdict is not None:
            data = verdict.model_dump()
    except Exception as exc:  # noqa: BLE001
        log.warning("verify: the review call failed [%s] - %s", rule_id, exc)
        return _reject(
            state,
            "The automated review could not be completed. Re-check the probe against the "
            "schema and the business rules, and correct anything that looks wrong.",
            spent,
        )

    if data is None:
        try:
            raw = chat(system, user, temperature=0.0)
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
        parsed = extract_json(raw, default=_UNREADABLE)
        data = parsed if isinstance(parsed, dict) and parsed is not _UNREADABLE else None

    if data is None or "ok" not in data:
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

    # THE THIRD VERDICT. Without it this node could only approve or reject, and neither is
    # honest when a rule's subject matter simply is not recorded in this database: approving
    # stores a probe that reports "0 examined, clean" forever, and rejecting spends every
    # rewrite asking the author to fix something no query can fix. Observed in practice - the
    # reviewer approved a zero-scope probe while explaining, correctly, that the rule could
    # not be implemented against the schema. It needed a way to say that as a verdict.
    if bool(data.get("not_applicable")):
        reason = str(data.get("reason") or "").strip() or note or "no reason given"
        log.info("verify: %s is NOT APPLICABLE to this database - %s", rule_id, reason[:160])
        return {
            "applicable": False,
            "not_applicable_reason": reason,
            "status": "not_applicable",
            "verify_ok": True,   # nothing is pending a rewrite; the graph moves to the catalog
            "verify_feedback": "",
            "verifier_note": note,
            "llm_calls": spent,
        }

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
        # Appended, never replaced: the whole point is that the NEXT review can see what this
        # one demanded.
        "feedback_history": list(state.get("feedback_history") or [])
        + [truncate(feedback, MAX_FEEDBACK_CHARS)],
        "verifier_note": note,
        "verify_retry_count": state.get("verify_retry_count", 0) + 1,
        "retry_count": 0,
        "llm_calls": spent,
    }
