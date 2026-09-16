"""Persist the compiled probes, and decide which of them are stale.

THIS FILE IS WHY A DETECTION RUN IS ALMOST FREE. Compiling is the expensive stage - grounding,
authoring and reviewing every rule against a live schema. Running is not: it executes SQL that
was already proved correct. The catalog is the boundary between the two, and everything here
exists to keep a rule on the cheap side of it for as long as that is honest.

STALENESS IS DECIDED PER RULE, ON TWO INDEPENDENT SIGNALS
---------------------------------------------------------
  rule_hash              the rule's own markdown. Editing one rule recompiles THAT rule, not
                         the catalog. Prose counts, because prose reaches the author and the
                         reviewer and can legitimately change the SQL.
  structure_fingerprint  the database's STRUCTURE, deliberately excluding row counts.

The second is the one that matters most in practice. Row counts move every day as data loads;
a catalog keyed on the full schema fingerprint would be invalidated daily and recompiled at
full cost, which would defeat the entire point of compiling once. A probe's SQL depends on the
shape of the tables it reads, not on how many rows are in them this morning.

A FAILED PROBE IS KEPT, NOT DISCARDED. It is recorded with its error so the report can say a
check is NOT running. Dropping it would leave the operator believing in a check that does not
exist, which is the worst failure available to a data-quality tool. It is retried on the next
compile only when asked (`--retry-failed`), because a rule that failed for a real reason will
usually fail again, at full cost, every single run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from app.observability import get_logger
from app.rules.spec import AnomalyRule, CompiledProbe

log = get_logger()

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
_CATALOG_PATH = os.path.join(_CACHE_DIR, "anomaly_catalog.json")

# Bumped when the MEANING of a stored field changes. Without it an older catalog would be
# loaded and trusted by newer code that reads its fields differently - and the failure would be
# silent, because the JSON still parses perfectly.
CATALOG_VERSION = 1


@dataclass
class Catalog:
    """Every compiled probe, keyed by rule id."""

    probes: dict[str, CompiledProbe]
    structure_fingerprint: str = ""
    compiled_at: str = ""
    version: int = CATALOG_VERSION

    def get(self, rule_id: str) -> CompiledProbe | None:
        return self.probes.get(rule_id)

    @property
    def active(self) -> list[CompiledProbe]:
        return [p for p in self.probes.values() if p.status == "active"]

    @property
    def failed(self) -> list[CompiledProbe]:
        return [p for p in self.probes.values() if p.status == "failed"]

    @property
    def not_applicable(self) -> list[CompiledProbe]:
        return [p for p in self.probes.values() if p.status == "not_applicable"]

    def counts(self) -> str:
        return (
            f"{len(self.active)} active, {len(self.failed)} failed, "
            f"{len(self.not_applicable)} not applicable"
        )


def load() -> Catalog:
    """Read the catalog. A missing, unreadable or outdated file yields an EMPTY catalog.

    Never raises. A corrupt catalog is a recoverable situation - everything in it can be
    rebuilt from the rule files and the database - so the honest response is to say so and
    recompile, not to stop the operator from running anything at all.
    """
    if not os.path.exists(_CATALOG_PATH):
        return Catalog(probes={})
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("catalog: could not be read (%s) - it will be rebuilt", exc)
        return Catalog(probes={})

    version = int(data.get("version") or 0)
    if version != CATALOG_VERSION:
        log.warning(
            "catalog: written by format version %s, this build expects %s - rebuilding",
            version or "unknown", CATALOG_VERSION,
        )
        return Catalog(probes={})

    probes: dict[str, CompiledProbe] = {}
    for raw in data.get("probes") or []:
        try:
            probe = CompiledProbe.from_json(raw)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not lose the rest
            log.warning("catalog: skipping an unreadable entry (%s)", exc)
            continue
        if probe.rule_id:
            probes[probe.rule_id] = probe

    catalog = Catalog(
        probes=probes,
        structure_fingerprint=str(data.get("structure_fingerprint") or ""),
        compiled_at=str(data.get("compiled_at") or ""),
        version=version,
    )
    log.info("catalog: loaded %d probe(s) - %s", len(probes), catalog.counts())
    return catalog


def save(catalog: Catalog) -> str:
    """Write the catalog atomically and return its path.

    Written to a temporary file and then moved into place, so an interrupted save leaves the
    previous catalog intact. A half-written catalog would be far worse than a stale one: it
    would silently drop checks, and nothing downstream could tell.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    payload = {
        "version": CATALOG_VERSION,
        "structure_fingerprint": catalog.structure_fingerprint,
        "compiled_at": catalog.compiled_at,
        "probes": [p.to_json() for p in catalog.probes.values()],
    }
    tmp = _CATALOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, _CATALOG_PATH)
    log.info("catalog: saved %d probe(s) - %s", len(catalog.probes), catalog.counts())
    return _CATALOG_PATH


def structure_moved(
    probe: CompiledProbe,
    structure_fingerprint: str,
    table_signatures: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """(has the structure this probe depends on changed, why).

    Deliberately shared by the compiler and the run's catalog loader. The two must never
    disagree about whether a probe is current: one deciding to recompile while the other is
    happy to execute the old SQL is precisely how stale queries reach a report.

    Preference order, most specific first:
      1. a table the probe reads has GONE - it cannot run at all, whatever else is true;
      2. the probe's own tables, when both sides know them;
      3. the whole database, for an entry compiled before per-table hashes existed.
    """
    from app.db import introspect

    if table_signatures:
        gone = introspect.missing_tables(probe.tables, table_signatures)
        if gone:
            return True, (
                "it reads " + ", ".join(gone) + ", which no longer exist(s) in the database"
            )
        if probe.table_fingerprint and probe.tables:
            current = introspect.probe_fingerprint(probe.tables, table_signatures)
            if current and current != probe.table_fingerprint:
                return True, "the structure of the tables it reads changed"
            return False, ""

    if structure_fingerprint and probe.structure_fingerprint != structure_fingerprint:
        return True, "the database structure changed"
    return False, ""


def is_stale(
    probe: CompiledProbe | None,
    rule: AnomalyRule,
    structure_fingerprint: str,
    retry_failed: bool = False,
    table_signatures: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """(needs recompiling, why). The why is logged, so a recompile is never mysterious.

    `table_signatures` narrows the structural test to the tables this probe actually reads.
    Without it the whole-database fingerprint is used, which is correct but blunt: it marks
    every probe stale over a column added to a table none of them touch.
    """
    if probe is None:
        return True, "not compiled yet"
    if probe.rule_hash != rule.rule_hash:
        return True, "the rule definition changed"

    structure_changed, why = structure_moved(probe, structure_fingerprint, table_signatures)
    if structure_changed:
        return True, why
    if probe.status == "failed":
        if retry_failed:
            return True, "retrying a rule that previously failed"
        # Reported at INFO rather than passed over in silence: a check that is not running must
        # never be invisible, even when skipping it is the deliberate default.
        log.info(
            "catalog: %s is still failed and was NOT retried (use --retry-failed) - %s",
            rule.rule_id, (probe.error or "")[:120],
        )
        return False, ""
    return False, ""


def runnable_reason(probe: CompiledProbe, rule: AnomalyRule | None) -> str:
    """"" when this probe may run, otherwise WHY it may not - in words for the report.

    TWO STATUSES DECIDE THIS, NOT ONE. The probe's own status says whether its SQL compiled;
    the RULE's status says whether the business still wants the check. Only the first was ever
    consulted, so turning a rule off in the markdown did nothing at all: DQ-D01 and DQ-D02 were
    marked `disabled` precisely because they double-report with a structural family, and both
    still ran - inflating the flagged-record count and the score with duplicates.

    That is the worst failure this tool can have. A data-quality report is worth exactly what
    its numbers are worth, and a check the operator believes is off must actually be off.

    Returning a REASON rather than a boolean is deliberate: a check that is not running has to
    appear in the report saying why, or the document overstates its own coverage.
    """
    if rule is not None and not rule.runnable:
        return f"the rule is {rule.status} in the rule file"
    if probe.status != "active":
        return probe.error or f"the probe is {probe.status}"
    return ""


def prune_removed(catalog: Catalog, rule_ids: set[str]) -> list[str]:
    """Drop probes whose rule no longer exists, returning what was removed.

    A rule deleted from the markdown, or a generic probe whose schema feature is gone, must not
    keep running: it would report findings against a definition nobody can look up any more.

    ⚠ Pass the ids of rules that are RUNNABLE, not every id the file mentions. A rule switched
    to `disabled` or `draft` still has an id, so passing all of them keeps its compiled probe in
    the catalog for ever - and `runnable_reason` above then has to keep catching it on every
    single run.
    """
    gone = [rule_id for rule_id in catalog.probes if rule_id not in rule_ids]
    for rule_id in gone:
        del catalog.probes[rule_id]
    if gone:
        log.info(
            "catalog: removed %d probe(s) whose rule no longer exists: %s",
            len(gone), ", ".join(sorted(gone)[:10]) + (" ..." if len(gone) > 10 else ""),
        )
    return gone


def path() -> str:
    return _CATALOG_PATH


def duplicate_probes(catalog: Catalog) -> list[tuple[str, ...]]:
    """Groups of DECLARED probes that measure the same thing. Empty when all are distinct.

    WHY ONLY DECLARED PROBES. A structural family is one rule applied to every matching schema
    feature, so its members share a shape BY CONSTRUCTION - and the feature list they expand
    over is already deduplicated upstream, so two members cannot target the same thing. Include
    them and the check reports nine groups of false duplicates, which is how a useful warning
    gets switched off. A duplicate that matters is one a PERSON wrote twice.

    THE CASE THIS EXISTS FOR, observed live: a rule meaning "pegging exists but no deadline can
    be computed" lost its scope filter during a recompile and became identical to "the expected
    rig-on date is missing" - same table, same predicate, same (absent) scope. Both reported the
    same 61 wells in one report, under two ids, and every total that summed them was wrong by
    those 61.

    REPORTED, NEVER DISABLED. Two rules converging usually means one has drifted from its
    intent, but which one is a judgement about what the business wanted - and silently switching
    a check off is exactly the surprise this engine exists to avoid.
    """
    from app.rules.contract import probe_signature

    groups: dict[str, list[str]] = {}
    for probe in catalog.probes.values():
        if probe.status != "active" or probe.source == "expanded":
            continue
        signature = probe_signature(probe.summary_sql)
        if signature:
            groups.setdefault(signature, []).append(probe.rule_id)
    return [tuple(sorted(ids)) for ids in groups.values() if len(ids) > 1]
