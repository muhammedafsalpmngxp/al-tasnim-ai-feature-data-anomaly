"""Evidence Check node (deterministic) - drops any proposal not grounded in a measurement.

THE ANTI-HALLUCINATION GATE, and the reason this feature can be trusted enough to leave switched
on. The Scout is the only agent here whose output nothing downstream can verify: the Author is
checked by the database and the Verifier, but a proposal is prose, and prose is always plausible.

So the check is not "is this a good idea" - no code can answer that - it is "did a measurement
actually cause this". Every proposal must name an OBS-nnn that the context builder really
produced, and that id is matched against the list rather than merely parsed. A model that
invents an id, or cites none, produces nothing a person ever has to read.

    "Some task dates look unusual"                                    -> no id, dropped
    "Every record in this table is flagged (OBS-999)"                 -> unknown id, dropped
    "6,054 task records name a well that does not exist (OBS-014)"    -> kept

WHAT THIS DOES NOT DO. It does not check that the proposal FOLLOWS from the observation - a
model can cite OBS-014 while proposing something unrelated to it. Catching that needs judgement,
so it belongs to the person clicking Accept, and the observation is shown beside the proposal in
the UI precisely so they can make it.
"""
from __future__ import annotations

from app.graph.discover_state import DiscoverState
from app.observability import get_logger

log = get_logger()

# Fields a proposal cannot be useful without. `do_not_flag` is deliberately NOT here: "nothing
# is legitimately excluded" is a real answer, and demanding text would only invite filler.
_REQUIRED = ("title", "what_is_wrong", "why_it_matters", "how_to_detect")


def evidence_check_node(state: DiscoverState) -> dict:
    proposals = state.get("proposals") or []
    known = {o["id"] for o in (state.get("observations") or [])}

    kept: list[dict] = []
    dropped = list(state.get("dropped") or [])

    for proposal in proposals:
        title = str(proposal.get("title") or "").strip() or "(untitled)"

        missing = [f for f in _REQUIRED if not str(proposal.get(f) or "").strip()]
        if missing:
            dropped.append({
                "title": title, "reason": "incomplete",
                "detail": "the proposal left out " + ", ".join(missing),
            })
            continue

        cited = str(proposal.get("observation_id") or "").strip().upper()
        if not cited:
            dropped.append({
                "title": title, "reason": "no evidence",
                "detail": "no measurement was cited, so nothing supports it",
            })
            continue
        if cited not in known:
            dropped.append({
                "title": title, "reason": "evidence not found",
                "detail": f"cited {cited}, which was never measured",
            })
            continue

        # The observation's own wording is attached here rather than trusting the model's
        # paraphrase of it. The number a person sees beside a proposal is then the measured one.
        observation = next(o for o in (state.get("observations") or []) if o["id"] == cited)
        proposal["observation_id"] = cited
        proposal["observation_fact"] = observation["fact"]
        proposal.setdefault("evidence", observation["fact"])
        proposal["tables"] = [t for t in (observation.get("table"),) if t]
        kept.append(proposal)

    if len(kept) != len(proposals):
        log.info(
            "scout: %d of %d proposal(s) had no usable evidence and were dropped",
            len(proposals) - len(kept), len(proposals),
        )
    return {"proposals": kept, "dropped": dropped}
