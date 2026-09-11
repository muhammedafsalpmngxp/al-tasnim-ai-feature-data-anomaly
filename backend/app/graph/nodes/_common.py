"""Formatting helpers shared by the compile nodes.

Every function here bounds what it renders. That is the point of the module: these are the
only places a database result becomes prompt text, so the context window is protected in one
reviewable place rather than in each node's string building.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.config import settings

# A single cell in a rendered sample. Long free-text columns (remarks, descriptions) would
# otherwise dominate a sample that exists only to show the SHAPE of a result.
_MAX_CELL_CHARS = 120


def cell(value: Any) -> str:
    if value is None:
        return "NULL"          # explicit: an agent must be able to tell a null from a blank
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).replace("|", "/").replace("\n", " ").strip()
    return text if len(text) <= _MAX_CELL_CHARS else text[: _MAX_CELL_CHARS - 1] + "…"


def rows_to_table(columns: list[str], rows: list[list[Any]], max_rows: int | None = None) -> str:
    """A compact pipe-separated table, capped at `max_rows` (default: the LLM sample cap).

    Not markdown: no separator row and no padding. An agent reads the values, and on a
    twenty-five row sample the alignment characters are pure token cost.
    """
    if not columns:
        return "(no columns)"
    if not rows:
        return "(no rows)"
    limit = settings.sample_rows if max_rows is None else max_rows
    shown = rows[:limit]
    lines = [" | ".join(str(c) for c in columns)]
    lines += [" | ".join(cell(v) for v in row) for row in shown]
    if len(rows) > limit:
        lines.append(f"... ({len(rows) - limit} further row(s) not shown)")
    return "\n".join(lines)


def summary_line(columns: list[str], row: list[Any] | None) -> str:
    """The single SUMMARY row as `name=value` pairs - the counts an agent must reason from."""
    if not columns or not row:
        return "(the summary query returned nothing)"
    return ", ".join(f"{c}={cell(v)}" for c, v in zip(columns, row))


def numbered(items: list[str], empty: str = "") -> str:
    """A numbered list, for feedback that is acted on item by item."""
    if not items:
        return empty
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


def rule_brief(state: dict) -> str:
    """The rule as the agents must read it: its identity, its metadata, and its intent.

    Assembled in one place so the Author and the Verifier are shown the SAME description of the
    rule. Two slightly different renderings is how a reviewer ends up judging against something
    the author was never asked for.
    """
    tolerance = state.get("tolerance") or ""
    parts = [
        f"RULE {state.get('rule_id', '')} - {state.get('title', '')}",
        f"category: {state.get('category', '')} | severity: {state.get('severity', '')} | "
        f"entity: {state.get('entity', '')} | method: {state.get('method', '')}"
        + (f" | tolerance: {tolerance}" if tolerance else ""),
    ]
    if state.get("grounding_note"):
        parts.append(f"HOW THIS MAPS ONTO THE TABLES: {state['grounding_note']}")
    body = (state.get("body") or "").strip()
    if body:
        parts.append("")
        parts.append(body)
    return "\n".join(parts)


def probe_sql(summary_sql: str, detail_sql: str) -> str:
    """Both halves of a probe, labelled, for a reviewer to read as one artefact."""
    return f"SUMMARY QUERY:\n{summary_sql}\n\nDETAIL QUERY:\n{detail_sql}"
