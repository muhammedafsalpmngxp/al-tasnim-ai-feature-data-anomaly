"""The data model shared by every layer: rule -> compiled probe -> result -> finding.

These are the ONLY shapes that cross a layer boundary, which is what keeps the loader, the
graph, the catalog and the report builders independent of each other. A node never reaches into
a markdown file and a report builder never reaches into the database.

Nothing here names a table or a column. `entity_key` and `evidence_*` are positions in a
contract, filled by whatever the rule's own SQL selects.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# ── Enumerations ───────────────────────────────────────────────────────────────
# Kept as validated strings rather than Enums: they are read from markdown written by business
# users and round-trip through JSON, and a plain string keeps both directions obvious.

SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
# Report ordering. Index, not alphabetical - "critical" must outrank "high", and "low" must not
# sort above "medium" just because l < m.
SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}

# How the anomaly is decided, which selects the knowledge block the SQL Author is shown.
#   rule        a deterministic condition stated by the business
#   statistical an outlier against the population's own distribution
#   rollup      a parent total that disagrees with its children
METHODS: tuple[str, ...] = ("rule", "statistical", "rollup")

# What the compile graph does with the SQL in the rule file. See README.
#   pinned    the SQL is used verbatim; the Author is skipped entirely (1 LLM call: verify)
#   seed      the SQL is a starting point the Author adapts to the live schema
#   authored  no SQL present; generated from the prose alone
SQL_MODES: tuple[str, ...] = ("pinned", "seed", "authored")

STATUSES: tuple[str, ...] = ("active", "draft", "disabled")

# Where a rule came from. "declared" is written as itself in data_anomalies.md; "expanded" is
# one concrete member of a structural family, produced by applying one such rule to every
# matching feature the schema declares (app/rules/expand.py).
SOURCES: tuple[str, ...] = ("declared", "expanded")

# The value that means "derive the threshold from the data, do not substitute a literal".
AUTO = "auto"


# ── The SUMMARY / DETAIL contract ──────────────────────────────────────────────
# Every probe is a PAIR of queries, and this is the whole reason cost stays bounded:
# SUMMARY always returns exactly one row, so it is always safe to show an LLM and always cheap
# to run; DETAIL is executed only when SUMMARY reports a non-zero count.
#
# These names are checked against the driver's own cursor.description after execution - never
# by parsing SQL, which is fragile and would disagree with what actually came back.

SUMMARY_REQUIRED: tuple[str, ...] = ("rule_id", "scope_total", "anomaly_count")
SUMMARY_OPTIONAL: tuple[str, ...] = ("anomaly_pct", "worst_severity_val")

DETAIL_REQUIRED: tuple[str, ...] = ("entity_key",)
DETAIL_RECOMMENDED: tuple[str, ...] = ("entity_label", "severity_value", "explain_text")
# Columns proving the anomaly. At least one is required: a finding a reader cannot verify from
# the row itself is an assertion, not evidence, and a data-quality report made of assertions is
# worthless to the person who has to go and fix the record.
EVIDENCE_PREFIX = "evidence_"


@dataclass(frozen=True)
class AnomalyRule:
    """One anomaly, as declared in markdown or generated from the schema.

    `raw` and `rule_hash` exist for per-rule cache invalidation: editing one rule recompiles
    that rule alone, instead of paying to recompile the whole catalog.
    """

    rule_id: str
    title: str
    category: str = "Uncategorised"
    severity: str = "medium"
    entity: str = "row"
    method: str = "rule"
    sql_mode: str = "authored"
    status: str = "active"
    source: str = "declared"
    tags: tuple[str, ...] = ()

    # A STRUCTURAL family: one rule written once, applied to every matching feature the schema
    # has. `expands_over: foreign_key` on a rule about orphan rows becomes one probe per
    # declared foreign key - 36 of them here - each reported separately, so a finding names the
    # relationship that is broken rather than a single number covering all of them.
    #
    # Named after the SCHEMA FEATURE, never a table: that is what keeps one sentence of business
    # prose portable to a database with different tables and a different number of them.
    # Empty for an ordinary rule, which is about one specific thing and expands over nothing.
    expands_over: str = ""

    # Set on each rule PRODUCED by expanding a family: the id of the family rule it came from.
    # Recorded explicitly rather than parsed back out of "DQ-G01-004", because deriving identity
    # from a naming convention is one rename away from being silently wrong - and this one
    # decides which probes share an authored query.
    family_id: str = ""

    @property
    def is_family(self) -> bool:
        """A rule still waiting to be applied to many features. Never compiled itself.

        `expands_over` stays set on the expansions too - it is the feature KIND, and the author
        needs it to know which tokens its query may use. What tells the two apart is
        `family_id`: present only once a rule IS one concrete feature.
        """
        return bool(self.expands_over) and not self.family_id

    @property
    def is_expanded(self) -> bool:
        """One concrete probe produced FROM a family rule."""
        return bool(self.family_id)

    # The prose sections (what is wrong / why it matters / how to detect / do NOT flag).
    # Fed to the SQL Author and to the Verifier verbatim - this is the business intent, and
    # the Verifier cannot judge a probe without knowing what it was meant to find.
    body: str = ""

    # Placeholder-substituted SQL. Empty for an `authored` rule, which has none yet.
    summary_sql: str = ""
    detail_sql: str = ""

    # Every extra metadata key from the markdown, available as {{placeholder}} in the SQL.
    params: dict[str, str] = field(default_factory=dict)

    raw: str = ""
    source_file: str = ""
    line_no: int = 0

    @property
    def rule_hash(self) -> str:
        """Identity of this rule's DEFINITION, for per-rule cache invalidation.

        Hashes the raw markdown block, so any edit - prose, metadata or SQL - invalidates this
        rule and nothing else. Prose is included deliberately: it reaches the Author and the
        Verifier, so changing it can legitimately change the compiled SQL.
        """
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()[:16]

    @property
    def runnable(self) -> bool:
        return self.status == "active"

    @property
    def has_sql(self) -> bool:
        return bool(self.summary_sql.strip() and self.detail_sql.strip())

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, len(SEVERITIES))


@dataclass
class CompiledProbe:
    """A rule after compilation: the SQL that will actually run, plus how it got there.

    Persisted to .cache/anomaly_catalog.json. `rule_hash` and `structure_fingerprint` together
    decide whether this entry is still valid, or whether this one rule must be recompiled.
    """

    rule_id: str
    summary_sql: str
    detail_sql: str
    # active | failed | not_applicable | disabled
    status: str = "active"
    # WHERE THIS SQL CAME FROM - "declared" (a rule written as itself) or "expanded" (one
    # member of a structural family). An agent wrote the query either way.
    #
    # Recorded EXPLICITLY rather than inferred from the "GEN-" id prefix. Provenance is the
    # first thing anyone asks of a data-quality finding - "did a model write this check?" - and
    # answering it from a naming convention is one rename away from being wrong. It also keeps
    # `llm_calls == 0` meaning what it says: a reused probe reports 0 calls for THIS compile,
    # which is not the same as never having been written by a model.
    source: str = "declared"
    rule_hash: str = ""
    structure_fingerprint: str = ""
    # The structure of just THIS probe's tables (introspect.probe_fingerprint). Checked before
    # the whole-database fingerprint above, which moves whenever anything anywhere changes and
    # would otherwise force a full recompile over a column added to an unrelated table.
    #
    # Empty on an entry written before this existed; staleness then falls back to the
    # whole-database fingerprint, which is the conservative direction - recompiling something
    # that did not need it costs money, missing something that did costs correctness.
    table_fingerprint: str = ""
    compiled_at: str = ""

    # Which tables the probe reads, from Grounding. Used to prune the schema block on a wide
    # database and to explain a probe in the report without re-parsing its SQL.
    tables: tuple[str, ...] = ()
    # Grounding's short justification, and the Verifier's note on approval. Both are kept so a
    # reviewer can see WHY a probe looks the way it does without re-running the compile.
    grounding_note: str = ""
    verifier_note: str = ""
    # How a threshold was arrived at. Required when the rule used tolerance: auto - a report
    # that cannot say where its threshold came from is not auditable.
    threshold_note: str = ""
    # Populated when status is failed/not_applicable: the last error, for the report's
    # transparency section. A rule that could not be compiled must be VISIBLE, never silent.
    error: str = ""
    llm_calls: int = 0

    @property
    def authored_by_agent(self) -> bool:
        """True when an LLM wrote this SQL. False only for hand-written pinned SQL."""
        return self.source != "pinned"

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "summary_sql": self.summary_sql,
            "detail_sql": self.detail_sql,
            "status": self.status,
            "source": self.source,
            "rule_hash": self.rule_hash,
            "structure_fingerprint": self.structure_fingerprint,
            "table_fingerprint": self.table_fingerprint,
            "compiled_at": self.compiled_at,
            "tables": list(self.tables),
            "grounding_note": self.grounding_note,
            "verifier_note": self.verifier_note,
            "threshold_note": self.threshold_note,
            "error": self.error,
            "llm_calls": self.llm_calls,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CompiledProbe":
        return cls(
            rule_id=data.get("rule_id", ""),
            summary_sql=data.get("summary_sql", ""),
            detail_sql=data.get("detail_sql", ""),
            status=data.get("status", "active"),
            source=data.get("source") or "declared",
            rule_hash=data.get("rule_hash", ""),
            structure_fingerprint=data.get("structure_fingerprint", ""),
            table_fingerprint=data.get("table_fingerprint", ""),
            compiled_at=data.get("compiled_at", ""),
            tables=tuple(data.get("tables") or ()),
            grounding_note=data.get("grounding_note", ""),
            verifier_note=data.get("verifier_note", ""),
            threshold_note=data.get("threshold_note", ""),
            error=data.get("error", ""),
            llm_calls=int(data.get("llm_calls") or 0),
        )


@dataclass
class ProbeResult:
    """One rule's outcome in one detection run.

    `detail_rows` holds at most the report cap - the full result is streamed straight to Excel
    and never accumulated in memory. `anomaly_count` therefore comes from SUMMARY, not from
    len(detail_rows), and those two numbers are NOT interchangeable: the second is capped.
    """

    rule_id: str
    ok: bool = True
    error: str = ""

    scope_total: int = 0
    anomaly_count: int = 0
    anomaly_pct: float = 0.0
    worst_severity_val: float | None = None

    detail_columns: list[str] = field(default_factory=list)
    detail_rows: list[list[Any]] = field(default_factory=list)
    # True when detail_rows was cut short by the report cap, so the report can say "showing the
    # worst N of M" rather than implying it listed everything.
    detail_truncated: bool = False
    # How many detail rows were actually READ, which is not len(detail_rows): that list stops at
    # the Word cap while the spool keeps going to the Excel cap. Reporting the length of a
    # capped list as the number of findings is the mistake this field exists to prevent.
    detail_total: int = 0
    # Where the FULL result was streamed, one JSON row per line. Excel is built from this file
    # rather than from memory, which is what keeps a hundred-thousand-row rule affordable.
    # Empty when the probe found nothing, or when spooling was not requested.
    spool_path: str = ""
    # True when the probe found more rows than ANOMALY_EXPORT_MAX_ROWS allows, so even the
    # Excel file is not the complete picture and must say so.
    spool_truncated: bool = False
    # DETAIL was not executed because SUMMARY reported nothing anomalous. This is the run's
    # main economy, and it is recorded so "no rows" is never confused with "not looked at".
    detail_skipped: bool = False

    summary_seconds: float = 0.0
    detail_seconds: float = 0.0
    # Deterministic observations handed to the report (and, at compile time, to the Verifier).
    # Advisory: a concern is something to look at, never on its own a reason to fail a rule.
    concerns: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return self.ok and self.anomaly_count > 0
