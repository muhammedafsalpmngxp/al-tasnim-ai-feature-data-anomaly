"""Run the Scout, and record what the operator decides about what it found.

THE THIN LAYER, like app/compiler.py and app/runner.py. It owns no judgement: the graph
measures and proposes, this drives it and writes down decisions.

WHY ACCEPT AND REJECT ARE NOT GRAPH RUNS. They spend no model calls and touch no database - each
is a few lines of file editing. Making them graph invocations would dress a file append up as an
agent and leave a reader wondering what the AI decided. It decided nothing: a person did.

THE DECISION IS THE ONLY WAY IN. A proposal reaches the catalog through exactly one path -
accept() writes it into domain/anomalies/discovered.md, and the ORDINARY compile picks it up
from there like any other rule. There is no second compiler, no second author, no bypass.
"""
from __future__ import annotations

import time

from app.llm import get_usage_report, start_usage_tracking, usage_call_count
from app.observability import get_logger
from app.rules import discoveries as discovery_store
from app.rules.expand import expand_families
from app.rules.loader import load_rules

log = get_logger()


def _all_rules() -> list:
    """Every rule the engine can see, expanded - what "already covered" must be judged against.

    Families are expanded for the same reason the compiler expands them: DQ-G01 covers forty
    real relationships, and a Scout shown only the family's one-line description would happily
    propose a check for one of the forty it already covers.
    """
    rules, errors = load_rules()
    rules, notes = expand_families(rules)
    for problem in errors + notes:
        log.warning("load: %s", problem)
    return rules


def _compiled_status() -> dict[str, str]:
    """Each rule's compiled outcome, so the Scout can be told what this schema cannot express."""
    try:
        from app.rules import catalog as catalog_store

        return {p.rule_id: p.status for p in catalog_store.load().probes.values()}
    except Exception:  # noqa: BLE001 - a missing catalog is not a reason to refuse discovery
        return {}


def discover(max_proposals: int = 8, progress=None) -> dict:
    """One discovery pass. Returns the final state: proposals, what was dropped, and why."""
    from app.graph.build import build_discover_graph

    started = time.perf_counter()
    start_usage_tracking()

    state = {
        "rules": _all_rules(),
        # Passed IN rather than read inside the graph. The Scout is told which rules this schema
        # already proved it cannot express, and it must have that before it proposes - attaching
        # it afterwards would be too late to stop it suggesting five more of the same.
        "compiled_status": _compiled_status(),
        "max_proposals": max_proposals,
        "llm_calls": 0,
    }

    graph = build_discover_graph()
    if progress is None:
        final = graph.invoke(state)
    else:
        # Streamed for the same reason a compile is: the model call takes tens of seconds, and a
        # caller with no feedback cannot tell a slow call from a hung one.
        final = state
        stages = ("context_builder", "scout", "evidence_check", "dedup_filter",
                  "proposal_writer")
        done = 0
        for step in graph.stream(state):
            for name, update in step.items():
                final = {**final, **(update or {})}
                done += 1
                progress(done, len(stages), name)

    seconds = time.perf_counter() - started
    log.info(
        "scout: done in %.1fs - %d proposal(s), %d dropped, %d LLM call(s)",
        seconds, len(final.get("proposals") or []), len(final.get("dropped") or []),
        usage_call_count(),
    )
    return {
        "proposals": final.get("proposals") or [],
        "dropped": final.get("dropped") or [],
        "observations": len(final.get("observations") or []),
        "error": final.get("context_error") or final.get("scout_error") or "",
        "seconds": seconds,
        "llm_calls": usage_call_count(),
        "usage": get_usage_report(),
    }


# ── Decisions ──────────────────────────────────────────────────────────────────

def accept(proposal_hash: str, status: str = "probation") -> dict:
    """Admit one proposal as a rule. Returns {rule_id, title}, or {} when it is not pending.

    `probation` by default, and that default is the safety of this whole feature. A rule
    accepted into probation RUNS - so it can be judged on what it actually finds rather than on
    how well it reads - while counting towards nothing on the dashboard. A proposal that turns
    out to be wrong wastes a compile and moves no number anyone reports.
    """
    if status not in ("probation", "active"):
        raise ValueError(f"a proposal can be accepted into probation or active, not {status!r}")

    proposal = discovery_store.take_pending(proposal_hash)
    if proposal is None:
        return {}

    # Allocated HERE, at the moment of decision, and never when the proposal was made. Two
    # databases can each hold a pending list, and ids handed out at proposal time would collide
    # the moment the second one was accepted.
    rule_id = discovery_store.next_id()
    discovery_store.append(discovery_store.render(proposal, rule_id, status))
    log.info("scout: %s accepted as %s - %s", rule_id, status, proposal.get("title", ""))
    return {"rule_id": rule_id, "title": proposal.get("title", ""), "status": status}


def reject(proposal_hash: str, reason: str = "") -> dict:
    """Refuse one proposal, permanently and for a stated reason.

    RECORDED AS A RULE, not deleted. A rejection kept only in the cache is forgotten the moment
    the cache is cleared - which an operator is explicitly told they may do - and the Scout then
    proposes the same refused idea every run until they stop reading the list. Written here it
    is durable, and it is fed back into both the Scout's prompt and the deterministic filter.
    """
    proposal = discovery_store.take_pending(proposal_hash)
    if proposal is None:
        return {}

    rule_id = discovery_store.next_id()
    discovery_store.append(
        discovery_store.render(proposal, rule_id, "rejected", reason=reason)
    )
    log.info("scout: %s rejected - %s", rule_id, reason or "no reason given")
    return {"rule_id": rule_id, "title": proposal.get("title", ""), "status": "rejected"}


def set_status(rule_id: str, status: str, reason: str = "") -> bool:
    """Promote, refuse-after-accepting, or restore. All three are one status change.

    Nothing is ever deleted. What was decided, and when, is the point of the file - and the
    existing engine does the rest by itself: a rule that stops being runnable stops running on
    the next detection run, and its probe is pruned from the catalog on the next compile.
    """
    if status not in ("active", "probation", "rejected", "disabled"):
        raise ValueError(f"unsupported status {status!r}")
    changed = discovery_store.set_status(rule_id, status, reason)
    if changed:
        log.info("scout: %s -> %s%s", rule_id, status, f" ({reason})" if reason else "")
    return changed


def state() -> dict:
    """Everything the Discover screen renders: pending, decided, and what was filtered out."""
    decided = discovery_store.decided()
    return {
        "pending": discovery_store.load_pending(),
        "dropped": discovery_store.load_dropped(),
        "accepted": [d for d in decided if d["status"] in ("probation", "active")],
        "rejected": [d for d in decided if d["status"] == "rejected"],
        "path": discovery_store.DISCOVERED_PATH,
    }
