"""Extraction helpers for LLM output.

An LLM returns prose around the thing you asked for, however firmly the prompt says otherwise.
These pull the payload out without ever raising: a node must be able to detect "nothing usable
came back" and retry, rather than crash a whole compile on one malformed reply.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ```sql ... ``` with an optional tag after the language, e.g. ```sql summary
# The tag is how a rule file distinguishes its SUMMARY query from its DETAIL query. Captured
# here rather than parsed separately so both consumers (the domain loader and the SQL Author's
# reply) use one implementation and cannot drift apart.
_FENCE = re.compile(
    r"```[ \t]*sql[ \t]*([A-Za-z_][A-Za-z0-9_-]*)?[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
# Any fenced block, used only as a fallback when the model omitted the language tag.
_ANY_FENCE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def extract_sql_blocks(text: str) -> dict[str, str]:
    """Every ```sql <tag>``` block in `text`, keyed by its lowercased tag.

    An untagged block is keyed "". A duplicate tag keeps the FIRST occurrence: a model that
    re-states a query after explaining it must not have the restatement silently win, because
    the restatement is the one more likely to have been paraphrased.
    """
    out: dict[str, str] = {}
    for tag, body in _FENCE.findall(text or ""):
        key = (tag or "").strip().lower()
        body = body.strip()
        if body and key not in out:
            out[key] = body
    return out


def extract_sql(text: str) -> str:
    """The single SQL statement in `text`.

    Falls back through: a tagged/untagged ```sql block -> any fenced block -> the raw text when
    it already looks like a query. Returns "" when nothing usable is present, which the caller
    treats as a retryable failure rather than an error.
    """
    blocks = extract_sql_blocks(text)
    if blocks:
        # Prefer an untagged block, else whichever came first.
        return blocks.get("", next(iter(blocks.values())))

    m = _ANY_FENCE.search(text or "")
    if m and m.group(1).strip():
        return m.group(1).strip()

    stripped = (text or "").strip()
    lowered = stripped.lower()
    if lowered.startswith("select") or lowered.startswith("with"):
        return stripped
    return ""


def extract_json(text: str, default: Any = None) -> Any:
    """The first JSON object in `text`.

    Tries the whole string first (the common case when the model obeyed), then a fenced block,
    then a brace-balanced scan. The scan is brace-COUNTING rather than a greedy regex because a
    verdict's feedback string routinely contains braces, and a greedy match would swallow
    trailing prose and fail to parse.

    Returns `default` on any failure. Callers pass a sentinel object so that an unparseable
    reply is distinguishable from a genuine `{"ok": true}` - see the Verifier, which must fail
    CLOSED rather than read a malformed reply as approval.
    """
    raw = (text or "").strip()
    if not raw:
        return default

    for candidate in (raw, *_ANY_FENCE.findall(raw)):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass

    start = raw.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except (ValueError, TypeError):
                        break  # malformed; try the next opening brace
        start = raw.find("{", start + 1)

    return default


_WS = re.compile(r"\s+")


def normalize_sql(sql: str) -> str:
    """Whitespace/case-insensitive form, for recognising a query already attempted.

    Only ever used to DETECT a repeat - never to alter what is executed. Collapse whitespace
    BEFORE trimming the trailing semicolon, otherwise "SELECT 1 ;" keeps a trailing space and
    compares unequal to "SELECT 1".
    """
    return _WS.sub(" ", (sql or "")).strip().rstrip(";").strip().lower()


def truncate(text: str, limit: int, suffix: str = " [...truncated]") -> str:
    """Bound `text` without ever cutting mid-sentence.

    Cuts back to the last sentence end or line break inside the budget, so whatever survives is
    a complete instruction. A hard slice is genuinely dangerous here: verifier feedback is fed
    straight to the SQL Author, and a cut landing inside the "Fix:" clause leaves it acting on
    a dangling fragment.
    """
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("\n"), window.rfind("; "))
    if cut < limit // 2:  # no usable boundary late enough; fall back to a word break
        cut = window.rfind(" ")
    if cut <= 0:
        cut = limit
    return window[: cut + 1].rstrip() + suffix
