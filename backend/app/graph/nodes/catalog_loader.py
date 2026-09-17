"""Catalog Loader node (deterministic) - decides which probes this run will execute.

It answers one awkward question honestly: is the stored SQL still trustworthy?

A probe is stale when the STRUCTURE IT READS has moved under it. Row counts moving is not
staleness - data changes every day and the probes were written against the shape of the
tables, not their contents. That distinction is the whole reason a daily run is nearly free.

Judged PER PROBE, against its own tables. The whole-database fingerprint is still the
fallback for an entry compiled before per-table hashes existed, but on its own it is far too
blunt to act on: one column added anywhere marks every probe stale, so a node that refused to
run stale SQL would refuse the entire run, and one that recompiled would recompile all of it.

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
from app.rules.expand import expand_families
from app.rules.loader import load_rules

log = get_logger()


def _all_rules_by_id() -> dict:
    """Every rule definition, so the report can show a title and prose beside each finding."""
    rules, errors = load_rules()
    # Families are expanded here for the same reason the compiler expands them: a family rule
    # is a description, and what actually ran was one concrete rule per schema feature. Without
    # this the report has no title or prose for any of them and falls back to the bare id.
    rules, notes = expand_families(rules)
    for problem in errors + notes:
        log.warning("load: %s", problem)
    return {r.rule_id: r for r in rules}


def _recompile(rule_ids: list[str], note: str) -> tuple[dict[str, str], str]:
    """Rebuild exactly the outdated probes, then report what is still not runnable.

    Imported inside the function on purpose: app.compiler imports the compile graph, and the
    compile graph imports this package. A module-level import would be a cycle.

    Only the named rules are compiled. A full recompile here would be minutes of LLM calls in
    the middle of what the operator asked to be a detection run, to rebuild probes that were
    already correct.
    """
    from app.compiler import compile_rules

    log.warning("catalog: recompiling %d outdated probe(s) before running", len(rule_ids))
    try:
        catalog, report, _errors = compile_rules(only=rule_ids)
    except Exception as exc:  # noqa: BLE001 - a failed recompile must not lose the whole run
        log.warning("catalog: the automatic recompile failed (%s)", exc)
        return (
            {rule_id: f"it is out of date and the automatic recompile failed: {exc}"
             for rule_id in rule_ids},
            note + f" The automatic recompile failed ({exc}), so they were not run.",
        )

    # Whatever did not come back active still must not run, and must still be visible.
    still_bad = {
        rule_id: (
            (catalog.get(rule_id).error if catalog.get(rule_id) else "")
            or "it could not be recompiled against the current schema"
        )
        for rule_id in rule_ids
        if not (catalog.get(rule_id) and catalog.get(rule_id).status == "active")
    }
    rebuilt = len(rule_ids) - len(still_bad)
    note += (
        f" {rebuilt} were rebuilt automatically before this run"
        f" ({report.llm_calls} LLM call(s), {report.seconds:.0f}s)."
    )
    if still_bad:
        note += f" {len(still_bad)} could not be rebuilt and did not run."
    log.warning("catalog: %s", note)
    return still_bad, note


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
    try:
        signatures = introspect.table_signatures()
    except Exception as exc:  # noqa: BLE001
        log.warning("probe: per-table signatures unavailable (%s)", exc)
        signatures = {}

    only = {r.strip().lower() for r in (state.get("only") or [])}
    selected = [
        p for p in catalog.probes.values()
        if not only or p.rule_id.lower() in only
    ]

    # Judged per probe, against the tables it actually reads. A column added to a table this
    # probe never touches is not a reason to distrust - or recompile - its SQL.
    outdated: dict[str, str] = {}
    for probe in selected:
        # Staleness is only worth deciding for a probe that would otherwise run. A probe blocked
        # by its own status or by a switched-off rule is not going to execute either way, and
        # counting it here would push `outdated` past ANOMALY_AUTO_COMPILE_MAX_RULES and stop
        # the whole run over checks nobody asked for.
        if catalog_store.runnable_reason(probe, rules.get(probe.rule_id)):
            continue
        moved, why = catalog_store.structure_moved(probe, fingerprint, signatures)
        if moved:
            outdated[probe.rule_id] = why

    stale = bool(outdated)
    note = ""
    if stale:
        note = (
            f"{len(outdated)} of {len(selected)} compiled probe(s) no longer match the database "
            "structure they were written against."
        )
        log.warning("catalog: %s", note)
        # The choice this node exists to make. Running SQL whose tables have moved is not among
        # the options: it either fails loudly or - far worse - still executes and quietly
        # measures something else, and a report cannot tell you which happened.
        too_many = len(outdated) > settings.auto_compile_max_rules
        if settings.auto_compile and not too_many:
            outdated, note = _recompile(sorted(outdated), note)
            # RECOMPUTED, NOT LEFT AS IT WAS. `stale` was decided before the rebuild and never
            # revisited, so a staleness the run had already FIXED was still reported as
            # outstanding - the report printed "WARNING" over a note whose own text said the
            # probes had been rebuilt. `outdated` now holds only what could not be rebuilt, so
            # it is the honest answer to "is anything still stale?".
            stale = bool(outdated)
            catalog = catalog_store.load()
            rules = _all_rules_by_id()
            selected = [
                p for p in catalog.probes.values()
                if not only or p.rule_id.lower() in only
            ]
        else:
            note += (
                " They were NOT run, because their stored SQL can no longer be trusted against "
                "this schema. Recompile with: python -m app.cli compile"
            )
            if too_many:
                # A whole-schema change, not drift. Rebuilding it is a deliberate, budgeted
                # operation, not something to start inside a run the operator is waiting on.
                note += (
                    f" (Too many to rebuild automatically - {len(outdated)} exceeds "
                    f"ANOMALY_AUTO_COMPILE_MAX_RULES={settings.auto_compile_max_rules}, which "
                    "usually means the schema changed broadly rather than drifted.)"
                )
            else:
                note += "  (or set ANOMALY_AUTO_COMPILE=true to rebuild them before each run)."
            log.warning("catalog: refusing to run stale probes - %s", note)

    probes: list = []
    not_running: list[dict[str, str]] = []

    for probe in selected:
        # The rule's status is consulted as well as the probe's - see catalog.runnable_reason.
        # Checking only the probe meant a rule switched off in the markdown kept running.
        blocked = catalog_store.runnable_reason(probe, rules.get(probe.rule_id))
        if not blocked and probe.rule_id not in outdated:
            probes.append(probe)
            continue
        rule = rules.get(probe.rule_id)
        if probe.rule_id in outdated:
            status = "stale"
        elif rule is not None and not rule.runnable:
            status = rule.status
        else:
            status = probe.status
        not_running.append({
            "rule_id": probe.rule_id,
            "title": getattr(rule, "title", ""),
            "status": status,
            "reason": outdated.get(probe.rule_id) or blocked,
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
