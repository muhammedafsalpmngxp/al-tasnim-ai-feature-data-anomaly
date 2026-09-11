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
