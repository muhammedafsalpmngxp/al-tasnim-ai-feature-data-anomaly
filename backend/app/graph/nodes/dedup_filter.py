"""Dedup Filter node (deterministic) - drops what is already covered or already refused.

THE GUARANTEE, AS OPPOSED TO THE REQUEST. The Scout's prompt asks it not to re-propose existing
or refused checks, and mostly it complies. This node is what makes it certain. A prompt
instruction is a request to a model; a comparison in code is a fact about the output.

WHY THE COMPARISON IS OVER MEANING, NOT WORDING
------------------------------------------------
The obvious design - remember each proposal by its id, refuse ids seen before - does not work,
and the failure is silent. A pending proposal's id is a hash of text the Scout wrote. Run it
again next week and the same anomaly comes back phrased slightly differently, hashes to
something else, and every id-based check waves it through. The operator then refuses the same
idea over and over and concludes the feature is broken.

So proposals are compared as SETS OF TERMS drawn from what they are about - the title and the
detection logic, stripped of the words every rule uses. Two descriptions of the same defect
overlap heavily on the words that carry meaning ("task", "well", "exist", "reference") whatever
else differs, and that is exactly what is measured.

THE THRESHOLD IS ASYMMETRIC, DELIBERATELY. Refused proposals are matched more loosely than
existing rules. Re-showing something a person already said no to is the failure that makes them
abandon the feature; showing one slightly-redundant new idea costs a single click. When in
doubt, drop it - and say so in `dropped`, so nothing disappears without a trace.
"""
from __future__ import annotations

import re

from app.graph.discover_state import DiscoverState
from app.observability import get_logger

log = get_logger()

# Shared with app.graph.context in spirit but kept local: this compares rule PROSE, not schema
# text, and the two should be free to diverge without one silently changing the other.
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")

# Words that appear in nearly every anomaly description and therefore distinguish nothing. A
# match driven by these is a match on the genre, not the subject.
_NOISE = frozenset((
    "the", "and", "for", "that", "this", "with", "not", "are", "was", "were", "has", "have",
    "had", "its", "their", "which", "when", "where", "what", "any", "all", "one", "two",
    "record", "records", "row", "rows", "value", "values", "data", "date", "dates", "field",
    "column", "table", "flag", "flagged", "check", "checks", "rule", "rules", "anomaly",
    "anomalies", "report", "reported", "should", "must", "cannot", "does", "some", "every",
    "business", "system", "database", "missing", "wrong", "problem", "issue",
))

# Share of the smaller term set that must be shared before two rules are "the same thing".
_COVERED_THRESHOLD = 0.60
_REJECTED_THRESHOLD = 0.45


def _stem(word: str) -> str:
    """Crude singularisation, and that is all.

    "a task REFERENCES a well" and "task REFERENCE integrity" are the same subject, and without
    this they share no term at all - which was enough, measured, to let a re-worded duplicate of
    an existing rule through the filter entirely. Nothing more elaborate is wanted here: real
    stemming would start collapsing words that mean different things, and a false match silently
    discards a proposal nobody ever sees.
    """
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _terms(*texts: str) -> set[str]:
    words = set()
    for text in texts:
        words |= {_stem(w) for w in _WORD.findall(str(text or "").lower())}
    return words - _NOISE


def _overlap(a: set[str], b: set[str]) -> float:
    """Share of the SMALLER set that both contain.

    Smaller rather than the union: a terse existing title ("Task has no crew assigned") and a
    wordy new proposal describing the same defect have a poor union score purely because one is
    longer, and length is not meaning.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def dedup_filter_node(state: DiscoverState) -> dict:
    proposals = state.get("proposals") or []
    dropped = list(state.get("dropped") or [])

    covered = [
        (rule, _terms(rule.get("title"), rule.get("category"), rule.get("tags")))
        for rule in (state.get("covered") or [])
    ]
    # THE TITLE ONLY, never the rejection reason. The reason explains why a person said no -
    # "valid historical snapshot behaviour" - and says nothing about what the rule was ABOUT.
    # Folding it in enlarges the term set with words no proposal will ever share, which drives
    # the overlap down and lets the refused idea back through. Measured: including the reason
    # dropped a true match from 0.75 to 0.38, under the threshold.
    rejected = [
        (rule, _terms(rule.get("title")))
        for rule in (state.get("rejected") or [])
    ]

    kept: list[dict] = []
    for proposal in proposals:
        mine = _terms(
            proposal.get("title"), proposal.get("what_is_wrong"), proposal.get("how_to_detect")
        )

        match = max(
            ((rule, _overlap(mine, terms)) for rule, terms in rejected),
            key=lambda pair: pair[1], default=(None, 0.0),
        )
        if match[0] is not None and match[1] >= _REJECTED_THRESHOLD:
            dropped.append({
                "title": proposal.get("title", ""), "reason": "previously refused",
                "detail": (
                    f"matches {match[0].get('rule_id', '')} \"{match[0].get('title', '')}\""
                    + (f" - refused because: {match[0]['reason']}" if match[0].get("reason") else "")
                ),
            })
            continue

        match = max(
            ((rule, _overlap(mine, terms)) for rule, terms in covered),
            key=lambda pair: pair[1], default=(None, 0.0),
        )
        if match[0] is not None and match[1] >= _COVERED_THRESHOLD:
            dropped.append({
                "title": proposal.get("title", ""), "reason": "already covered",
                "detail": (
                    f"matches {match[0].get('rule_id', '')} \"{match[0].get('title', '')}\""
                ),
            })
            continue

        # Two proposals from the SAME run can also describe one defect. Rare, but a duplicate
        # pair in the pending list makes the whole list look careless.
        if any(_overlap(mine, _terms(k.get("title"), k.get("what_is_wrong"),
                                     k.get("how_to_detect"))) >= _COVERED_THRESHOLD
               for k in kept):
            dropped.append({
                "title": proposal.get("title", ""), "reason": "duplicate",
                "detail": "another proposal in this same run describes the same defect",
            })
            continue

        kept.append(proposal)

    if dropped:
        log.info(
            "scout: %d proposal(s) kept, %d dropped (%s)",
            len(kept), len(dropped),
            ", ".join(sorted({d["reason"] for d in dropped})),
        )
    return {"proposals": kept, "dropped": dropped}
