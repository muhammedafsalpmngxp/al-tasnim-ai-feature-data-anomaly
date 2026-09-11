"""Validator node (deterministic) - the hard safety gate, plus the static contract.

NO MODEL IS INVOLVED HERE, AND THAT IS THE POINT. This is the boundary that guarantees the
engine can never modify the source database, whatever privileges the configured login happens
to hold and whatever a model was persuaded to write. A guarantee that depends on a model
behaving is not a guarantee.

It checks two separate things, both decidable from the SQL text alone and both cheap:

  1. SAFETY   a single read-only statement, nothing that reaches outside the approved schemas.
  2. CONTRACT the aliases, ordering and guards the report and the runner require, delegated to
              app/rules/contract.py so the prompt, the compile and the test suite all enforce
              one definition.

Both run BEFORE any database round trip, so a defect costs a few microseconds rather than a
connection, a scan and a wasted reviewer call. Both feed the same MAX_SQL_RETRIES budget:
each is a mechanical fault with an obvious fix, unlike a semantic rejection from the reviewer.
"""
from __future__ import annotations

import re

from app.graph.state import CompileState
from app.observability import get_logger
from app.rules.contract import static_problems

log = get_logger()

# Whole-word keywords that must never appear in a query this engine runs.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|"
    r"GRANT|REVOKE|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|INTO|USE)\b",
    re.IGNORECASE,
)
_PROC = re.compile(r"\b(sp_|xp_)\w+", re.IGNORECASE)

# Read-side dangers. Each is legal inside a query - so the rules above let them through - but
# each reaches OUTSIDE the approved schemas: the server's filesystem, a linked server, or the
# login catalogue (sys.sql_logins exposes password hashes). They cannot modify data; they leak
# it, which for a tool that writes its findings into a shared report is just as serious.
_UNSAFE = re.compile(
    r"\b(OPENROWSET|OPENQUERY|OPENDATASOURCE|OPENXML|BULK|WAITFOR)\b"
    r"|\bfn_(get_audit_file|trace_gettable)\b"
    r"|\bsys\.(sql_logins|server_principals|credentials|master_key_passwords)\b",
    re.IGNORECASE,
)

# A schema-qualified table reference. The dot is required on purpose: a bare word after FROM or
# JOIN is virtually always a CTE name or a subquery alias, and the author is told to always
# schema-qualify a real table, so this flags a genuine reference and never a CTE.
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+(\w+\.\w+)", re.IGNORECASE)
# "TABLE schema.table" lines in the rendered schema block.
_TABLE_DECL = re.compile(r"^TABLE (\S+)", re.MULTILINE)


def _scrub(sql: str) -> str:
    """Blank out string literals, bracketed identifiers and comments in ONE pass.

    Only this inspection copy is scrubbed; what actually runs is always the original text.

    A single character scan rather than two regex passes, for two reasons:

    * a keyword inside DATA is not a command. A predicate matching the text 'update required'
      was being rejected as an UPDATE, so a legitimate probe failed and burned both retries;
    * handling strings and comments separately is exploitable. Strip comments first and a `--`
      inside a string breaks the literal; blank strings first and a lone quote inside a comment
      can swallow a real statement, so that

          SELECT a -- '
          DROP TABLE t -- '

      would hide the DROP while SQL Server still executes it as a second statement.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

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

        if ch == "'":  # string literal; '' escapes a quote inside it
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

        # A bracketed identifier, only when properly closed - so an unmatched '[' can never
        # swallow the rest of the statement.
        if ch == "[":
            end = sql.find("]", i + 1)
            if end != -1:
                out.append("[x]")
                i = end + 1
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def is_read_only(sql: str) -> tuple[bool, str]:
    """(ok, reason). One read-only statement that touches only the approved schemas."""
    if not sql or not sql.strip():
        return False, "The query is empty."
    clean = _scrub(sql).strip().rstrip(";").strip()

    if ";" in clean:
        return False, "Multiple statements are not allowed - write a single query."

    lowered = clean.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "A query must begin with SELECT, or with WITH for a CTE chain."

    m = _FORBIDDEN.search(clean)
    if m:
        return False, (
            f"Forbidden keyword {m.group(0)!r} - this engine is strictly read-only, so a probe "
            f"may only read."
        )
    if _PROC.search(clean):
        return False, "Stored-procedure calls (sp_ / xp_) are not allowed."
    m = _UNSAFE.search(clean)
    if m:
        return False, (
            f"{m.group(0)!r} is not allowed - a probe may read the approved schemas only, never "
            f"remote servers, files on disk, or login and credential data."
        )
    return True, ""


def unknown_tables(sql: str, schema_text: str) -> list[str]:
    """Schema-qualified tables the query references that the schema block does not declare.

    Catches a name slip before it ever reaches the database, where it would surface as an
    invalid-object error only after a full round trip. Returns nothing when the schema block
    carries no declarations at all: having no schema to check against must never be the reason
    a correct query is rejected.
    """
    valid = {m.lower() for m in _TABLE_DECL.findall(schema_text or "")}
    if not valid:
        return []
    referenced = {m.lower() for m in _TABLE_REF.findall(_scrub(sql))}
    return sorted(referenced - valid)


def validator_node(state: CompileState) -> dict:
    rule_id = state.get("rule_id", "")
    summary_sql = state.get("summary_sql", "")
    detail_sql = state.get("detail_sql", "")
    problems: list[str] = []

    for sql, which in ((summary_sql, "SUMMARY"), (detail_sql, "DETAIL")):
        if not sql.strip():
            continue  # a missing half is reported by the static contract, in the author's terms
        ok, reason = is_read_only(sql)
        if not ok:
            problems.append(f"{which}: {reason}")
            continue
        unknown = unknown_tables(sql, state.get("schema_block", ""))
        if unknown:
            problems.append(
                f"{which} references {', '.join(unknown)}, which is not in the SCHEMA block. "
                f"Use only the exact schema-qualified names listed there."
            )

    problems += static_problems(summary_sql, detail_sql, rule_id)

    if problems:
        joined = "\n".join(f"- {p}" for p in problems)
        # Each problem is logged, not just the count. A bare count is unusable when a rule
        # burns its whole budget: the log says it was rejected three times and never says what
        # for, so the only way to find out is to re-run the compile and pay for it again.
        log.warning("validation rejected [%s]: %d problem(s)", rule_id, len(problems))
        for text in problems:
            log.warning("validation rejected: - %s", text[:200])
        return {
            "validation_error": joined,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    log.info("validation ok [%s]: two read-only queries, contract satisfied", rule_id)
    return {"validation_error": ""}
