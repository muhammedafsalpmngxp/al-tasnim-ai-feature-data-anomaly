"""Plain-language labels for internal enum values that reach a reader.

Both report writers need these, so they live here rather than in either one. The reports
are read by people who do not know this tool's internals: naming the mechanism ("dedup",
"blank_to_null", "rows removed") invites the reader to assume the source database was
edited to fix the problem. It never is -- the connection used to read it has no write
permission -- so nothing user-facing should imply otherwise.
"""
from __future__ import annotations

ACTION_LABELS = {
    "dedup": "Duplicate rows",
    "snapshot_pin": "Repeated history rows",
    "placeholder_date": "Stand-in dates (e.g. 1900-01-01)",
    "blank_to_null": "Blank entries",
    "sentinel_to_null": "Status text instead of a value",
}

# One sentence, used wherever a report shows what was set aside for the analysis.
NO_DB_CHANGE_NOTE = (
    "Rows and values listed here were set aside for this analysis only. Nothing in the "
    "source database was changed or deleted -- the connection used to read it has no "
    "permission to write."
)


def action_label(kind: str) -> str:
    return ACTION_LABELS.get(kind, kind)
