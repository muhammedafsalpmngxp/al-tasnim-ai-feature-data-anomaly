"""Scorer node (deterministic) - ranks the findings and computes the data-quality score.

NO MODEL DECIDES HOW BAD THE DATA IS. That is the entire point of this node existing
separately from the Summarizer. A score that drifts because a model was in a different mood is
not a metric - it cannot be tracked week to week, and the first time it moves without the data
moving, nobody trusts it again. The summary PROSE is written by a model; every NUMBER it is
given was computed here, arithmetically, from what the probes returned.

THE SCORE, AND WHY IT IS SHAPED THIS WAY
-----------------------------------------
A severity-weighted MEAN of how clean each check came back:

    share(rule)  = anomaly_count / scope_total          0 when the check is clean
    score        = 100 * (1 - SUM(weight * share) / SUM(weight))

over every rule that actually examined records.

IT IS A WEIGHTED MEAN, NOT A SUM OF PENALTIES, AND THAT MATTERS.
The first version subtracted a penalty per rule from 100. On this database that produced a
score of 0.0 - seven tables are over 90% duplicated, and those alone exceeded a hundred points.
A score pinned at zero is worse than useless: it cannot move. Fix half the problems and it
still reads 0, so nobody can tell whether the work helped, and the number stops being looked
at. A mean is bounded by construction, so every improvement to any rule moves it, always.

Three further properties are deliberate:

  * it uses the SHARE, not the count. A rule finding 900 bad rows out of 900,000 is a 0.1%
    problem; one finding 9 out of 10 is a catastrophe. Ranking by raw count would put them the
    wrong way round and send people to work on the wrong thing.
  * severity WEIGHTS, so a critical rule cannot be diluted by a hundred low ones.
  * a probe that examined nothing is excluded from BOTH sides of the fraction - it is not
    evidence of health and must not dilute the denominator either. It is reported as a coverage
    gap, which is a different and more urgent problem than a finding.

The score is a TREND INDICATOR, and the findings table beside it is the detail. Read alone it
will understate a small number of severe problems among many healthy checks, which is exactly
why the report never shows it alone.

The formula is printed in the report next to the number. A score whose derivation is not
published is a number people quote without understanding, and this one is arithmetic anybody
can re-do from the table beside it.
"""
from __future__ import annotations

from app.graph.run_state import RunState
from app.observability import get_logger
from app.rules.spec import SEVERITIES, SEVERITY_RANK

log = get_logger()

# How much one rule's anomaly share costs the score, per severity. Ratios matter more than the
# absolute values: a critical finding weighs eight times a low one.
SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 40.0,
    "high": 20.0,
    "medium": 8.0,
    "low": 5.0,
}
_DEFAULT_WEIGHT = 8.0

SCORE_BASIS = (
    "Score = 100 x (1 - the severity-weighted average share of examined records that were "
    "anomalous), over every check that examined at least one record. Severity weights: "
    "critical 40, high 20, medium 8, low 5. Checks that examined no records are excluded "
    "entirely and reported separately as coverage gaps. The score is a trend indicator - read "
    "it alongside the findings table, which carries the severity of individual problems."
)


def _severity_of(rule) -> str:
    return getattr(rule, "severity", "medium") or "medium"


def scorer_node(state: RunState) -> dict:
    results = state.get("results") or []
    rules = state.get("rules") or {}

    # Both sides of the weighted mean. A clean rule contributes 0 to the numerator and its full
    # weight to the denominator - which is what makes a clean check actually raise the score,
    # rather than merely not lowering it.
    weighted_share = 0.0
    total_weight = 0.0
    by_severity: dict[str, int] = {s: 0 for s in SEVERITIES}
    by_category: dict[str, dict] = {}
    ranked: list[dict] = []

    examined = 0
    flagged = 0
    clean_probes = 0
    empty_scope: list[str] = []

    for result in results:
        rule = rules.get(result.rule_id)
        severity = _severity_of(rule)
        category = getattr(rule, "category", "Uncategorised") or "Uncategorised"
        title = getattr(rule, "title", result.rule_id)

        if not result.ok:
            continue

        examined += result.scope_total
        flagged += result.anomaly_count

        if result.scope_total <= 0:
            # Excluded from BOTH sides of the fraction. A probe that examined nothing has
            # proved nothing: letting it improve the score would reward a broken check, and
            # letting it into the denominator would dilute the real findings.
            empty_scope.append(result.rule_id)
            continue

        weight = SEVERITY_WEIGHT.get(severity, _DEFAULT_WEIGHT)
        share = result.anomaly_count / result.scope_total
        # Every examined rule joins the denominator, clean or not - that is what a mean means.
        total_weight += weight
        weighted_share += weight * share

        if result.anomaly_count <= 0:
            clean_probes += 1
            continue

        contribution = weight * share
        by_severity[severity] = by_severity.get(severity, 0) + 1

        bucket = by_category.setdefault(
            category, {"category": category, "rules": 0, "anomalies": 0}
        )
        bucket["rules"] += 1
        bucket["anomalies"] += result.anomaly_count

        ranked.append({
            "rule_id": result.rule_id,
            "title": title,
            "category": category,
            "severity": severity,
            "scope_total": result.scope_total,
            "anomaly_count": result.anomaly_count,
            "anomaly_pct": result.anomaly_pct or round(100.0 * share, 4),
            "share": share,
            "contribution": round(contribution, 4),
            "detail_total": result.detail_total,
            "concerns": list(result.concerns),
        })

    # Worst first: severity outranks size, because a critical rule affecting a few records
    # usually needs attention before a low one affecting many. Within a severity, by share.
    ranked.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), -r["share"]))

    # Bounded by construction: weighted_share cannot exceed total_weight, because every share
    # is at most 1. The clamp is belt and braces against a probe reporting more anomalies than
    # it examined - which sanity_concerns already flags as impossible, but which must not be
    # allowed to produce a negative score in the meantime.
    mean_share = (weighted_share / total_weight) if total_weight else 0.0
    score = max(0.0, min(100.0, 100.0 * (1.0 - mean_share)))
    totals = {
        "probes_run": len(results),
        "probes_failed": sum(1 for r in results if not r.ok),
        "probes_clean": clean_probes,
        "probes_with_findings": len(ranked),
        "probes_empty_scope": len(empty_scope),
        "records_examined": examined,
        "records_flagged": flagged,
        "not_running": len(state.get("not_running") or []),
    }

    log.info(
        "score: %.1f/100 - %d rule(s) with findings, %s record(s) flagged of %s examined",
        score, len(ranked), f"{flagged:,}", f"{examined:,}",
    )
    if empty_scope:
        log.warning(
            "score: %d probe(s) examined NO records and are coverage gaps, not clean results: %s",
            len(empty_scope), ", ".join(empty_scope[:10]),
        )

    return {
        "score": round(score, 1),
        "score_basis": SCORE_BASIS,
        "totals": totals,
        "by_severity": by_severity,
        "by_category": sorted(
            by_category.values(), key=lambda c: -c["anomalies"]
        ),
        "ranked": ranked,
        "empty_scope": empty_scope,
    }
