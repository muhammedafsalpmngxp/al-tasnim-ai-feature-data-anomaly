"""Summarizer node (LLM, MAIN tier) - the ONE model call in an entire detection run.

WHY A RUN'S COST IS FLAT
------------------------
This node is shown the scorer's AGGREGATE table - one line per rule - plus a handful of example
records from the worst few. It is never shown the findings in bulk. So a run that finds twelve
anomalies and a run that finds twelve million cost the same single call, and no volume of bad
data can push the prompt past the context window.

That bound is structural, not a guess: `ranked` holds one small dict per rule, and the samples
are sliced from the in-memory Word cap, itself already bounded by the probe runner.

IT IS NEVER GIVEN THE SCHEMA OR THE BUSINESS RULES. Its output is the part of the report a
manager actually reads, and it is told never to name a table or a column - so handing it a
document full of table names would create exactly the leak the instruction forbids, for no
benefit. It reasons about counts, not about SQL.

A FAILURE HERE DOES NOT FAIL THE RUN. The findings are the report's substance and they are
already computed; the executive summary is the presentation layer. If the model is unreachable
the report is written with a plain deterministic summary and says so, rather than throwing away
a complete set of results.
"""
from __future__ import annotations

from app.config import settings
from app.graph.nodes._common import rows_to_table
from app.graph.prompts import SUMMARIZER_SYSTEM
from app.graph.run_state import RunState
from app.llm import chat
from app.observability import get_logger

log = get_logger()

# How many rules get illustrative rows. The table gives every rule's numbers; examples only
# help for the few the summary will actually single out.
_SAMPLE_RULES = 5
_SAMPLE_ROWS = 3
# Rules listed individually before the rest are folded into category totals. Comfortably above
# a realistic number of DISTINCT problems while bounding a pathological catalog.
_MAX_LISTED = 40


def _headline(state: RunState) -> str:
    totals = state.get("totals") or {}
    return (
        f"Overall data quality score: {state.get('score', 0)} out of 100.\n"
        f"Probes run: {totals.get('probes_run', 0)}. "
        f"With findings: {totals.get('probes_with_findings', 0)}. "
        f"Clean: {totals.get('probes_clean', 0)}. "
        f"Failed: {totals.get('probes_failed', 0)}.\n"
        f"Records examined: {totals.get('records_examined', 0):,}. "
        f"Records flagged: {totals.get('records_flagged', 0):,}."
    )


def _findings_table(ranked: list[dict]) -> str:
    lines = ["severity | records affected | of examined | share | what was found"]
    for row in ranked[:_MAX_LISTED]:
        lines.append(
            f"{row['severity']} | {row['anomaly_count']:,} | {row['scope_total']:,} | "
            f"{row['anomaly_pct']:.2f}% | {row['title']}"
        )
    if len(ranked) > _MAX_LISTED:
        rest = ranked[_MAX_LISTED:]
        lines.append(
            f"... and {len(rest)} further rule(s) affecting "
            f"{sum(r['anomaly_count'] for r in rest):,} records in total"
        )
    return "\n".join(lines)


def _examples(state: RunState) -> str:
    """A few real rows from the worst rules, so the summary can be concrete."""
    results = {r.rule_id: r for r in (state.get("results") or [])}
    blocks: list[str] = []
    for row in (state.get("ranked") or [])[:_SAMPLE_RULES]:
        result = results.get(row["rule_id"])
        if result is None or not result.detail_rows:
            continue
        blocks.append(
            f"Examples for: {row['title']}\n"
            + rows_to_table(result.detail_columns, result.detail_rows, max_rows=_SAMPLE_ROWS)
        )
    return "\n\n".join(blocks)


def _deterministic_summary(state: RunState) -> str:
    """The fallback. Plain, accurate, and obviously machine-written."""
    ranked = state.get("ranked") or []
    totals = state.get("totals") or {}
    if not ranked:
        return (
            f"{totals.get('probes_run', 0)} checks ran and none found anything to report. "
            f"{totals.get('records_examined', 0):,} records were examined."
        )
    lines = [
        f"{len(ranked)} of {totals.get('probes_run', 0)} checks found problems, affecting "
        f"{totals.get('records_flagged', 0):,} of {totals.get('records_examined', 0):,} "
        f"records examined. The most serious are:"
    ]
    for row in ranked[:5]:
        lines.append(
            f"- {row['title']} ({row['severity']}): {row['anomaly_count']:,} records, "
            f"{row['anomaly_pct']:.2f}% of those checked."
        )
    return "\n".join(lines)


def summarizer_node(state: RunState) -> dict:
    ranked = state.get("ranked") or []
    empty_scope = state.get("empty_scope") or []
    not_running = state.get("not_running") or []

    parts = ["HEADLINE NUMBERS:\n" + _headline(state)]
    if ranked:
        parts.append("WHAT EACH CHECK FOUND, worst first:\n" + _findings_table(ranked))
    else:
        parts.append("No check found anything to report.")

    examples = _examples(state)
    if examples:
        parts.append(
            f"EXAMPLE RECORDS (at most {_SAMPLE_ROWS} per check, purely illustrative - the "
            "counts above are the truth):\n" + examples
        )

    # A check that flagged its ENTIRE scope has no working denominator, so its count is not
    # comparable with any other check's. The section for that finding already carries the
    # caveat; the summary did not, and quoted the number as plain fact - "6,435 tasks cannot be
    # traced to an activity" - where 6,435 was also everything the check looked at. A reader of
    # the summary alone was given a figure nobody had qualified.
    saturated = [
        row for row in ranked
        if row.get("scope_total") and row["anomaly_count"] == row["scope_total"]
    ]
    # NEARLY saturated, kept as its OWN list rather than folded into the one above. Both need a
    # caveat, but not the same one: with no denominator at all the count cannot be quoted as a
    # proportion of anything, whereas at 99.98% the proportion exists and is merely unlikely to
    # mean what it appears to. Merging them would force one wording to cover both and overstate
    # the weaker case - the summary would call a possibly-real 99.98% finding unmeasurable.
    from app.rules.contract import nearly_saturated

    near = [
        row for row in ranked
        if nearly_saturated(row.get("anomaly_count"), row.get("scope_total"))
    ]
    if near:
        named = ", ".join(
            f"{r['title']} ({r['anomaly_count']:,} of {r['scope_total']:,})" for r in near[:5]
        )
        parts.append(
            f"QUALIFY THESE FIGURES: {len(near)} check(s) flagged almost everything they "
            f"examined - {named}. A share this high usually means the scope was selected by "
            "the same condition being tested, so the percentage describes the selection rather "
            "than the extent of the problem. Give the count if you mention one, and say the "
            "proportion needs confirming; do not lead with the percentage."
        )
    if saturated:
        named = ", ".join(f"{r['title']} ({r['anomaly_count']:,})" for r in saturated[:5])
        parts.append(
            f"TREAT THESE COUNTS AS UNCONFIRMED: {len(saturated)} check(s) flagged every "
            f"record they examined - {named}. Where a check's scope equals its findings there "
            "is no denominator, so the count measures the size of the population it selected "
            "rather than how much of anything is wrong. If you mention any of these, say the "
            "figure needs confirming; never present it beside a measured percentage as though "
            "the two were the same kind of number."
        )

    # Coverage gaps are stated separately and in these words, because they are the one thing a
    # reader is most likely to misread as good news.
    if empty_scope:
        parts.append(
            f"COVERAGE GAPS: {len(empty_scope)} check(s) examined NO records at all. This is "
            "NOT a clean result - those checks proved nothing and their subject matter is "
            "currently unmonitored."
        )
    if not_running:
        parts.append(
            f"NOT RUNNING: {len(not_running)} check(s) could not be run at all, so their "
            "subject matter is unmonitored."
        )

    parts.append("Write the executive summary now.")

    try:
        summary = chat(SUMMARIZER_SYSTEM, "\n\n".join(parts), temperature=0.3).strip()
    except Exception as exc:  # noqa: BLE001 - never lose a complete set of findings over prose
        log.warning("summary: the model call failed (%s) - using a plain summary", exc)
        return {"summary": _deterministic_summary(state), "llm_calls": 0}

    if not summary:
        log.warning("summary: the model returned nothing - using a plain summary")
        return {"summary": _deterministic_summary(state), "llm_calls": 1}

    log.info("summary: written (%d chars, 1 llm call)", len(summary))
    return {"summary": summary, "llm_calls": 1}
