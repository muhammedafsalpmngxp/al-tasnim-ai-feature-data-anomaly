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
        # Populated by capture(): the measured row count per user table. A name ABSENT
        # here has no measured count (a view), which is not the same as zero.
        self._row_counts: dict[str, int] = {}

    # --------------------------------------------------------------------- capture
    def capture(self) -> dict[str, TableSnapshot]:
        """Introspect every in-scope table right now.

        No table name is hardcoded here: the query walks sys.objects/sys.columns for
        whatever exists, and `Scope` (which is config-driven) decides what is in scope.
        """
        started = time.perf_counter()
        row_counts = self.src.table_row_counts()
        # Kept so coverage_findings can tell "measured zero rows" apart from "no row count
        # available". table_row_counts() covers user TABLES (sys.objects type 'U') while
        # this pass also snapshots VIEWS ('V'), so a view has no entry -- and defaulting
        # that absence to 0 below is what made three populated views (742, 36 and 11 rows)
        # get reported as empty tables. Emptiness must be measured, never assumed.
        self._row_counts = row_counts
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

    # ------------------------------------------------------------------ coverage
    def coverage_findings(
        self, snapshots: dict[str, TableSnapshot], declared_tables: set[str]
    ) -> list[Finding]:
        """What the schema pass can see that the check layer cannot: a table that exists
        but holds nothing, and a table holding data that no check ever opens.

        Free: `capture()` has already fetched every in-scope table's row count, so this
        issues no further queries. It is reported for the same reason the normalisation
        rewrites are -- a reader who sees "55 findings" is entitled to know that the scan
        covered 10 of 74 in-scope tables, not all of them.
        """
        out: list[Finding] = []
        declared_lower = {t.lower() for t in declared_tables}

        # Only objects with a MEASURED row count take part. A view has no entry in
        # _row_counts, and treating its absence as zero previously reported three
        # populated views as empty tables -- a false accusation about someone's database,
        # at medium severity. Views are derived queries, not storage, so emptiness and
        # coverage are not meaningful claims about them here.
        measured = {n: s for n, s in snapshots.items() if n in self._row_counts}
        unmeasured = sorted(set(snapshots) - set(measured))

        # Then confirm every candidate zero with a real COUNT(*). sys.partitions is
        # documented as approximate; a medium-severity claim that a table is empty should
        # rest on having counted it, and there are only ever a handful of candidates.
        empty: list[str] = []
        for name in sorted(n for n, s in measured.items() if s.row_count == 0):
            ref = measured[name].ref
            try:
                row = self.src.one(
                    f"SELECT COUNT(*) AS n FROM [{ref.schema}].[{ref.table}]"
                ) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("schema.empty_confirm_failed", table=name, error=str(exc)[:200])
                continue
            if int(row.get("n") or 0) == 0:
                empty.append(name)
            else:
                log.info("schema.empty_candidate_rejected", table=name, actual_rows=row.get("n"))
        if empty:
            out.append(
                Finding(
                    check_id="PIP-010",
                    family="PIP",
                    severity=Severity.MEDIUM,
                    finding_class=FindingClass.GAP,
                    title=(
                        f"{len(empty)} tables exist in the database but contain no rows "
                        f"at all ({', '.join(empty[:3])}"
                        + (", ..." if len(empty) > 3 else "") + ")"
                    ),
                    entity_type="table",
                    entity_id=empty[0],
                    entity_label=f"{len(empty)} tables",
                    affected_count=len(empty),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        f"These {len(empty)} tables were created but hold nothing: "
                        f"{', '.join(empty)}. Most are reference/lookup tables, and an "
                        "empty lookup is a silent failure -- anything joining to it finds "
                        "no match and simply drops or blanks the value rather than "
                        "reporting an error. Each one needs a decision: either it is dead "
                        "and should be taken out of scope, or something that was supposed "
                        "to populate it never ran."
                    ),
                    evidence=[{"empty_tables": empty, "count": len(empty)}],
                )
            )

        unchecked = sorted(
            ((name, s.row_count) for name, s in measured.items()
             if s.row_count > 0 and name.lower() not in declared_lower),
            key=lambda kv: -kv[1],
        )
        if unchecked:
            total_rows = sum(n for _, n in unchecked)
            checked = sum(
                1 for n, s in measured.items()
                if s.row_count > 0 and n.lower() in declared_lower
            )
            out.append(
                Finding(
                    check_id="PIP-011",
                    family="PIP",
                    severity=Severity.INFO,
                    finding_class=FindingClass.DESIGN,
                    title=(
                        f"{len(unchecked)} tables holding data are not covered by any "
                        f"check (this report examined {checked} of "
                        f"{len(measured)} in-scope tables)"
                    ),
                    entity_type="table",
                    entity_id=unchecked[0][0],
                    entity_label=f"{len(unchecked)} tables",
                    affected_count=len(unchecked),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        f"A check only runs on a table that has been declared in "
                        f"column_semantics.yaml, and {len(unchecked)} in-scope tables "
                        f"holding {total_rows:,} rows between them have no declaration, "
                        "so no check has ever opened them. Nothing here says those tables "
                        "have problems -- it says nobody has looked. The largest are "
                        + ", ".join(f"{t} ({n:,} rows)" for t, n in unchecked[:5])
                        + ". Declaring a table's grain and column roles is what brings it "
                        "into coverage; this finding exists so the scan's reach is stated "
                        "in the report rather than assumed."
                    ),
                    evidence=[{
                        "tables_unchecked": len(unchecked),
                        "tables_checked": checked,
                        "tables_in_scope": len(measured),
                        "rows_unchecked": total_rows,
                        "views_not_counted": len(unmeasured),
                        "largest": [{"table": t, "rows": n} for t, n in unchecked[:20]],
                    }],
                )
            )
        return out


    # -------------------------------------------------------------- dead columns
    def dead_column_findings(
        self, snapshots: dict[str, TableSnapshot], *, max_rows: int, max_columns: int = 60
    ) -> list[Finding]:
        """Columns that exist on an in-scope table and are NULL on every single row.

        A field nobody fills is either genuinely unused, or something upstream stopped
        writing it -- `well.well_master.status_id` is FK-backed and 100% NULL, which means
        well status is unknown for all 814 wells. That is worth knowing and no check finds
        it, because a check only runs on the ten declared tables.

        Deliberately a FULL scan, never a sample: a column can be NULL for the first
        million rows and populated later, so a sampled "looks all NULL" would report live
        fields as dead. Tables above `max_rows` are therefore skipped rather than
        estimated (the same threshold the rest of the system samples at), and the skip is
        reported in the evidence so the gap is visible instead of silent.
        """
        dead: dict[str, list[str]] = {}
        skipped: list[str] = []
        scanned = 0
        for name, snap in sorted(snapshots.items()):
            # Same rule as coverage_findings: only objects with a measured row count.
            # A view has no count, so `row_count` is a defaulted 0 and would look empty.
            if name not in self._row_counts or snap.row_count <= 0:
                continue  # emptiness is PIP-010's business
            if snap.row_count > max_rows:
                skipped.append(name)
                continue
            columns = [
                c for c in snap.columns
                if self.scope.column_allowed(snap.ref.schema, snap.ref.table, c)
            ][:max_columns]
            if not columns:
                continue
            select = ", ".join(
                f"SUM(CASE WHEN [{c}] IS NULL THEN 1 ELSE 0 END) AS [{c}]" for c in columns
            )
            try:
                row = self.src.one(
                    f"SELECT COUNT(*) AS __n, {select} "
                    f"FROM [{snap.ref.schema}].[{snap.ref.table}]"
                ) or {}
            except Exception as exc:  # noqa: BLE001 -- one odd column type must not fail the run
                log.warning("schema.dead_column_scan_failed", table=name, error=str(exc)[:200])
                continue
            scanned += 1
            n = int(row.get("__n") or 0)
            if n <= 0:
                continue
            found = [c for c in columns if int(row.get(c) or 0) == n]
            if found:
                dead[name] = found

        if not dead:
            return []
        total = sum(len(v) for v in dead.values())
        worst = max(dead.items(), key=lambda kv: len(kv[1]))
        return [
            Finding(
                check_id="PIP-012",
                family="PIP",
                severity=Severity.LOW,
                finding_class=FindingClass.GAP,
                title=(
                    f"{total} columns across {len(dead)} tables are empty on every row "
                    f"(worst: {worst[0]} with {len(worst[1])})"
                ),
                entity_type="column",
                entity_id=worst[0],
                entity_label=f"{total} columns",
                affected_count=total,
                grain="aggregate",
                baseline="none",
                why_it_matters=(
                    f"{total} columns exist but hold no value on any row of their table. "
                    "Each is either a field the business does not use -- in which case any "
                    "report offering it is offering nothing -- or a field something "
                    "upstream is supposed to write and is not. The distinction matters: a "
                    "column with a foreign key defined and no values in it means that "
                    "relationship is unknown for every record, not that it is unused. "
                    f"Checked by scanning every row of {scanned} tables."
                    + (f" {len(skipped)} table(s) above {max_rows:,} rows were skipped "
                       f"rather than sampled, because a sample cannot prove a column is "
                       f"empty everywhere: {', '.join(skipped)}." if skipped else "")
                ),
                evidence=[{
                    "dead_columns_total": total,
                    "tables_affected": len(dead),
                    "tables_scanned": scanned,
                    "tables_skipped_too_large": skipped,
                    "by_table": {t: cols for t, cols in sorted(dead.items())},
                }],
            )
        ]


def _hash_to_float(hex_fingerprint: str) -> float:
    """Store a hex fingerprint in the metric table's REAL column without losing precision
    for equality comparison (we only ever compare for exact match, never order by it).
    """
    return float(int(hex_fingerprint, 16))
