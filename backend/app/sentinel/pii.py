"""PII exclusion at the LLM / knowledge boundary.

`config/column_semantics.yaml` declares `pii_columns`, and `NormalisationSpec.is_pii()`
resolves those patterns correctly -- but until this module existed **nothing called it**.
`DQ_EXCLUDED_COLUMNS` was empty too, so `Scope.column_allowed()` returned True for
`supervisor_email`, `task_assignee`, `ref.employee.email` and `dbo.employee_contact.*`.
The README's "PII is excluded from every LLM payload" and `llm/tools.py`'s "the agent never
learns such a column exists" were both aspirational rather than enforced.

WHY THIS IS SEPARATE FROM `Scope.column_allowed` rather than folded into it:

  * `SchemaSnapshotter.capture()` uses `column_allowed()` to record the structural snapshot
    that drift detection diffs. Hiding PII columns there would make the next run report 8
    columns as REMOVED -- a false drift event -- and would permanently blind PIP-007 to a
    real schema change on those columns.
  * The two concerns genuinely differ: PII must not reach a prompt or a report, but we do
    still want to know if an email column is dropped from the database.

So structural monitoring keeps seeing every column, and everything that builds an LLM
payload or a human-facing artifact routes through here instead.
"""
from __future__ import annotations

from functools import lru_cache

from app.sentinel.normalise.spec import NormalisationSpec, load_spec


@lru_cache(maxsize=1)
def _spec() -> NormalisationSpec:
    """Cached: `load_spec()` re-reads and re-parses two YAML files, and this is called
    once per column of every table."""
    return load_spec()


def is_pii(table: str, column: str) -> bool:
    """True when `column` on `table` matches a declared `pii_columns` pattern."""
    return _spec().is_pii(table, column)


def visible_columns(table: str, columns: list[str]) -> list[str]:
    """Drop PII columns entirely -- not flag them, drop them.

    Deliberately removal rather than masking: `llm/tools.py` states the intent as "the
    agent never learns such a column exists, not merely that it may not query it". A
    column listed as `[redacted]` still tells a model the field is there and invites it to
    reason about, or ask for, what it holds.
    """
    return [c for c in columns if not is_pii(table, c)]


def reset_cache() -> None:
    """Test hook: drop the cached spec so a test can point at a different YAML."""
    _spec.cache_clear()
