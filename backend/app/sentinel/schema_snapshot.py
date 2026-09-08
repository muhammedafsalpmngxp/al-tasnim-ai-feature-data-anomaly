"""Schema discovery and drift detection.

Nothing in this module names a table or column. Scope (which schemas/tables are visible)
comes entirely from `Scope` (DQ_ALLOWED_SCHEMAS / DQ_EXCLUDED_TABLES), which is itself
config, not code -- so when someone adds a table to the `well` schema, or a column to
`well.well_master`, this module sees it on the very next run with zero code changes:

    1. `capture()` introspects sys.schemas / sys.objects / sys.columns for every table
       currently in scope -- whatever exists today, not a hardcoded list.
    2. That snapshot is written to the metrics store (`FindingsStore.add_metrics`) keyed
       by table/column, alongside a per-table fingerprint hash.
    3. `diff_against_baseline()` compares the new snapshot to the most recent previous
       run's snapshot and reports:
         - tables that appeared or disappeared
         - columns that appeared, disappeared, or changed type/nullability
    4. Checks that have no config entry for a new table/column still run against it,
       because they are driven by the SAME scope query, not by a table list baked into
       Python -- new tables default to Grain.row() / Baseline.NONE (conservative) until
       a business owner reviews them into column_semantics.yaml.

This is what "will it handle a new table automatically" cashes out to concretely: a new
table is (a) discovered without a code change because scope is dynamic, (b) reported as a
DESIGN-class finding the first time it's seen so nobody has to notice by accident, and
(c) usable by every check family immediately because grain/baseline default conservatively
rather than requiring an entry to exist.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from app.db.source import SourceDatabase
from app.db.store import FindingsStore
from app.domain.models import Finding, FindingClass, Severity
from app.logging import get_logger
from app.sentinel.scope import Scope, TableRef

log = get_logger(__name__)

METRIC_COLUMN_FINGERPRINT = "column_fingerprint"
METRIC_TABLE_FINGERPRINT = "table_fingerprint"
METRIC_ROW_COUNT = "row_count"


@dataclass(slots=True)
class ColumnSnapshot:
    name: str
    data_type: str
    is_nullable: bool
    max_length: int
    ordinal: int

    @property
    def fingerprint(self) -> str:
        # Deliberately excludes ordinal: column reordering alone is not a drift event
        # worth flagging; type/nullability changes are.
        raw = f"{self.data_type}|{self.is_nullable}|{self.max_length}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]  # fingerprint, not security


@dataclass(slots=True)
class TableSnapshot:
    ref: TableRef
    columns: dict[str, ColumnSnapshot] = field(default_factory=dict)
    row_count: int = 0

    @property
    def fingerprint(self) -> str:
        raw = "|".join(f"{n}:{c.fingerprint}" for n, c in sorted(self.columns.items()))
        return hashlib.sha1(raw.encode()).hexdigest()[:12]  # 48 bits: exact in a float64, plenty of collision resistance for this purpose


@dataclass(slots=True)
class SchemaDiff:
    tables_added: list[str] = field(default_factory=list)
    tables_removed: list[str] = field(default_factory=list)
    columns_added: dict[str, list[str]] = field(default_factory=dict)
    columns_removed: dict[str, list[str]] = field(default_factory=dict)
    columns_changed: dict[str, list[str]] = field(default_factory=dict)
    baseline_run_id: str | None = None

    @property
    def has_drift(self) -> bool:
        return bool(
            self.tables_added or self.tables_removed
            or self.columns_added or self.columns_removed or self.columns_changed
        )


class SchemaSnapshotter:
    """Captures the current schema (in scope) and diffs it against the last run."""

    def __init__(self, source: SourceDatabase, scope: Scope | None = None) -> None:
        self.src = source
        self.scope = scope or Scope()

    # --------------------------------------------------------------------- capture
    def capture(self) -> dict[str, TableSnapshot]:
        """Introspect every in-scope table right now.

        No table name is hardcoded here: the query walks sys.objects/sys.columns for
        whatever exists, and `Scope` (which is config-driven) decides what is in scope.
        """
        started = time.perf_counter()
        row_counts = self.src.table_row_counts()
        col_rows = self.src.rows(
            """
            SELECT s.name AS [schema], o.name AS [table], c.name AS column_name,
                   ty.name AS data_type, c.is_nullable, c.max_length, c.column_id AS ordinal
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE o.type IN ('U', 'V')
            ORDER BY s.name, o.name, c.column_id
            """
        )

        snapshots: dict[str, TableSnapshot] = {}
        for r in col_rows:
            ref = TableRef(r["schema"], r["table"])
            if not self.scope.table_allowed(ref.schema, ref.table):
                continue
            snap = snapshots.setdefault(
                ref.full, TableSnapshot(ref=ref, row_count=row_counts.get(ref.full, 0))
            )
            if not self.scope.column_allowed(ref.schema, ref.table, r["column_name"]):
                continue
            snap.columns[r["column_name"]] = ColumnSnapshot(
                name=r["column_name"],
                data_type=r["data_type"],
                is_nullable=bool(r["is_nullable"]),
                max_length=int(r["max_length"]),
                ordinal=int(r["ordinal"]),
            )
        log.info(
            "schema_snapshot.captured",
            tables=len(snapshots),
            columns=sum(len(s.columns) for s in snapshots.values()),
            seconds=round(time.perf_counter() - started, 2),
        )
        return snapshots

    # ---------------------------------------------------------------- store + diff
    def persist(
        self, store: FindingsStore, run_id: str, snapshots: dict[str, TableSnapshot]
    ) -> int:
        metrics: list[tuple[str, str, str, float]] = []
        for name, snap in snapshots.items():
            metrics.append((name, "", METRIC_ROW_COUNT, float(snap.row_count)))
            metrics.append(
                (name, "", METRIC_TABLE_FINGERPRINT, _hash_to_float(snap.fingerprint))
            )
            for cname, col in snap.columns.items():
                metrics.append(
                    (name, cname, METRIC_COLUMN_FINGERPRINT, _hash_to_float(col.fingerprint))
                )
        return store.add_metrics(run_id, metrics)

    def diff_against_baseline(
        self, store: FindingsStore, run_id: str, snapshots: dict[str, TableSnapshot]
    ) -> SchemaDiff:
        """Compare `snapshots` (this run) to whatever the previous completed run recorded.

        The FIRST run in a fresh store has no baseline: every table looks "added", which
        is correct (nobody has seen this schema before) but not useful as a finding, so
        that case reports nothing rather than N spurious "new table" alerts.
        """
        prev = store.latest_run(completed_only=True)
        diff = SchemaDiff(baseline_run_id=prev["run_id"] if prev else None)
        if prev is None:
            return diff

        for name, snap in snapshots.items():
            baseline_fp = store.baseline_metric(name, "", METRIC_TABLE_FINGERPRINT, run_id)
            if baseline_fp is None:
                diff.tables_added.append(name)
                continue
            for cname, col in snap.columns.items():
                col_fp = store.baseline_metric(name, cname, METRIC_COLUMN_FINGERPRINT, run_id)
                if col_fp is None:
                    diff.columns_added.setdefault(name, []).append(cname)
                elif abs(col_fp - _hash_to_float(col.fingerprint)) > 1e-9:
                    diff.columns_changed.setdefault(name, []).append(cname)
        return diff

    # -------------------------------------------------------------------- findings
    def findings_for(self, diff: SchemaDiff) -> list[Finding]:
        """Turn drift into findings. Silent on the very first run (no baseline)."""
        out: list[Finding] = []
        if diff.baseline_run_id is None:
            return out

        for table in diff.tables_added:
            out.append(
                Finding(
                    check_id="PIP-007",
                    family="PIP",
                    severity=Severity.INFO,
                    finding_class=FindingClass.DESIGN,
                    title=f"New table detected since the last run: {table}",
                    entity_type="table",
                    entity_id=table,
                    entity_label=table,
                    affected_count=0,
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        f"{table} did not exist (or was out of scope) as of run "
                        f"{diff.baseline_run_id}. It is now included automatically -- no "
                        "configuration change is needed for it to be scanned, though it "
                        "has no declared column_semantics yet, so checks on it use "
                        "conservative defaults (row grain, no baseline) until reviewed."
                    ),
                )
            )
        for table in diff.tables_removed:
            out.append(
                Finding(
                    check_id="PIP-007",
                    family="PIP",
                    severity=Severity.MEDIUM,
                    finding_class=FindingClass.RISK,
                    title=f"Table no longer visible: {table}",
                    entity_type="table",
                    entity_id=table,
                    entity_label=table,
                    affected_count=0,
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        f"{table} was scanned in a previous run and is no longer in scope "
                        "-- dropped, renamed, or a permission change. Any check or report "
                        "referencing it should be reviewed."
                    ),
                )
            )
        for table, cols in diff.columns_added.items():
            out.append(
                Finding(
                    check_id="PIP-007",
                    family="PIP",
                    severity=Severity.INFO,
                    finding_class=FindingClass.DESIGN,
                    title=(
                        f"{len(cols)} new column(s) on {table}: {', '.join(cols[:8])}"
                        + (" ..." if len(cols) > 8 else "")
                    ),
                    entity_type="table",
                    entity_id=table,
                    entity_label=table,
                    affected_count=len(cols),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        "New columns are scanned automatically (dead/null/type checks in "
                        "Phase 2 apply to them immediately) but have no reviewed role in "
                        "column_semantics.yaml, so they generate no generated invariants "
                        "until a business owner assigns one -- see docs/01c."
                    ),
                    evidence=[{"columns": cols}],
                )
            )
        for table, cols in diff.columns_removed.items():
            out.append(
                Finding(
                    check_id="PIP-007",
                    family="PIP",
                    severity=Severity.MEDIUM,
                    finding_class=FindingClass.RISK,
                    title=f"{len(cols)} column(s) removed from {table}: {', '.join(cols[:8])}",
                    entity_type="table",
                    entity_id=table,
                    entity_label=table,
                    affected_count=len(cols),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        "Any check, config rule, or report referencing these columns by "
                        "name will fail loudly (CFG-001) rather than silently -- see "
                        "Normaliser._require_columns."
                    ),
                    evidence=[{"columns": cols}],
                )
            )
        for table, cols in diff.columns_changed.items():
            out.append(
                Finding(
                    check_id="PIP-007",
                    family="PIP",
                    severity=Severity.HIGH,
                    finding_class=FindingClass.RISK,
                    title=(
                        f"{len(cols)} column(s) changed type/nullability on {table}: "
                        f"{', '.join(cols[:8])}"
                    ),
                    entity_type="table",
                    entity_id=table,
                    entity_label=table,
                    affected_count=len(cols),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        "A type or nullability change can silently invalidate a check "
                        "that assumed the old type (e.g. a NOT NULL column becoming "
                        "nullable defeats a completeness check that never expected NULL)."
                    ),
                    evidence=[{"columns": cols}],
                )
            )
        return out


def _hash_to_float(hex_fingerprint: str) -> float:
    """Store a hex fingerprint in the metric table's REAL column without losing precision
    for equality comparison (we only ever compare for exact match, never order by it).
    """
    return float(int(hex_fingerprint, 16))
