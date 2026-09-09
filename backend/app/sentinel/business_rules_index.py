"""Parses `docs/BUSINESS_RULES.md`'s own numbered `## N. Title` headers into an index, so
a finding's terse `business_rule_ref` (e.g. "§4, §7") can be expanded into something a
reader doesn't have to go open that file to understand. Reads the exact same file
`llm/prompts.py` feeds to the LLM verbatim, so a report can never describe a rule
differently than the LLM was told it means -- one source, two presentations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import REPO_ROOT

_SECTION = re.compile(
    r"^##\s+(\d+)\.\s+(.+?)\s*$\n(.*?)(?=^##\s+\d+\.|\Z)",
    re.MULTILINE | re.DOTALL,
)
_REF_TOKEN = re.compile(r"§?\s*(\d+)")


@dataclass(slots=True, frozen=True)
class BusinessRuleSection:
    number: str
    title: str
    text: str


@lru_cache(maxsize=1)
def _raw_text() -> str:
    path = REPO_ROOT / "docs" / "BUSINESS_RULES.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def sections() -> dict[str, BusinessRuleSection]:
    """{'4': BusinessRuleSection(number='4', title='Milestone deadlines', text='...')}"""
    return {
        m.group(1): BusinessRuleSection(m.group(1), m.group(2).strip(), m.group(3).strip())
        for m in _SECTION.finditer(_raw_text())
    }


def _tokens(ref: str) -> list[str]:
    # A ref has used both ", " (business_rules.py) and "/" (column_semantics.yaml's
    # date_order, e.g. "§4/§5") as separators between multiple citations -- split on both.
    return [t.strip() for t in re.split(r"[,/]", ref) if t.strip()]


def describe(ref: str | None) -> str:
    """'§4, §7' -> '§4 Milestone deadlines; §7 Lifecycle order'.

    A token that isn't a recognised section number (or BUSINESS_RULES.md can't be found)
    passes through unchanged -- this must never raise or silently drop a ref just because
    it can't be expanded; the raw citation is still better than nothing.
    """
    if not ref:
        return ""
    known = sections()
    out = []
    for tok in _tokens(ref):
        m = _REF_TOKEN.match(tok)
        sec = known.get(m.group(1)) if m else None
        out.append(f"§{sec.number} {sec.title}" if sec else tok)
    return "; ".join(out)


def cites(ref: str | None, section_number: str) -> bool:
    """Whether a `business_rule_ref` string cites a given section number, tolerant of
    both separator conventions (",", "/") and the optional "§" prefix.
    """
    if not ref:
        return False
    for tok in _tokens(ref):
        m = _REF_TOKEN.match(tok)
        if m and m.group(1) == section_number:
            return True
    return False


def referenced_sections(refs: list[str | None]) -> list[BusinessRuleSection]:
    """The distinct, known sections actually cited across a list of refs (e.g. every
    finding in one run), in section-number order -- what a report's reference appendix
    should print, not the full document regardless of whether this run touched it.
    """
    known = sections()
    seen: dict[str, BusinessRuleSection] = {}
    for ref in refs:
        if not ref:
            continue
        for tok in _tokens(ref):
            m = _REF_TOKEN.match(tok)
            if m and m.group(1) in known and m.group(1) not in seen:
                seen[m.group(1)] = known[m.group(1)]
    return [seen[k] for k in sorted(seen, key=int)]
