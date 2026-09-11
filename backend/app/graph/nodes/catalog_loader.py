"""Catalog Loader node (deterministic) - decides which probes this run will execute.

It answers one awkward question honestly: is the stored SQL still trustworthy?

A catalog is stale when the database STRUCTURE has moved under it. Row counts moving is not
staleness - data changes every day and the probes were written against the shape of the
tables, not their contents. That distinction is the whole reason a daily run is nearly free.

WHEN THE CATALOG IS STALE THERE ARE ONLY TWO HONEST OPTIONS, AND BOTH ARE OFFERED:
  ANOMALY_AUTO_COMPILE=true   recompile the affected rules first, then run. Correct, slower.
  ANOMALY_AUTO_COMPILE=false  refuse, and say what to run. Correct, and cheap.
Running stale SQL anyway is not among them: a probe whose column was renamed either fails
loudly or - far worse - still executes and quietly measures something else.

EVERY PROBE THAT WILL NOT RUN IS RECORDED WITH ITS REASON and carried into the report. A
failed or not-applicable check must be visible, because the alternative is an operator
believing in coverage that does not exist.
"""
from __future__ import annotations

from app.config import settings
from app.db import introspect
from app.graph.run_state import RunState
from app.observability import get_logger
from app.rules import catalog as catalog_store
from app.rules.generic import generate as generate_generic
from app.rules.loader import load_rules

log = get_logger()


def _all_rules_by_id() -> dict:
    """Every rule definition, so the report can show a title and prose beside each finding."""
    rules, errors = load_rules()
    for problem in errors:
        log.warning("load: %s", problem)
    by_id = {r.rule_id: r for r in rules}
    if settings.generic_probes:
        try:
            for rule in generate_generic():
                by_id.setdefault(rule.rule_id, rule)
        except Exception as exc:  # noqa: BLE001 - the declared rules must survive this
            log.warning("probe: generic definitions unavailable (%s)", exc)
    return by_id


def catalog_loader_node(state: RunState) -> dict:
    catalog = catalog_store.load()
    rules = _all_rules_by_id()

    if not catalog.probes:
        return {
            "probes": [], "rules": rules, "not_running": [], "catalog_stale": True,
            "catalog_note": (
                "No probes have been compiled yet. Run: python -m app.cli compile"
            ),
        }

    # Structure only - deliberately blind to row counts, so a nightly data load does not
    # invalidate SQL that is still perfectly correct.
    try:
        fingerprint = introspect.structure_fingerprint()
    except Exception as exc:  # noqa: BLE001
        log.warning("probe: structure fingerprint unavailable (%s) - assuming current", exc)
        fingerprint = ""

    stale = bool(fingerprint and catalog.structure_fingerprint and
                 fingerprint != catalog.structure_fingerprint)
    note = ""
    if stale:
        note = (
            "The database structure has changed since these probes were compiled. Recompile "
            "with: python -m app.cli compile"
        )
        log.warning("catalog: %s", note)

    only = {r.strip().lower() for r in (state.get("only") or [])}
    probes: list = []
    not_running: list[dict[str, str]] = []

    for probe in catalog.probes.values():
        if only and probe.rule_id.lower() not in only:
            continue
        if probe.status == "active":
            probes.append(probe)
            continue
        not_running.append({
            "rule_id": probe.rule_id,
            "title": getattr(rules.get(probe.rule_id), "title", ""),
            "status": probe.status,
            "reason": probe.error or "",
        })

    # Sorted so a run's log and its report list rules in the same, stable order every time.
    probes.sort(key=lambda p: p.rule_id)
    not_running.sort(key=lambda n: n["rule_id"])

    log.info(
        "catalog: %d probe(s) will run, %d will not (%s)",
        len(probes), len(not_running), catalog.counts(),
    )
    return {
        "probes": probes,
        "rules": rules,
        "not_running": not_running,
        "catalog_stale": stale,
        "catalog_note": note,
    }
