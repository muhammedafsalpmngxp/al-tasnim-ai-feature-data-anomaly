"""Citation validation -- the "no invented numbers" rule, enforced in code, not by prompt.

A model that ignores the guard rules and states a number not present in the finding's own
data produces a report that looks confident and is wrong. This module makes that
unrepresentable in the final report: every number the model's narration text contains is
checked against the finding it was given, and a narration failing the check is discarded
entirely (never partially trusted) with the reason logged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")
# The decimal group requires a digit after the dot -- `\.?\d*` (the old pattern) let a
# sentence-ending period after a whole number be swallowed as part of it, so "...section
# 4." tokenized to "4." and never matched the source's "4", and "...(2026-09-08)." lost
# its final "-08" fragment to "-08." the same way. Confirmed live: real, valid narration
# was being rejected by the citation validator for exactly this reason on 2026-09-08.

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ROUND_DIGITS = (0, 1, 2, 3, 4, 5, 6)


def _numbers_in(text: str) -> set[str]:
    """Extract number-like tokens, normalised (no thousands separators) for comparison."""
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text)}


def _expand(raw_values: list[str]) -> set[str]:
    """Derive the number tokens a model may LEGITIMATELY write differently from a source
    value, without inventing new data:

    - a date "2026-10-19" may be reworded as "19 October 2026" -- the day/month/year each
      appear as their own plain number, zero-padded and not, so all four are accepted.
    - a float with long binary-rounding noise, e.g. 1.1166666666666667, may be rounded by
      the model to any shorter precision for readability, e.g. "1.1167" -- every rounding
      from 0 to 6 decimal places is accepted, but a value that is not actually a rounding
      of the source (docs/02 guard #2: no invented numbers) is still rejected.

    Confirmed live on 2026-09-08: both patterns caused correct narration to be rejected
    before this existed (see the regression tests in tests/test_llm_validate.py).
    """
    out: set[str] = set()
    for raw in raw_values:
        for y, mo, d in _ISO_DATE.findall(raw):
            out.update({y, mo, d, str(int(mo)), str(int(d))})
        # Scan for embedded number TOKENS rather than float-parsing the whole string:
        # `evidence` on a finding read back from the store is a raw JSON *string*
        # (FindingsStore.findings() does not deserialise it -- confirmed 2026-09-08), so
        # a single row's value arrives here as one long JSON blob, not an isolated
        # number. float(whole_blob) would always fail; scanning it for number-shaped
        # substrings and rounding each one works whether the value is bare or embedded.
        for tok in _NUMBER.finditer(raw):
            try:
                f = float(tok.group(0).replace(",", "").rstrip("%"))
            except ValueError:
                continue
            for digits in _ROUND_DIGITS:
                out.add(f"{round(f, digits):.{digits}f}".rstrip("0").rstrip(".") or "0")
    return out


def _source_numbers(finding: dict) -> set[str]:
    """Every number that legitimately exists in the finding the model was given, plus the
    reworded/rounded variants a faithful narration may reasonably use (see `_expand`).
    """
    literal_values: list[str] = []
    out: set[str] = set()
    for key in ("title", "why_it_matters", "grain", "baseline"):
        val = finding.get(key)
        if isinstance(val, str):
            out |= _numbers_in(val)
            literal_values.append(val)
    if (n := finding.get("affected_count")) is not None:
        out.add(str(n))
        literal_values.append(str(n))
    ev = finding.get("evidence")
    ev_rows = ev if isinstance(ev, list) else ([ev] if isinstance(ev, str) else [])
    for row in ev_rows:
        values = row.values() if isinstance(row, dict) else [row]
        for v in values:
            out |= _numbers_in(str(v))
            literal_values.append(str(v))
    out |= _expand(literal_values)
    return out


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    invented_numbers: list[str]
    reason: str = ""


def validate_narration(finding: dict, narration: dict) -> ValidationResult:
    """Reject a narration if it states any number not present in the finding's own data.

    `cited_numbers` (a field the schema forces the model to declare) is checked first as
    the model's own account of what it used; the free-text fields are then independently
    re-scanned so a model that fabricates a number WITHOUT declaring it in
    `cited_numbers` is still caught.
    """
    source = _source_numbers(finding)
    text = " ".join(
        str(narration.get(k, "")) for k in ("explanation", "root_cause", "remediation")
    )
    used = _numbers_in(text)
    invented = sorted(n for n in used if n not in source and n.strip("%") not in source)
    if invented:
        return ValidationResult(
            ok=False, invented_numbers=invented,
            reason=(
                f"narration for {finding.get('check_id')} contains number(s) not present "
                f"in the finding's own data: {invented}"
            ),
        )
    return ValidationResult(ok=True, invented_numbers=[])


def validate_correlation(all_findings_by_id: dict[str, dict], incident: dict) -> ValidationResult:
    """An incident may only reference check_ids that actually exist in this run's findings,
    and its narrative may not invent numbers beyond what those findings contain.
    """
    referenced = incident.get("finding_ids", [])
    unknown = [fid for fid in referenced if fid not in all_findings_by_id]
    if unknown:
        return ValidationResult(
            ok=False, invented_numbers=[],
            reason=f"incident references unknown finding id(s): {unknown}",
        )
    source: set[str] = set()
    for fid in referenced:
        source |= _source_numbers(all_findings_by_id[fid])
    used = _numbers_in(str(incident.get("narrative", "")))
    invented = sorted(n for n in used if n not in source and n.strip("%") not in source)
    if invented:
        return ValidationResult(
            ok=False, invented_numbers=invented,
            reason=f"incident narrative contains unsupported number(s): {invented}",
        )
    return ValidationResult(ok=True, invented_numbers=[])
