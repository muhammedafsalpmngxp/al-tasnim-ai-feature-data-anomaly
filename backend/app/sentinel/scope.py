"""Which tables and columns the Sentinel is allowed to look at.

Deliberately separate from the chat feature's ALLOWED_SCHEMAS / EXCLUDED_TABLES: that list
hides four tables this feature must read (dbo.activity_master_mapping is required by
business rule §3; the weightage data lives in the two activity_task_plan tables). Editing
the shared list would break the other feature, so the Sentinel gets its own DQ_* variables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

# Names that look like credentials are hidden regardless of configuration.
SECRET_NAME = re.compile(r"pass|pwd|secret|token|api_?key|salt|hash|credential", re.I)


@dataclass(frozen=True, slots=True)
class TableRef:
    schema: str
    table: str

    @property
    def full(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def bracketed(self) -> str:
        # Same quoter as every other identifier path -- see db.source.quote_ident.
        from app.db.source import quote_ident
        return f"{quote_ident(self.schema)}.{quote_ident(self.table)}"

    @classmethod
    def parse(cls, value: str) -> "TableRef":
        if "." not in value:
            raise ValueError(f"table reference must be schema.table, got {value!r}")
        schema, table = value.split(".", 1)
        return cls(schema.strip(), table.strip())


class Scope:
    """Resolves DQ_ALLOWED_SCHEMAS / DQ_EXCLUDED_TABLES / DQ_EXCLUDED_COLUMNS."""

    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        self.allowed_schemas = {x.lower() for x in s.dq_allowed_schemas}
        self._excluded_tables = {x.lower() for x in s.dq_excluded_tables}
        self._excluded_columns = {x.lower() for x in s.dq_excluded_columns}
        self.large_table_row_limit = s.dq_large_table_row_limit
        self.sample_percent = s.dq_sample_percent

    # -------------------------------------------------------------------- tables
    def table_allowed(self, schema: str, table: str) -> bool:
        if schema.lower() not in self.allowed_schemas:
            return False
        full = f"{schema}.{table}".lower()
        bare = table.lower()
        for pat in self._excluded_tables:
            if pat == full or ("." not in pat and pat == bare):
                return False
        return True

    def exclusion_reason(self, schema: str, table: str) -> str | None:
        if schema.lower() not in self.allowed_schemas:
            return f"schema {schema!r} not in DQ_ALLOWED_SCHEMAS"
        full = f"{schema}.{table}".lower()
        bare = table.lower()
        for pat in self._excluded_tables:
            if pat == full or ("." not in pat and pat == bare):
                return f"matched DQ_EXCLUDED_TABLES entry {pat!r}"
        return None

    # ------------------------------------------------------------------- columns
    def column_allowed(self, schema: str, table: str, column: str) -> bool:
        if SECRET_NAME.search(column):
            return False
        full = f"{schema}.{table}.{column}".lower()
        mid = f"{table}.{column}".lower()
        bare = column.lower()
        for pat in self._excluded_columns:
            if pat in (full, mid) or ("." not in pat and pat == bare):
                return False
        return True

    # ------------------------------------------------------------------ sampling
    def sampling_for(self, rows: int) -> int | None:
        """Return a TABLESAMPLE percent, or None to scan fully.

        No check may scan 17.9M rows unbounded.
        """
        if rows > self.large_table_row_limit:
            return self.sample_percent
        return None

    def source_expression(self, ref: TableRef, rows: int) -> tuple[str, bool]:
        """FROM-clause expression plus whether sampling was applied."""
        pct = self.sampling_for(rows)
        if pct is None:
            return ref.bracketed, False
        return f"{ref.bracketed} TABLESAMPLE ({pct} PERCENT)", True

    # -------------------------------------------------------------------- summary
    def describe(self) -> dict[str, Any]:
        return {
            "allowed_schemas": sorted(self.allowed_schemas),
            "excluded_tables": sorted(self._excluded_tables),
            "excluded_columns": sorted(self._excluded_columns),
            "large_table_row_limit": self.large_table_row_limit,
            "sample_percent": self.sample_percent,
        }

    def filter_tables(self, row_counts: dict[str, int]) -> list[TableRef]:
        out: list[TableRef] = []
        for full in sorted(row_counts):
            schema, table = full.split(".", 1)
            if self.table_allowed(schema, table):
                out.append(TableRef(schema, table))
        return out
