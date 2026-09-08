"""Prompt templates.

`BUSINESS_RULES.md` is loaded verbatim and included in every system prompt -- it is the
ground truth the model must reason within, not a paraphrase. The guard rules below are
restated explicitly on top of it because they are the specific failure modes documented in
this project's own history (docs/01c, docs/02 §1): an early date is good news, never a
defect; a column's meaning comes from `column_semantics.yaml`'s role, never from its name.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config import REPO_ROOT

GUARD_RULES = """
STRICT RULES -- violating any of these makes your output unusable and it will be rejected:

1. EARLY IS GOOD. If a finding shows an actual date earlier than an expected/target date,
   that is ACCELERATION, a positive outcome -- never call it a defect, a delay, or an
   anomaly. Only a LATER actual date is ever a delay.
2. NEVER INVENT A NUMBER. Every number in your explanation/root_cause/remediation MUST
   appear verbatim in the finding's own data (title, affected_count, evidence) that you
   were given. Do not compute, round, estimate, or introduce any number not already present.
3. NEVER INVENT A DATE COLUMN'S MEANING. Only use the role/baseline/authoritative
   information given to you for a column. Do not assume a column is authoritative because
   of its name (e.g. a column named "start" is not necessarily the authoritative baseline).
4. REPORT THE STORED VALUE. If data conflicts with a business rule, describe the conflict
   -- never silently resolve it or pick a side that isn't stated in the finding.
5. OWNERSHIP MATTERS. Location Construction delays are Al Tasnim's responsibility
   (penalty risk). Flowline Construction delays are PDO's responsibility, per the business
   rules -- never attribute a Flowline delay to Al Tasnim.
6. BE CONCISE AND CONCRETE. Write for a project manager, not a data analyst. No hedging
   filler ("it appears that", "it seems"). State the fact and its consequence.
"""


@lru_cache(maxsize=1)
def business_rules_text() -> str:
    path = REPO_ROOT / "docs" / "BUSINESS_RULES.md"
    if not path.exists():
        return "(BUSINESS_RULES.md not found -- proceed using only the finding's own data.)"
    return path.read_text(encoding="utf-8")


def narrate_system_prompt() -> str:
    return (
        "You are a data-quality analyst writing findings for a well-construction project "
        "report (PDO / Al Tasnim). You are given ONE finding, already computed by SQL. "
        "Explain it in plain business language.\n\n"
        f"{GUARD_RULES}\n\n"
        "BUSINESS RULES (ground truth for interpretation):\n"
        f"{business_rules_text()}"
    )


def narrate_user_prompt(finding: dict) -> str:
    return (
        "Explain this finding. Use ONLY the data below -- do not add any number, date, "
        "or fact not present here.\n\n"
        f"check_id: {finding.get('check_id')}\n"
        f"family: {finding.get('family')}\n"
        f"severity: {finding.get('severity')}\n"
        f"finding_class: {finding.get('finding_class')}\n"
        f"title: {finding.get('title')}\n"
        f"affected_count: {finding.get('affected_count')}\n"
        f"grain: {finding.get('grain')}\n"
        f"baseline: {finding.get('baseline')}\n"
        f"business_rule_ref: {finding.get('business_rule_ref')}\n"
        f"owner: {finding.get('owner')}\n"
        f"why_it_matters (from the check itself): {finding.get('why_it_matters')}\n"
        f"evidence: {finding.get('evidence')}\n"
    )


def correlate_system_prompt() -> str:
    return (
        "You are correlating data-quality findings from a well-construction project into "
        "INCIDENTS -- groups of findings that share one underlying root cause. Only group "
        "findings where the connection is clear and specific; a large group of unrelated "
        "findings under a vague title is worse than no grouping at all. Most findings will "
        "not belong to any incident -- that is expected and correct.\n\n"
        f"{GUARD_RULES}\n\n"
        "BUSINESS RULES (ground truth for interpretation):\n"
        f"{business_rules_text()}"
    )


def correlate_user_prompt(findings: list[dict]) -> str:
    lines = [
        f"- {f.get('check_id')} [{f.get('severity')}/{f.get('finding_class')}] "
        f"{f.get('title')} (affected={f.get('affected_count')})"
        for f in findings
    ]
    return (
        "Group these findings into incidents where a shared root cause is genuinely "
        "identifiable. Reference each finding only by its check_id.\n\n" + "\n".join(lines)
    )


def summary_system_prompt() -> str:
    return (
        "You are writing the executive summary of a data-quality report for a well-"
        "construction project (PDO / Al Tasnim), read by a project manager who has limited "
        "time. Lead with the single most important fact.\n\n"
        f"{GUARD_RULES}\n\n"
        "BUSINESS RULES (ground truth for interpretation):\n"
        f"{business_rules_text()}"
    )


def suggest_system_prompt(existing_check_titles: list[str]) -> str:
    return (
        "You are a data-quality analyst exploring a well-construction project database "
        "(PDO / Al Tasnim) to find NEW anomaly-detection ideas that no existing check "
        "already covers. You have three exploration tools (list_tables, describe_table, "
        "run_query) and one conclusion tool (propose_check).\n\n"
        "PROCESS: explore with the tools, form a hypothesis, then use run_query to VERIFY "
        "it actually finds something in the real data before proposing it. Never propose "
        "a check you have not verified with run_query. Prefer a small number of "
        "well-verified proposals over many speculative ones -- 2-4 strong proposals beat "
        "10 guesses.\n\n"
        "IF run_query FAILS: read the error and change the query -- a trailing semicolon "
        "is a common cause. NEVER resubmit the exact same query text after it fails; that "
        "wastes your limited number of tool calls with no chance of a different result.\n\n"
        "DO NOT propose anything close to a check that already exists:\n"
        + "\n".join(f"- {t}" for t in existing_check_titles[:120])
        + f"\n{'(list truncated)' if len(existing_check_titles) > 120 else ''}\n\n"
        f"{GUARD_RULES}\n\n"
        "BUSINESS RULES (ground truth for interpretation):\n"
        f"{business_rules_text()}"
    )


def suggest_user_prompt() -> str:
    return (
        "Explore the database and propose 2-4 NEW, verified candidate checks for anomalies "
        "not already covered. Stop once your tool budget is running low and you have at "
        "least one solid, verified proposal -- do not keep exploring past that point."
    )


def summary_user_prompt(
    severity_counts: dict, class_counts: dict, top_findings: list[dict], incidents: list[dict]
) -> str:
    return (
        f"Severity counts: {severity_counts}\n"
        f"Finding-class counts: {class_counts}\n\n"
        "Top findings by severity:\n"
        + "\n".join(
            f"- {f.get('check_id')} [{f.get('severity')}] {f.get('title')} "
            f"(affected={f.get('affected_count')})"
            for f in top_findings
        )
        + "\n\nIncidents identified:\n"
        + "\n".join(f"- {i.get('title')}: {i.get('root_cause')}" for i in incidents)
    )
