"""Context Builder node (deterministic) - measures, so the Scout only has to judge.

THIS NODE DECIDES WHETHER DISCOVERY IS USEFUL OR NOISE.

Asking a model "what might be wrong with this database?" invites it to invent plausible-sounding
problems, and a list of invented problems is worse than no list: somebody has to read and refuse
each one, and after a few rounds of that they stop reading. So the model is never asked that
question. This node extracts FACTS - counted, from the schema and the measured hints the engine
already builds - and the Scout is asked only which of those facts are business problems.

Every observation here is derived by code from something already measured. No LLM, no sampling,
no guessing. That is what lets the next node refuse any proposal that cannot cite one.

WHAT IS LOOKED FOR, AND WHY EACH IS ANOMALY-SHAPED
--------------------------------------------------
  UNCONSTRAINED KEY   a column named like a foreign key with no foreign key declared behind it.
                      This is the single highest-value signal in the whole file: DQ-D36 - 6,054
                      task records naming a well that does not exist - is exactly this shape,
                      and nothing structural caught it precisely BECAUSE no constraint exists to
                      be violated. The engine's own foreign-key family can only check
                      relationships the database declares; this finds the ones it does not.

  OUT OF RANGE        a number whose measured maximum or minimum falls outside the range its
                      classified scale implies. A progress column reading 340 on a 0-100 scale
                      is not a judgement call.

  UNREADABLE NUMBER   a column that holds quantities as text where some values do not parse.

  MOSTLY EMPTY        a column populated so rarely that anything joining or filtering on it
                      silently drops most of the data.

  HISTORY GRAIN       a table marked MANY ROWS PER something. Regressions, contradictions
                      between updates and "the latest record disagrees with the previous one"
                      all live in these tables and nowhere else.

  SINGLETON CODE      a coded column where a value appears once or twice among thousands. Either
                      a typo, or a category nobody maintains.

WHAT IS DELIBERATELY NOT LOOKED FOR. Anything requiring a query against the data. This node runs
from CACHED artefacts only - they are already fingerprinted against the live database, so they
are current by the time it reads them - which keeps discovery cheap enough to run on a whim and
means it never adds load to the source system.
"""
from __future__ import annotations

import re

from app.db import introspect
from app.graph.discover_state import DiscoverState
from app.observability import get_logger
from app.rules import discoveries as discovery_store
from app.rules.schema_index import load_index

log = get_logger()

# Column-name shapes that mean "this points at another thing". Conservative on purpose: a false
# positive here becomes a proposal somebody has to refuse, which is exactly the cost this whole
# design is trying to avoid.
_KEY_SUFFIX = re.compile(r"(_id|_code|_no|_number|_key|_ref)$", re.IGNORECASE)
# Names that look like keys but identify the row itself rather than pointing anywhere.
_SELF_KEY = re.compile(r"^(id|row_?id|uid|guid|seq|sequence|line_?no)$", re.IGNORECASE)

# A column this empty makes any join or filter through it lose most of the table.
_MOSTLY_EMPTY_SHARE = 0.60
# A coded value this rare among many is a typo or an unmaintained category, not a real class.
_SINGLETON_MAX_COUNT = 2
_SINGLETON_MIN_ROWS = 500
# Tables smaller than this are not interesting: a "problem" affecting 3 rows is noise, and a
# proposal built on one wastes a decision.
_MIN_TABLE_ROWS = 50


def _observation(kind: str, table: str, column: str, fact: str, **extra) -> dict:
    return {"kind": kind, "table": table, "column": column, "fact": fact, **extra}


def _unconstrained_keys(index) -> list[dict]:
    """Columns named like a key with no declared relationship behind them."""
    out: list[dict] = []
    for name, table in sorted(index.tables.items()):
        if (table.row_count or 0) < _MIN_TABLE_ROWS:
            continue
        constrained = {fk.child_column.lower() for fk in (table.foreign_keys or [])}
        for column in table.columns:
            lower = column.name.lower()
            if lower in constrained or _SELF_KEY.match(lower) or not _KEY_SUFFIX.search(lower):
                continue
            # A plausible target: another table whose name echoes the column's stem.
            stem = _KEY_SUFFIX.sub("", lower)
            targets = [
                other for other in index.tables
                if stem and stem in other.lower().split(".")[-1] and other != name
            ]
            if not targets:
                continue
            out.append(_observation(
                "unconstrained_key", name, column.name,
                f"{name}.{column.name} is named like a reference and {name} holds "
                f"{table.row_count:,} rows, but the database declares NO relationship for it. "
                f"A table it may be meant to point at: {targets[0]}. Nothing verifies that the "
                f"values actually exist there.",
                candidate_target=targets[0],
            ))
    return out


def _numeric_problems(index) -> list[dict]:
    """Values outside the range their own measured scale implies, and unreadable quantities.

    The range comes from NumericStat.bounds(), never from a scale string parsed here. That
    method derives the range from the column's AVERAGE on purpose - the maximum is precisely the
    value most likely to be corrupt, so choosing bounds from it would let one bad row widen the
    range until that row looks legal. Re-deriving bounds locally would quietly undo that.
    """
    out: list[dict] = []
    for name, table in sorted(index.tables.items()):
        if (table.row_count or 0) < _MIN_TABLE_ROWS:
            continue
        for column in table.columns:
            stat = (table.stats or {}).get(column.name)
            if stat is None:
                continue

            bound = stat.bounds()
            if bound is not None:
                low, high = bound
                if stat.hi is not None and stat.hi > high:
                    out.append(_observation(
                        "out_of_range", name, column.name,
                        f"{name}.{column.name} holds values on a {low:g} to {high:g} scale "
                        f"(measured average {stat.avg:,.4g}), but its highest observed value is "
                        f"{stat.hi:,.4g} - above the range it should occupy.",
                    ))
                if stat.lo is not None and stat.lo < low:
                    out.append(_observation(
                        "out_of_range", name, column.name,
                        f"{name}.{column.name} holds values on a {low:g} to {high:g} scale but "
                        f"its lowest observed value is {stat.lo:,.4g} - below that range.",
                    ))

            # A quantity kept as text where some values are not numbers. Excludes columns the
            # classifier decided hold codes: nothing parses there, and that is correct, not a
            # defect.
            if stat.text_total and stat.text_bad and not stat.text_is_codes:
                share = stat.text_bad / stat.text_total
                out.append(_observation(
                    "unreadable_number", name, column.name,
                    f"{name}.{column.name} keeps a quantity as text, and {stat.text_bad:,} of "
                    f"{stat.text_total:,} non-blank values ({share:.1%}) cannot be read as a "
                    f"number. Every calculation over this column silently omits them.",
                ))
    return out


def _mostly_empty(index) -> list[dict]:
    """Columns populated so rarely that joining or filtering through them loses most rows.

    Only columns the profiler measured are visible here - it records null_pct for the numeric
    and quantity-shaped columns it examined, not for every column in the database. So this is a
    sample of the problem, never a complete census of it, and the Scout is told as much.
    """
    out: list[dict] = []
    for name, table in sorted(index.tables.items()):
        rows = table.row_count or 0
        if rows < _MIN_TABLE_ROWS:
            continue
        for column in table.columns:
            stat = (table.stats or {}).get(column.name)
            if stat is None or not stat.null_pct:
                continue
            if stat.null_pct >= _MOSTLY_EMPTY_SHARE * 100:
                out.append(_observation(
                    "mostly_empty", name, column.name,
                    f"{name}.{column.name} is empty in {stat.null_pct}% of {rows:,} rows. "
                    f"Anything joined or filtered through it loses that share of the table "
                    f"without reporting an error.",
                ))
    return out


def _history_tables(schema_text: str) -> list[dict]:
    """Tables the schema marks as holding many rows per entity."""
    out: list[dict] = []
    for line in schema_text.splitlines():
        if "MANY ROWS PER " not in line:
            continue
        table = ""
        head = re.match(r"^TABLE\s+(\S+)", line)
        if head:
            table = head.group(1)
        marker = line.split("MANY ROWS PER ", 1)[1].strip().rstrip(".")
        out.append(_observation(
            "history_grain", table, marker,
            f"{table or 'a table'} holds MANY ROWS PER {marker} - it is a history of updates, "
            f"not one row per thing. Contradictions between successive records, and values that "
            f"go backwards, can only be seen in a table shaped like this.",
        ))
    return out


def _singleton_codes(value_hints: str) -> list[dict]:
    """Coded values that appear once or twice among many - a typo, or a dead category."""
    out: list[dict] = []
    table = column = ""
    for line in value_hints.splitlines():
        head = re.match(r"^(\S+)\.(\S+)\s*[:\-]", line.strip())
        if head:
            table, column = head.group(1), head.group(2)
        for value, count in re.findall(r"'([^']{1,60})'\s*[x×(]\s*(\d[\d,]*)", line):
            n = int(count.replace(",", ""))
            if n <= _SINGLETON_MAX_COUNT and table:
                out.append(_observation(
                    "singleton_code", table, column,
                    f"{table}.{column} holds the value '{value}' in only {n} record(s) while "
                    f"other values are common. A one-off code is usually a typo or a category "
                    f"nobody maintains.",
                ))
    return out


def context_builder_node(state: DiscoverState) -> dict:
    """Gather what is known, and measure what is odd about it."""
    try:
        schema_text = introspect.build_schema_text()
        value_hints = introspect.build_value_hints()
        numeric_hints = introspect.build_numeric_hints()
    except Exception as exc:  # noqa: BLE001 - never let a cache miss look like a model failure
        log.warning("scout: the database description is unavailable (%s)", exc)
        return {"observations": [], "context_error": (
            "The database has not been described yet, so there is nothing to look at. Run a "
            "compile first - it builds the schema and the measured hints this reads."
        )}

    index = load_index(schema_text, numeric_hints)

    observations: list[dict] = []
    observations += _unconstrained_keys(index)
    observations += _numeric_problems(index)
    observations += _mostly_empty(index)
    observations += _history_tables(schema_text)
    observations += _singleton_codes(value_hints)

    # Numbered once, here, so an id in a proposal can be checked against this exact list.
    for n, obs in enumerate(observations, 1):
        obs["id"] = f"OBS-{n:03d}"

    # WHAT IS ALREADY COVERED, from all three places a rule can live. Passing only one of them
    # is how the Scout ends up re-proposing something the operator refused last week.
    compiled = state.get("compiled_status") or {}
    covered = [
        {"rule_id": r.rule_id, "title": r.title, "category": r.category,
         "tags": ", ".join(r.tags or ()), "status": r.status,
         # What this schema already PROVED it cannot express. The engine paid real LLM calls to
         # learn that this database holds no work-breakdown table; telling the Scout means it
         # will not propose five more rules that depend on one.
         "compiled_status": compiled.get(r.rule_id, "")}
        for r in (state.get("rules") or [])
    ]
    decided = discovery_store.decided()
    rejected = [d for d in decided if d["status"] == "rejected"]

    log.info(
        "scout: %d observation(s) from %d table(s); %d rule(s) already cover ground, "
        "%d previously refused",
        len(observations), len(index.tables), len(covered), len(rejected),
    )
    return {
        "schema_block": schema_text,
        "observations": observations,
        "covered": covered,
        "rejected": rejected,
        "context_error": "",
    }
