"""Layer 0 — normalisation.

Produces, for each configured table, a `NormalisedSource`: a SELECT that downstream checks
use in place of the raw table. Because the source account is read-only we cannot create
views, so the cleaned definition is SQL text that checks embed as a subquery.

Two principles:

1.  Every rewrite is measured and reported. "98,771 rows had to be removed before your data
    could be analysed" is a finding, not a silent fix.
2.  Nothing is guessed. The rules come from config/normalisation.yaml, and a rule naming a
    column that does not exist is an error, not a skip.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.db.source import SourceDatabase
from app.domain.models import (
    Finding,
    FindingClass,
    Grain,
    GrainKind,
    NormalisationAction,
    NormalisationKind,
    Severity,
)
from app.logging import get_logger
from app.sentinel.normalise.spec import (
    DedupRule,
    NormalisationSpec,
    SnapshotRule,
    load_spec,
)
from app.sentinel.scope import Scope, TableRef

log = get_logger(__name__)


def _q(name: str) -> str:
    """Bracket-quote an identifier."""
    return f"[{name.replace(']', ']]')}]"


@dataclass(slots=True)
class NormalisedSource:
    """A table as the checks should see it."""

    ref: TableRef
    sql: str
    grain: Grain
    sampled: bool = False
    rows_raw: int = 0
    rows_effective: int = 0
    transforms: list[str] = field(default_factory=list)
    pinned_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.ref.full

    def subquery(self, alias: str = "src") -> str:
        """The normalised rows, UNCOLLAPSED -- history intact. A daily log's one-row-per-
        day is the data; do not use this alone where the declared grain matters (see
        `at_grain` below).
        """
        return f"(\n{self.sql}\n) AS {alias}"

    def at_grain(self, alias: str = "src") -> str:
        """The source collapsed to its declared grain (config/column_semantics.yaml).

        This is what stops the per-row/per-task mistake recurring (docs/01c Section 2:
        counting `well.task_daily` per row instead of per task overstated one finding
        15x). Every check that needs "one row per subject" must go through this, not
        `subquery()` -- `subquery()` alone is history; this is the current state.
        """
        if self.grain.kind in (GrainKind.ROW, GrainKind.AGGREGATE):
            return self.subquery(alias)
        keys = ", ".join(f"[{k}]" for k in self.grain.keys)
        return (
            f"(\n  SELECT * FROM (\n"
            f"    SELECT g.*, ROW_NUMBER() OVER (PARTITION BY {keys} "
            f"ORDER BY {self.grain.order_by}) AS __grain_rn\n"
            f"    FROM (\n{self.sql}\n) AS g\n"
            f"  ) AS __gr WHERE __gr.__grain_rn = 1\n) AS {alias}"
        )

    @property
    def rows_removed(self) -> int:
        return max(0, self.rows_raw - self.rows_effective)


class Normaliser:
    """Builds normalised sources and the findings that describe the rewrites."""

    def __init__(
        self,
        source: SourceDatabase,
        spec: NormalisationSpec | None = None,
        scope: Scope | None = None,
        as_of: str | None = None,
    ) -> None:
        self.src = source
        self.spec = spec or load_spec()
        self.scope = scope or Scope()
        self.as_of = as_of
        self.actions: list[NormalisationAction] = []
        self.findings: list[Finding] = []
        self.sources: dict[str, NormalisedSource] = {}
        self._columns: dict[str, list[dict[str, Any]]] = {}
        self._row_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ metadata
    def _load_row_counts(self) -> None:
        if not self._row_counts:
            self._row_counts = self.src.table_row_counts()

    def columns_of(self, ref: TableRef) -> list[dict[str, Any]]:
        if ref.full not in self._columns:
            self._columns[ref.full] = self.src.rows(
                """
                SELECT c.name AS column_name, ty.name AS data_type, c.column_id AS ord,
                       c.is_nullable, c.max_length, c.is_identity
                FROM sys.columns c
                JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                WHERE c.object_id = OBJECT_ID(?)
                ORDER BY c.column_id
                """,
                (ref.full,),
            )
        return self._columns[ref.full]

    def _require_columns(self, ref: TableRef, names: list[str], context: str) -> list[str]:
        """Validate that configured columns exist. Missing ones are reported, not ignored."""
        actual = {c["column_name"].lower(): c["column_name"] for c in self.columns_of(ref)}
        resolved, missing = [], []
        for n in names:
            real = actual.get(n.lower())
            (resolved.append(real) if real else missing.append(n))
        if missing:
            log.error(
                "normalise.config_columns_missing",
                table=ref.full, context=context, missing=missing,
            )
            self.findings.append(
                Finding(
                    check_id="CFG-001",
                    family="CFG",
                    severity=Severity.HIGH,
                    finding_class=FindingClass.RISK,
                    title=f"Normalisation config names columns that do not exist on {ref.full}",
                    entity_type="table",
                    entity_id=ref.full,
                    entity_label=ref.full,
                    affected_count=len(missing),
                    grain="aggregate",
                    baseline="none",
                    why_it_matters=(
                        f"The {context} rule for {ref.full} references {missing}, which are "
                        "not present. The rule did not run, so any check depending on it is "
                        "reading un-normalised data."
                    ),
                    evidence=[{"context": context, "missing_columns": missing}],
                )
            )
        return resolved

    # -------------------------------------------------------------------- helpers
    def _select_list(self, ref: TableRef) -> str:
        """Explicit column list with placeholder/blank/sentinel wrappers applied.

        Order matters: blank is tested BEFORE any date cast, because in SQL Server
        TRY_CAST('' AS date) succeeds and returns 1900-01-01 (docs/01b §1.4).
        """
        placeholder_cols = {
            c.lower() for c in self._require_columns(
                ref, self.spec.placeholder_columns(ref.full), "placeholder_dates"
            )
        }
        blank_cols = {
            c.lower() for c in self._require_columns(
                ref, self.spec.blank_columns(ref.full), "blank_to_null"
            )
        }
        sentinel_cols = {
            c.lower() for c in self._require_columns(
                ref, self.spec.sentinel_columns(ref.full), "sentinels"
            )
        }
        placeholder_values = (
            self.spec.placeholder_dates.values if self.spec.placeholder_dates else []
        )
        sentinel_values = self.spec.sentinels.values if self.spec.sentinels else []

        parts: list[str] = []
        for col in self.columns_of(ref):
            name = col["column_name"]
            lower = name.lower()
            # Always qualify with the base alias: the snapshot pin joins a derived table
            # that repeats the partition columns, which makes a bare name ambiguous.
            expr = f"t.{_q(name)}"
            if lower in blank_cols:
                expr = f"NULLIF(LTRIM(RTRIM({expr})), '')"
            if lower in sentinel_cols and sentinel_values:
                lits = ", ".join(f"'{v.replace(chr(39), chr(39) * 2)}'" for v in sentinel_values)
                expr = (
                    f"CASE WHEN UPPER(LTRIM(RTRIM(CAST({expr} AS nvarchar(200))))) "
                    f"IN ({lits}) THEN NULL ELSE {expr} END"
                )
            if lower in placeholder_cols and placeholder_values:
                for v in placeholder_values:
                    expr = f"NULLIF({expr}, '{v}')"
            parts.append(
                expr if expr == f"t.{_q(name)}" else f"{expr} AS {_q(name)}"
            )
        return ",\n       ".join(parts)

    # ------------------------------------------------------------ snapshot pinning
    def _pin_snapshot(self, ref: TableRef, rule: SnapshotRule) -> tuple[str, dict[str, Any]]:
        """Restrict to the latest snapshot per partition.

        Pinning by DATE is not enough: 2026-07-29 holds two snapshots, so a date pin
        double-counts every weightage sum.
        """
        ts = self._require_columns(ref, [rule.timestamp_column], "snapshots")
        parts = self._require_columns(ref, rule.partition_by, "snapshots")
        if not ts:
            return "", {}
        ts_col = _q(ts[0])
        rows = self.src.rows(
            f"""
            SELECT {', '.join(_q(p) + ' AS ' + _q(p) for p in parts) + ',' if parts else ''}
                   CONVERT(varchar(30), MAX({ts_col}), 121) AS pinned_at,
                   COUNT(*) AS rows_in_table,
                   COUNT(DISTINCT {ts_col}) AS snapshots
            FROM {ref.bracketed}
            {'GROUP BY ' + ', '.join(_q(p) for p in parts) if parts else ''}
            """
        )
        pinned = {
            "table": ref.full,
            "timestamp_column": ts[0],
            "partition_by": parts,
            "partitions": [
                {
                    **{p: str(r.get(p)) for p in parts},
                    "pinned_at": r["pinned_at"],
                    "snapshots_available": r["snapshots"],
                }
                for r in rows
            ],
        }
        if parts:
            join_on = " AND ".join(f"t.{_q(p)} = m.{_q(p)}" for p in parts)
            group_by = ", ".join(_q(p) for p in parts)
            predicate = (
                f"INNER JOIN (SELECT {group_by}, MAX({ts_col}) AS __pin "
                f"FROM {ref.bracketed} GROUP BY {group_by}) AS m "
                f"ON {join_on} AND t.{ts_col} = m.__pin"
            )
        else:
            predicate = (
                f"INNER JOIN (SELECT MAX({ts_col}) AS __pin FROM {ref.bracketed}) AS m "
                f"ON t.{ts_col} = m.__pin"
            )

        total = sum(int(r["rows_in_table"]) for r in rows)
        kept = int(
            self.src.scalar(
                f"""
                WITH pinned AS (
                    SELECT t.* FROM {ref.bracketed} AS t {predicate}
                )
                SELECT COUNT(*) FROM pinned
                """
            )
            or 0
        )
        self._record(
            NormalisationAction(
                kind=NormalisationKind.SNAPSHOT_PIN,
                target=ref.full,
                rows_affected=total - kept,
                rows_total=total,
                check_id=rule.check_id,
                detail={
                    "snapshots_available": max(
                        (int(r["snapshots"]) for r in rows), default=0
                    ),
                    "pinned": pinned["partitions"],
                    "note": rule.note,
                },
            ),
            title=f"{ref.full} is a snapshot table — pinned to the latest snapshot per partition",
            finding_class=FindingClass.DESIGN,
            severity=Severity.INFO,
            why=(
                f"{ref.full} appends a full copy on every sync: "
                f"{max((int(r['snapshots']) for r in rows), default=0)} snapshots over "
                f"{total:,} rows. Reading it unpinned double-counts every aggregate — WBS "
                "weightage sums come out roughly 2x. This is history working as designed, "
                "not corruption: the row key (partition + timestamp) is unique. Pinning by "
                "date would still double-count, because one day holds two snapshots."
            ),
        )
        return predicate, pinned

    # ---------------------------------------------------------------------- dedup
    def _measure_dedup(self, ref: TableRef, rule: DedupRule) -> dict[str, int]:
        keys = self._require_columns(ref, rule.keys, "dedup")
        if len(keys) != len(rule.keys):
            return {}
        key_list = ", ".join(_q(k) for k in keys)

        # Row-unique columns must NOT enter the payload comparison. An IDENTITY column
        # differs on every row, so including it makes every duplicate group look like it
        # "disagrees" — which reported 22,630 conflicts where only 3,085 exist. Excluded:
        #   - SQL Server IDENTITY columns
        #   - anything named in the rule's ORDER BY (that is the tie-breaker, by definition)
        #   - columns declared role: identity in column_semantics.yaml
        sem = self.spec.semantics_for(ref.full)
        order_tokens = {
            tok.strip("[], ").lower()
            for tok in rule.order_by.replace(",", " ").split()
            if tok.upper() not in ("ASC", "DESC")
        }
        skip = {k.lower() for k in keys} | order_tokens

        if rule.compare_columns:
            # Explicit business decision about what constitutes a conflict.
            payload_cols = self._require_columns(ref, rule.compare_columns, "dedup.compare_columns")
        else:
            # Derived from column_semantics: only columns a person actually writes and
            # that are not recomputed downstream.
            NOISY = {"identity", "derived", "unused", "machine_plan", "display_only"}
            payload_cols = []
            for c in self.columns_of(ref):
                name = c["column_name"]
                if name.lower() in skip or c.get("is_identity"):
                    continue
                if c["data_type"] in ("text", "ntext", "image", "xml", "geography", "geometry"):
                    continue
                if c["data_type"] in ("varchar", "nvarchar") and c["max_length"] == -1:
                    continue
                col_sem = sem.column(name) if sem else None
                if col_sem is not None:
                    if col_sem.role in NOISY or not col_sem.authoritative:
                        continue
                payload_cols.append(name)
            payload_cols = payload_cols[:12]
        payload = (
            "CONCAT("
            + ", '|', ".join(
                f"COALESCE(CAST({_q(c)} AS nvarchar(100)), '<NULL>')" for c in payload_cols
            )
            + ")"
            if payload_cols
            else "''"
        )
        row = self.src.one(
            f"""
            WITH g AS (
                SELECT {key_list}, COUNT(*) AS n,
                       COUNT(DISTINCT {payload}) AS distinct_payloads
                FROM {ref.bracketed}
                GROUP BY {key_list}
                HAVING COUNT(*) > 1
            )
            SELECT COUNT(*)                                    AS dup_groups,
                   ISNULL(SUM(n), 0)                           AS rows_in_dup_groups,
                   ISNULL(SUM(n) - COUNT(*), 0)                AS excess_rows,
                   ISNULL(MAX(n), 0)                           AS worst_group,
                   SUM(CASE WHEN distinct_payloads > 1 THEN 1 ELSE 0 END) AS groups_disagreeing,
                   SUM(CASE WHEN distinct_payloads = 1 THEN 1 ELSE 0 END) AS groups_identical
            FROM g
            """
        ) or {}
        out = {k: int(v or 0) for k, v in row.items()}
        out["__payload_columns"] = payload_cols  # type: ignore[assignment]
        return out

    def _dedup_predicate(self, ref: TableRef, rule: DedupRule) -> str:
        keys = ", ".join(_q(k) for k in rule.keys)
        return f"ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {rule.order_by}) AS __rn"

    def _report_dedup(self, ref: TableRef, rule: DedupRule, stats: dict[str, int]) -> None:
        excess = stats.get("excess_rows", 0)
        total = int(self._row_counts.get(ref.full, 0))
        disagreeing = stats.get("groups_disagreeing", 0)
        identical = stats.get("groups_identical", 0)

        if rule.mode == "exact_only":
            why = (
                f"{ref.full} has {stats.get('dup_groups', 0):,} duplicate groups on "
                f"({', '.join(rule.keys)}) holding {excess:,} excess rows. "
                f"{identical:,} groups are byte-identical and can be collapsed "
                f"automatically; **{disagreeing:,} groups hold different values** — two "
                "different truths for the same key, which needs a person, not a dedup rule."
            )
        else:
            why = (
                f"{ref.full} has {stats.get('dup_groups', 0):,} duplicate groups on "
                f"({', '.join(rule.keys)}) holding {excess:,} excess rows of {total:,}. "
                "Any statistic computed before deduplication is drawn from an inflated "
                "population."
            )
        self._record(
            NormalisationAction(
                kind=NormalisationKind.DEDUP,
                target=ref.full,
                rows_affected=excess,
                rows_total=total,
                check_id=rule.check_id,
                detail={
                    **{k: v for k, v in stats.items() if not k.startswith("__")},
                    "keys": rule.keys,
                    "mode": rule.mode,
                    "compared_columns": stats.get("__payload_columns", []),
                    "note": rule.note,
                },
            ),
            title=(
                f"{excess:,} duplicate rows removed from {ref.full} "
                f"before analysis ({', '.join(rule.keys)})"
            ),
            finding_class=FindingClass.DEFECT if excess else FindingClass.DESIGN,
            severity=rule.severity if excess else Severity.INFO,
            why=why,
            evidence=[
                {
                    "keys": rule.keys,
                    "duplicate_groups": stats.get("dup_groups", 0),
                    "excess_rows": excess,
                    "worst_group_size": stats.get("worst_group", 0),
                    "groups_identical_safe_to_dedup": identical,
                    "groups_disagreeing_need_human": disagreeing,
                }
            ],
        )
        # A disagreeing duplicate is a separate, higher-value finding: it cannot be
        # resolved automatically, because picking a row discards a value someone recorded.
        if disagreeing > 0:
            self.findings.append(
                Finding(
                    check_id="CON-011",
                    family="CON",
                    severity=Severity.HIGH,
                    finding_class=FindingClass.DEFECT,
                    title=(
                        f"{disagreeing:,} duplicate groups in {ref.full} hold conflicting "
                        "values for the same key"
                    ),
                    entity_type="table",
                    entity_id=ref.full,
                    entity_label=ref.full,
                    affected_count=disagreeing,
                    grain=f"group by ({', '.join(rule.keys)})",
                    baseline="none",
                    why_it_matters=(
                        "These cannot be de-duplicated automatically: the rows disagree, so "
                        "picking one silently discards a value someone recorded. Each needs "
                        "a decision about which value is correct."
                    ),
                    evidence=[
                        {
                            "keys": rule.keys,
                            "groups_disagreeing": disagreeing,
                            "groups_identical": identical,
                            "compared_columns": stats.get("__payload_columns", []),
                        }
                    ],
                )
            )

    # ------------------------------------------------------------------- phantoms
    def _phantom_predicate(self, ref: TableRef) -> list[str]:
        preds: list[str] = []
        for rule in self.spec.phantom_keys_for(ref.full):
            cols = self._require_columns(ref, [rule.column], "phantom_keys")
            if not cols or not rule.values:
                continue
            lits = ", ".join(
                str(v) if isinstance(v, (int, float)) else f"'{v}'" for v in rule.values
            )
            col = _q(cols[0])
            affected = int(
                self.src.scalar(
                    f"SELECT COUNT(*) FROM {ref.bracketed} WHERE {col} IN ({lits})"
                )
                or 0
            )
            if rule.excludes_rows:
                preds.append(f"(t.{_q(cols[0])} IS NULL OR t.{_q(cols[0])} NOT IN ({lits}))")
            if affected:
                self.findings.append(
                    Finding(
                        check_id=rule.check_id,
                        family="VAL",
                        severity=rule.severity,
                        finding_class=(
                            FindingClass.DEFECT if rule.excludes_rows else FindingClass.GAP
                        ),
                        title=(
                            f"{affected:,} rows in {ref.full} carry "
                            f"{cols[0]} = {rule.values} — "
                            + ("excluded from analysis" if rule.excludes_rows
                               else "kept, but not attributable to a well")
                        ),
                        entity_type="column",
                        entity_id=f"{ref.full}.{cols[0]}",
                        entity_label=f"{ref.full}.{cols[0]}",
                        affected_count=affected,
                        grain="row",
                        baseline="none",
                        why_it_matters=(
                            rule.note
                            or f"{affected:,} rows carry a key value that is not a real entity."
                        ),
                        evidence=[
                            {
                                "column": cols[0],
                                "values": rule.values,
                                "rows": affected,
                                "mode": rule.mode,
                                "rows_removed": affected if rule.excludes_rows else 0,
                            }
                        ],
                    )
                )
        return preds

    # -------------------------------------------------------------------- recording
    def _record(
        self,
        action: NormalisationAction,
        *,
        title: str,
        finding_class: FindingClass,
        severity: Severity,
        why: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        self.actions.append(action)
        self.findings.append(
            Finding(
                check_id=action.check_id or "NRM-000",
                family=(action.check_id or "NRM").split("-")[0],
                severity=severity,
                finding_class=finding_class,
                title=title,
                entity_type="table",
                entity_id=action.target,
                entity_label=action.target,
                affected_count=action.rows_affected,
                grain="aggregate",
                baseline="none",
                why_it_matters=why,
                evidence=evidence or [action.detail],
            )
        )

    # --------------------------------------------------------------------- build
    def build(self, ref: TableRef) -> NormalisedSource:
        """Assemble the normalised SELECT for one table."""
        self._load_row_counts()
        rows_raw = int(self._row_counts.get(ref.full, 0))
        from_expr, sampled = self.scope.source_expression(ref, rows_raw)
        transforms: list[str] = []
        pinned: dict[str, Any] = {}
        joins = ""
        where: list[str] = []
        window = ""

        snap = self.spec.snapshot_for(ref.full)
        if snap:
            joins, pinned = self._pin_snapshot(ref, snap)
            if joins:
                transforms.append(f"snapshot pinned on {snap.timestamp_column}")

        dedup = self.spec.dedup_for(ref.full)
        if dedup:
            stats = self._measure_dedup(ref, dedup)
            if stats:
                self._report_dedup(ref, dedup, stats)
            if dedup.mode == "keep_latest":
                window = self._dedup_predicate(ref, dedup)
                transforms.append(f"deduplicated on ({', '.join(dedup.keys)})")

        where.extend(self._phantom_predicate(ref))
        if where:
            transforms.append("phantom key values excluded")
        elif self.spec.phantom_keys_for(ref.full):
            transforms.append("phantom key values flagged (kept)")

        if self.spec.placeholder_columns(ref.full):
            transforms.append("1900-01-01 placeholders nulled")
        if self.spec.blank_columns(ref.full):
            transforms.append("blank strings nulled")
        if self.spec.sentinel_columns(ref.full):
            transforms.append("sentinel strings nulled")

        select_list = self._select_list(ref)
        inner_select = f"{select_list},\n       {window}" if window else select_list
        inner = (
            f"SELECT {inner_select}\n"
            f"FROM {from_expr} AS t\n"
            + (f"{joins}\n" if joins else "")
            + (f"WHERE {' AND '.join(where)}\n" if where else "")
        )
        if window:
            # Explicit column list -- NOT `SELECT *` -- so the internal `__rn` dedup
            # marker never leaks into what a check sees. A check that itself needs a
            # row-number (e.g. collapsing to "latest per well" for a different grain
            # than the table's declared one) would otherwise collide with this name and
            # SQL Server would refuse the query outright ("column '__rn' specified
            # multiple times") -- found by CON-007/CON-009 doing exactly that.
            real_cols = ", ".join(_q(c["column_name"]) for c in self.columns_of(ref))
            sql = (
                f"SELECT {real_cols} FROM (\n{inner}) AS __d\nWHERE __d.__rn = 1"
            )
        else:
            sql = inner.rstrip()

        rows_effective = rows_raw
        try:
            rows_effective = int(
                self.src.scalar(f"SELECT COUNT_BIG(*) FROM (\n{sql}\n) AS __c") or 0
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("normalise.count_failed", table=ref.full, error=str(exc)[:200])

        source = NormalisedSource(
            ref=ref,
            sql=sql,
            grain=self.spec.grain_for(ref.full),
            sampled=sampled,
            rows_raw=rows_raw,
            rows_effective=rows_effective,
            transforms=transforms,
            pinned_snapshot=pinned,
        )
        self.sources[ref.full] = source
        log.info(
            "normalise.built",
            table=ref.full,
            rows_raw=rows_raw,
            rows_effective=rows_effective,
            removed=source.rows_removed,
            grain=source.grain.describe(),
            sampled=sampled,
            transforms=transforms,
        )
        return source

    def build_all(self, tables: list[TableRef] | None = None) -> dict[str, NormalisedSource]:
        """Normalise every table the spec touches (or an explicit list)."""
        self._load_row_counts()
        if tables is None:
            targets: list[TableRef] = []
            for name in sorted(self.spec.tables_touched | set(self.spec.tables)):
                ref = TableRef.parse(name)
                if not self.scope.table_allowed(ref.schema, ref.table):
                    log.warning(
                        "normalise.table_out_of_scope",
                        table=name,
                        reason=self.scope.exclusion_reason(ref.schema, ref.table),
                    )
                    continue
                if self._row_counts.get(ref.full, 0) == 0:
                    log.info("normalise.table_empty_skipped", table=name)
                    continue
                targets.append(ref)
        else:
            targets = tables

        started = time.perf_counter()
        for ref in targets:
            try:
                self.build(ref)
            except Exception as exc:  # noqa: BLE001
                log.error("normalise.failed", table=ref.full, error=str(exc)[:300])
                self.findings.append(
                    Finding(
                        check_id="CFG-002",
                        family="CFG",
                        severity=Severity.HIGH,
                        finding_class=FindingClass.RISK,
                        title=f"Normalisation failed for {ref.full}",
                        entity_type="table",
                        entity_id=ref.full,
                        entity_label=ref.full,
                        affected_count=0,
                        grain="aggregate",
                        baseline="none",
                        why_it_matters=(
                            "Checks on this table would read un-normalised data, so it is "
                            "excluded from this run rather than reported on unreliably."
                        ),
                        evidence=[{"error": str(exc)[:500]}],
                    )
                )
        log.info(
            "normalise.complete",
            tables=len(self.sources),
            actions=len(self.actions),
            findings=len(self.findings),
            seconds=round(time.perf_counter() - started, 1),
        )
        return self.sources

    # -------------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        return {
            "tables_normalised": len(self.sources),
            "rows_raw": sum(s.rows_raw for s in self.sources.values()),
            "rows_effective": sum(s.rows_effective for s in self.sources.values()),
            "rows_removed": sum(s.rows_removed for s in self.sources.values()),
            "actions": len(self.actions),
            "findings": len(self.findings),
            "snapshots_pinned": {
                name: s.pinned_snapshot
                for name, s in self.sources.items()
                if s.pinned_snapshot
            },
        }
