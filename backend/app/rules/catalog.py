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


def is_stale(
    probe: CompiledProbe | None,
    rule: AnomalyRule,
    structure_fingerprint: str,
    retry_failed: bool = False,
) -> tuple[bool, str]:
    """(needs recompiling, why). The why is logged, so a recompile is never mysterious."""
    if probe is None:
        return True, "not compiled yet"
    if probe.rule_hash != rule.rule_hash:
        return True, "the rule definition changed"
    if structure_fingerprint and probe.structure_fingerprint != structure_fingerprint:
        return True, "the database structure changed"
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


def prune_removed(catalog: Catalog, rule_ids: set[str]) -> list[str]:
    """Drop probes whose rule no longer exists, returning what was removed.

    A rule deleted from the markdown, or a generic probe whose schema feature is gone, must not
    keep running: it would report findings against a definition nobody can look up any more.
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
