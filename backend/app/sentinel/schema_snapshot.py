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

from app.db.source import SourceDatabase, quote_ident as _q
from app.db.store import FindingsStore
from app.domain.models import Finding, FindingClass, Severity
from app.logging import get_logger
from app.sentinel.normalise.spec import NormalisationSpec
from app.sentinel.scope import Scope, TableRef

log = get_logger(__name__)

METRIC_COLUMN_FINGERPRINT = "column_fingerprint"
METRIC_TABLE_FINGERPRINT = "table_fingerprint"
METRIC_ROW_COUNT = "row_count"

# COUNT(DISTINCT ...) is invalid on these in SQL Server -- found live on
# dbo.mapping_master, where the grain probe crashed with error 8117.
_UNCOUNTABLE_TYPES = {"text", "ntext", "image", "xml", "geography", "geometry"}


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


@dataclass(slots=True, frozen=True)
class ForeignKeyRef:
    """One declared FK column and where it points. `nullable` matters on its own: an
    enforced FK makes an ORPHAN impossible, so the only checkable defect on a declared
    relationship is a row that is simply UNLINKED -- which is possible exactly when the
    referencing column allows NULL. Measured: 41 FKs here, 0 disabled, 0 untrusted, 36
    nullable columns."""

    column: str
    target_table: str
    target_column: str
    nullable: bool


@dataclass(slots=True, frozen=True)
class GrainCandidate:
    """One candidate key and its MEASURED rows-per-distinct-value.

    A candidate, never a conclusion. Live measurement showed why the distinction matters:
    `well.well_master` is one row per well, but probing `well_type_id` (a dimension with
    ~12 values) returns 67.8 rows per value -- which is low cardinality, not duplication.
    Reporting that as "the grain" would be a false positive on a clean table, and picking
    the other extreme (closest to unique) hides the real 206x duplication on
    `dbo.activity_task_plan`'s business key. No naming rule separates a dimension from a
    key, so the measurement is published and the judgement is left to config or a human.
    """

    columns: tuple[str, ...]
    rows_per_value: float

    @property
    def is_unique(self) -> bool:
        return self.rows_per_value <= 1.0

    def describe(self) -> str:
        return f"{', '.join(self.columns)} -> {self.rows_per_value:.2f}"


@dataclass(slots=True)
class TableSnapshot:
    ref: TableRef
    columns: dict[str, ColumnSnapshot] = field(default_factory=dict)
    row_count: int = 0
    # Key metadata, for the agent's knowledge base and for graincheck. Empty for a view.
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKeyRef, ...] = ()
    # The REVIEWED grain from column_semantics.yaml, when the table has one. Authoritative:
    # it is already measured and human-confirmed, so it is never overridden by a probe.
    declared_grain: str | None = None
    declared_rows_per_key: float | None = None
    # Measured candidates for a table with NO declared grain. Ordered closest-to-unique
    # first. Explicitly unconfirmed -- a compiled check on such a table is registered as
    # `needs_review` until someone states which of these is the entity key.
    grain_candidates: tuple[GrainCandidate, ...] = ()

    @property
    def grain_is_confirmed(self) -> bool:
        return self.declared_grain is not None

    @property
    def fingerprint(self) -> str:
        """Columns only -- the DRIFT fingerprint, unchanged.

        Deliberately NOT extended with the key metadata added above. `diff_against_baseline`
        compares this against the value stored by the previous run, so folding keys in would
        make every one of the 74 tables report as drifted exactly once after deploy: 74
        spurious PIP-007 findings, and a real schema change hidden among them. Use
        `compile_fingerprint` for anything that needs to notice a key change.
        """
        raw = "|".join(f"{n}:{c.fingerprint}" for n, c in sorted(self.columns.items()))
        return hashlib.sha1(raw.encode()).hexdigest()[:12]  # 48 bits: exact in a float64, plenty of collision resistance for this purpose

    @property
    def compile_fingerprint(self) -> str:
        """Everything a generated check could depend on: columns, PK, FKs, and the
        measured grain.

        Separate from `fingerprint` because the two answer different questions. Drift asks
        "did the structure change, should a human be told?"; this asks "could a check
        compiled against this table still be valid?" -- and a dropped FK or a grain that
        moved from 1.0 to 3.03 rows per key invalidates a compiled check while changing
        nothing a column-only hash would see.
        """
        parts = [
            "|".join(f"{n}:{c.fingerprint}" for n, c in sorted(self.columns.items())),
            "pk=" + ",".join(sorted(self.primary_key)),
            "fk=" + ",".join(
                f"{f.column}->{f.target_table}.{f.target_column}:{int(f.nullable)}"
                for f in sorted(self.foreign_keys, key=lambda x: (x.column, x.target_table))
            ),
            f"grain={self.declared_grain or ''}:{self.declared_rows_per_key or ''}",
            "cand=" + ";".join(c.describe() for c in self.grain_candidates),
        ]
        return hashlib.sha1("||".join(parts).encode()).hexdigest()[:16]


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
        self._attach_keys(snapshots)
        log.info(
            "schema_snapshot.captured",
            tables=len(snapshots),
            columns=sum(len(s.columns) for s in snapshots.values()),
            primary_keys=sum(1 for s in snapshots.values() if s.primary_key),
            foreign_keys=sum(len(s.foreign_keys) for s in snapshots.values()),
            seconds=round(time.perf_counter() - started, 2),
        )
        return snapshots

    # ------------------------------------------------------------------- key metadata
    def _attach_keys(self, snapshots: dict[str, TableSnapshot]) -> None:
        """Fill in primary_key / foreign_keys from the catalogue. Two metadata queries,
        no table scans.

        A PK column that Scope hides is still recorded: the point of knowing the key is to
        reason about GRAIN, and a hidden column still determines how many rows there are
        per entity. Nothing here is rendered into a prompt directly -- `introspect.py`
        applies the PII/visibility filter when it writes the knowledge files.
        """
        try:
            pk_rows = self.src.rows(
                """
                SELECT s.name AS [schema], t.name AS [table], c.name AS column_name,
                       ic.key_ordinal
                FROM sys.indexes i
                JOIN sys.index_columns ic
                     ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                JOIN sys.columns c
                     ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                JOIN sys.tables t ON t.object_id = i.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE i.is_primary_key = 1
                ORDER BY s.name, t.name, ic.key_ordinal
                """
            )
            fk_rows = self.src.rows(
                """
                SELECT s.name AS [schema], t.name AS [table], c.name AS column_name,
                       rs.name AS target_schema, rt.name AS target_table,
                       rc.name AS target_column, c.is_nullable
                FROM sys.foreign_key_columns fkc
                JOIN sys.tables t ON t.object_id = fkc.parent_object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                JOIN sys.columns c
                     ON c.object_id = fkc.parent_object_id
                    AND c.column_id = fkc.parent_column_id
                JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
                JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
                JOIN sys.columns rc
                     ON rc.object_id = fkc.referenced_object_id
                    AND rc.column_id = fkc.referenced_column_id
                ORDER BY s.name, t.name, c.name
                """
            )
        except Exception as exc:  # noqa: BLE001 -- metadata is additive; a run must not fail for it
            log.warning("schema_snapshot.key_metadata_failed", error=str(exc)[:300])
            return

        pks: dict[str, list[str]] = {}
        for r in pk_rows:
            pks.setdefault(f"{r['schema']}.{r['table']}", []).append(r["column_name"])
        fks: dict[str, list[ForeignKeyRef]] = {}
        for r in fk_rows:
            fks.setdefault(f"{r['schema']}.{r['table']}", []).append(
                ForeignKeyRef(
                    column=r["column_name"],
                    target_table=f"{r['target_schema']}.{r['target_table']}",
                    target_column=r["target_column"],
                    nullable=bool(r["is_nullable"]),
                )
            )
        for name, snap in snapshots.items():
            snap.primary_key = tuple(pks.get(name, ()))
            snap.foreign_keys = tuple(fks.get(name, ()))

    def measure_grain(
        self,
        snapshots: dict[str, TableSnapshot],
        *,
        spec: NormalisationSpec | None = None,
        min_rows: int = 100,
        max_candidates: int = 6,
    ) -> int:
        """Measure rows-per-key, reporting the WORST duplication found, never the first.

        A wrong grain is the recurring defect in this project, so this is the most
        load-bearing line in the knowledge base -- and the first version of it was
        DANGEROUSLY wrong, in a way only live data showed:

          * `well.task_daily`: probing the first `*id` column found the surrogate `id`,
            measured 1.0, and declared the table CLEAN. Its real grain is 3.03 rows per
            (well_id, task_code) -- the exact table this project's documented grain bug
            came from. A false "clean" is worse than no measurement, because it tells the
            agent the most dangerous table in the database is safe.
          * `dbo.activity_task_plan`: skipped entirely because it has a single-column PK,
            yet it repeats (well_id, task_code, schedule_id) 206x-2,718x. "Has a PK" is
            not "one row per entity" when the PK is a surrogate.
          * `dbo.mapping_master`: the probe CRASHED -- COUNT(DISTINCT ...) is invalid on a
            `text` column.

        So now: a DECLARED grain is read from config rather than guessed (it is already
        measured and human-reviewed there); otherwise several candidate keys are probed and
        the worst ratio wins; uncountable types are skipped; and a single-column PK marks
        the PK itself as unique WITHOUT concluding anything about the business key.
        """
        declared = 0
        probes = 0
        for name, snap in sorted(snapshots.items()):
            if name not in self._row_counts or snap.row_count < min_rows:
                continue

            # 1. A reviewed grain is authoritative -- never re-guessed, only measured.
            sem = spec.semantics_for(name) if spec else None
            keys = tuple(getattr(getattr(sem, "grain", None), "keys", ()) or ()) if sem else ()
            if keys:
                snap.declared_grain = ", ".join(keys)
                ratio = self._ratio_for(snap, list(keys))
                if ratio is not None:
                    probes += 1
                    snap.declared_rows_per_key = round(ratio, 2)
                declared += 1
                continue

            # 2. No declared grain: probe candidates and publish them ALL, ordered
            # closest-to-unique first. Deliberately no winner is chosen -- see
            # GrainCandidate for the two live cases that make any single rule wrong.
            if snap.row_count > self.scope.large_table_row_limit:
                # A multi-column COUNT(DISTINCT ...) over 17.9M rows took 10.7s live.
                # Skipped rather than paid for on every introspect, and the skip is
                # visible because grain_candidates stays empty.
                log.info("schema_snapshot.grain_probe_skipped_large", table=name,
                         rows=snap.row_count)
                continue
            countable = [
                c for c, col in snap.columns.items()
                if col.data_type.lower() not in _UNCOUNTABLE_TYPES
            ]
            combos: list[list[str]] = []
            if snap.primary_key:
                combos.append(list(snap.primary_key))
            # `code` alongside `id`: found live on wbs.WBS_master, which has no PK and no
            # `*id`-suffixed column at all, only WBS_Code/Activity_code/Cluster_code/
            # Plant_Code -- an `endswith("id")`-only rule missed every one of them and
            # this table (which is exactly the kind the agent most needs to reach) would
            # have gone into the knowledge base with zero grain candidates.
            key_cols = [c for c in countable if c.lower().endswith(("id", "code"))]
            if len(key_cols) > 1:
                combos.append(key_cols[:4])
                combos.extend([key_cols[i], key_cols[i + 1]] for i in range(len(key_cols) - 1))
            combos.extend([c] for c in key_cols)
            seen: set[tuple[str, ...]] = set()
            found: list[GrainCandidate] = []
            for combo in combos:
                key = tuple(combo)
                if not combo or key in seen:
                    continue
                seen.add(key)
                if len(found) >= max_candidates:
                    break
                ratio = self._ratio_for(snap, combo)
                if ratio is None:
                    continue
                probes += 1
                found.append(GrainCandidate(columns=key, rows_per_value=round(ratio, 2)))
            snap.grain_candidates = tuple(
                sorted(found, key=lambda c: c.rows_per_value)
            )

        log.info(
            "schema_snapshot.grain_measured",
            declared_from_config=declared,
            candidates_measured=sum(len(s.grain_candidates) for s in snapshots.values()),
            probes_run=probes,
        )
        return probes

    def _ratio_for(self, snap: TableSnapshot, columns: list[str]) -> float | None:
        """rows / distinct(columns) for one candidate key. None if it cannot be measured."""
        if not columns:
            return None
        # COUNT(DISTINCT a, b) is not valid T-SQL, so a composite is counted over its
        # concatenation. ISNULL guards a NULL member collapsing the whole key to NULL,
        # which would silently under-count distinct values and overstate duplication.
        if len(columns) == 1:
            expr = _q(columns[0])
        else:
            expr = " + '|' + ".join(
                f"ISNULL(CAST({_q(c)} AS nvarchar(200)), '')" for c in columns
            )
        try:
            row = self.src.one(
                f"SELECT COUNT(*) AS n, COUNT(DISTINCT {expr}) AS d "
                f"FROM {_q(snap.ref.schema)}.{_q(snap.ref.table)}"
            ) or {}
        except Exception as exc:  # noqa: BLE001 -- one odd type must not stop the sweep
            log.warning(
                "schema_snapshot.grain_probe_failed",
                table=snap.ref.full, key=",".join(columns), error=str(exc)[:160],
            )
            return None
        n, d = int(row.get("n") or 0), int(row.get("d") or 0)
        return (n / d) if d else None

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
