"""What the numbers and codes in a probe's tables MEAN, as one hash.

THE FAILURE THIS EXISTS TO CATCH, AND WHY NOTHING ELSE CATCHES IT
-----------------------------------------------------------------
Every staleness signal in this engine is STRUCTURAL. `structure_fingerprint` and
`table_signatures` fold in column names, declared types, lengths, nullability and keys - so a
rename, a retype or a dropped foreign key correctly invalidates the probes that read it.

None of them notice a change of MEANING. Suppose a progress column that held 0-100 starts
holding 0-1: same name, same `numeric` type, same nullability, same keys. Every fingerprint is
unchanged, so the probe is judged current, and its stored `WHERE progress >= 100` keeps running
and returns ZERO anomalies. The run reports a clean bill of health, in writing, for a column
that is now entirely mismeasured.

That is the worst outcome this system can produce. The prompts already call it out - "the single
most damaging mistake a probe can make, because it fails SILENTLY and convincingly" - and they
guard it at COMPILE time, where the author is shown the measured scale. Afterwards nothing was
watching. This module is what watches.

WHAT IS FOLDED IN, AND WHAT IS DELIBERATELY NOT
------------------------------------------------
ONLY facts about meaning, never facts about volume. That distinction is the whole design, for
the same reason `structure_fingerprint` excludes row counts: a signal that moves on every
nightly load invalidates everything daily, and an engine that recompiles everything daily is one
nobody leaves switched on.

    IN   the scale verdict per numeric column   FRACTION_1 / PERCENT_100 / ...
         the bounds derived from it             (0,1) or (0,100)
         whether a text column is codes         a quantity-looking column that parses as none
         the real coded values of a lookup      'National' / 'Expat' - a probe filters on these

    OUT  min, max, average, standard deviation  move with the data, every load
         null rate                              moves with the data
         row counts                             move with the data

The coded values are in because a probe WRITES THEM AS LITERALS: a rule that filters
`nationality_type = 'National'` is wrong the moment that lookup is recoded, and the column
declaration will not have changed by one character.

MEASURED BEHAVIOUR, ON THE LIVE TrialDB_Test HINTS
--------------------------------------------------
Verified rather than assumed, because "does not move on a data reload" is the one property
that decides whether this check is usable at all:

    row counts 999,999           unchanged     stdev -> 1.0        unchanged
    min/max widened              unchanged     null rate 5% -> 6%  unchanged
    a column going 100% null to 77% null       unchanged
    FRACTION_1 -> PERCENT_100    CHANGED       a lookup recoded    CHANGED
    a text column's parse rate crossing from "codes" to numeric    CHANGED

ONE DELIBERATE SENSITIVITY, AND IT IS NOT A DEFECT. `bounds()` derives its range from the
column AVERAGE - at or below 1.0 it is a 0-1 fraction, at or below 100 a percentage - so an
average that crosses 1.0 or 100.0 moves the derived bounds and this fingerprint with it. That
is a real change in what a range probe should compare against, not noise: a column whose
average has moved from 0.26 to 42 is no longer a 0-1 fraction, whatever it was when the probe
was written. The raw average is NOT folded in - only the bucket it lands in - so ordinary drift
within a bucket costs nothing, and there are exactly two cliffs rather than a continuum.

IT FAILS OPEN, LIKE EVERY OTHER UNVERIFIABLE CHECK HERE.
An empty result means "cannot be computed" - no hints yet, no index, an unresolvable table - and
an empty fingerprint compares equal to everything, so the probe is left alone. A probe must
never be failed on the strength of a check that could not run; the structural signals still
guard it, exactly as before this module existed.
"""
from __future__ import annotations

import hashlib
import re

from app.observability import get_logger

log = get_logger()

# A value-hints line: "- schema.table (col, col): value; value". Matched here rather than
# imported from app.graph.context so that app.rules never depends on app.graph - the dependency
# runs the other way everywhere else, and one backwards import is how a cycle starts.
_VALUE_LINE_RE = re.compile(r"^-\s+(\S+)\s+\(")


def _numeric_facts(tables: tuple[str, ...], schema_text: str, numeric_hints: str) -> list[str]:
    """One line per column carrying its MEASURED MEANING, for the tables this probe reads."""
    try:
        from app.rules.schema_index import load_index

        index = load_index(schema_text or None, numeric_hints or None)
    except Exception as exc:  # noqa: BLE001 - an unverifiable check must not fail a probe
        log.debug("semantics: index unavailable (%s) - no semantic fingerprint", exc)
        return []

    facts: list[str] = []
    for name in tables:
        table = index.tables.get(name) or index.get(name)
        if table is None:
            # A table we cannot resolve makes the whole fingerprint unsound: it would hash the
            # columns we DID find and silently declare the probe semantically unchanged.
            return []
        for column, stat in sorted(table.stats.items()):
            bounds = stat.bounds()
            facts.append(
                f"{name}.{column}|scale={(stat.scale or '').strip().upper()}"
                f"|bounds={'' if bounds is None else f'{bounds[0]:g}..{bounds[1]:g}'}"
                f"|codes={int(bool(stat.text_is_codes))}"
            )
    return facts


def _value_facts(tables: tuple[str, ...], value_hints: str) -> list[str]:
    """The coded-value lines for these tables - the literals a probe may have written in."""
    if not value_hints:
        return []
    keep = {t.lower() for t in tables}
    out: list[str] = []
    for line in value_hints.splitlines():
        m = _VALUE_LINE_RE.match(line)
        if m is not None and m.group(1).lower() in keep:
            out.append(line.strip())
    return sorted(out)


def semantic_fingerprint(
    tables,
    schema_text: str = "",
    value_hints: str = "",
    numeric_hints: str = "",
) -> str:
    """One hash standing for the MEANING of every column a probe reads. "" when unknowable.

    `tables` is the probe's own table list, so this answers the narrow question a probe cares
    about - "has the meaning of anything I read moved?" - rather than a whole-database question
    that would mark every probe stale over an unrelated lookup being recoded.
    """
    names = tuple(dict.fromkeys(str(t).strip() for t in (tables or []) if str(t).strip()))
    if not names:
        return ""

    facts = _numeric_facts(names, schema_text, numeric_hints)
    values = _value_facts(names, value_hints)
    if not facts and not values:
        # Nothing measurable about these tables: no numeric columns and no coded values. There
        # is no meaning to drift, so "" is the honest answer rather than a hash of emptiness -
        # which would be a constant, and would read as "verified unchanged".
        return ""

    h = hashlib.sha256()
    h.update(b"semantics-v1\n")
    for line in sorted(facts) + values:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def describe_drift(tables) -> str:
    """The message shown when a probe's semantics have moved. Names what to go and look at."""
    listed = ", ".join(sorted(str(t) for t in (tables or []))[:4]) or "its tables"
    return (
        "the MEASURED MEANING of a column it reads has changed - a numeric scale, a derived "
        f"bound or a lookup's coded values moved in {listed}, while the column declarations "
        "did not. Its stored comparisons may now be against the wrong scale, which is a silent "
        "false pass, so it is rebuilt rather than trusted."
    )
