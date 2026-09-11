"""State passed between the detection RUN graph's nodes.

ONE STATE FOR THE WHOLE RUN, unlike the compile graph's one-state-per-rule. The two stages
have opposite shapes and this is why:

  COMPILE  decides whether each rule's SQL is correct. Rules are independent - one failing
           must not disturb another - so each gets its own state and its own budgets.
  RUN      produces ONE report about the database as a whole. The scorer has to rank rules
           against each other, and the summarizer needs the complete picture in a single call.
           A per-rule state could express neither.

WHAT IS DELIBERATELY NOT HERE: the findings themselves, beyond the Word cap. Each ProbeResult
carries at most `report_rows` rows in memory and a path to the rest on disk. A run that held
every finding of every rule would fail on precisely the database that most needed checking.
"""
from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    # ── Input ──
    run_id: str
    only: list[str]               # restrict to these rule ids; empty means every active probe
    # Formats to produce. Empty means both. Kept as data rather than two booleans so the API
    # and the CLI pass the same thing.
    formats: list[str]

    # ── Catalog loader ──
    probes: list[Any]             # CompiledProbe objects that will actually be executed
    rules: dict[str, Any]         # rule_id -> AnomalyRule, for titles, severity and prose
    catalog_stale: bool
    catalog_note: str
    # Probes that exist but will NOT run, with the reason. Carried all the way into the report:
    # a check the operator believes is running when it is not is this tool's worst failure.
    not_running: list[dict[str, str]]

    # ── Probe runner ──
    results: list[Any]            # ProbeResult, one per executed probe
    executed: int
    failed: int

    # ── Scorer (deterministic) ──
    score: float                  # 0-100, higher is cleaner
    score_basis: str              # how the number was derived, for the report to state
    totals: dict[str, Any]        # headline counts
    by_severity: dict[str, int]
    by_category: list[dict[str, Any]]
    ranked: list[dict[str, Any]]  # findings worth reporting, worst first
    # Probes that examined ZERO records. Held apart from both the clean and the flagged rules
    # because they are neither: they are coverage gaps, and reporting one as a pass would be
    # the most misleading thing this system could print.
    empty_scope: list[str]

    # ── Summarizer (the run's ONLY LLM call) ──
    summary: str
    llm_calls: int

    # ── Report builder ──
    report_paths: dict[str, str]  # "xlsx" / "docx" -> absolute path
    report_error: str

    seconds: float
