"""Load and validate the normalisation + column-semantics configuration.

Both YAML files are the reviewable contract between the business and the engine. Nothing in
here guesses: a rule that names a missing table or column is reported as a configuration
error rather than silently skipped, because a silently skipped dedup rule is how 98,771
duplicate rows get back into a statistic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.domain.models import Baseline, Grain, GrainKind, Severity
from app.logging import get_logger

log = get_logger(__name__)


class ConfigError(ValueError):
    """Raised when the YAML contract is internally inconsistent."""


def _sev(value: str | None, default: Severity = Severity.MEDIUM) -> Severity:
    if not value:
        return default
    try:
        return Severity(value.lower())
    except ValueError as exc:  # noqa: BLE001
        raise ConfigError(f"unknown severity {value!r}") from exc


# ------------------------------------------------------------------ rule dataclasses
@dataclass(slots=True)
class SnapshotRule:
    table: str
    timestamp_column: str
    partition_by: list[str]
    check_id: str = "PIP-001"
    note: str = ""


@dataclass(slots=True)
class DedupRule:
    table: str
    keys: list[str]
    order_by: str
    mode: str                      # 'keep_latest' | 'exact_only'
    check_id: str
    severity: Severity = Severity.HIGH
    enabled: bool = True
    # Which columns count as a real disagreement between duplicate rows. Empty means
    # "derive from column_semantics": authoritative, non-derived columns only. Comparing
    # P6-derived fields (duration, remaining_duration) counts a plan re-export as a
    # conflict, which is noise rather than a finding.
    compare_columns: list[str] = field(default_factory=list)
    note: str = ""

    def __post_init__(self) -> None:
        if self.mode not in ("keep_latest", "exact_only"):
            raise ConfigError(f"{self.table}: dedup mode must be keep_latest or exact_only")
        if not self.keys:
            raise ConfigError(f"{self.table}: dedup requires at least one key")


@dataclass(slots=True)
class ColumnTargetRule:
    table: str
    columns: list[str]
    note: str = ""


@dataclass(slots=True)
class PlaceholderRule:
    values: list[str]
    check_id: str
    severity: Severity
    targets: list[ColumnTargetRule] = field(default_factory=list)


@dataclass(slots=True)
class SentinelRule:
    values: list[str]
    check_id: str
    severity: Severity
    targets: list[ColumnTargetRule] = field(default_factory=list)


@dataclass(slots=True)
class GroupingKeyRule:
    check_id: str
    severity: Severity
    strip_chars: list[str]
    collapse_whitespace: bool
    case_insensitive: bool
    targets: list[ColumnTargetRule] = field(default_factory=list)


@dataclass(slots=True)
class PhantomKeyRule:
    table: str
    column: str
    values: list[Any]
    check_id: str
    severity: Severity
    mode: str = "flag_only"          # 'flag_only' keeps the rows; 'exclude' drops them
    note: str = ""

    def __post_init__(self) -> None:
        if self.mode not in ("flag_only", "exclude"):
            raise ConfigError(
                f"{self.table}.{self.column}: phantom_keys mode must be "
                f"flag_only or exclude, got {self.mode!r}"
            )

    @property
    def excludes_rows(self) -> bool:
        return self.mode == "exclude"


# --------------------------------------------------------------- column semantics
@dataclass(slots=True)
class ColumnSemantics:
    name: str
    owner: str = "unknown"
    role: str = "unknown"
    authoritative: bool = False
    all_null: bool = False
    note: str = ""
    label: str | None = None
    placeholder: str | None = None
    derived_from: list[str] = field(default_factory=list)
    value_range: tuple[float, float] | None = None
    minimum: float | None = None
    allowed: list[Any] | None = None
    pii: bool = False
    coverage: float | None = None
    inclusive: bool = False
    cumulative: bool = False

    @property
    def generates_invariants(self) -> bool:
        """A column that is dead or explicitly unused produces no checks.

        This is what stops the generator emitting the false pairs the name-based prototype
        produced (docs/01e §3) — e.g. `material_po_date > eng_finish_date`, where both
        columns are 100% NULL.
        """
        return not self.all_null and self.role != "unused"

    @property
    def baseline(self) -> Baseline:
        return {
            "machine_plan": Baseline.P6,
            "commitment": Baseline.TARGET,
            "display_only": Baseline.COMMITTED,
            "actual": Baseline.ACTUAL,
            "master_date": Baseline.MASTER_DATE,
        }.get(self.role, Baseline.NONE)


@dataclass(slots=True)
class DateOrderRule:
    """One explicitly declared `before <= after` invariant for a table.

    Declared, not inferred from column names -- name-based inference is exactly what
    mispaired two 100%-NULL columns in the discovery prototype (docs/01e Section 3).
    """

    table: str
    before: str
    after: str
    check_id: str
    severity: Severity = Severity.HIGH
    business_rule_ref: str | None = None
    note: str = ""


@dataclass(slots=True)
class TableSemantics:
    name: str
    grain: Grain
    grain_note: str = ""
    columns: dict[str, ColumnSemantics] = field(default_factory=dict)
    date_order: list[DateOrderRule] = field(default_factory=list)

    def column(self, name: str) -> ColumnSemantics | None:
        return self.columns.get(name) or self.columns.get(name.lower())


def _parse_grain(raw: dict[str, Any] | None, table: str) -> tuple[Grain, str]:
    if not raw:
        return Grain.row(), ""
    kind = str(raw.get("kind", "row")).lower()
    note = str(raw.get("note", "") or "").strip()
    keys = [str(k) for k in raw.get("keys", []) or []]
    order_by = raw.get("order_by")
    if kind == "row":
        return Grain.row(), note
    if kind == "aggregate":
        return Grain.aggregate(), note
    if not order_by:
        raise ConfigError(f"{table}: grain {kind!r} requires order_by")
    if kind == "latest_per":
        return Grain.latest_per(*keys, order_by=str(order_by)), note
    if kind == "snapshot_pinned":
        return Grain.snapshot_pinned(*keys, order_by=str(order_by)), note
    raise ConfigError(f"{table}: unknown grain kind {kind!r}")


def _parse_column(name: str, raw: dict[str, Any]) -> ColumnSemantics:
    rng = raw.get("range")
    return ColumnSemantics(
        name=name,
        owner=str(raw.get("owner", "unknown")),
        role=str(raw.get("role", "unknown")),
        authoritative=bool(raw.get("authoritative", False)),
        all_null=bool(raw.get("all_null", False)),
        note=str(raw.get("note", "") or "").strip(),
        label=raw.get("label"),
        placeholder=raw.get("placeholder"),
        derived_from=[str(x) for x in raw.get("derived_from", []) or []],
        value_range=(float(rng[0]), float(rng[1])) if rng else None,
        minimum=float(raw["min"]) if "min" in raw else None,
        allowed=raw.get("allowed"),
        pii=bool(raw.get("pii", False)),
        coverage=float(raw["coverage"]) if "coverage" in raw else None,
        inclusive=bool(raw.get("inclusive", False)),
        cumulative=bool(raw.get("cumulative", False)),
    )


# ------------------------------------------------------------------------- the spec
@dataclass(slots=True)
class NormalisationSpec:
    snapshots: list[SnapshotRule] = field(default_factory=list)
    dedup: list[DedupRule] = field(default_factory=list)
    placeholder_dates: PlaceholderRule | None = None
    blank_to_null: SentinelRule | None = None
    sentinels: SentinelRule | None = None
    grouping_keys: GroupingKeyRule | None = None
    phantom_keys: list[PhantomKeyRule] = field(default_factory=list)
    html_entities: SentinelRule | None = None
    mojibake_replacements: dict[str, str] = field(default_factory=dict)
    tables: dict[str, TableSemantics] = field(default_factory=dict)
    pii_columns: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ accessors
    def snapshot_for(self, table: str) -> SnapshotRule | None:
        return next((r for r in self.snapshots if r.table.lower() == table.lower()), None)

    def dedup_for(self, table: str) -> DedupRule | None:
        return next(
            (r for r in self.dedup if r.table.lower() == table.lower() and r.enabled), None
        )

    def semantics_for(self, table: str) -> TableSemantics | None:
        return self.tables.get(table) or self.tables.get(table.lower())

    def grain_for(self, table: str) -> Grain:
        sem = self.semantics_for(table)
        return sem.grain if sem else Grain.row()

    def placeholder_columns(self, table: str) -> list[str]:
        out: list[str] = []
        if self.placeholder_dates:
            for t in self.placeholder_dates.targets:
                if t.table.lower() == table.lower():
                    out.extend(t.columns)
        return out

    def blank_columns(self, table: str) -> list[str]:
        out: list[str] = []
        if self.blank_to_null:
            for t in self.blank_to_null.targets:
                if t.table.lower() == table.lower():
                    out.extend(t.columns)
        return out

    def sentinel_columns(self, table: str) -> list[str]:
        out: list[str] = []
        if self.sentinels:
            for t in self.sentinels.targets:
                if t.table.lower() == table.lower():
                    out.extend(t.columns)
        return out

    def phantom_keys_for(self, table: str) -> list[PhantomKeyRule]:
        return [r for r in self.phantom_keys if r.table.lower() == table.lower()]

    def is_pii(self, table: str, column: str) -> bool:
        needle = f"{table}.{column}".lower()
        for pat in self.pii_columns:
            p = pat.lower()
            if p == needle or (p.endswith(".*") and needle.startswith(p[:-1])):
                return True
        sem = self.semantics_for(table)
        col = sem.column(column) if sem else None
        return bool(col and col.pii)

    @property
    def tables_touched(self) -> set[str]:
        names = {r.table for r in self.snapshots}
        names |= {r.table for r in self.dedup if r.enabled}
        names |= {r.table for r in self.phantom_keys}
        for rule in (self.placeholder_dates, self.blank_to_null, self.sentinels):
            if rule:
                names |= {t.table for t in rule.targets}
        if self.grouping_keys:
            names |= {t.table for t in self.grouping_keys.targets}
        return names


def _targets(raw: list[dict[str, Any]] | None) -> list[ColumnTargetRule]:
    out: list[ColumnTargetRule] = []
    for item in raw or []:
        cols = item.get("columns") or ([item["column"]] if "column" in item else [])
        out.append(
            ColumnTargetRule(
                table=str(item["table"]),
                columns=[str(c) for c in cols],
                note=str(item.get("note", "") or "").strip(),
            )
        )
    return out


def load_spec(
    normalisation_path: Path | None = None,
    semantics_path: Path | None = None,
) -> NormalisationSpec:
    cfg_dir = get_settings().config_dir
    npath = normalisation_path or cfg_dir / "normalisation.yaml"
    spath = semantics_path or cfg_dir / "column_semantics.yaml"
    for p in (npath, spath):
        if not p.exists():
            raise ConfigError(f"configuration file not found: {p}")

    nraw = yaml.safe_load(npath.read_text(encoding="utf-8")) or {}
    sraw = yaml.safe_load(spath.read_text(encoding="utf-8")) or {}

    spec = NormalisationSpec()

    for item in nraw.get("snapshots", []) or []:
        spec.snapshots.append(
            SnapshotRule(
                table=str(item["table"]),
                timestamp_column=str(item["timestamp_column"]),
                partition_by=[str(x) for x in item.get("partition_by", []) or []],
                check_id=str(item.get("check_id", "PIP-001")),
                note=str(item.get("note", "") or "").strip(),
            )
        )

    for item in nraw.get("dedup", []) or []:
        spec.dedup.append(
            DedupRule(
                table=str(item["table"]),
                keys=[str(x) for x in item.get("keys", []) or []],
                order_by=str(item.get("order_by", "")),
                mode=str(item.get("mode", "keep_latest")),
                check_id=str(item.get("check_id", "DUP-000")),
                severity=_sev(item.get("severity"), Severity.HIGH),
                enabled=bool(item.get("enabled", True)),
                compare_columns=[str(x) for x in item.get("compare_columns", []) or []],
                note=str(item.get("note", "") or "").strip(),
            )
        )

    if pd := nraw.get("placeholder_dates"):
        spec.placeholder_dates = PlaceholderRule(
            values=[str(v) for v in pd.get("values", []) or []],
            check_id=str(pd.get("check_id", "PLC-001")),
            severity=_sev(pd.get("severity"), Severity.CRITICAL),
            targets=_targets(pd.get("targets")),
        )

    if bn := nraw.get("blank_to_null"):
        spec.blank_to_null = SentinelRule(
            values=[""],
            check_id=str(bn.get("check_id", "BLK-001")),
            severity=_sev(bn.get("severity"), Severity.LOW),
            targets=_targets(bn.get("targets")),
        )

    if sn := nraw.get("sentinels"):
        spec.sentinels = SentinelRule(
            values=[str(v) for v in sn.get("values", []) or []],
            check_id=str(sn.get("check_id", "SNT-001")),
            severity=_sev(sn.get("severity"), Severity.MEDIUM),
            targets=_targets(sn.get("targets")),
        )

    if gk := nraw.get("grouping_key_normalisation"):
        spec.grouping_keys = GroupingKeyRule(
            check_id=str(gk.get("check_id", "FMT-007")),
            severity=_sev(gk.get("severity"), Severity.CRITICAL),
            strip_chars=[str(c) for c in gk.get("strip_chars", []) or []],
            collapse_whitespace=bool(gk.get("collapse_whitespace", True)),
            case_insensitive=bool(gk.get("case_insensitive", True)),
            targets=_targets(gk.get("targets")),
        )

    if pk := nraw.get("phantom_keys"):
        sev = _sev(pk.get("severity"), Severity.HIGH)
        cid = str(pk.get("check_id", "VAL-018"))
        for item in pk.get("targets", []) or []:
            spec.phantom_keys.append(
                PhantomKeyRule(
                    table=str(item["table"]),
                    column=str(item["column"]),
                    values=list(item.get("values", []) or []),
                    check_id=cid,
                    severity=sev,
                    mode=str(item.get("mode", "flag_only")),
                    note=str(item.get("note", "") or "").strip(),
                )
            )

    tc = nraw.get("text_cleanup") or {}
    if he := tc.get("html_entities"):
        spec.html_entities = SentinelRule(
            values=["&amp;", "&lt;", "&gt;", "&quot;", "&#39;"],
            check_id=str(he.get("check_id", "FMT-011")),
            severity=_sev(he.get("severity"), Severity.LOW),
            targets=_targets(he.get("targets")),
        )
    if mj := tc.get("mojibake"):
        spec.mojibake_replacements = {
            str(k): str(v) for k, v in (mj.get("replacements") or {}).items()
        }

    for table_name, traw in (sraw.get("tables") or {}).items():
        grain, note = _parse_grain(traw.get("grain"), table_name)
        cols = {
            cname: _parse_column(cname, craw or {})
            for cname, craw in (traw.get("columns") or {}).items()
        }
        date_order = [
            DateOrderRule(
                table=table_name,
                before=str(d["before"]),
                after=str(d["after"]),
                check_id=str(d.get("id", f"GEN-DATE-{table_name}")),
                severity=_sev(d.get("severity"), Severity.HIGH),
                business_rule_ref=d.get("business_rule_ref"),
                note=str(d.get("note", "") or "").strip(),
            )
            for d in (traw.get("date_order") or [])
        ]
        spec.tables[table_name] = TableSemantics(
            name=table_name, grain=grain, grain_note=note, columns=cols,
            date_order=date_order,
        )

    spec.pii_columns = [str(x) for x in (sraw.get("pii_columns") or [])]

    log.info(
        "spec.loaded",
        snapshots=len(spec.snapshots),
        dedup_rules=len([r for r in spec.dedup if r.enabled]),
        dedup_disabled=len([r for r in spec.dedup if not r.enabled]),
        tables_with_semantics=len(spec.tables),
        pii_patterns=len(spec.pii_columns),
    )
    return spec
