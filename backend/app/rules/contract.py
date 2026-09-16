"""Deterministic checks on a probe - on its SQL text before it runs, and on what it returned.

Three kinds, kept apart on purpose:

  static_problems                HARD CONTRACT, checked on the SQL TEXT before any database
                                 round trip. Everything here is decidable without executing
                                 anything, so catching it early saves a connection, a scan and
                                 a wasted verifier call. Fed back to the SQL Author, and paid
                                 for by the MAX_SQL_RETRIES budget.

  check_summary / check_detail   HARD CONTRACT, checked on what actually came back. A
                                 violation means the query cannot be used at all - the report
                                 has nowhere to put its output. Paid for by the same
                                 MAX_SQL_RETRIES budget.

  sanity_concerns                ADVISORY. The query ran and satisfies the contract, but the
                                 NUMBERS look like a mistake. Never fails a rule on its own:
                                 it is handed to the Verifier as evidence to adjudicate, the
                                 same way app/graph/sqlcheck.py hands over schema-derived
                                 concerns. A conservative check can flag a probe that is fine.

WHY static_problems() EXISTS RATHER THAN LIVING IN THE TEST SUITE
-----------------------------------------------------------------
These requirements were originally asserted only in eval/test_rules.py, which reads the SQL a
person wrote in markdown. Authored SQL - written by a model, never reviewed by a person - was
held to a LOWER bar than hand-written SQL, which is exactly backwards. One implementation now
serves both, so the rules stated in the SQL Author's prompt, the rules enforced at compile
time, and the rules the test suite checks cannot drift apart.

WHY THE CONTRACT IS CHECKED AGAINST cursor.description, NEVER BY PARSING SQL
---------------------------------------------------------------------------
Parsing SELECT aliases out of T-SQL is fragile - CTEs, nested selects, computed expressions,
bracketed identifiers - and worse, it answers the wrong question. What matters is what the
driver actually handed back. The driver tells us for free, and it cannot disagree with itself.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from app.rules.spec import (
    DETAIL_RECOMMENDED,
    DETAIL_REQUIRED,
    EVIDENCE_PREFIX,
    SUMMARY_OPTIONAL,
    SUMMARY_REQUIRED,
)

# ── Static contract: decidable from the SQL text alone ─────────────────────────
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
_SELECT_TOP = re.compile(r"\bselect\s+top\b", re.IGNORECASE)
# Division by an identifier or a parenthesised expression. A literal divisor (/ 100) cannot be
# zero, so it is deliberately not matched.
_DIVISION = re.compile(r"/\s*[A-Za-z_(]")
_UNRESOLVED = re.compile(r"\{\{")
# How far after a '/' the guard may appear. Long enough for CAST(... / NULLIF(...)) spacing,
# short enough that an unrelated NULLIF later in the query cannot vouch for this division.
_NULLIF_WINDOW = 40


def _alias(column: str) -> re.Pattern:
    """Matches the column being ALIASED, tolerating extra spacing and [brackets].

    Deliberately more permissive than a plain substring test: `AS  [anomaly_count]` is correct
    T-SQL, and failing it would spend a rewrite on whitespace.
    """
    return re.compile(rf"\bAS\s+\[?{re.escape(column)}\]?\b", re.IGNORECASE)


def blank_literals(sql: str) -> str:
    """Replace every '...' string literal with '' so its contents cannot be read as code.

    Same reasoning as the Validator's scrubber: a keyword or an operator inside DATA is not
    code. A '/' inside text is not a division, and flagging it would make the guard below cry
    wolf until somebody switched it off. Written as a scan rather than a regex because ''
    escapes a quote inside a literal.
    """
    out: list[str] = []
    i, n = 0, len(sql or "")
    while i < n:
        if sql[i] == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments, preserving the length-independent structure.

    REQUIRED, not tidy. A comment is prose, and every structural check below would otherwise
    read it as code, in both directions:

      * a false POSITIVE - an author explaining a weighting as "equal-weighted (1/N)" had the
        probe rejected for dividing without a NULLIF guard. Observed on a real rule, three
        times, until the retries ran out and a correct probe was recorded as failed;
      * a false NEGATIVE, which is worse - a commented-out ORDER BY would satisfy the check
        that DETAIL is ordered, and the probe would then report an arbitrary sample of rows as
        though they were the worst ones.

    A comment marker inside a string literal is not a comment, so literals are skipped here
    rather than being handled in a separate pass - the same reasoning as the Validator's
    scrubber, where doing the two separately is outright exploitable.
    """
    out: list[str] = []
    i, n = 0, len(sql or "")
    while i < n:
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            out.append(" ")
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        if sql[i] == "'":
            start = i
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(sql[start:i])
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def code_of(sql: str) -> str:
    """The executable part of a query: no comments, and no literal contents."""
    return blank_literals(strip_comments(sql))


def _division_problems(sql: str, which: str) -> list[str]:
    problems: list[str] = []
    bare = code_of(sql)
    for m in _DIVISION.finditer(bare):
        window = bare[m.start(): m.start() + _NULLIF_WINDOW]
        if "nullif" in window.lower():
            continue
        problems.append(
            f"{which} divides without a NULLIF guard near {window.strip()[:32]!r}. A probe runs "
            f"unattended, so a divide-by-zero is a failed rule rather than a visible error - "
            f"write the divisor as NULLIF(<divisor>, 0)."
        )
    return problems


def static_problems(summary_sql: str, detail_sql: str, rule_id: str) -> list[str]:
    """Hard contract violations visible in the SQL text. Empty list means it passed.

    Every message is phrased as an instruction, because it is fed verbatim to the SQL Author.
    """
    problems: list[str] = []
    summary = summary_sql or ""
    detail = detail_sql or ""

    if not summary.strip():
        problems.append("The SUMMARY query is missing entirely. A probe is always a PAIR.")
    if not detail.strip():
        problems.append("The DETAIL query is missing entirely. A probe is always a PAIR.")
    if not summary.strip() or not detail.strip():
        return problems

    # Every structural check runs on the EXECUTABLE text. An alias, an ORDER BY or a TOP that
    # appears only in a comment is not in the query at all, and reading one as though it were
    # would approve a probe that does not do what the comment says.
    summary_code = code_of(summary)
    detail_code = code_of(detail)
    # Literals survive here: the rule_id check below is specifically looking for one.
    summary_text = strip_comments(summary)

    for column in SUMMARY_REQUIRED:
        if not _alias(column).search(summary_code):
            problems.append(
                f"SUMMARY does not alias a column {column}. It must be aliased exactly "
                f"AS {column}, because the runner reads the result by that name."
            )
    if rule_id and f"'{rule_id}'" not in summary_text:
        problems.append(
            f"SUMMARY must carry its own id as a literal aliased AS rule_id, written exactly "
            f"'{rule_id}'. The report keys every finding on it."
        )

    for column in DETAIL_REQUIRED:
        if not _alias(column).search(detail_code):
            problems.append(
                f"DETAIL does not alias a column {column}. Every findings row must carry "
                f"{column} - the stable identifier of the thing that is wrong, so somebody can "
                f"go and fix that record."
            )
    if EVIDENCE_PREFIX not in detail_code.lower():
        problems.append(
            f"DETAIL has no {EVIDENCE_PREFIX}* column. Add at least one column carrying the "
            f"actual values that PROVE the anomaly (the dates, the percentages, the counts). A "
            f"finding the reader cannot verify from the row itself is an assertion, not a "
            f"finding."
        )
    if not _ORDER_BY.search(detail_code):
        problems.append(
            "DETAIL has no ORDER BY. The runner caps how many rows are kept, so without an "
            "explicit worst-first ordering the report would show an arbitrary sample instead of "
            "the rows that matter."
        )
    if _SELECT_TOP.search(detail_code):
        problems.append(
            "DETAIL uses TOP. Remove it - the runner applies the cap. A hand-added TOP makes "
            "anomaly_count and the number of detail rows disagree, which is reported as the two "
            "queries contradicting each other."
        )

    problems += _division_problems(summary, "SUMMARY")
    problems += _division_problems(detail, "DETAIL")

    if _UNRESOLVED.search(summary + detail):
        problems.append(
            "An unresolved {{placeholder}} is still present. Write the real value - the query is "
            "stored ready to run, with no substitution step left after this one."
        )
    return problems


def columns_of(cursor_description: Sequence[Any] | None) -> list[str]:
    """Column names from a pyodbc cursor.description, lowercased.

    Lowercased because T-SQL is case-insensitive about aliases: a probe writing
    `AS Anomaly_Count` is correct SQL and must not fail a contract check over capitalisation.
    """
    if not cursor_description:
        return []
    return [str(d[0]).strip().lower() for d in cursor_description]


def check_summary(columns: Sequence[str], row_count: int) -> list[str]:
    """Hard contract for a SUMMARY query. Empty list means it passed."""
    problems: list[str] = []
    have = {c.lower() for c in columns}

    missing = [c for c in SUMMARY_REQUIRED if c not in have]
    if missing:
        problems.append(
            "SUMMARY is missing required column(s): "
            + ", ".join(missing)
            + f". It must SELECT exactly one row aliased {', '.join(SUMMARY_REQUIRED)}"
            + f" (optionally also {', '.join(SUMMARY_OPTIONAL)})."
        )

    # Exactly one row. A SUMMARY returning many rows means the query forgot to aggregate, and
    # every count downstream - the dashboard, the severity rollup, the headline figure in the
    # report - would silently describe an arbitrary first row.
    if row_count == 0:
        problems.append(
            "SUMMARY returned NO rows. It must always return exactly one row, including when "
            "nothing is anomalous (anomaly_count = 0). Aggregate over the scope rather than "
            "filtering the scope down to the anomalies."
        )
    elif row_count > 1:
        problems.append(
            f"SUMMARY returned {row_count} rows; it must return exactly one. Remove the "
            "GROUP BY, or aggregate it away in an outer SELECT."
        )
    return problems


def check_detail(columns: Sequence[str]) -> list[str]:
    """Hard contract for a DETAIL query. Empty list means it passed."""
    problems: list[str] = []
    have = {c.lower() for c in columns}

    missing = [c for c in DETAIL_REQUIRED if c not in have]
    if missing:
        problems.append(
            "DETAIL is missing required column(s): "
            + ", ".join(missing)
            + ". Every findings row must carry entity_key - the stable identifier of the thing "
            "that is wrong, so someone can go and fix that record."
        )

    if not any(c.startswith(EVIDENCE_PREFIX) for c in have):
        problems.append(
            f"DETAIL has no {EVIDENCE_PREFIX}* column. Add at least one column carrying the "
            "actual values that PROVE the anomaly (the dates, the percentages, the counts). "
            "A finding the reader cannot verify from the row itself is an assertion, not a "
            "finding."
        )
    return problems


def detail_advice(columns: Sequence[str]) -> list[str]:
    """Non-blocking notes on a DETAIL result - recommended columns that are absent.

    Deliberately NOT part of check_detail: a probe without entity_label is still usable, and
    failing it would spend a rewrite on cosmetics.
    """
    have = {c.lower() for c in columns}
    return [
        f"DETAIL has no {c} column - the report will fall back to a less readable default"
        for c in DETAIL_RECOMMENDED
        if c not in have
    ]


def summary_values(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    """The SUMMARY row as a dict keyed by lowercased column name.

    Tolerates extra columns: a probe may legitimately select more than the contract requires,
    and the extras are simply ignored rather than treated as a violation.
    """
    return {str(c).strip().lower(): v for c, v in zip(columns, row)}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sanity_concerns(values: dict[str, Any], detail_row_count: int | None = None) -> list[str]:
    """Advisory observations about a probe's NUMBERS. Never fails a rule by itself.

    Each one is a pattern that is usually a bug and occasionally legitimate, which is exactly
    why they are adjudicated by the Verifier rather than enforced here.
    """
    concerns: list[str] = []
    scope = _as_int(values.get("scope_total"))
    count = _as_int(values.get("anomaly_count"))

    if scope == 0:
        # The single most common silent failure in a generated probe. A query joining on the
        # wrong column, or filtering on a value that does not exist, returns scope_total = 0 -
        # and then anomaly_count = 0, which reads as a clean bill of health. It is not: the
        # probe examined nothing.
        concerns.append(
            "scope_total is 0 - the probe examined NO rows at all. This is almost always a "
            "join on the wrong column or a filter on a value that does not exist, not a clean "
            "result. Confirm the query can ever match anything."
        )
    elif count is not None and scope is not None:
        if count > scope:
            concerns.append(
                f"anomaly_count ({count}) exceeds scope_total ({scope}), which is impossible - "
                "the two are being counted at different grains. The detail query is probably "
                "joining a one-to-many table without de-duplicating it."
            )
        elif count == scope and scope > 0:
            concerns.append(
                f"every row in scope is flagged ({count} of {scope}). Occasionally real, but "
                "usually means the condition is inverted, the scale is wrong (comparing a 0-1 "
                "fraction against 100), or the filter belongs in the scope rather than the test."
            )

    # SUMMARY and DETAIL are two separate queries and can drift apart - especially a hand-pinned
    # pair, where an edit to one is easy to forget in the other. When the detail was NOT capped,
    # the two row counts must agree exactly.
    if (
        detail_row_count is not None
        and count is not None
        and detail_row_count != count
    ):
        concerns.append(
            f"SUMMARY reports {count} anomalies but DETAIL returned {detail_row_count} rows. "
            "The two queries disagree - they must apply the SAME condition to the SAME grain."
        )

    pct = values.get("anomaly_pct")
    if pct is not None and scope:
        try:
            expected = 100.0 * (count or 0) / scope
            if abs(float(pct) - expected) > 0.5:
                concerns.append(
                    f"anomaly_pct ({float(pct):.2f}) does not match anomaly_count/scope_total "
                    f"({expected:.2f}). Compute it as "
                    "100.0 * anomaly_count / NULLIF(scope_total, 0)."
                )
        except (TypeError, ValueError):
            concerns.append("anomaly_pct is not numeric.")

    return concerns


# ── Grain: one findings row per THING, not per stored record ───────────────────

# Below this many sample rows there is not enough evidence: two rows sharing a key could be
# chance, and rejecting on it would spend a full authoring cycle to find that out.
_GRAIN_MIN_SAMPLE = 5
# How introspection marks a table that holds more than one row per entity. Written by
# db/introspect.py; matched here rather than re-derived, so the two cannot disagree about what
# a repeated grain looks like.
_GRAIN_MARKER = "MANY ROWS PER "
# A sample is judged repetitive once its distinct rows fall to this share of its total.
#
# MEASURED, not chosen. On the report that prompted this, the 25-row samples separated
# completely: every correct probe returned 25 distinct rows from 25 (a share of 1.00), while
# every probe counting update history returned 5 to 15 (0.20 to 0.60). 0.80 sits in the empty
# gap between them, so neither a coincidental duplicate nor a genuinely repetitive probe lands
# near the line.
_GRAIN_MAX_DISTINCT_SHARE = 0.80


def _indistinguishable(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> tuple[int, int]:
    """(sample rows, how many are distinct once entity_key is ignored).

    A reader cannot act on two findings that differ only by a record id. Comparing everything
    EXCEPT the key is what exposes the real duplication: in the report that prompted this, the
    keys were all distinct - they were record ids - while the task code, the progress, the flag
    and the sentence were identical four times over.
    """
    have = [str(c).strip().lower() for c in columns]
    keep = [i for i, c in enumerate(have) if c != "entity_key"]
    if not keep:
        return 0, 0
    seen = {tuple(str(r[i]) if i < len(r) else "" for i in keep) for r in rows}
    return len(rows), len(seen)


def _worst_repeat(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """The most-repeated finding in the sample, quoted - or "" when nothing repeats.

    NAMING THE REPEAT IS WHAT MAKES THE REJECTION ACTIONABLE. The first version of this message
    reported only the arithmetic - "25 rows reduce to 10" - and left the author to work out
    WHICH column identified the thing being repeated. Three rules then failed three attempts
    each without ever reaching the reviewer, while the rules that happened to guess the right
    column passed first time. The difference was guesswork, not capability.

    The sample already contains the answer, so it is quoted back: the label that repeats, how
    often, and the columns that were identical every time. That turns "de-duplicate somehow"
    into "these rows are the same record - group on this".
    """
    have = [str(c).strip().lower() for c in columns]
    keep = [i for i, c in enumerate(have) if c != "entity_key"]
    if not keep or not rows:
        return ""

    groups: dict[tuple, list[Sequence[Any]]] = {}
    for row in rows:
        key = tuple(str(row[i]) if i < len(row) else "" for i in keep)
        groups.setdefault(key, []).append(row)

    worst_key, worst_rows = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(worst_rows) < 2:
        return ""

    label = ""
    if "entity_label" in have:
        at = have.index("entity_label")
        label = str(worst_rows[0][at]) if at < len(worst_rows[0]) else ""
    if not label:
        label = next((v for v in worst_key if v), "")

    keys = ""
    if "entity_key" in have:
        at = have.index("entity_key")
        values = [str(r[at]) for r in worst_rows[:4] if at < len(r)]
        keys = ", ".join(values)

    return (
        f" For example {label[:60]!r} appears {len(worst_rows)} times in the sample, under "
        f"entity_key {keys} - identical in every other column. Those are one thing, not "
        f"{len(worst_rows)}: whatever distinguishes those rows is a version of the record, not "
        f"a separate finding, so group on the value that identifies the THING and keep only its "
        f"current version."
    )


def grain_concerns(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """ADVISORY: the findings repeat, but whether that is wrong depends on the rule.

    Kept separate from the hard check on purpose. Rows identical except for their key are
    SOMETIMES legitimate - a hundred child records pointing at one missing parent are a hundred
    real records to fix, and they will look alike. The reviewer has the rule in front of it and
    can tell the two apart; this function cannot, so it reports rather than refuses.
    """
    total, distinct = _indistinguishable(columns, rows)
    if total < _GRAIN_MIN_SAMPLE or distinct == 0:
        return []
    if distinct > total * _GRAIN_MAX_DISTINCT_SHARE:
        return []
    return [
        f"the findings repeat: {total} sample row(s) reduce to {distinct} once entity_key is "
        f"ignored, so the same record is being reported several times under different keys. "
        "Where the source keeps one row per update, this means the probe is counting UPDATES "
        "rather than things - every count it produces is multiplied by the number of times each "
        "thing was touched. Confirm the probe resolves to the current record per entity before "
        "judging it."
    ]


def grain_problems(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    truncated: bool = False,
    schema_block: str = "",
) -> list[str]:
    """Hard check: does DETAIL return one row per entity, and is entity_key an identifier?

    THIS EXISTS BECAUSE A REPORT WENT OUT WITH FOUR TIMES THE FINDINGS IT HAD.

    The task data keeps one record per update. Several probes read it without resolving to the
    current record, so DETAIL returned the same task once per historical update: 43,534 rows
    for 10,860 distinct tasks, and the same 75-79% duplication on three more checks. The
    headline "130,085 records flagged" counted history rows, and someone opening the workbook to
    fix data got four copies of every row. Every one of those rules says in plain words to take
    the most recent record per task; nothing mechanically checked that it had happened.

    Both checks below are decided from the sample DETAIL rows the executor has already fetched -
    no extra query, no model call:

      * A REPEATED entity_key means the wrong grain. The contract defines entity_key as "the
        stable identifier of the thing that is wrong", so the same identifier twice is either a
        missing de-duplication or a join that fans out. Either way the counts are inflated.

      * A DENSE 1..N SEQUENCE means entity_key is a row number, not an identifier. Observed on a
        probe whose key column read 1, 2, 3, 4 while the task code sat in a separate column -
        the finding could not be traced back to anything a person could go and fix.

    Returns [] when the sample is too small to judge, because a false rejection here costs a
    full authoring cycle.
    """
    have = [str(c).strip().lower() for c in columns]
    if "entity_key" not in have or len(rows) < _GRAIN_MIN_SAMPLE:
        return []
    at = have.index("entity_key")

    keys = [r[at] for r in rows if at < len(r) and r[at] is not None]
    if len(keys) < _GRAIN_MIN_SAMPLE:
        return []

    problems: list[str] = []
    distinct = len({str(k) for k in keys})
    if distinct < len(keys):
        repeats = len(keys) - distinct
        problems.append(
            f"DETAIL returns the same entity_key more than once: {len(keys)} sample row(s) "
            f"carry only {distinct} distinct key(s), so {repeats} row(s) repeat a record "
            "already reported. entity_key must identify ONE thing that is wrong, so every "
            "count derived from these rows is inflated. Resolve to one row per entity before "
            "judging it - where the source keeps a history, select the current record per "
            "entity first, and make SUMMARY count at that same grain."
        )

    # A row number, not an identifier: consecutive integers starting at 1.
    try:
        numbers = sorted(int(k) for k in keys)
    except (TypeError, ValueError):
        numbers = []
    if numbers and numbers[0] == 1 and numbers == list(range(1, len(numbers) + 1)):
        problems.append(
            "entity_key is a positional row number (1, 2, 3 ...), not an identifier. A reader "
            "cannot act on a finding that names a position in a result set. Select the real "
            "key of the record - the code or id that identifies it in its own table."
        )

    # THE CASE THAT ACTUALLY SHIPPED, and which neither check above sees.
    #
    # The keys were all distinct - they were record ids - while everything a reader looks at
    # was identical: 43,534 rows carrying 10,860 task codes, each repeated once per historical
    # update, with the same progress and the same sentence every time.
    #
    # Repetition alone is only a CONCERN (grain_concerns), because a hundred records pointing
    # at one missing parent legitimately look alike. It becomes a REFUSAL only when the probe
    # reads a table the schema itself marks as holding many rows per entity - at that point the
    # duplication is the documented trap, the rule prose already says to take the current record
    # per entity, and there is nothing left to adjudicate.
    if _GRAIN_MARKER in (schema_block or ""):
        total, distinct = _indistinguishable(columns, rows)
        if (total >= _GRAIN_MIN_SAMPLE and distinct
                and distinct <= total * _GRAIN_MAX_DISTINCT_SHARE):
            markers = sorted(set(re.findall(
                rf"{_GRAIN_MARKER}(\S+)", schema_block or ""
            )))
            named = (
                " The SCHEMA marks the repeated grain on: " + ", ".join(markers[:4]) + "."
                if markers else ""
            )
            problems.append(
                f"DETAIL reports the same record repeatedly: {total} sample row(s) reduce to "
                f"{distinct} once entity_key is ignored, and a table this probe reads is marked "
                f"in the SCHEMA as holding MANY ROWS PER entity. The probe is counting updates "
                "rather than things, so every count it produces is multiplied by how many times "
                "each thing was touched."
                + _worst_repeat(columns, rows)
                + named
                + " Select the CURRENT record per entity first - the one with the latest update, "
                "using ROW_NUMBER() OVER (PARTITION BY <the entity> ORDER BY <the update marker> "
                "DESC) and keeping row 1 - and make SUMMARY count at that same grain, so "
                "scope_total and anomaly_count are both per entity."
            )
    return problems


def saturation_problems(values: dict[str, Any]) -> list[str]:
    """Hard check: a probe whose scope IS its anomalies measures nothing.

    scope_total is the denominator - how many records were EXAMINED. When it equals
    anomaly_count the percentage is 100% by construction, and the reader is told "100% of this
    is broken" about a population defined as the broken things. Three checks in one report read
    that way, one of which had a real denominator the week before (373 of 422) and had since
    collapsed onto its own failures.

    WHY THERE IS A FLOOR RATHER THAN A FLAT REJECTION. A small population genuinely can be
    entirely bad - the same check legitimately reported 5 of 5 when only five records were in
    scope, and failing that would be wrong. A saturated scope in the thousands is not that.
    The floor is configurable for exactly this reason.

    sanity_concerns() still raises this as an advisory below the floor, so nothing is hidden -
    it is adjudicated by the reviewer there instead of being refused here.
    """
    from app.config import settings

    scope = _as_int(values.get("scope_total"))
    count = _as_int(values.get("anomaly_count"))
    if scope is None or count is None or scope < settings.saturation_floor:
        return []
    if count != scope:
        return []
    return [
        f"Every record in scope is flagged ({count} of {scope}), so scope_total is not a "
        "denominator - it is the anomalies counted twice, and the percentage is 100% by "
        "construction rather than by measurement. SUMMARY must aggregate over the POPULATION "
        "the rule is about and mark each record anomalous or not; only the DETAIL query "
        "narrows to the offenders. Move the condition that identifies the anomaly out of the "
        "scope and into the CASE expression."
    ]


# ── Two rules measuring the same thing ─────────────────────────────────────────

# The rule's own id appears as a literal in every SUMMARY ("'DQ-B03' AS rule_id"), so it must be
# removed before two probes can be compared - otherwise no two probes are ever alike.
_OWN_ID = re.compile(r"'[^']*'\s+AS\s+rule_id", re.IGNORECASE)
# A real table in a FROM or JOIN: schema-qualified, which is what excludes CTE names. A CTE is
# an internal construction and two probes may name theirs differently while reading the same
# tables, so only the actual sources count.
_FROM_JOIN_TABLE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
# "CASE WHEN <condition> THEN 1" - the expression that decides a record is anomalous. This is the
# probe's actual claim; everything around it is scaffolding.
_FLAG_CONDITION = re.compile(
    r"\bCASE\s+WHEN\s+(.+?)\s+THEN\s+1\b", re.IGNORECASE | re.DOTALL
)
# The WHERE clause - what puts a record IN SCOPE, as opposed to what makes it anomalous.
_SCOPE_CONDITION = re.compile(
    r"\bWHERE\s+(.+?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\)\s*(?:,|\bSELECT\b)|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def probe_signature(summary_sql: str) -> str:
    """What this probe MEASURES: the tables it reads, and the condition that flags a record.

    NOT the whole query. Comparing full text finds nothing - two probes that ask the database
    exactly the same question still differ in unused CTE columns, alias names and formatting.
    The live case that prompted this differed only in two columns selected and never used, while
    the table and the predicate were character-for-character identical.

    So the signature is the semantic core: every table in a FROM or JOIN, plus every "flag this
    record" condition, with alias prefixes stripped so `wm.x` and `w.x` compare equal.

    WHY NOTHING ELSE CAN CATCH THIS. The Verifier reviews ONE rule at a time and has never seen
    the others, so it cannot notice that two rules have converged on the same measurement. A
    rule meaning "pegging exists but no deadline can be computed" lost its scope filter and
    became equivalent to "the expected rig-on date is missing"; both then reported the same 61
    wells under two ids in one report, inflating every total that summed them. Only comparing
    the catalog against itself finds that.
    """
    code = _OWN_ID.sub("", code_of(summary_sql or ""))

    def _norm(parts) -> list[str]:
        # `wm.ex_rig_on_date` and `w.ex_rig_on_date` are the same column; the alias is arbitrary.
        return sorted(
            re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\.", "", re.sub(r"\s+", " ", p).strip().lower())
            for p in parts
        )

    tables = sorted({t.lower() for t in _FROM_JOIN_TABLE.findall(code)})
    flags = _norm(m.group(1) for m in _FLAG_CONDITION.finditer(code))
    # THE SCOPE IS PART OF THE IDENTITY, and leaving it out accused correct rules of being
    # duplicates. Two rules legitimately share a flag while examining different populations:
    # "the expected rig-on date is missing" and "the rig arrived but none was ever planned" both
    # flag the same absent date - the second only among wells where the rig actually arrived.
    # Same claim, different question. It is when the SCOPE also matches that they are one rule
    # written twice.
    scopes = _norm(m.group(1) for m in _SCOPE_CONDITION.finditer(code))

    if not tables or not flags:
        # Nothing comparable was found. Returning "" rather than a partial signature means this
        # probe simply never matches another - a missed duplicate costs a line in a report, a
        # FALSE duplicate would accuse two correct rules of being the same.
        return ""
    return "|".join(tables) + "||" + "|".join(flags) + "||" + "|".join(scopes)


# "TABLE schema.name  -- 110,184 rows", followed later by its grain marker. Read from the schema
# block the author was shown, so this can never disagree with what the author was told.
_TABLE_LINE = re.compile(r"^TABLE\s+(\S+)[^\n]*?--\s*([\d,]+)\s+rows", re.MULTILINE)


def dedup_problems(values: dict[str, Any], schema_block: str) -> list[str]:
    """Hard check: the probe claims a per-entity grain but examined every stored row.

    THE CASE THIS EXISTS FOR. A probe wrote a textbook de-duplication -

        ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY t.updated_at DESC) ... WHERE row_num = 1

    - and it reduced nothing, because `t.id` is the row's OWN key: every partition held exactly
    one row. Its scope came back as 110,181 against a table of 110,184 rows holding ~35,749
    things. The Verifier read the shape and approved it ("correctly selects one latest row per
    task"), and the sample rows happened to be distinct so the grain check passed too. Three
    rules on one table ended up counting things while three counted updates, and no percentage
    was comparable with any other.

    TESTING THE OUTCOME, NOT THE MECHANISM, is the point. Checking *how* a probe de-duplicates
    can always be satisfied by writing something that looks right; checking that the scope
    actually shrank cannot. Any future variation of the same mistake fails this.

    Both numbers are already in hand - the summary row the executor fetched, and the schema
    block the author was shown. No query, no model.
    """
    from app.config import settings

    scope = _as_int(values.get("scope_total"))
    if not scope:
        return []

    for m in _TABLE_LINE.finditer(schema_block or ""):
        rows = int(m.group(2).replace(",", ""))
        if not rows:
            continue
        # Only a table the SCHEMA ITSELF marks as holding many rows per entity. Everywhere else,
        # one row genuinely is one thing and a scope equal to the row count is correct.
        block = schema_block[m.start():]
        end = block.find("\nTABLE ")
        if _GRAIN_MARKER not in (block[:end] if end > 0 else block):
            continue
        # A near match, not an exact one: a probe legitimately drops rows with a NULL measure,
        # which is why the real case read 110,181 against 110,184.
        if abs(scope - rows) <= rows * settings.dedup_scope_tolerance:
            return [
                f"SUMMARY examined {scope:,} records, which is the entire row count of "
                f"{m.group(1)} ({rows:,}) - a table the SCHEMA marks as holding MANY ROWS PER "
                "entity. Whatever de-duplication this query performs is not reducing anything: "
                "check what you partitioned by, because partitioning by a column that is already "
                "unique per row (an id, a surrogate key) leaves every row in its own group and "
                "keeps them all. Partition by the value that identifies the THING - the code or "
                "key the schema marks the grain on - so scope_total counts things, not updates. "
                "Every percentage this probe reports is otherwise measured against the wrong "
                "denominator, and cannot be compared with any other check on this table."
            ]
    return []
