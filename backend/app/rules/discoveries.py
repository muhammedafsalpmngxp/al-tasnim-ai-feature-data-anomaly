"""The Scout's proposals, and what the operator decided about each of them.

TWO STORES, SPLIT BY DURABILITY. That split is the whole design.

    .cache/discovered.<db>.json      PENDING proposals
                                     Per-database, regenerable, safe to delete. The Scout looks
                                     at ONE database's tables, hints and measured values, so a
                                     proposal derived from AlTasnimBI means nothing against
                                     TrialDB_Test. Clearing the cache costs nothing here: run
                                     the Scout again and the same observations produce the same
                                     proposals.

    domain/anomalies/discovered.md   DECIDED rules, accepted AND rejected
                                     Global, durable, git-tracked. This is business history -
                                     "we looked at this idea and said yes / no, for this reason"
                                     - and it must survive a cache wipe, because the cache is
                                     something an operator is explicitly told they may delete.

WHY REJECTIONS MUST BE AS DURABLE AS ACCEPTANCES. A rejection that lives only in the cache is
forgotten the moment the cache is cleared, and the Scout then proposes the same refused idea
every single run until the operator stops reading the list at all. The rejected rules here are
fed back into the Scout's prompt AND into the deterministic duplicate filter, which is what
makes "do not propose this again" a guarantee rather than a request.

WHY A REJECTED RULE STILL GETS A REAL ID AND A REAL RULE BLOCK. This file is parsed by the
ordinary rule loader - it sits in domain/anomalies/, which loader._domain_files() already
scans - so everything in it must be a valid rule. Giving both outcomes an id makes the file
uniformly parseable, and makes Restore a one-word status change rather than a special case.
A `rejected` rule is not runnable, so it never compiles, never reaches the catalog, and never
appears in a report.

THE ONE FILE THIS ENGINE MAY WRITE UNDER domain/. business_rules.md, data_anomalies.md and
few_shots.md remain strictly read-only to every part of this system - they are what the business
means, and a machine cannot be the author of that. This file is different in kind: the engine
appends to it only when a person has clicked Accept or Reject, so every line in it records a
human decision. eval/test_rules.py enforces exactly that boundary.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from app.db import identity
from app.observability import get_logger

log = get_logger()

_DOMAIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "domain"
)
# Inside the directory loader._domain_files() already scans, so an accepted rule is picked up by
# the next compile with no loader change at all.
DISCOVERED_DIR = os.path.join(_DOMAIN_DIR, "anomalies")
DISCOVERED_PATH = os.path.join(DISCOVERED_DIR, "discovered.md")

# Discovered rules carry their own id range. Not cosmetic: the Scout proposes against one
# database while the file is global, so two databases proposing concurrently must not be able to
# collide on a number - and a DQ-S id in a report says "a person accepted this from a proposal"
# at a glance, without looking anything up.
ID_PREFIX = "DQ-S"
_ID_RE = re.compile(rf"^##\s+RULE\s+({ID_PREFIX}\d+)\b", re.MULTILINE)

_HEADER = """# DISCOVERED ANOMALIES

Proposals from the Scout that a person has decided on. **This file is maintained by the
application** - Accept and Reject in the UI append to it. Everything here was proposed by the
Scout and kept, or refused, by a human being.

Rules live here rather than in data_anomalies.md so that the two never mix: that file is yours
alone, and deleting this one reverts every machine-proposed rule in a single step.

- `status: probation` runs, and its findings are reported SEPARATELY. It counts towards nothing
  on the dashboard - not the score, not the flagged total, not the check counts - until someone
  promotes it to `active`.
- `status: active` is a fully trusted rule, identical in every way to one written by hand.
- `status: rejected` never runs and never appears in a report. It is kept only so the Scout
  cannot propose the same idea again.

---
"""


# ── Pending proposals (per database, regenerable) ──────────────────────────────

def pending_path() -> str:
    """THIS database's pending file. Named by app.db.identity, like every other cache."""
    return identity.cache_path("discovered", "json")


def load_pending() -> list[dict]:
    """Proposals awaiting a decision. [] when the Scout has not run for this database."""
    try:
        with open(pending_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get("proposals") if isinstance(data, dict) else data
    return [p for p in (items or []) if isinstance(p, dict)]


def save_pending(proposals: list[dict], dropped: list[dict] | None = None) -> str:
    """Replace this database's pending list. Atomic, so a concurrent read never sees a half-file.

    REPLACES RATHER THAN MERGES, deliberately. An undecided proposal the Scout still believes in
    is produced again by the next run - the observations that caused it have not changed - so
    merging would only accumulate stale duplicates of things it has since stopped proposing.
    """
    os.makedirs(os.path.dirname(pending_path()), exist_ok=True)
    payload = {
        "database": identity.current_database(),
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "proposals": proposals,
        # What the filters discarded, and why. Shown in the UI so "the Scout found 8, you see 3"
        # is explainable rather than looking like the model went quiet.
        "dropped": dropped or [],
    }
    tmp = pending_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, pending_path())
    return pending_path()


def take_pending(proposal_hash: str) -> dict | None:
    """Remove one proposal from the pending list and return it. None when it is not there.

    Removal and decision are one step: a proposal that has been decided must not still be
    offered for decision, and the pending file is rewritten atomically so two clicks racing each
    other cannot both take the same proposal.
    """
    pending = load_pending()
    keep = [p for p in pending if p.get("hash") != proposal_hash]
    if len(keep) == len(pending):
        return None
    taken = next(p for p in pending if p.get("hash") == proposal_hash)
    save_pending(keep, load_dropped())
    return taken


def load_dropped() -> list[dict]:
    try:
        with open(pending_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return [d for d in (data.get("dropped") or []) if isinstance(d, dict)]
    except (OSError, ValueError):
        return []


# ── Decided rules (global, durable) ────────────────────────────────────────────

def _read() -> str:
    try:
        with open(DISCOVERED_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def next_id() -> str:
    """The next free DQ-S number, read from the file itself.

    Allocated at DECISION time rather than when a proposal is made. Two databases can each have
    a pending proposal, and if ids were handed out at proposal time both could be DQ-S01 and
    collide the moment the second one is accepted. Reading the file at the point of writing to
    it makes that impossible.
    """
    used = [int(m.group(1)[len(ID_PREFIX):]) for m in _ID_RE.finditer(_read())]
    return f"{ID_PREFIX}{(max(used) + 1) if used else 1:02d}"


def decided() -> list[dict]:
    """Every decided rule as {rule_id, title, status, reason}, newest last.

    Parsed with a small local reader rather than the full rule loader: this is called by the API
    to render a list and by the Scout to avoid re-proposing, and neither needs a rule object -
    while both would be broken by the loader raising on a malformed block somebody hand-edited.
    """
    out: list[dict] = []
    for block in _read().split("\n## ")[1:]:
        head = block.splitlines()[0] if block else ""
        m = re.match(rf"RULE\s+({ID_PREFIX}\d+)\s*[-–—:]\s*(.+?)\s*$", head)
        if not m:
            continue
        meta = dict(
            re.findall(r"^-\s*([a-z_]+)\s*:\s*(.+?)\s*$", block, re.MULTILINE)
        )
        out.append({
            "rule_id": m.group(1),
            "title": m.group(2),
            "status": (meta.get("status") or "").lower(),
            "reason": meta.get("reason", ""),
            "evidence": meta.get("evidence", ""),
            "discovered_from": meta.get("discovered_from", ""),
            "decided": meta.get("decided", ""),
        })
    return out


def _write(text: str) -> None:
    """Atomic write. The only path in this engine that may modify a file under domain/."""
    os.makedirs(DISCOVERED_DIR, exist_ok=True)
    tmp = DISCOVERED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, DISCOVERED_PATH)


def append(rule_block: str) -> None:
    existing = _read() or _HEADER
    _write(existing.rstrip() + "\n\n" + rule_block.strip() + "\n")


def set_status(rule_id: str, status: str, reason: str = "") -> bool:
    """Change one rule's status in place. Returns False when the id is not in the file.

    This is Promote, Reject-an-accepted-rule and Restore - all three are this one operation,
    because the lifecycle is carried entirely by the status field. Nothing is ever deleted: the
    history of what was decided, and when, is the point of the file.
    """
    text = _read()
    if not text:
        return False
    blocks = text.split("\n## ")
    hit = False
    for i, block in enumerate(blocks):
        if i == 0 or not block.startswith(f"RULE {rule_id} "):
            continue
        hit = True
        block = re.sub(r"^-\s*status\s*:.*$", f"- status: {status}", block, count=1,
                       flags=re.MULTILINE)
        block = re.sub(r"^-\s*reason\s*:.*\n?", "", block, flags=re.MULTILINE)
        if reason:
            block = re.sub(r"^(-\s*status\s*:.*)$", rf"\1\n- reason: {reason}", block, count=1,
                           flags=re.MULTILINE)
        block = re.sub(r"^-\s*decided\s*:.*$",
                       f"- decided: {dt.date.today().isoformat()}", block, count=1,
                       flags=re.MULTILINE)
        blocks[i] = block
    if hit:
        _write("\n## ".join(blocks))
    return hit


def render(proposal: dict, rule_id: str, status: str, reason: str = "") -> str:
    """One decided proposal as a rule block the ordinary loader can parse.

    Written in the same shape as a hand-written rule so that everything downstream - the loader,
    the compiler, the author, the verifier - treats it identically. The only thing that marks it
    out is `source: discovered`, and that label exists so a reader of the report can always tell
    which findings came from a machine's suggestion.
    """
    def _clean(value: str) -> str:
        return " ".join(str(value or "").split())

    lines = [
        f"## RULE {rule_id} - {_clean(proposal.get('title'))}",
        "",
        f"- category: {_clean(proposal.get('category')) or 'Uncategorised'}",
        f"- severity: {_clean(proposal.get('severity')) or 'medium'}",
        f"- entity: {_clean(proposal.get('entity')) or 'row'}",
        "- method: rule",
        "- sql_mode: authored",
        f"- status: {status}",
        "- source: discovered",
    ]
    if reason:
        lines.append(f"- reason: {reason}")
    if proposal.get("evidence"):
        lines.append(f"- evidence: {_clean(proposal['evidence'])}")
    # Which database and when. The evidence is a SNAPSHOT, not a live figure - showing "6,054
    # records" beside this rule while pointed at a different database would be a lie - so the
    # provenance travels with it wherever it is displayed.
    lines.append(f"- discovered_from: {proposal.get('database') or identity.current_database()['name']}")
    lines.append(f"- decided: {dt.date.today().isoformat()}")
    if proposal.get("tags"):
        lines.append(f"- tags: {_clean(proposal['tags'])}")
    # THE COMPACT FOUR LINES, not four bold headings with blank lines between them.
    #
    # The fields and their MEANING are unchanged - `Never flag:` is still binding on the
    # Verifier, and the Author still reads all four. Only the layout is shorter: one labelled
    # line each instead of a heading, a blank line and a paragraph. Measured on the rules
    # already in this file, that removes about a third of the block before a single word of
    # prose is cut, and what is left reads like notes a person would actually write.
    #
    # `Never flag:` rather than `Do NOT flag:` keeps the imperative force in fewer characters.
    # The label was NOT softened to something like "Skip" on purpose: the Verifier is told to
    # treat this field as binding, and a weaker word invites a weaker reading of it.
    lines += [
        "",
        f"Wrong: {_clean(proposal.get('what_is_wrong'))}",
        f"Matters: {_clean(proposal.get('why_it_matters'))}",
        f"Detect: {_clean(proposal.get('how_to_detect'))}",
        "Never flag: " + (
            _clean(proposal.get("do_not_flag")) or "Nothing has been excluded yet."
        ),
        "",
        "---",
    ]
    return "\n".join(lines)
