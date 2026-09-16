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


# ── Reference material: business_rules.md and few_shots.md ─────────────────────
#
# THE SECOND-LARGEST COST IN A COMPILE, AND UNTIL NOW THE ONLY UNPRUNED ONE.
#
# The schema is cut hard for each rule (77 tables -> 9 on a recent compile, 32,554 -> 3,977
# chars). The two reference files were not cut at all: every one of ~24,000 characters went
# into EVERY author call and, for the business rules, every verifier call as well - about
# 8,000 tokens per author call regardless of what the rule was about. A rule about a missing
# milestone date carried the full task-code-to-WBS mapping chain; a rule about a work breakdown
# carried the milestone deadlines.
#
# WHY THIS IS SAFE, AND WHERE THE LINE IS DRAWN.
#
# Pruning reference material is riskier than pruning a schema. A missing table produces a query
# that fails loudly; a missing DEFINITION produces a query that runs and measures the wrong
# thing. So the bar here is deliberately higher than it is above:
#
#   * SMALL SECTIONS ARE NEVER DROPPED. Dropping a 300-character policy statement saves
#     essentially nothing and can cost a whole probe. Only the large topical sections are
#     candidates at all, and they are the only ones worth cutting anyway.
#   * A section is kept unless it is CLEARLY unrelated to the rule, measured by how much of the
#     rule's own vocabulary it speaks to. Ties and near-misses keep the section.
#   * If the result is not meaningfully smaller, the FULL text is returned - the same rule the
#     schema prune follows. A marginal saving is never worth a missing definition.
#   * Everything dropped is logged by heading, so any suspect probe can be traced back to what
#     its author was and was not shown.
#
# Nothing here knows what a business rule says. It reads the headings in the file at runtime.

# Words that carry no topic. Deliberately generic English only - adding a domain word here
# would be exactly the hardcoding this engine exists without.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here it its is are was were be
been being to of in on at by for from with without into over under again more most other some
such only own same so no nor not too very can will just should now which who whom what when
where why how all any both each few own too s t don ll re ve y ain aren couldn didn doesn hadn
hasn haven isn ma mightn mustn needn shan shouldn wasn weren won wouldn as has have had do does
did doing about against between during before after above below up down out off through
one two three must never always also may might would could per rather instead whether
rule rules check checks flag flagged data record records row rows value values
""".split())

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _terms(text: str) -> set[str]:
    """Topic words in a piece of text, with snake_case split into its parts.

    `ex_rig_on_date` contributes rig and date, so a rule speaking of "the expected rig-on date"
    in plain business language still matches a section written in column names. That matters
    more since the anomaly descriptions were rewritten into business language - the two sides
    no longer share a technical vocabulary, and matching on whole identifiers alone would find
    almost nothing and prune almost everything.
    """
    out: set[str] = set()
    for word in _WORD_RE.findall(text.lower()):
        for part in word.split("_"):
            if len(part) > 2 and part not in _STOPWORDS:
                out.add(part)
    return out


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, whole section including its heading)], with any preamble under heading ""."""
    out: list[tuple[str, str]] = []
    marks = list(_HEADING_RE.finditer(text or ""))
    if not marks:
        return [("", text or "")]
    if marks[0].start() > 0:
        out.append(("", text[: marks[0].start()]))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), text[m.start():end]))
    return out


def prune_reference(
    text: str, rule_text: str, keywords: tuple[str, ...] = (), label: str = "reference"
) -> str:
    """Cut a reference file down to the sections this one rule plausibly needs.

    `rule_text` is everything known about the rule - title, category, entity, method and full
    prose - so the match is made against the rule as a whole.

    `keywords` is the rule's OWN CLASSIFICATION: its tags, category and entity. A section
    mentioning any of them is kept outright, whatever the overall overlap says.

    THAT OVERRIDE IS NOT BELT-AND-BRACES; IT IS LOAD-BEARING, and it was added because the
    measurement below got a real rule wrong. Overlap is diluted by the length of the rule's
    prose, so a rule whose subject is named in two words but described in three hundred scores
    low on the very section that defines its subject. Observed on the weightage rollup rule:
    once its description was rewritten into plain business language the word "weightage"
    survived only in its tags, overlap fell to 3%, and the section defining weightage would
    have been dropped from the one rule that cannot be written without it.

    Tags are a deliberate statement of what a rule is ABOUT, made by whoever wrote it. That is a
    better signal than word frequency and it is the right thing to trust here.
    """
    if not settings.reference_prune or not (text or "").strip():
        return text

    sections = split_sections(text)
    if len(sections) < 4:
        # Too few sections to prune meaningfully, and the risk per section is correspondingly
        # higher. Nothing to gain.
        return text

    wanted = _terms(rule_text)
    if not wanted:
        return text

    # The rule's own classification, split the same way section text is, so a tag of "wbs" or
    # "weightage" matches however the reference file happens to spell it.
    subject = _terms(" ".join(keywords))

    kept: list[str] = []
    dropped: list[str] = []
    for heading, body in sections:
        if not heading or len(body) <= settings.reference_keep_below:
            kept.append(body)
            continue
        section_terms = _terms(heading + "\n" + body)
        if subject & section_terms:
            # This section names something the rule declares itself to be about. Keep it
            # regardless of overlap - see the docstring.
            kept.append(body)
            continue
        # How much of THIS RULE's vocabulary the section speaks to. Normalising by the rule
        # rather than by the section is deliberate: a long section is not penalised for being
        # long, and a rule is not starved because its description happens to be short.
        overlap = len(section_terms & wanted) / len(wanted)
        if overlap >= settings.reference_min_overlap:
            kept.append(body)
        else:
            dropped.append(f"{heading} ({overlap:.0%})")

    if not dropped:
        return text

    out = "".join(kept).strip()
    # The same safety rule the schema prune follows: a marginal saving is never worth the risk
    # of having cut something the rule needed.
    if len(out) > len(text) * _MIN_SAVING_RATIO:
        return text

    log.info(
        "prune: %s %d -> %d chars, dropped %s",
        label, len(text), len(out), "; ".join(dropped),
    )
    return out


# ── Worked examples: which ones this rule could actually learn from ────────────

_APPLIES_RE = re.compile(r"^\s*[-*]\s*applies\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
# "nulls 62%" in a numeric hint line. A column nobody would call substantially empty is not
# evidence for the lesson about joining through one, so the bar is well above incidental nulls.
_NULL_PCT_RE = re.compile(r"nulls\s+(\d+)%")
_SUBSTANTIALLY_EMPTY = 40


def example_conditions(
    schema_block: str, hint_block: str, method: str = "", tolerance: str = ""
) -> set[str]:
    """Which worked-example conditions hold for the rule about to be written.

    Every condition is decided from something ALREADY MEASURED and already in the prompt - the
    schema block the author will read, the hints it will read, and what the rule declared about
    itself. Nothing is inferred from a name and nothing is asked of a model.
    """
    facts = {"always"}
    if "MANY ROWS PER " in (schema_block or ""):
        facts.add("grain")
    if (hint_block or "").strip():
        # The hint block exists only when a numeric column in scope carries measured
        # statistics - which is exactly when a proportion can be misread for its scale.
        facts.add("scale")
    if any(int(m.group(1)) >= _SUBSTANTIALLY_EMPTY for m in _NULL_PCT_RE.finditer(hint_block or "")):
        facts.add("nulls")
    if (method or "").strip().lower() == "statistical" or (tolerance or "").strip():
        facts.add("threshold")
    return facts


def prune_examples(text: str, conditions: set[str]) -> str:
    """Keep the worked examples whose declared `applies:` condition holds for this rule.

    WHY THIS IS DECLARED IN THE FILE AND NOT MEASURED HERE. The first attempt at cutting this
    file scored each example's words against the rule's words, the way the business rules are
    cut. It removed "Scope is what you EXAMINED, not what you flagged" from 55 of 60 rules and
    "A threshold must come from the data" from 55 - the two lessons that prevent the most common
    and the most expensive failures this engine has.

    The cause is structural, not a bad threshold: these examples teach probe craft in the
    ENGINE's vocabulary, while the anomalies are described in the BUSINESS's. The two will never
    match well, and they match even less now that the anomaly file has been rewritten into plain
    language. So the file declares its own applicability and this function only reads it.

    An example with no recognised declaration is KEPT. The failure this whole path must avoid is
    silently withholding a lesson, so anything unclear resolves to sending it.
    """
    if not settings.reference_prune or not (text or "").strip():
        return text

    sections = split_sections(text)
    kept: list[str] = []
    dropped: list[str] = []
    for heading, body in sections:
        m = _APPLIES_RE.search(body)
        applies = m.group(1).strip().lower() if m else "always"
        if not heading or applies in conditions:
            kept.append(body)
        else:
            dropped.append(f"{heading} (needs {applies})")

    if not dropped:
        return text
    out = "".join(kept).strip()
    log.info(
        "prune: worked examples %d -> %d chars, dropped %s",
        len(text), len(out), "; ".join(dropped),
    )
    return out
