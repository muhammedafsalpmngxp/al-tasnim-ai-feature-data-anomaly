"""Cut the reference material down to what ONE rule actually needs.

THIS MODULE IS THE CONTEXT-WINDOW AND COST CONTROL. Measured on the live database:

    schema.txt         37,162 chars   ~9,300 tokens
    numeric_hints.txt  30,341 chars   ~7,600 tokens
    value_hints.txt     6,010 chars   ~1,500 tokens
                                     ~18,400 tokens, on EVERY author and verifier call

A rule concerns a handful of tables, not eighty-two. Handing over the other seventy-odd costs
real money on every retry of every rule and - more importantly - buries the tables that matter
in noise, which measurably degrades the SQL. Grounding names the relevant tables; everything
here slices the three artefacts down to them.

The prune is a SUPERSET of what Grounding named: a named table, plus anything one declared
foreign key away in either direction. Joins are how a probe reaches its evidence, and a block
listing a table without the table it joins to is worse than no prune at all.

SAFETY RULE, APPLIED THROUGHOUT: pruning can only ever be an optimisation. Whenever the result
would be empty, doubtful, or barely smaller than the original, the FULL text is returned. A
starved author is far more expensive than a large prompt - it writes a wrong query, which costs
a validator round trip, an executor round trip, a verifier call and a rewrite.

Nothing here names a table or a column. Every name is read at runtime from the artefacts that
introspection already wrote.
"""
from __future__ import annotations

import re

from app.config import settings
from app.observability import get_logger

log = get_logger()

# "TABLE schema.table" - the block delimiter in schema.txt (see introspect._render()).
#
# THE TRAILING ANYTHING IS LOAD-BEARING. introspect._render() appends a comment to this line
# ("TABLE dbo.foo  -- 10,302 rows"), and an end-anchored pattern silently stopped matching it.
# Nothing failed: split_blocks() simply returned the handful of tables whose row count was
# unknown, len(blocks) fell under the prune threshold, and prune_schema() returned the FULL
# schema on every author and verifier call. A prune that quietly stops pruning costs money on
# every call and is invisible in every log, so this regex must tolerate anything after the name.
_TABLE_RE = re.compile(r"^TABLE\s+(\S+)[^\n]*$", re.MULTILINE)
# "  FK: child_col -> schema.table.parent_col"
_FK_RE = re.compile(r"^\s+FK:\s+\S+\s*->\s*(\S+)\.\w+\s*$", re.MULTILINE)
# A value-hints line: "- schema.table (col, col): value; value"
_VALUE_LINE_RE = re.compile(r"^-\s+(\S+)\s+\(")
# A numeric-hints table heading: "schema.table  (12,345 rows)"
_NUMERIC_HEAD_RE = re.compile(r"^(\S+)\s+\([\d,]+\s+rows\)\s*$")
# A column declaration line, used only to list column names for the grounding index.
_COLUMN_NAME_RE = re.compile(r"^\s+-\s+(\[[^\]]+\]|\S+)\s", re.MULTILINE)

# Upper bound on a pruned block. A rule legitimately touching thirty tables is not a prune
# candidate - at that point the full block is both simpler and no more expensive.
_MAX_PRUNED_TABLES = 25
# Below this saving, pruning is not worth the risk of having cut something the rule needed.
_MIN_SAVING_RATIO = 0.85


def split_blocks(schema_text: str) -> dict[str, str]:
    """schema.table -> its block of the schema text, in file order."""
    out: dict[str, str] = {}
    for part in re.split(r"(?=^TABLE\s)", schema_text or "", flags=re.MULTILINE):
        m = _TABLE_RE.search(part)
        if m:
            out[m.group(1)] = part.rstrip() + "\n"
    return out


def table_names(schema_text: str) -> list[str]:
    return list(split_blocks(schema_text).keys())


def _bare(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def table_index(schema_text: str, numeric_text: str = "") -> str:
    """One line per table: its name, HOW MANY ROWS IT HOLDS, and its column names.

    This is what Grounding reads instead of the full schema. Types, nullability, primary keys
    and foreign keys are what the AUTHOR needs in order to write correct SQL; Grounding only
    has to answer "which tables is this rule about?", and a name list is both sufficient for
    that and roughly a third of the size.

    THE ROW COUNT IS THERE BECAUSE NAMES ALONE MISLEAD IT. This database holds an EMPTY table
    whose name and 41 columns read almost identically to the populated one beside it. Shown
    only names, Grounding cannot tell them apart and picked the empty one - so seven rules
    compiled against a table with no rows, examined nothing, and were filed "not applicable to
    this database" when the concept was perfectly expressible against its populated twin.

    A count is measured evidence, not another naming rule, and it is the one fact that
    distinguishes them.
    """
    counts: dict[str, int | None] = {}
    if numeric_text:
        try:
            from app.rules.schema_index import load_index

            counts = {
                name: table.row_count
                for name, table in load_index(schema_text, numeric_text).tables.items()
            }
        except Exception:  # noqa: BLE001 - a missing count must not cost the whole index
            counts = {}

    lines: list[str] = []
    for table, block in split_blocks(schema_text).items():
        cols = [_bare(c) for c in _COLUMN_NAME_RE.findall(block)]
        rows = counts.get(table)
        # "EMPTY" rather than "0 rows": it has to be impossible to skim past. A table with no
        # rows can support no finding at all, whatever its name suggests.
        size = ""
        if table in counts:
            size = f" [{rows:,} rows]" if rows else " [EMPTY - nothing can be found here]"
        lines.append(f"{table}{size}: {', '.join(cols)}")
    return "\n".join(lines)


def _neighbours(blocks: dict[str, str], wanted: set[str]) -> set[str]:
    """`wanted` plus every table one declared foreign key away, in either direction.

    Both directions matter, for different reasons. Forward (a named table's own FK lines) finds
    the lookup a probe must join in order to decode a code. Reverse (another table whose FK
    points INTO a named table) finds the child rows a rollup rule has to aggregate - and a
    rollup is exactly the kind of rule that is impossible to write without them.
    """
    out = set(wanted)
    for table in wanted:
        for parent in _FK_RE.findall(blocks.get(table, "")):
            if parent in blocks:
                out.add(parent)
    for table, block in blocks.items():
        if table in out:
            continue
        if any(parent in wanted for parent in _FK_RE.findall(block)):
            out.add(table)
    return out


def _resolve(blocks: dict[str, str], names: list[str]) -> set[str]:
    """Match what Grounding named against what actually exists.

    A model asked for table names returns them in whatever form it read them, so an exact match
    is not enough: it may drop the schema prefix or change the case. Both are resolved here
    rather than being treated as "named nothing", which would silently disable pruning for the
    whole rule. A name matching nothing is dropped and logged - never invented into existence.
    """
    by_lower = {t.lower(): t for t in blocks}
    by_bare: dict[str, list[str]] = {}
    for t in blocks:
        by_bare.setdefault(t.partition(".")[2].lower(), []).append(t)

    resolved: set[str] = set()
    for raw in names or []:
        name = (raw or "").strip().replace("[", "").replace("]", "").lower()
        if not name:
            continue
        if name in by_lower:
            resolved.add(by_lower[name])
            continue
        candidates = by_bare.get(name.rpartition(".")[2], [])
        if len(candidates) == 1:
            resolved.add(candidates[0])
        elif candidates:
            # The same table name in several schemas. Keeping them all is the safe reading:
            # dropping the wrong one starves the author of the table it actually needs.
            resolved.update(candidates)
        else:
            log.info("prune: grounding named %r, which is not in the schema - ignored", raw)
    return resolved


def prune_schema(schema_text: str, names: list[str]) -> tuple[str, list[str]]:
    """(schema block for the prompt, the tables it contains).

    Returns the FULL text unchanged whenever pruning is not clearly safe and worthwhile: the
    database is small, Grounding named nothing resolvable, the rule spans too many tables, or
    the saving does not justify the risk of a missing join.
    """
    blocks = split_blocks(schema_text)
    if not blocks:
        return schema_text, []

    if len(blocks) <= settings.schema_prune_above_tables:
        return schema_text, list(blocks)

    wanted = _resolve(blocks, names)
    if not wanted:
        log.info(
            "prune: nothing resolvable was named - using the full schema (%d tables)", len(blocks)
        )
        return schema_text, list(blocks)

    keep = _neighbours(blocks, wanted)
    if len(keep) > _MAX_PRUNED_TABLES:
        # Drop the FK expansion before abandoning the prune entirely: the named tables are what
        # the rule is actually about, and they are still far fewer than the whole database.
        keep = wanted
    if len(keep) > _MAX_PRUNED_TABLES or len(keep) > len(blocks) * _MIN_SAVING_RATIO:
        log.info(
            "prune: %d of %d tables is not worth pruning - using the full schema",
            len(keep), len(blocks),
        )
        return schema_text, list(blocks)

    ordered = [t for t in blocks if t in keep]
    text = "\n".join(blocks[t] for t in ordered).strip()
    log.info(
        "prune: schema %d -> %d tables (%s -> %s chars)",
        len(blocks), len(ordered), f"{len(schema_text):,}", f"{len(text):,}",
    )
    return text, ordered


def slice_value_hints(hints: str, tables: list[str]) -> str:
    """Value hints for `tables` only, keeping the explanatory header line.

    Returns "" when no table matched, rather than a lone heading: a section promising real
    coded values and then listing none is actively misleading to the model reading it.
    """
    if not hints or not tables:
        return hints
    keep = {t.lower() for t in tables}
    lines = hints.splitlines()
    out = lines[:1]
    matched = 0
    for line in lines[1:]:
        m = _VALUE_LINE_RE.match(line)
        if m is not None and m.group(1).lower() in keep:
            out.append(line)
            matched += 1
    return "\n".join(out) if matched else ""


def slice_numeric_hints(hints: str, tables: list[str]) -> str:
    """Numeric hints for `tables` only.

    Structure-aware rather than line-filtered: a column line is indented and carries no table
    name of its own, so it is kept or dropped according to the heading it currently sits under.
    """
    if not hints or not tables:
        return hints
    keep = {t.lower() for t in tables}
    lines = hints.splitlines()
    out = lines[:1]
    including = False
    matched = 0
    for line in lines[1:]:
        m = _NUMERIC_HEAD_RE.match(line)
        if m is not None:
            including = m.group(1).lower() in keep
            if including:
                out.append(line)
                matched += 1
            continue
        if including:
            out.append(line)
    return "\n".join(out) if matched else ""


def build_context(
    schema_text: str, value_hints: str, numeric_hints: str, names: list[str]
) -> tuple[str, str, list[str]]:
    """(schema block, hint block, tables) - everything one rule's prompts need, pruned together.

    The three artefacts are sliced against ONE resolved table list so they can never describe
    different sets of tables. Slicing them independently is how a prompt ends up stating the
    measured scale of a column whose table is not in the schema block at all.
    """
    schema_block, tables = prune_schema(schema_text, names)
    values = slice_value_hints(value_hints, tables)
    numbers = slice_numeric_hints(numeric_hints, tables)
    hint_block = "\n\n".join(p for p in (values, numbers) if p.strip())
    return schema_block, hint_block, tables
