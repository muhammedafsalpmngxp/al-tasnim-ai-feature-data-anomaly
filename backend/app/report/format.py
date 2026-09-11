"""Shared rendering decisions, so the Word document and the Excel workbook cannot disagree.

Both formats are built from the SAME rows and the same helpers here. That is not tidiness: a
report whose spreadsheet and document state different numbers for one rule is worse than no
report, because the reader cannot tell which half to believe and stops trusting both.
"""
from __future__ import annotations

import datetime as dt
import os
import re

_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports"
)

# Severity ordering and colour, used by both formats and by the UI.
SEVERITY_COLOURS: dict[str, str] = {
    "critical": "C00000",
    "high": "E36C0A",
    "medium": "BF9000",
    "low": "808080",
}


def reports_dir() -> str:
    os.makedirs(_REPORTS_DIR, exist_ok=True)
    return _REPORTS_DIR


def header(name: str) -> str:
    """A column name in business language.

    The contract's own prefixes are stripped: a reader does not need to see that a column was
    called evidence_rig_off_date to understand "Rig Off Date". The prefix is a machine
    convention and it has done its job by the time the report is written.
    """
    text = str(name)
    for prefix in ("evidence_", "entity_", "explain_", "severity_"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return text.replace("_", " ").strip().title() or str(name)


def cell(value) -> str:
    """A value as display TEXT. Used by Word, and by Excel for anything non-numeric."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def number_or_text(value):
    """Excel wants real numbers right-aligned and sortable; everything else becomes text."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return value
    return cell(value)


# ── Column sizing ───────────────────────────────────────────────────────────────
# A full pass would defeat the point of streaming a large export, and the first few hundred
# rows are representative enough to stop a column being visibly too narrow.
WIDTH_SAMPLE_ROWS = 200
MIN_WIDTH_CHARS = 9
MAX_WIDTH_CHARS = 46


def content_widths(columns: list[str], rows: list[list]) -> list[int]:
    """Width per column, wide enough for the HEADER and a sample of the values.

    Sizing on the header alone is not enough - a long header still clips - and sizing on every
    row is unaffordable, so a bounded sample decides it.
    """
    widths = [len(header(c)) for c in columns]
    for row in rows[:WIDTH_SAMPLE_ROWS]:
        for i, value in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell(value)))
    return [max(MIN_WIDTH_CHARS, min(MAX_WIDTH_CHARS, w + 2)) for w in widths]


# ── Sheet names ─────────────────────────────────────────────────────────────────
# Excel rejects these in a sheet name and openpyxl raises on them. Taken from openpyxl's own
# INVALID_TITLE_REGEX rather than guessed.
_INVALID_SHEET_CHARS = frozenset('\\*?:/[]')


def sheet_title(title: str, used: set[str] | None = None) -> str:
    """A sheet name Excel will accept: legal characters, <=31 chars, unique, never empty.

    Uniqueness matters here in a way it does not in a single-sheet export: this workbook has
    one sheet per rule, and two rules whose titles agree in their first 31 characters is not
    hypothetical - generated probes are named after the table and column they check.
    """
    cleaned = "".join(" " if c in _INVALID_SHEET_CHARS else c for c in (title or ""))
    name = " ".join(cleaned.split())[:31].strip() or "Findings"
    if used is None:
        return name
    if name not in used:
        used.add(name)
        return name
    for suffix in range(2, 1000):
        tag = f" ({suffix})"
        candidate = name[: 31 - len(tag)].strip() + tag
        if candidate not in used:
            used.add(candidate)
            return candidate
    unique = name[:27] + " ~~~"
    used.add(unique)
    return unique


_SAFE_FILE = re.compile(r"[^A-Za-z0-9._-]+")


def report_filename(run_id: str, ext: str, prefix: str = "data-quality") -> str:
    return os.path.join(reports_dir(), f"{prefix}-{_SAFE_FILE.sub('-', run_id)}.{ext}")


def cap_note(total: int, shown: int, limit_name: str) -> str:
    """The sentence a capped table must carry. A silent cap is a lie by omission."""
    if total <= shown:
        return ""
    return (
        f"Showing the {shown:,} most serious of {total:,} affected records. The complete list "
        f"is in the Excel workbook ({limit_name})."
    )
