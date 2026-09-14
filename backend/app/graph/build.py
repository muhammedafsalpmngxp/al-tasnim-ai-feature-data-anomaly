"""Assemble the COMPILE graph - the state machine that turns one rule into a stored probe.

    rule_loader -> grounding -> sql_author -> validator -> executor -> sanity_gate -> verifier
                                    ^            |           |            |             |
                                    +------------+-----------+------------+-------------+
                                         (rewrite, while there is budget)
                                                                                        |
                                                                          catalog_writer <
EVERY RETRY DECISION LIVES IN THIS FILE, NOT IN A NODE.
A node reports what it found; the routing decides whether there is budget to act on it. That
is why the Validator never checks a counter and the Verifier never asks whether it is allowed
to reject: one place governs every reason a probe gets rewritten, so the budgets cannot be
quietly overridden in a node that happens to know better.

TWO BUDGETS, NEVER ONE.
  max_sql_retries  MECHANICAL faults - rejected by the safety gate, a database error, a broken
                   column contract. The fix is obvious and local.
  verify_retries   SEMANTIC rejections - the probe works but measures the wrong thing.
Sharing one counter lets a couple of syntax errors early in a rule leave the reviewer with no
rewrites: it rejects, and is overruled in the same breath. That is the worst of both.

THREE WAYS IN, AND THEY ARE THE COST MODEL.
  expanded  one member of a structural family. Its tables come from the schema feature it was
            expanded from, so grounding is skipped - but it is written and reviewed like any
            other rule, and the query is then reused across the whole family for no further
            calls. Two to four calls PER FAMILY, not per probe.
  pinned    hand-written SQL. The author is skipped; the reviewer still runs, because a person
            can be wrong about a live schema too. One LLM call.
  seed /    grounded, written and reviewed. Three to four calls.
  authored
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.graph.nodes.catalog_writer import catalog_writer_node
from app.graph.nodes.executor import executor_node
from app.graph.nodes.grounding import grounding_node
from app.graph.nodes.rule_loader import rule_loader_node
from app.graph.nodes.sanity_gate import sanity_gate_node
from app.graph.nodes.sql_author import sql_author_node
from app.graph.nodes.validator import validator_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import CompileState


def _mechanical_budget_left(state: CompileState) -> bool:
    return state.get("retry_count", 0) <= settings.max_sql_retries


def _repeated_attempt(state: CompileState) -> bool:
    """True when the author re-emitted a probe it has already tried, after a rejection.

    Identical SQL returns identical data, so re-running and re-reviewing it cannot produce a
    different verdict - it only spends a database round trip and a reasoning-tier call.

    Gated on verify_retry_count deliberately: a repeat following a TRANSIENT database error (a
    deadlock, a dropped connection) is a legitimate retry that may well succeed on the second
    attempt, and short-circuiting that would turn a blip into a failed rule.
    """
    if state.get("verify_retry_count", 0) < 1:
        return False
    tried = state.get("tried_sql") or []
    return len(tried) > 1 and tried[-1] in tried[:-1]


def _route_after_loader(state: CompileState) -> str:
    # An EXPANDED rule skips grounding: the schema feature it was expanded from already names
    # its tables exactly, so grounding could only guess at what is certain. Its SQL still has
    # to be WRITTEN, so it goes straight to the author - and it is reviewed afterwards like any
    # other authored query, because a model wrote it.
    #
    # PINNED rules do NOT skip grounding, even though their SQL is final. The reviewer has to be
    # shown the tables the query actually reads, and without grounding it would be reviewing
    # against the whole database - which on a wide schema is the same as reviewing against
    # nothing.
    if state.get("source") == "expanded":
        return "sql_author"
    return "grounding"


def _route_after_grounding(state: CompileState) -> str:
    # A rule this database cannot express is retired here, before any expensive call.
    if state.get("applicable") is False:
        return "catalog_writer"
    if state.get("sql_mode") == "pinned":
        return "validator"
    return "sql_author"


def _route_after_validator(state: CompileState) -> str:
    if state.get("validation_error"):
        if _rewritable(state) and _mechanical_budget_left(state):
            return "sql_author"
        return "catalog_writer"
    return "executor"


def _route_after_executor(state: CompileState) -> str:
    if state.get("exec_error"):
        if _rewritable(state) and _mechanical_budget_left(state):
            return "sql_author"
        return "catalog_writer"
    return "sanity_gate"


def _route_after_sanity(state: CompileState) -> str:
    if state.get("contract_error"):
        if _rewritable(state) and _mechanical_budget_left(state):
            return "sql_author"
        return "catalog_writer"
    return "verifier"


def _route_after_verifier(state: CompileState) -> str:
    if state.get("verify_ok"):
        return "catalog_writer"
    if not _rewritable(state):
        return "catalog_writer"
    if state.get("verify_retry_count", 0) > settings.verify_retries:
        return "catalog_writer"
    if _repeated_attempt(state):
        # The cheapest possible exit from a loop that cannot converge: no model call, no
        # database round trip. The probe is recorded as failed with the reviewer's reason.
        return "catalog_writer"
    return "sql_author"


def _rewritable(state: CompileState) -> bool:
    """Whether an author exists to rewrite this probe at all.

    A pinned probe has no author in its path, so routing one to sql_author on a failure would
    hand a hand-written query to a model that was never asked to write it - and silently
    replace it. Such a probe fails honestly instead.
    """
    return state.get("sql_mode") != "pinned"


def build_compile_graph():
    g = StateGraph(CompileState)
    g.add_node("rule_loader", rule_loader_node)
    g.add_node("grounding", grounding_node)
    g.add_node("sql_author", sql_author_node)
    g.add_node("validator", validator_node)
    g.add_node("executor", executor_node)
    g.add_node("sanity_gate", sanity_gate_node)
    g.add_node("verifier", verifier_node)
    g.add_node("catalog_writer", catalog_writer_node)

    g.add_edge(START, "rule_loader")
    g.add_conditional_edges(
        "rule_loader", _route_after_loader,
        {"grounding": "grounding", "validator": "validator", "sql_author": "sql_author"},
    )
    g.add_conditional_edges(
        "grounding", _route_after_grounding,
        {"sql_author": "sql_author", "validator": "validator",
         "catalog_writer": "catalog_writer"},
    )
    g.add_edge("sql_author", "validator")
    g.add_conditional_edges(
        "validator", _route_after_validator,
        {"sql_author": "sql_author", "executor": "executor",
         "catalog_writer": "catalog_writer"},
    )
    g.add_conditional_edges(
        "executor", _route_after_executor,
        {"sql_author": "sql_author", "sanity_gate": "sanity_gate",
         "catalog_writer": "catalog_writer"},
    )
    g.add_conditional_edges(
        "sanity_gate", _route_after_sanity,
        {"sql_author": "sql_author", "verifier": "verifier",
         "catalog_writer": "catalog_writer"},
    )
    g.add_conditional_edges(
        "verifier", _route_after_verifier,
        {"sql_author": "sql_author", "catalog_writer": "catalog_writer"},
    )
    g.add_edge("catalog_writer", END)
    return g.compile()


# ── The RUN graph ───────────────────────────────────────────────────────────────
# A SEPARATE graph with its own state, deliberately. The two stages answer different questions
# and share nothing but the catalog between them:
#
#   COMPILE  is the SQL right?          per rule, retried, LLM-heavy, rare
#   RUN      what does the data say?    whole database, linear, ONE LLM call, frequent
#
# Folding them into one graph would put compile-time routing in front of every detection run,
# where a mistake would be paid for daily. Keeping them apart also means build_compile_graph()
# above is provably untouched by anything added here.
#
#   catalog_loader -> probe_runner -> scorer -> summarizer -> report_builder
#
# Strictly linear, with no retries anywhere. That is not an omission: there is no author here
# to rewrite anything, so a probe that fails has no recourse and is reported as failed. The
# only branch is whether there is anything to run at all.


def _route_after_catalog(state) -> str:
    """Nothing compiled means nothing to execute - and that is a report worth writing.

    It still goes through the scorer and the report builder rather than stopping: a document
    stating plainly that no check is currently running is exactly what an operator in that
    situation needs to see, and silently producing nothing is how the situation persists.
    """
    return "probe_runner" if state.get("probes") else "scorer"


def build_run_graph():
    from app.graph.nodes.catalog_loader import catalog_loader_node
    from app.graph.nodes.probe_runner import probe_runner_node
    from app.graph.nodes.report_builder import report_builder_node
    from app.graph.nodes.scorer import scorer_node
    from app.graph.nodes.summarizer import summarizer_node
    from app.graph.run_state import RunState

    g = StateGraph(RunState)
    g.add_node("catalog_loader", catalog_loader_node)
    g.add_node("probe_runner", probe_runner_node)
    g.add_node("scorer", scorer_node)
    g.add_node("summarizer", summarizer_node)
    g.add_node("report_builder", report_builder_node)

    g.add_edge(START, "catalog_loader")
    g.add_conditional_edges(
        "catalog_loader", _route_after_catalog,
        {"probe_runner": "probe_runner", "scorer": "scorer"},
    )
    g.add_edge("probe_runner", "scorer")
    g.add_edge("scorer", "summarizer")
    g.add_edge("summarizer", "report_builder")
    g.add_edge("report_builder", END)
    return g.compile()
