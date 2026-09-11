"""Deterministic, schema-derived observations about a probe's SQL. Advisory, never fatal.

These are handed to the Rule Verifier as EVIDENCE TO ADJUDICATE, not as rules to obey. The
Verifier's prompt tells it not to reject on a hunch; a concern derived from the database's own
foreign keys, grain markers and measured scales is not a hunch, so it gives the Verifier
something factual to rule on. The check is deliberately conservative and can flag a probe that
is perfectly fine - which is exactly why a model adjudicates it rather than a hard gate.

NOTHING HERE IS SPECIFIC TO ANY DATABASE. Every rule is derived at runtime from artefacts
introspection already wrote:

  * join keys        the `FK:` lines in schema.txt
  * grain            the `MANY ROWS PER` markers introspection adds when a table holds more
                     rows than distinct key values
  * measured scale   the `FRACTION_1` / `PERCENT_100` verdicts in numeric_hints.txt

Point the engine at a different database and the checks follow it, with no code change.

WHY SCALE IS CHECKED HERE AND NOT LEFT TO THE MODEL
---------------------------------------------------
Comparing a 0-1 fraction against 100 is the single most damaging mistake an anomaly probe can
make, because it fails SILENTLY and in the most convincing direction: the query runs, returns a
clean-looking zero, and reports the data as healthy. Nothing downstream can tell that apart
from a genuine pass. The measured scale is in the hints, so this is checkable arithmetic rather
than an opinion, and it is checked.
"""
from __future__ import annotations

import re

from app.observability import get_logger

log = get_logger()

_TABLE_RE = re.compile(r"^TABLE\s+(\S+)", re.MULTILINE)
_FK_RE = re.compile(r"^\s+FK:\s+(\w+)\s*->\s*(\S+)\.(\w+)\s*$", re.MULTILINE)
# The marker line carries explanatory text after the key, so it must not be anchored to the
# end of the line. The key may be [bracketed] when the name needs quoting.
_DUP_RE = re.compile(r"MANY ROWS PER\s+(\[[^\]]+\]|\w+)")

# Ways a query collapses a table to one row per entity. Any one of them is enough for the
# grain concern not to apply.
_COLLAPSING = (
    "count(distinct", "group by", "row_number", "distinct ", "sum(", "avg(", "max(", "min(",
)

# A numeric-hints table heading: "schema.table  (12,345 rows)"
_NUM_HEAD_RE = re.compile(r"^(\S+)\s+\([\d,]+\s+rows\)\s*$")
# A numeric-hints column line; only the name and the trailing scale verdict are needed here.
_NUM_COL_RE = re.compile(r"^\s+-\s+(\S+):\s+min\s.*\|\s*(.+?)\s*$")
# "col > 100", "x.col >= 0.5" - a comparison against a bare numeric literal.
_COMPARISON = r"(?:\w+\.)?{col}\s*(>=|<=|<>|!=|=|>|<)\s*([0-9]+(?:\.[0-9]+)?)"


def _blocks(schema: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in re.split(r"(?=^TABLE\s)", schema or "", flags=re.MULTILINE):
        m = _TABLE_RE.search(block)
        if m:
            out.append((m.group(1), block))
    return out


def _bare(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _scales(numeric_text: str) -> dict[str, dict[str, str]]:
    """table -> column -> the measured scale verdict, for ratio columns only.

    Plain numbers and unmeasured columns are dropped: they have no natural bounds, so a
    comparison against any literal is legitimate and flagging one would be noise.
    """
    out: dict[str, dict[str, str]] = {}
    table = ""
    for line in (numeric_text or "").splitlines():
        head = _NUM_HEAD_RE.match(line)
        if head is not None:
            table = head.group(1)
            continue
        if not table:
            continue
        m = _NUM_COL_RE.match(line)
        if m is None:
            continue
        verdict = m.group(2).upper()
        if "FRACTION_1" in verdict:
            out.setdefault(table, {})[m.group(1)] = "FRACTION_1"
        elif "PERCENT_100" in verdict:
            out.setdefault(table, {})[m.group(1)] = "PERCENT_100"
    return out


def _scale_concerns(sql_low: str, table: str, columns: dict[str, str]) -> list[str]:
    found: list[str] = []
    for column, scale in columns.items():
        pattern = _COMPARISON.format(col=re.escape(column.lower()))
        for op, literal in re.findall(pattern, sql_low):
            try:
                value = float(literal)
            except ValueError:
                continue
            if scale == "FRACTION_1" and value > 1:
                found.append(
                    f"{table}.{column} was MEASURED as a 0-1 fraction, but the query compares "
                    f"it {op} {literal}. On a 0-1 column that condition is either always or "
                    f"never true, so the probe would report a silent, confident zero. Compare "
                    f"against a 0-1 value, or multiply the column by 100 first."
                )
            elif scale == "PERCENT_100" and 0 < value < 1:
                found.append(
                    f"{table}.{column} was MEASURED as a 0-100 percentage, but the query "
                    f"compares it {op} {literal}, which is a fraction. Compare against a 0-100 "
                    f"value."
                )
    return found


def check(sql: str, schema: str, numeric_text: str = "") -> list[str]:
    """Human-readable concerns about one query. An empty list means nothing suspicious.

    A clean result is NOT a guarantee of correctness - it means these specific traps were
    avoided.
    """
    if not sql or not schema:
        return []
    low = " ".join(sql.split()).lower()
    concerns: list[str] = []
    scales = _scales(numeric_text)

    for table, block in _blocks(schema):
        if table.lower() not in low:
            continue

        # 1. A table introspection flagged as holding many rows per key, read without
        #    collapsing it. In an anomaly probe this is worse than in a chatbot answer: it
        #    inflates anomaly_count above scope_total, which contract.sanity_concerns then
        #    reports as an impossible result.
        for key in _DUP_RE.findall(block):
            if not any(token in low for token in _COLLAPSING):
                concerns.append(
                    f"{table} holds many rows per {_bare(key)} but the query does not aggregate "
                    f"or de-duplicate it, so each entity is counted more than once"
                )
                break

        # 2. A declared foreign key joined to the wrong target column.
        for column, target_table, target_column in _FK_RE.findall(block):
            if f"{column.lower()} =" not in low and f"{column.lower()}=" not in low:
                continue
            if target_table.lower() not in low:
                continue
            if target_column.lower() not in low:
                concerns.append(
                    f"{table}.{column} is declared to join {target_table}.{target_column}, but "
                    f"that column does not appear in the query"
                )

        # 3. A measured ratio column compared against a literal on the wrong side of 1.
        concerns.extend(_scale_concerns(low, table, scales.get(table, {})))

    return concerns


def check_probe(summary_sql: str, detail_sql: str, schema: str, numeric_text: str = "") -> list[str]:
    """Concerns across both halves of a probe, de-duplicated and order-preserving.

    The two queries are checked together because they are meant to express the SAME condition
    at the SAME grain: a concern raised on only one of them is usually the drift itself.
    """
    try:
        found = check(summary_sql, schema, numeric_text) + check(detail_sql, schema, numeric_text)
    except Exception as exc:  # noqa: BLE001 - an advisory signal must never break a compile
        log.warning("sanity: schema check skipped (%s)", exc)
        return []
    return list(dict.fromkeys(found))
