"""Drive the compile stage: every rule, one at a time, through the compile graph.

ONE GRAPH INVOCATION PER RULE, NOT ONE FOR ALL OF THEM. That is the decision this file exists
to implement, and it buys three things:

  * a rule that fails fails ALONE. It is recorded with its error and the compile carries on.
    For a tool whose entire job is reporting problems honestly, a single bad rule aborting the
    run would be self-defeating;
  * each rule gets its own retry budgets, so one pathological rule cannot spend what the next
    twenty need;
  * the work is resumable. Anything already compiled and still valid is skipped, so a compile
    interrupted half way costs only the rules it had not reached.

THE CIRCUIT BREAKER IS CHECKED HERE, BETWEEN RULES, NOT INSIDE THE GRAPH. A budget enforced
mid-rule would abandon probes in a half-compiled state; enforced between them, every rule is
either finished or untouched.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.config import settings
from app.db import introspect
from app.graph.build import build_compile_graph
from app.llm import get_usage_report, start_usage_tracking
from app.observability import get_logger
from app.rules import catalog as catalog_store
from app.rules.expand import (
    PLACEHOLDERS,
    expand_families,
    substitute,
    unfilled_tokens,
)
from app.rules.generic import coverage_summary, generate as generate_generic
from app.rules.loader import load_patterns, load_rules
from app.rules.spec import AnomalyRule, CompiledProbe

log = get_logger()

# LangGraph stops a graph that takes more steps than this, as a loop guard. The budgets already
# bound the work - at most (max_sql_retries + verify_retries + 1) author attempts of six nodes
# each - but the default of 25 sits uncomfortably close to that, and being stopped by the
# framework produces a far less useful error than being stopped by our own budget.
_RECURSION_LIMIT = 60


@dataclass
class CompileReport:
    """What one compile did, for the CLI, the API and the log to render."""

    compiled: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    llm_calls: int = 0
    seconds: float = 0.0
    stopped_early: str = ""
    usage: list[dict] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return len(self.compiled) + len(self.failed) + len(self.not_applicable)


def _all_rules() -> tuple[list[AnomalyRule], list[str]]:
    """Declared rules first, then the generated structural probes.

    Order matters for id collisions: a declared rule always wins, because a person wrote it
    deliberately and a generated id colliding with it is an accident of naming.
    """
    rules, errors = load_rules()
    # A family rule is a description applied to every matching feature in the schema, so it is
    # replaced here by one concrete rule per feature. Done at LOAD time, before anything else
    # sees the list: the catalog, the pruning of probes whose rule has gone, and the API's rule
    # list all work from "the set of rules that exist", and expanding later would leave dozens
    # of probes that no rule accounts for.
    rules, notes = expand_families(rules)
    errors += notes
    if settings.generic_probes:
        try:
            declared = {r.rule_id for r in rules}
            rules += [r for r in generate_generic() if r.rule_id not in declared]
        except FileNotFoundError as exc:
            errors.append(
                f"generic probes could not be generated: {exc}. Run: python -m app.cli introspect"
            )
        except Exception as exc:  # noqa: BLE001 - never lose the declared rules over these
            errors.append(f"generic probes could not be generated: {exc}")
    return rules, errors


def _seed_state(
    rule: AnomalyRule,
    fingerprint: str,
    schema: str,
    values: str,
    numbers: str,
    patterns: str,
    coverage: str,
    table_signatures: dict[str, str],
) -> dict:
    return {
        # Read once per compile, not once per rule: it is a metadata query over the whole
        # database, and the catalog writer needs it for every probe it stores.
        "table_signatures": table_signatures,
        "rule_id": rule.rule_id,
        "title": rule.title,
        "body": rule.body,
        "category": rule.category,
        "severity": rule.severity,
        "entity": rule.entity,
        "method": rule.method,
        "sql_mode": rule.sql_mode,
        "source": rule.source,
        "tolerance": rule.params.get("tolerance", ""),
        "params": dict(rule.params),
        # Set only for a rule produced by expanding a family. The author then writes its query
        # with these tokens in place of literal names, so the one query can serve every feature.
        "expands_over": rule.expands_over,
        "placeholders": PLACEHOLDERS.get(rule.expands_over, ()) if rule.is_expanded else (),
        "summary_sql_template": "",
        "detail_sql_template": "",
        "rule_hash": rule.rule_hash,
        "structure_fingerprint": fingerprint,
        "seed_summary_sql": rule.summary_sql,
        "seed_detail_sql": rule.detail_sql,
        "schema": schema,
        "value_hints": values,
        "numeric_hints": numbers,
        "patterns": patterns,
        # A generic probe is never shown the coverage list: it IS the coverage, and telling it
        # not to re-express itself is both meaningless and a waste of tokens.
        "coverage": coverage if rule.source != "generic" else "",
        # Defaults, so a node reading one of these before it is set never sees a missing key.
        "schema_block": schema,
        "hint_block": "",
        "tables": [],
        "tried_sql": [],
        "concerns": [],
        "advice": [],
        "retry_count": 0,
        "verify_retry_count": 0,
        "feedback_history": [],
        "llm_calls": 0,
        "applicable": True,
    }


def _instantiate(
    rule: AnomalyRule,
    template: tuple[str, str] | None,
    fingerprint: str,
    signatures: dict[str, str],
) -> CompiledProbe:
    """Build one family member's probe from the query authored for its family.

    No model call and no database round trip: the template was already executed and reviewed
    against a real feature, and every member differs only in the names substituted into it.

    The substituted SQL is CHECKED for leftover tokens. A template that used a literal table
    name where a token belonged would otherwise be cloned across the whole family, and every
    copy would silently measure the one table the author happened to be looking at.
    """
    if not template or not template[0].strip() or not template[1].strip():
        return _failed_probe(
            rule, fingerprint,
            "the query for this rule family could not be authored, so this member of it was "
            "never built",
        )

    summary_sql = substitute(template[0], rule.params)
    detail_sql = substitute(template[1], rule.params)
    leftover = unfilled_tokens(summary_sql) + unfilled_tokens(detail_sql)
    if leftover:
        return _failed_probe(
            rule, fingerprint,
            "the family's query still contains " + ", ".join(sorted(set(leftover)))
            + " after substitution - it names a token this feature does not supply",
        )

    tables = tuple(dict.fromkeys(
        value for key, value in rule.params.items() if key.endswith("table") and value
    ))
    return CompiledProbe(
        rule_id=rule.rule_id,
        summary_sql=summary_sql,
        detail_sql=detail_sql,
        status="active",
        source=rule.source,
        rule_hash=rule.rule_hash,
        structure_fingerprint=fingerprint,
        table_fingerprint=introspect.probe_fingerprint(tables, signatures),
        compiled_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        tables=tables,
        grounding_note=(
            f"built from the query authored for {rule.family_id}, with this feature's names "
            "substituted in"
        ),
    )


def _failed_probe(rule: AnomalyRule, fingerprint: str, error: str) -> CompiledProbe:
    """Record a rule that never reached the graph, so it is still visible as not running."""
    return CompiledProbe(
        rule_id=rule.rule_id,
        summary_sql="",
        detail_sql="",
        status="failed",
        source=rule.source,
        rule_hash=rule.rule_hash,
        structure_fingerprint=fingerprint,
        compiled_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        error=error,
    )


def compile_rules(
    only: list[str] | None = None,
    force: bool = False,
    retry_failed: bool = False,
    source: str | None = None,
    progress=None,
) -> tuple[catalog_store.Catalog, CompileReport, list[str]]:
    """Compile every rule that needs it. Returns (catalog, report, rule-file errors).

    `only` restricts the run to specific rule ids; `force` recompiles regardless of staleness.
    `progress` is called as progress(done, total, rule_id) for a CLI bar or an SSE stream.

    `source` restricts to "declared" or "generic", which separates the two kinds of work by
    what actually invalidates them. A generic probe is rendered from the schema and owes
    nothing to the domain markdown, so it can be compiled - for zero LLM calls - while the
    business rules are still being written. Compiling the two together would mean recompiling
    all of them every time a sentence of prose changed.
    """
    import time

    started = time.perf_counter()
    report = CompileReport()
    start_usage_tracking()

    rules, errors = _all_rules()
    for problem in errors:
        log.warning("load: %s", problem)

    if source:
        rules = [r for r in rules if r.source == source]
        log.info("compile: restricted to %s rule(s) - %d match", source, len(rules))
    if only:
        wanted = {r.strip().lower() for r in only}
        rules = [r for r in rules if r.rule_id.lower() in wanted]
        if not rules:
            log.warning("compile: no rule matched %s", ", ".join(sorted(wanted)))

    runnable = [r for r in rules if r.runnable]
    skipped = len(rules) - len(runnable)
    if skipped:
        log.info("compile: %d rule(s) are draft or disabled and were not compiled", skipped)

    # The structure-only fingerprint. Its whole purpose is to NOT move when data is reloaded,
    # so a daily refresh does not trigger a full-cost recompile.
    try:
        fingerprint = introspect.structure_fingerprint()
    except Exception as exc:  # noqa: BLE001
        # Without it nothing can be judged stale, so the safe reading is "everything is", which
        # is expensive and silent. Say so loudly and reuse what is there instead.
        log.warning(
            "compile: the structure fingerprint is unavailable (%s) - compiled probes cannot "
            "be checked for staleness, so only uncompiled rules will be built", exc,
        )
        fingerprint = ""

    # Per-table structure hashes, so a probe records what its OWN tables looked like when it
    # was written. Unavailable is not fatal: staleness falls back to the whole-database
    # fingerprint, which is blunter but never wrong in the dangerous direction.
    try:
        signatures = introspect.table_signatures()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "compile: per-table signatures unavailable (%s) - staleness will be judged over the "
            "whole database, so unrelated changes will force recompiles", exc,
        )
        signatures = {}

    schema = introspect.build_schema_text()
    values = introspect.build_value_hints()
    numbers = introspect.build_numeric_hints()
    patterns = load_patterns()

    catalog = catalog_store.load()
    coverage = ""
    if settings.generic_probes:
        coverage = coverage_summary([r for r in rules if r.source == "generic"])

    # Only prune on a FULL compile. On ANY filtered run - by id or by source - the other rules
    # were never considered, so pruning against this shortened list would delete working probes
    # merely because they were not asked for. Compiling only the generic probes would otherwise
    # wipe every declared one, which is the most expensive thing this function could do.
    if not only and not source:
        report.removed = catalog_store.prune_removed(catalog, {r.rule_id for r in rules})

    graph = build_compile_graph()
    total = len(runnable)
    log.info(
        "compile: %d runnable rule(s) - %d declared, %d generic",
        total,
        sum(1 for r in runnable if r.source == "declared"),
        sum(1 for r in runnable if r.source == "generic"),
    )

    # ONE AUTHORED QUERY PER FAMILY, not per feature. A family's members differ only in which
    # table and columns they name, so writing thirty-six near-identical queries would spend
    # thirty-six times the tokens to get the same query back, with thirty-six chances of a
    # different mistake in each. One member is authored and reviewed against real data; the
    # rest reuse its template with their own values substituted, for no further calls.
    #
    # Decided for the family as a WHOLE: if any member needs rebuilding, the template is
    # re-authored and every member re-instantiated from it. Re-substituting a member that was
    # already current costs nothing and keeps a family from drifting into two versions of the
    # same query.
    authors: dict[str, str] = {}        # family_id -> the member that will be authored
    templates: dict[str, tuple[str, str]] = {}
    for rule in runnable:
        if not rule.is_expanded:
            continue
        authors.setdefault(rule.family_id, rule.rule_id)
    if not force:
        current = {
            family for family, first in authors.items()
            if not any(
                catalog_store.is_stale(
                    catalog.get(r.rule_id), r, fingerprint, retry_failed, signatures
                )[0]
                for r in runnable if r.family_id == family
            )
        }
        authors = {f: r for f, r in authors.items() if f not in current}

    for index, rule in enumerate(runnable, 1):
        if progress is not None:
            progress(index - 1, total, rule.rule_id)

        # A member of a family being re-authored this run: wait for its template, then clone.
        if rule.is_expanded and rule.family_id in authors:
            if authors[rule.family_id] != rule.rule_id:
                probe = _instantiate(rule, templates.get(rule.family_id), fingerprint, signatures)
                catalog.probes[rule.rule_id] = probe
                (report.compiled if probe.status == "active" else report.failed).append(
                    rule.rule_id
                )
                continue
            # else: this IS the member being authored - fall through to the graph below.
        else:
            existing = catalog.get(rule.rule_id)
            if not force:
                stale, why = catalog_store.is_stale(
                    existing, rule, fingerprint, retry_failed, signatures
                )
                if not stale and existing is not None:
                    report.reused.append(rule.rule_id)
                    continue
                if why:
                    log.info("compile: %s needs rebuilding - %s", rule.rule_id, why)

        # Checked BETWEEN rules so every rule is either finished or untouched. A generic probe
        # costs no calls, so it is never blocked by a budget it cannot spend.
        needs_llm = rule.source != "generic"
        if needs_llm and report.llm_calls >= settings.max_compile_calls:
            report.stopped_early = (
                f"the compile budget of {settings.max_compile_calls} LLM calls was reached; "
                f"{total - index + 1} rule(s) were not compiled"
            )
            log.warning("compile: %s", report.stopped_early)
            break

        state = _seed_state(
            rule, fingerprint, schema, values, numbers, patterns, coverage, signatures
        )
        try:
            final = graph.invoke(state, config={"recursion_limit": _RECURSION_LIMIT})
        except Exception as exc:  # noqa: BLE001 - one rule must never abort the compile
            log.warning("compile: %s raised %s: %s", rule.rule_id, type(exc).__name__, exc)
            catalog.probes[rule.rule_id] = _failed_probe(
                rule, fingerprint, f"the compile graph raised {type(exc).__name__}: {exc}"
            )
            report.failed.append(rule.rule_id)
            continue

        # Keep the token form so the rest of the family can be built from it without a call.
        # Only when the probe itself was accepted: cloning a template that failed review would
        # multiply one bad query across every feature it applies to.
        if rule.is_expanded and final.get("summary_sql_template"):
            candidate = final.get("probe")
            if candidate is not None and candidate.status == "active":
                templates[rule.family_id] = (
                    final["summary_sql_template"], final.get("detail_sql_template", "")
                )

        probe = final.get("probe")
        if probe is None:
            catalog.probes[rule.rule_id] = _failed_probe(
                rule, fingerprint, "the compile finished without producing a probe"
            )
            report.failed.append(rule.rule_id)
            continue

        catalog.probes[rule.rule_id] = probe
        report.llm_calls += probe.llm_calls
        if probe.status == "active":
            report.compiled.append(rule.rule_id)
        elif probe.status == "not_applicable":
            report.not_applicable.append(rule.rule_id)
        else:
            report.failed.append(rule.rule_id)

    if progress is not None:
        progress(total, total, "")

    catalog.structure_fingerprint = fingerprint
    catalog.compiled_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    catalog_store.save(catalog)

    report.seconds = time.perf_counter() - started
    report.usage = get_usage_report()
    log.info(
        "compile: done in %.1fs - %d compiled, %d reused, %d failed, %d not applicable, "
        "%d LLM call(s)",
        report.seconds, len(report.compiled), len(report.reused), len(report.failed),
        len(report.not_applicable), report.llm_calls,
    )
    return catalog, report, errors
