"""Anomaly SQL Author node (LLM, MAIN tier) - writes the SUMMARY and DETAIL probe pair.

BOTH QUERIES IN ONE CALL, DELIBERATELY.
They must apply the same condition at the same grain - contract.sanity_concerns() compares
them after execution and reports a disagreement as a defect. Writing them in two separate calls
is precisely how they drift: the second call re-derives the condition from the prose rather
than from the query it has to match. One call also halves the cost, since the schema, the
hints and the rulebook would otherwise be sent twice per attempt.

EVERY REJECTION REACHES THIS NODE AS TEXT.
The validator, the executor, the contract checks and the verifier all phrase their findings as
instructions, and they are appended below with the SQL that earned them. Showing the rejected
query alongside its criticism is not cosmetic: without it the author has to infer which of its
two queries the feedback refers to, and it frequently re-emits the same one.
"""
from __future__ import annotations

from app.graph.context import example_conditions
from app.graph.nodes._common import numbered, rule_brief
from app.graph.prompts import author_system
from app.graph.state import CompileState
from app.rules.expand import substitute
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_sql_blocks, normalize_sql

log = get_logger()


def _pair_signature(summary_sql: str, detail_sql: str) -> str:
    """Identity of an ATTEMPT, for recognising one already made.

    Both halves together: changing only the detail query is a genuinely different attempt and
    must not be mistaken for a repeat, while re-emitting both unchanged cannot earn a different
    verdict and only burns a cycle.
    """
    return normalize_sql(summary_sql) + " ;; " + normalize_sql(detail_sql)


def _feedback_sections(state: CompileState) -> list[str]:
    """Everything the previous attempt got wrong, most actionable first."""
    out: list[str] = []
    previous = ""
    if state.get("summary_sql") or state.get("detail_sql"):
        previous = (
            "YOUR PREVIOUS ATTEMPT WAS:\n\n"
            f"SUMMARY:\n{state.get('summary_sql', '')}\n\n"
            f"DETAIL:\n{state.get('detail_sql', '')}"
        )

    # EVERY OUTSTANDING OBJECTION, NOT JUST THE LATEST ONE.
    #
    # This was an if/elif chain, so exactly one message ever reached the author - and the state
    # above goes to deliberate trouble to KEEP verify_feedback alive across attempts for a
    # reason the chain then defeated. A rule rejected on MEANING, rewritten, and then tripping
    # a mechanical check was shown only the mechanical error: it fixed that, quietly reinstated
    # whatever the reviewer had refused, and was rejected again on the next review. Two attempts
    # spent alternating between two objections, neither ever satisfied, and the rule recorded as
    # failed with budget exhausted.
    #
    # Adding two hard checks in front of the reviewer (grain, saturation) made that collision
    # common rather than occasional, because both fire AFTER the reviewer has already spoken.
    #
    # Ordered mechanical-first: a query that cannot run cannot be reviewed, so that is what the
    # author must fix first - but the reviewer's instruction stays on the page beneath it,
    # where it cannot be forgotten while doing so.
    mechanical = False
    if state.get("validation_error"):
        out.append(
            previous + "\n\nIT WAS REJECTED BEFORE IT RAN:\n" + state["validation_error"]
        )
        mechanical = True
    elif state.get("exec_error"):
        out.append(
            previous + "\n\nIT FAILED WHEN RUN. The database reported:\n"
            + state["exec_error"]
            + "\nFix it - check the table and column names, the joins and the types against "
              "the SCHEMA block above."
        )
        mechanical = True
    elif state.get("contract_error"):
        out.append(
            previous + "\n\nIT RAN, BUT WHAT IT RETURNED CANNOT BE USED:\n"
            + state["contract_error"]
        )
        mechanical = True

    if state.get("verify_feedback"):
        # Without a mechanical error this IS the rejection, and carries the previous attempt
        # with it. Alongside one, it is the objection that still stands underneath.
        head = (
            "AN INDEPENDENT REVIEWER ALSO REJECTED THIS RULE EARLIER, AND THAT OBJECTION STILL "
            "STANDS. Satisfy it AS WELL as the problem above - a rewrite that fixes only the "
            "problem above will be rejected again for this:\n"
            if mechanical
            else previous + "\n\nAN INDEPENDENT REVIEWER REJECTED IT.\nWHAT TO FIX:\n"
        )
        out.append(
            head
            + state["verify_feedback"]
            + "\n\nWrite a MATERIALLY DIFFERENT probe that addresses this. Do not resubmit the "
              "same queries - they would return the same data and be rejected again. If you "
              "believe the previous version was already right, make the smallest change that "
              "satisfies the feedback."
        )

    if state.get("tried_sql"):
        out.append(
            f"You have already made {len(state['tried_sql'])} attempt(s) at this rule. Each new "
            "attempt must differ materially from the ones before it."
        )
    return out


def sql_author_node(state: CompileState) -> dict:
    parts: list[str] = [
        "SCHEMA (the ONLY tables and columns that exist - never reference another):\n"
        + state.get("schema_block", ""),
    ]
    if state.get("hint_block"):
        parts.append(state["hint_block"])
    if state.get("patterns"):
        parts.append("WORKED PROBE SHAPES:\n" + state["patterns"])
    if state.get("coverage"):
        parts.append(state["coverage"])

    parts.append("THE RULE TO IMPLEMENT:\n" + rule_brief(dict(state)))

    # A `seed` rule carries SQL written against a previous shape of this database. It is a
    # starting point, never an answer: offering it without saying so invites a verbatim copy,
    # which is exactly the stale query the recompile exists to replace.
    if state.get("sql_mode") == "seed" and state.get("seed_summary_sql"):
        parts.append(
            "STARTING POINT - a previous version of this probe. Treat it as a draft, not as "
            "truth: verify every table and column against the SCHEMA block above, and correct "
            "anything that has been renamed, retyped or removed. Keep its intent, fix its "
            "facts.\n\n"
            f"SUMMARY:\n{state['seed_summary_sql']}\n\nDETAIL:\n{state['seed_detail_sql']}"
        )

    if state.get("concerns"):
        parts.append(
            "AUTOMATED CHECKS RAISED THESE CONCERNS ABOUT THE PREVIOUS ATTEMPT:\n"
            + numbered(state["concerns"])
        )
    # A FAMILY rule is written once and applied to every matching feature in the schema, so the
    # query must name the feature through tokens rather than literally. The author still writes
    # against ONE real feature - the values below - so it can check its work against the schema
    # and the hints exactly as it would for any other rule; the tokens are only what makes the
    # result reusable for the other thirty-five.
    placeholders = state.get("placeholders") or ()
    params = state.get("params") or {}
    if placeholders:
        parts.append(
            "THIS QUERY IS WRITTEN ONCE AND REUSED.\n"
            f"It will be applied to every {state.get('expands_over', 'feature')} in this "
            "database, so wherever one of the following belongs, write the TOKEN, spelled "
            "exactly as shown, instead of the literal name:\n"
            + "\n".join(
                f"  {{{{{name}}}}}  = {params.get(name, '')}" for name in placeholders
            )
            + "\n\nThe values on the right are the real feature you are writing against - use "
            "them to check the schema, the types and the hints, then write the token. Every "
            "token above must appear in your queries; a literal name where a token belongs "
            "makes the same query run against the wrong table for every other feature."
            "\n\nUSE NO OTHER COLUMN OF THIS TABLE. The tokens above are the ONLY columns you "
            "may name. Every other column you can see in the SCHEMA block exists in the table "
            "you happen to be writing against and NOT in the others this query is copied to - "
            "naming one makes every other copy fail with \"invalid column name\". If you want a "
            "label, a key or a filter, it must come from a token or from the query's own "
            "computed values."
            "\n\nWRITE IT TO SURVIVE A DIFFERENT COLUMN TYPE. The other features this query is "
            "copied to have their own declared types, and yours is only one of them. In "
            "particular a `text`, `ntext` or `image` column cannot be compared, trimmed, "
            "grouped or sorted at all - so wrap every such use as CAST(col AS nvarchar(max)) "
            "even when the column you can see would not need it. A query that is correct only "
            "for your column's type fails at the database for every feature that differs, and "
            "that failure is not visible while you are writing it."
        )

    parts.extend(_feedback_sections(state))
    parts.append("Write the two queries now, as the two tagged blocks described above.")

    # The reference material is cut to THIS rule, so the prompt carries the business
    # definitions it needs and not the whole file. See prompts._for_rule().
    rule_text = " ".join(str(state.get(k) or "") for k in
                         ("title", "category", "entity", "method", "body"))
    tags = tuple(state.get("tags") or ())
    # Which worked examples this rule could actually learn from, decided from the same schema
    # and hints it is about to read - not from what the rule's prose happens to say.
    conditions = example_conditions(
        state.get("schema_block") or "",
        state.get("hint_block") or "",
        str(state.get("method") or ""),
        str(state.get("tolerance") or ""),
    )

    raw = chat(
        author_system(rule_text, tags, conditions),
        "\n\n".join(p for p in parts if p),
        temperature=0.0,
    )
    blocks = extract_sql_blocks(raw)
    summary_sql = blocks.get("summary", "")
    detail_sql = blocks.get("detail", "")

    # Keep the token form as the reusable template, and hand the SUBSTITUTED form downstream.
    # Everything after this node - the safety gate, the database, the contract check, the
    # reviewer - must judge a query that actually runs, against real data. Reviewing the
    # template instead would approve SQL nobody had executed.
    summary_template = detail_template = ""
    if placeholders and summary_sql and detail_sql:
        summary_template, detail_template = summary_sql, detail_sql
        values = {**params, "rule_id": state.get("rule_id", "")}
        summary_sql = substitute(summary_sql, values)
        detail_sql = substitute(detail_sql, values)

    if not summary_sql or not detail_sql:
        # Not raised as an error here: the Validator owns retry accounting, and static_problems
        # already reports a missing half as a contract violation in the author's own words.
        log.warning(
            "SQL[%s try%d]: reply had %s - expected both tagged blocks",
            state.get("rule_id", ""), state.get("retry_count", 0),
            ", ".join(sorted(blocks)) or "no tagged sql block",
        )
    else:
        log.info(
            "SQL[%s try%d]: summary %d chars, detail %d chars",
            state.get("rule_id", ""), state.get("retry_count", 0),
            len(summary_sql), len(detail_sql),
        )

    return {
        "summary_sql": summary_sql,
        "detail_sql": detail_sql,
        "summary_sql_template": summary_template,
        "detail_sql_template": detail_template,
        "validation_error": "",
        "exec_error": "",
        "contract_error": "",
        "concerns": [],
        # verify_feedback is deliberately NOT cleared. It is the reviewer's semantic
        # instruction and stays valid until the reviewer rules on the rewrite. Clearing it here
        # dropped it the moment a rewrite tripped the validator or the database: the next
        # attempt then saw only the syntax error, lost the reason it was rejected, and was free
        # to drift back to the query that had already been refused.
        "tried_sql": list(state.get("tried_sql", [])) + [_pair_signature(summary_sql, detail_sql)],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
