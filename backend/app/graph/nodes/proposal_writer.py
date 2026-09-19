"""Proposal Writer node (deterministic) - records what survived, for a person to decide on.

THE END OF THE MACHINE'S AUTHORITY. Everything upstream measured, proposed and filtered; this
writes the result to one regenerable file and stops. Nothing here touches the catalog, writes
SQL, or puts a rule anywhere the compiler can see it. A discovery run that nobody looks at has
changed nothing about what this engine checks.

THE HASH IS A HANDLE, NOT AN IDENTITY. It exists so a click in the UI can name one proposal, and
so the same proposal survives a page refresh. It is taken over the normalised SUBJECT - the
tables and the detection logic - rather than the model's sentences, because sentences drift
between runs while the defect does not. Real identity arrives only when a person decides, as a
DQ-S number allocated from the decision file; until then a proposal is not a thing, it is a
suggestion.
"""
from __future__ import annotations

import hashlib
import re

from app.graph.discover_state import DiscoverState
from app.observability import get_logger
from app.rules import discoveries as discovery_store

log = get_logger()

_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")


def _handle(proposal: dict) -> str:
    """A stable id for one proposal, over its subject rather than its prose.

    Sorted terms, not raw text: the same defect described in a different order, or with a
    synonym swapped in, must not present itself as something new when the Scout runs again.
    """
    terms = sorted(set(_WORD.findall(
        " ".join(str(proposal.get(f) or "") for f in ("title", "how_to_detect")).lower()
    )))
    tables = ",".join(sorted(proposal.get("tables") or ()))
    return hashlib.sha256(("|".join(terms) + "#" + tables).encode("utf-8")).hexdigest()[:12]


def proposal_writer_node(state: DiscoverState) -> dict:
    proposals = list(state.get("proposals") or [])
    database = ""
    try:
        from app.db import identity

        database = identity.current_database()["name"]
    except Exception:  # noqa: BLE001 - a label, never a reason to lose the run
        pass

    for proposal in proposals:
        proposal["hash"] = _handle(proposal)
        proposal["database"] = database

    path = discovery_store.save_pending(proposals, state.get("dropped") or [])
    log.info(
        "scout: %d proposal(s) awaiting a decision -> %s", len(proposals), path
    )
    return {"pending_path": path}
