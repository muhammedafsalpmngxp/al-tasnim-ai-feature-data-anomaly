"""JSON schemas for structured LLM output.

Every schema is `additionalProperties: false` with every field required -- the model
cannot return anything the validator (`validate.py`) doesn't expect, and cannot omit a
field silently. This is what makes "strict" structured output actually strict.
"""
from __future__ import annotations

from typing import Any

NARRATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["explanation", "root_cause", "remediation", "cited_numbers"],
    "properties": {
        "explanation": {
            "type": "string",
            "description": "Two plain-business-language sentences explaining this finding.",
        },
        "root_cause": {
            "type": "string",
            "description": "One sentence: the likely underlying cause.",
        },
        "remediation": {
            "type": "string",
            "description": "One concrete, actionable fix.",
        },
        "cited_numbers": {
            "type": "array",
            "description": (
                "Every number mentioned in explanation/root_cause/remediation, as they "
                "appear in the text (e.g. '449', '55%'). Empty array if no number was used."
            ),
            "items": {"type": "string"},
        },
    },
}

CORRELATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["incidents"],
    "properties": {
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "root_cause", "finding_ids", "narrative"],
                "properties": {
                    "title": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "finding_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "check_id values of the findings grouped into this incident.",
                    },
                    "narrative": {
                        "type": "string",
                        "description": "2-4 sentences: why these findings share one root cause.",
                    },
                },
            },
        }
    },
}

# ---------------------------------------------------------------------- Tier 2: suggest
# Tool definitions for the suggestion agent (app/sentinel/llm/suggest.py). The agent may
# call list_tables/describe_table/run_query any number of times within its session budget,
# then MUST call propose_check to record each candidate -- propose_check is the only way
# it can "conclude", so a session that runs out of budget without proposing anything
# yields zero suggestions rather than a half-formed answer.
EXPLORATION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function", "name": "list_tables",
        "description": "List every in-scope table with its approximate row count.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function", "name": "describe_table",
        "description": (
            "Get the column list, types, null%, and distinct count for one table. "
            "PII/secret-named columns are never shown."
        ),
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "schema.table, e.g. well.well_master"}},
            "required": ["table"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "run_query",
        "description": (
            "Execute a read-only SELECT/WITH statement to test a hypothesis. Results are "
            "capped to a small row sample -- use this to check whether a pattern you "
            "suspect actually occurs in the data, not to dump a table."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT or WITH statement."}},
            "required": ["sql"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "propose_check",
        "description": (
            "Record ONE candidate new data-quality check you have verified actually finds "
            "something with run_query. This does not create a real check -- it only queues "
            "the idea for a human to review."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "family", "severity_guess", "hypothesis", "sql_text", "table_ref"],
            "properties": {
                "title": {"type": "string", "description": "Short, specific title for this candidate check."},
                "family": {"type": "string", "description": "A short family code, e.g. MDM, REF, VAL, CON."},
                "severity_guess": {"type": "string", "enum": ["critical", "high", "medium", "low", "review"]},
                "hypothesis": {
                    "type": "string",
                    "description": "1-3 sentences: what this checks for and why it matters, citing what run_query actually showed.",
                },
                "sql_text": {"type": "string", "description": "The exact SELECT you verified with run_query."},
                "table_ref": {"type": "string", "description": "The primary schema.table this check concerns."},
            },
        },
        "strict": True,
    },
]

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "paragraphs", "top_priorities"],
    "properties": {
        "headline": {"type": "string", "description": "One sentence, the single most important fact."},
        "paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 short paragraphs for an executive summary.",
        },
        "top_priorities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 short bullet points, most urgent first.",
        },
    },
}
