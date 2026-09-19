"""State for the DISCOVER graph - one pass over one database, looking for unwritten checks.

SEPARATE FROM CompileState AND RunState, for the same reason those two are separate from each
other: the three graphs answer different questions and share nothing but the files on disk.

  COMPILE   is the SQL right?          per rule, retried, LLM-heavy, rare
  RUN       what does the data say?    whole database, linear, ONE LLM call, frequent
  DISCOVER  what is nobody checking?   whole database, linear, ONE LLM call, on demand

Nothing here reaches the catalog. A discovery run writes ONE file - this database's pending
proposal list - and that file is regenerable. A proposal only becomes a rule when a person
accepts it, and only becomes SQL when the ordinary compile graph writes it.
"""
from __future__ import annotations

from typing import Any, TypedDict


class DiscoverState(TypedDict, total=False):
    # ── in ──
    rules: list[Any]
    """Every rule the loader can see, expanded. What "already covered" is computed from."""

    compiled_status: dict[str, str]
    """Each rule's compiled outcome, so the Scout can be told what this schema cannot express."""

    max_proposals: int

    # ── context_builder ──
    schema_block: str
    observations: list[dict]
    """Measured facts, each with an OBS-nnn id. The ONLY thing a proposal may be grounded in."""

    covered: list[dict]
    rejected: list[dict]
    """Previously refused proposals, with reasons. Fed to the Scout AND to the filter."""

    context_error: str
    """Why there is nothing to look at - an empty cache, not a model failure. Ends the run."""

    # ── scout ──
    proposals: list[dict]
    scout_error: str

    # ── evidence_check / dedup_filter ──
    dropped: list[dict]
    """What was discarded and why, so "the Scout found 8, you see 3" is explainable rather than
    looking like the model went quiet."""

    # ── proposal_writer ──
    pending_path: str

    llm_calls: int
