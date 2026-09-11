"""Renders the compile-time agent's knowledge base: `.cache/schema.txt`,
`.cache/hint_data.txt`, `.cache/fingerprints.json`.

Three files, one purpose: give the (not-yet-built) LangGraph compiler in `app/agent/`
everything it needs to reason about a table WITHOUT it ever running its own exploratory
SQL against the live database. Read-only, run rarely (only when a table's structure
changed), and every value that reaches these files has already passed through
`app/sentinel/pii.py` -- nothing here is a second, unguarded path to the same columns.

This module does not decide what is an anomaly. It states facts: columns, keys, declared
or candidate grain, null percentages, distinct counts, small-lookup values. The agent
(elsewhere) turns facts into hypotheses; nothing here does.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, get_settings
from app.db.source import SourceDatabase, quote_ident as _q
from app.logging import get_logger
from app.sentinel import pii
from app.sentinel.normalise.spec import NormalisationSpec, load_spec
from app.sentinel.schema_snapshot import (
    _UNCOUNTABLE_TYPES,
    SchemaSnapshotter,
    TableSnapshot,
)
from app.sentinel.scope import Scope

log = get_logger(__name__)

# Numeric/date-ish types worth a min/max line. Deliberately a small allow-list rather than
# "everything but the uncountable types" -- a min/max of a varchar status code ("A" to "Z")
# is alphabetic noise, not a useful fact.
_DATETIME_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_RANGE_TYPES = {
    "int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money",
    *_DATETIME_TYPES,
}

# A lookup table is recognised by SHAPE (row count), not by schema or name -- so this
# follows whatever tables actually exist, matching the project's "nothing hardcoded" rule.
_LOOKUP_MAX_ROWS = 50
_LOOKUP_COL_SUFFIXES = ("name", "code", "description", "desc", "label", "status", "type")
_MAX_PROFILED_COLUMNS = 60  # same cap dead_column_findings uses, for the same reason


@dataclass(slots=True)
class ColumnProfile:
    null_pct: float
    distinct: int | None = None       # None for an uncountable type (text/ntext/image/...)
    min_value: str | None = None
    max_value: str | None = None


@dataclass(slots=True)
class TableProfile:
    row_count: int
    columns: dict[str, ColumnProfile] = field(default_factory=dict)
    lookup_values: list[str] | None = None  # only set for small, lookup-shaped tables


def _is_lookup_column(col: str) -> bool:
    lc = col.lower()
    return lc.endswith(_LOOKUP_COL_SUFFIXES) or lc in ("code", "name")


class KnowledgeBase:
    """Owns the three cache files. One instance per `introspect` run."""

    def __init__(
        self,
        source: SourceDatabase,
        scope: Scope,
        spec: NormalisationSpec,
        *,
        cache_dir: Path,
    ) -> None:
        self.src = source
        self.scope = scope
        self.spec = spec
        self.cache_dir = cache_dir
        self.snapshotter = SchemaSnapshotter(source, scope)

    # ---------------------------------------------------------------------- build
    def build(self) -> dict[str, str]:
        """Introspect the live database and write all three files. Returns their paths."""
        snapshots = self.snapshotter.capture()
        self.snapshotter.measure_grain(snapshots, spec=self.spec)
        profiles = self._profile_all(snapshots)

        schema_text = render_schema_text(snapshots)
        hint_text = render_hint_data(snapshots, profiles)
        fingerprints = {name: snap.compile_fingerprint for name, snap in snapshots.items()}

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "schema": self.cache_dir / "schema.txt",
            "hints": self.cache_dir / "hint_data.txt",
            "fingerprints": self.cache_dir / "fingerprints.json",
        }
        paths["schema"].write_text(schema_text, encoding="utf-8")
        paths["hints"].write_text(hint_text, encoding="utf-8")
        paths["fingerprints"].write_text(
            json.dumps(fingerprints, indent=2, sort_keys=True), encoding="utf-8"
        )
        log.info(
            "introspect.built",
            tables=len(snapshots),
            schema_chars=len(schema_text),
            hint_chars=len(hint_text),
        )
        return {k: str(v) for k, v in paths.items()}

    # ------------------------------------------------------------------- profiling
    def _profile_all(self, snapshots: dict[str, TableSnapshot]) -> dict[str, TableProfile]:
        profiles: dict[str, TableProfile] = {}
        for name, snap in sorted(snapshots.items()):
            if snap.row_count <= 0:
                continue  # emptiness is PIP-010's business, not the agent's knowledge base
            visible = self._visible_columns(snap)
            if not visible:
                continue
            t0 = time.perf_counter()
            profile = self._profile_one(snap, visible)
            elapsed = time.perf_counter() - t0
            if elapsed > 5:
                log.warning(
                    "introspect.profile_slow", table=name, seconds=round(elapsed, 1),
                    columns=len(visible), rows=snap.row_count,
                )
            if profile is None:
                continue
            if snap.row_count <= _LOOKUP_MAX_ROWS:
                profile.lookup_values = self._lookup_values(snap, visible)
            profiles[name] = profile
            log.info(
                "introspect.profiled", table=name, done=len(profiles), of=len(snapshots),
                seconds=round(elapsed, 1),
            )
        return profiles

    def _visible_columns(self, snap: TableSnapshot) -> list[str]:
        """Both filters: Scope (DQ_EXCLUDED_COLUMNS, secret-looking names) AND the
        declared `pii_columns` patterns -- the same double gate `llm/tools.py` applies,
        so a column invisible to the Tier 2 agent is equally invisible here."""
        schema, table = snap.ref.schema, snap.ref.table
        cols = [
            c for c in snap.columns
            if self.scope.column_allowed(schema, table, c) and not pii.is_pii(snap.ref.full, c)
        ]
        return cols[:_MAX_PROFILED_COLUMNS]

    def _profile_one(self, snap: TableSnapshot, columns: list[str]) -> TableProfile | None:
        # COUNT(DISTINCT ...) is the expensive part of this query -- SQL Server builds a
        # separate work area per DISTINCT expression, so the cost scales with column COUNT
        # as much as with row count. A first full sweep across all 74 in-scope tables with
        # no size cap here ran past 300s with zero progress visibility (fixed above) and no
        # way to tell "one pathological table" from "just cumulative". Above this
        # project's existing large-table threshold, null% (one cheap SUM per column,
        # regardless of how many) is still computed, but DISTINCT and MIN/MAX are skipped --
        # `measure_grain` and `dead_column_findings` already draw this same line for the
        # same reason, at the same threshold, so this is consistent rather than a new rule.
        skip_distinct = snap.row_count > self.scope.large_table_row_limit
        parts = []
        for c in columns:
            dtype = snap.columns[c].data_type.lower()
            parts.append(f"SUM(CASE WHEN {_q(c)} IS NULL THEN 1 ELSE 0 END) AS {_q(c + '__null')}")
            if not skip_distinct and dtype not in _UNCOUNTABLE_TYPES:
                parts.append(f"COUNT(DISTINCT {_q(c)}) AS {_q(c + '__distinct')}")
            if not skip_distinct and dtype in _RANGE_TYPES:
                # CONVERT to varchar server-side, never a raw fetch of MIN/MAX: found live
                # -- well.task_daily.time_stamp is `datetimeoffset` (ODBC type -155), which
                # crashed the whole table's profile with "ODBC SQL type -155 is not yet
                # supported" and silently dropped it from hint_data.txt entirely (caught
                # by the try/except, logged, but the table just went missing with no
                # error visible short of reading the log). Same fix already used in
                # generator.py/normaliser.py for the identical crash.
                if dtype in _DATETIME_TYPES:
                    parts.append(f"CONVERT(varchar(30), MIN({_q(c)}), 121) AS {_q(c + '__min')}")
                    parts.append(f"CONVERT(varchar(30), MAX({_q(c)}), 121) AS {_q(c + '__max')}")
                else:
                    parts.append(f"MIN({_q(c)}) AS {_q(c + '__min')}")
                    parts.append(f"MAX({_q(c)}) AS {_q(c + '__max')}")
        try:
            row = self.src.one(
                f"SELECT COUNT(*) AS __n, {', '.join(parts)} "
                f"FROM {_q(snap.ref.schema)}.{_q(snap.ref.table)}"
            ) or {}
        except Exception as exc:  # noqa: BLE001 -- one odd table must not stop the sweep
            log.warning("introspect.profile_failed", table=snap.ref.full, error=str(exc)[:200])
            return None
        n = int(row.get("__n") or 0)
        if n == 0:
            return None
        cols_out: dict[str, ColumnProfile] = {}
        for c in columns:
            dtype = snap.columns[c].data_type.lower()
            nulls = int(row.get(f"{c}__null") or 0)
            cols_out[c] = ColumnProfile(
                null_pct=round(100.0 * nulls / n, 1),
                distinct=(int(row[f"{c}__distinct"]) if f"{c}__distinct" in row and row[f"{c}__distinct"] is not None else None),
                min_value=(str(row[f"{c}__min"]) if row.get(f"{c}__min") is not None else None),
                max_value=(str(row[f"{c}__max"]) if row.get(f"{c}__max") is not None else None),
            )
        return TableProfile(row_count=n, columns=cols_out)

    def _lookup_values(self, snap: TableSnapshot, columns: list[str]) -> list[str] | None:
        """Real values from a SMALL table only -- `row_count` was already checked by the
        caller against `_LOOKUP_MAX_ROWS`, so this never scans a transactional table."""
        hint_cols = [c for c in columns if _is_lookup_column(c)]
        if not hint_cols:
            return None
        col_sql = ", ".join(_q(c) for c in hint_cols)
        try:
            rows = self.src.rows(
                f"SELECT DISTINCT TOP 30 {col_sql} "
                f"FROM {_q(snap.ref.schema)}.{_q(snap.ref.table)} ORDER BY {col_sql}"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("introspect.lookup_values_failed", table=snap.ref.full, error=str(exc)[:200])
            return None
        if not rows:
            return None
        out = []
        for r in rows:
            out.append("|".join(str(r.get(c, "")) if r.get(c) is not None else "" for c in hint_cols))
        return out


# ------------------------------------------------------------------------ rendering
def render_schema_text(snapshots: dict[str, TableSnapshot]) -> str:
    """One `TABLE schema.table` block per table: columns (+ PK marker), FK lines, and
    the grain line -- declared (authoritative) or candidates (measured, unconfirmed).

    Column visibility is NOT filtered here: a PII column still structurally exists on the
    table and the agent needs to know a table has, say, 14 columns even if it may never
    see one of their values. `hint_data.txt` is where PII is actually excluded from
    anything resembling real data.
    """
    lines: list[str] = []
    for name, snap in sorted(snapshots.items()):
        lines.append(f"TABLE {name}  ({snap.row_count:,} rows)")
        for col_name, col in sorted(snap.columns.items(), key=lambda kv: kv[1].ordinal):
            null = "" if not col.is_nullable else " NULL"
            pk = " PK" if col_name in snap.primary_key else ""
            lines.append(f"  - {col_name} {col.data_type}{null}{pk}")
        for fk in snap.foreign_keys:
            nullable = " (nullable -- an unlinked row is possible)" if fk.nullable else ""
            lines.append(f"  FK: {fk.column} -> {fk.target_table}.{fk.target_column}{nullable}")
        if snap.grain_is_confirmed:
            lines.append(
                f"  GRAIN (declared, confirmed): {snap.declared_grain}"
                + (f" -- measured {snap.declared_rows_per_key} rows/key" if snap.declared_rows_per_key else "")
            )
        elif snap.grain_candidates:
            lines.append("  GRAIN: UNCONFIRMED -- candidates, closest-to-unique first:")
            for cand in snap.grain_candidates:
                flag = "" if cand.is_unique else "  <- MANY ROWS PER THIS KEY"
                lines.append(f"    * {', '.join(cand.columns)} -> {cand.rows_per_value} rows/value{flag}")
        lines.append("")
    return "\n".join(lines).strip()


def render_hint_data(
    snapshots: dict[str, TableSnapshot], profiles: dict[str, TableProfile]
) -> str:
    """Per-column statistics plus real values from small lookup tables. Every value in
    this file has already passed the PII filter in `_visible_columns` -- nothing here
    needs a second check before being handed to a prompt.
    """
    lines: list[str] = []
    for name in sorted(profiles):
        profile = profiles[name]
        lines.append(f"TABLE {name}  ({profile.row_count:,} rows)")
        for col, cp in sorted(profile.columns.items()):
            bits = [f"{cp.null_pct}% null"]
            if cp.distinct is not None:
                bits.append(f"{cp.distinct} distinct")
            if cp.min_value is not None:
                bits.append(f"range [{cp.min_value} .. {cp.max_value}]")
            lines.append(f"  {col}: {' · '.join(bits)}")
        if profile.lookup_values:
            lines.append(f"  VALUES: {'; '.join(profile.lookup_values)}")
        lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- entrypoint
def build_knowledge_base(settings: Settings | None = None) -> dict[str, str]:
    s = settings or get_settings()
    source = SourceDatabase(s)
    scope = Scope(s)
    spec = load_spec()
    kb = KnowledgeBase(source, scope, spec, cache_dir=s.cache_dir)
    return kb.build()
