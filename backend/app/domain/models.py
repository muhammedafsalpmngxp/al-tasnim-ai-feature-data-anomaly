"""Domain model.

Two declarations carry the lessons from docs/01c and 01d and are therefore *mandatory* on
every check: `grain` (so history is not counted as defects) and `baseline` (so the wrong
plan is not used as the yardstick). A check omitting either fails registration.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums
class Severity(str, enum.Enum):
    """How much it matters. Orthogonal to FindingClass."""

    CRITICAL = "critical"   # reported figures are already wrong, or a fact is impossible
    HIGH = "high"           # business rule violated on real records
    MEDIUM = "medium"       # data unusable/ambiguous, not yet wrong downstream
    LOW = "low"             # hygiene
    REVIEW = "review"       # statistical outlier: a question, never a defect
    INFO = "info"           # positive or neutral observation

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.REVIEW: 4,
    Severity.INFO: 5,
}


class FindingClass(str, enum.Enum):
    """What kind of thing this is.

    A flat severity list is what let a non-issue sit at the top of the external report
    (docs/01d). PENDING and DESIGN still print, in their own sections, counted — so a
    reader can see they were examined and cleared.
    """

    VIOLATION = "violation"  # a business rule is broken on real records
    DEFECT = "defect"        # the data is internally impossible
    GAP = "gap"              # something genuinely due is absent
    PENDING = "pending"      # absent but NOT YET DUE — normal, not a fault
    DESIGN = "design"        # how the system works, not a fault
    RISK = "risk"            # not wrong yet, will bite
    REVIEW = "review"        # statistical outlier

    @property
    def is_actionable(self) -> bool:
        return self in (FindingClass.VIOLATION, FindingClass.DEFECT, FindingClass.GAP)


class Baseline(str, enum.Enum):
    """Which plan a comparison is measured against.

    TARGET is the planner-owned, authoritative baseline (business input 2026-09-07).
    P6 is machine-generated and is NOT what the business watches — using it reports 5.5x
    more late tasks (docs/01c §1).

    COMMITTED is registered but DISABLED: the business has confirmed its meaning is not yet
    established, so those columns are display-only. `Baseline.COMMITTED.enabled` is False
    and check registration rejects it.
    """

    TARGET = "target"
    P6 = "p6"
    COMMITTED = "committed"
    MASTER_DATE = "master_date"   # well_master.ex_rig_on_date — the §4 master date
    ACTUAL = "actual"
    NONE = "none"

    @property
    def enabled(self) -> bool:
        return self is not Baseline.COMMITTED

    @property
    def disabled_reason(self) -> str | None:
        if self is Baseline.COMMITTED:
            return (
                "baseline COMMITTED is not confirmed by the business "
                "(committed_start/committed_end are display_only) — use TARGET"
            )
        return None


class GrainKind(str, enum.Enum):
    ROW = "row"                          # every row is its own subject
    LATEST_PER = "latest_per"            # collapse history: latest row per key
    SNAPSHOT_PINNED = "snapshot_pinned"  # one snapshot per partition
    AGGREGATE = "aggregate"              # the check aggregates itself


@dataclass(frozen=True, slots=True)
class Grain:
    """The subject of a check.

    `well.task_daily` is a daily log at 3.03 rows per task; counting per row overstated
    one finding 15x (docs/01c §2). Declaring the grain makes that mistake unrepresentable.
    """

    kind: GrainKind
    keys: tuple[str, ...] = ()
    order_by: str | None = None

    @classmethod
    def row(cls) -> Grain:
        return cls(GrainKind.ROW)

    @classmethod
    def latest_per(cls, *keys: str, order_by: str) -> Grain:
        if not keys:
            raise ValueError("Grain.latest_per requires at least one key column")
        if not order_by:
            raise ValueError("Grain.latest_per requires order_by to pick the latest row")
        return cls(GrainKind.LATEST_PER, tuple(keys), order_by)

    @classmethod
    def snapshot_pinned(cls, *keys: str, order_by: str) -> Grain:
        return cls(GrainKind.SNAPSHOT_PINNED, tuple(keys), order_by)

    @classmethod
    def aggregate(cls) -> Grain:
        return cls(GrainKind.AGGREGATE)

    def describe(self) -> str:
        if self.kind is GrainKind.ROW:
            return "row"
        if self.kind is GrainKind.AGGREGATE:
            return "aggregate"
        return f"{self.kind.value}({', '.join(self.keys)} ORDER BY {self.order_by})"


# ------------------------------------------------------------------- normalisation
class NormalisationKind(str, enum.Enum):
    SNAPSHOT_PIN = "snapshot_pin"
    DEDUP = "dedup"
    PLACEHOLDER_DATE = "placeholder_date"
    BLANK_TO_NULL = "blank_to_null"
    SENTINEL_TO_NULL = "sentinel_to_null"


@dataclass(slots=True)
class NormalisationAction:
    """A rewrite Layer 0 performed. Every rewrite is itself reported —
    "98,771 rows had to be removed before your data could be analysed" is a finding.
    """

    kind: NormalisationKind
    target: str                       # schema.table or schema.table.column
    rows_affected: int
    rows_total: int
    detail: dict[str, Any] = field(default_factory=dict)
    check_id: str = ""                # the finding id this action reports as, e.g. DUP-001

    @property
    def pct(self) -> float | None:
        if not self.rows_total:
            return None
        return round(100.0 * self.rows_affected / self.rows_total, 2)


# ------------------------------------------------------------------------ findings
@dataclass(slots=True)
class Finding:
    check_id: str
    family: str
    severity: Severity
    finding_class: FindingClass
    title: str
    entity_type: str                  # 'well' | 'task' | 'table' | 'column' | 'project'
    entity_id: str | None = None
    entity_label: str | None = None   # e.g. well 36754 / SONRAKDS5277
    well_id: int | None = None        # the report's primary axis
    affected_count: int = 0
    grain: str = ""
    baseline: str = ""
    business_rule_ref: str | None = None
    owner: str | None = None          # PDO | AlTasnim | Unassigned
    why_it_matters: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    sql_text: str | None = None
    sampled: bool = False
    # populated by Phase 3 VERIFY
    verified_by: list[str] = field(default_factory=list)
    counter_query_count: int | None = None
    # populated by Phase 4 LLM
    llm_explanation: str | None = None
    llm_root_cause: str | None = None
    llm_remediation: str | None = None

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, default=str)


@dataclass(slots=True)
class CheckResult:
    """Recorded for every check that ran, including passes.

    45 of 101 generated invariants violated means 56 passed; those print in the Check
    Catalogue so a reader can see what was tested and found clean (docs/01e §5.3).
    """

    check_id: str
    family: str
    status: str                       # 'pass' | 'fail' | 'skipped' | 'error'
    rows_scanned: int = 0
    violations: int = 0
    duration_ms: int = 0
    grain: str = ""
    baseline: str = ""
    skip_reason: str | None = None
    error_text: str | None = None


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Run:
    run_id: str
    started_at: datetime
    status: RunStatus = RunStatus.RUNNING
    finished_at: datetime | None = None
    triggered_by: str | None = None
    db_name: str | None = None
    server_now: datetime | None = None
    as_of_date: date | None = None     # the date every deadline is judged against
    snapshot_pinned: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    rows_scanned: int = 0
    checks_run: int = 0
    checks_passed: int = 0
    checks_skipped: int = 0
    findings_total: int = 0
    llm_model: str | None = None
    llm_tokens: int = 0
    xlsx_path: str | None = None
    docx_path: str | None = None
    error_text: str | None = None
