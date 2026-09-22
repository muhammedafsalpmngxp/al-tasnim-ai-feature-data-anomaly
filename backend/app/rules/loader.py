"""Parse domain/data_anomalies.md into AnomalyRule objects. No LLM anywhere in this file.

That is a deliberate guarantee, not an optimisation. This file is the boundary between what a
business user wrote and what the engine will run, and a model reading it could quietly reinterpret
or drop a rule. A deterministic parser either understands a rule or reports precisely what is
wrong with it, on a numbered line, and nothing in between.

FILE FORMAT
-----------
    ## PATTERNS                       (optional, once, before the first rule)
    ...worked SQL templates shown to the SQL Author for `authored` rules...

    ## RULE <id> - <title>
    - category: ...
    - severity: critical | high | medium | low
    - entity: ...
    - method: rule | statistical | rollup
    - sql_mode: pinned | seed | authored
    - status: active | draft | disabled
    - tags: a, b, c
    - <anything else>: becomes a {{placeholder}} usable in the SQL below

    Wrong: ...        the defect itself
    Matters: ...      the consequence to the business
    Detect: ...       the logic in words, no SQL
    Never flag: ...   the exclusions, binding on the Verifier

    (The older layout - **What is wrong** / **Why it matters** / **How to detect** /
    **Do NOT flag**, each as a bold heading with a paragraph under it - means exactly the same
    and is still read. NOTHING HERE PARSES EITHER FORM: the prose below the metadata is kept
    verbatim as `body` and handed to the Author and the Verifier, which is why both layouts
    work without a parser change and why rules in the two styles can sit side by side.)

    ```sql summary
    ...exactly one row...
    ```

    ```sql detail
    ...the offending rows, ordered worst first...
    ```

Rules may also be split across domain/anomalies/*.md once one file gets unwieldy; both
locations are read and merged.
"""
from __future__ import annotations

import os
import re

from app.observability import get_logger
from app.rules.spec import (
    AUTO,
    METHODS,
    SEVERITIES,
    SOURCES,
    SQL_MODES,
    STATUSES,
    AnomalyRule,
)
from app.utils import extract_sql_blocks

log = get_logger()

# THE DOMAIN FILES ARE USER-OWNED. Nothing in this engine writes, generates or reorganises
# them: business_rules.md, data_anomalies.md and few_shots.md are the operator's control
# surface and live exactly where they put them. Only the DESCRIPTIONS of the database
# (schema.txt and the two hint files) are machine-maintained - see app/db/introspect.py.
_DOMAIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "domain"
)
_MAIN_FILE = "data_anomalies.md"
_SPLIT_DIR = "anomalies"

# "## RULE DQ-001 - Title", accepting an em dash, en dash or hyphen as the separator. Business
# users paste from Word, which silently converts "-" to an em dash; rejecting that would be a
# baffling failure with no visible cause.
#
# A DASH MUST HAVE A SPACE BEFORE IT; A COLON NEED NOT. That asymmetry is deliberate, because
# ids contain dashes themselves. With a fully optional space, "## RULE DQ-D27 Negative crew
# size" - a heading whose separator was simply forgotten - matched happily, splitting on the id's
# OWN hyphen to give id "DQ" and title "D27 Negative crew size". The rule then compiled, ran, and
# was filed in the catalog and the report under a name nobody could find. Requiring the space
# makes that heading fail outright, where _malformed_headings() reports it on its line number.
_RULE_HEADING = re.compile(
    r"^##\s+RULE\s+([A-Za-z0-9][A-Za-z0-9._-]*?)\s*(?::|\s+[—–-])\s*(.+?)\s*$", re.MULTILINE
)
# "- key: value" or "* key: value"
_META_LINE = re.compile(r"^\s*[-*]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
# {{name}}
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Metadata keys consumed as structure; anything else becomes a SQL placeholder.
_RESERVED_KEYS = frozenset(
    ("category", "severity", "entity", "method", "sql_mode", "status", "tags", "title",
     "expands_over")
)

# Schema features a rule may expand over - one probe per matching feature. Validated here
# rather than at expansion time so an unknown value fails when the file is parsed, on a
# numbered line, instead of silently producing no probes at all.
EXPANDS_OVER = (
    "foreign_key", "duplicate_key", "date_pair", "future_date", "numeric_range", "text_numeric",
)


class RuleParseError(Exception):
    """A rule that cannot be understood. Carries the file and line so it can be fixed."""


def _domain_files() -> list[str]:
    """Every markdown file holding rules, main file first so its ids win on a clash."""
    paths: list[str] = []
    main = os.path.join(_DOMAIN_DIR, _MAIN_FILE)
    if os.path.exists(main):
        paths.append(main)
    split = os.path.join(_DOMAIN_DIR, _SPLIT_DIR)
    if os.path.isdir(split):
        paths.extend(
            os.path.join(split, n) for n in sorted(os.listdir(split)) if n.endswith(".md")
        )
    return paths


# NOTE: the probe SHAPES that used to be parsed out of a "## PATTERNS" section here now live in
# app/graph/prompts.py as PROBE_PATTERNS. They describe THIS ENGINE's summary/detail contract,
# not the business - they change when the engine changes, never when a rule does - and keeping a
# hundred lines of T-SQL at the top of the one file a business owner opens made that file both
# intimidating and accidentally breakable. Nothing parses a PATTERNS section any more.


def _split_meta_body(block: str) -> tuple[dict[str, str], str]:
    """Separate the leading "- key: value" lines from the prose that follows.

    The block still carries its own "## RULE ..." heading (the caller slices from the heading
    so that `raw`, and therefore rule_hash, covers the whole rule). That line is skipped here
    before looking for metadata - it is not a "- key: value" line, so leaving it in place ends
    the metadata scan immediately and every rule silently falls back to its defaults. Severity,
    method and every {{placeholder}} would then be wrong, with nothing to indicate it.

    Only the leading run counts as metadata: scanning stops at the first non-blank line that
    is not "- key: value". A "- something: ..." bullet inside the prose is prose, and treating
    it as metadata would invent a placeholder out of an ordinary sentence.
    """
    meta: dict[str, str] = {}
    lines = block.splitlines()
    i = 1 if lines and lines[0].lstrip().startswith("#") else 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1  # blank lines are allowed between metadata lines
            continue
        m = _META_LINE.match(line)
        if not m:
            break
        meta[m.group(1).strip().lower()] = m.group(2).strip()
        i += 1
    return meta, "\n".join(lines[i:]).strip()


def _strip_sql_fences(body: str) -> str:
    """Prose with the ```sql blocks removed.

    The SQL is carried separately on the rule, so leaving it in `body` would send every query
    to the model twice - once as prose and once as SQL - on every retry of every rule.
    """
    return re.sub(r"```[ \t]*sql[^\n]*\n.*?```", "", body, flags=re.IGNORECASE | re.DOTALL).strip()


def _substitute(sql: str, params: dict[str, str], rule_id: str, where: str) -> str:
    """Replace every {{placeholder}} from the rule's own metadata.

    Substitution happens HERE, at load time, rather than as a bound parameter at execution.
    That is what keeps every compiled probe parameter-free: the catalog stores runnable SQL,
    not a template plus a separate argument list that could drift away from it.
    """
    unresolved: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1).lower()
        if name == "rule_id":
            return rule_id
        if name in params:
            value = params[name]
            if value.lower() == AUTO:
                unresolved.append(
                    f"{{{{{name}}}}} is used in the {where} SQL but {name} is set to 'auto'. "
                    f"'auto' means the query must DERIVE the threshold from the data "
                    f"(AVG/STDEV/PERCENTILE_CONT) - there is no literal to substitute. Either "
                    f"give {name} a number, or remove the placeholder and compute it in SQL."
                )
                return m.group(0)
            return value
        unresolved.append(
            f"{{{{{name}}}}} is used in the {where} SQL but no '{name}:' metadata line defines it."
        )
        return m.group(0)

    out = _PLACEHOLDER.sub(repl, sql)
    if unresolved:
        raise RuleParseError("; ".join(unresolved))
    return out


def _validate(key: str, value: str, allowed: tuple[str, ...], rule_id: str) -> str:
    if value not in allowed:
        raise RuleParseError(
            f"{key} is '{value}'; it must be one of: {', '.join(allowed)}"
        )
    return value


def _build_rule(rule_id: str, title: str, block: str, path: str, line_no: int) -> AnomalyRule:
    meta, body = _split_meta_body(block)
    blocks = extract_sql_blocks(body)
    summary_sql = blocks.get("summary", "")
    detail_sql = blocks.get("detail", "")

    # An untagged ```sql block is ambiguous - we cannot know which half of the contract it is,
    # and guessing would produce a probe that runs and reports the wrong thing.
    if "" in blocks and not (summary_sql or detail_sql):
        raise RuleParseError(
            "found an untagged ```sql block. Tag each one so it is unambiguous: "
            "```sql summary and ```sql detail."
        )
    if bool(summary_sql) != bool(detail_sql):
        missing = "detail" if summary_sql else "summary"
        raise RuleParseError(
            f"has a ```sql {'summary' if summary_sql else 'detail'} block but no "
            f"```sql {missing} block. A probe is always a PAIR: SUMMARY returns one row of "
            f"counts, DETAIL returns the offending rows. Supply both, or neither (which makes "
            f"the rule sql_mode: authored)."
        )

    params = {k: v for k, v in meta.items() if k not in _RESERVED_KEYS}
    if summary_sql:
        summary_sql = _substitute(summary_sql, params, rule_id, "summary")
    if detail_sql:
        detail_sql = _substitute(detail_sql, params, rule_id, "detail")

    # sql_mode defaults from what is actually present, so the common cases need no metadata
    # line at all and cannot be set to something the file contradicts.
    default_mode = "seed" if (summary_sql and detail_sql) else "authored"
    sql_mode = _validate("sql_mode", meta.get("sql_mode", default_mode), SQL_MODES, rule_id)

    expands_over = (meta.get("expands_over") or "").strip().lower()
    if expands_over:
        if expands_over not in EXPANDS_OVER:
            raise RuleParseError(
                f"expands_over is '{expands_over}', which is not a schema feature this engine "
                f"can enumerate. Use one of: {', '.join(EXPANDS_OVER)}."
            )
        if summary_sql or detail_sql:
            # The whole point of a family rule is that its SQL is written per feature. SQL
            # written here would name one table and be wrong for the other thirty-five.
            raise RuleParseError(
                "a rule with expands_over must not carry its own SQL - it is applied to every "
                "matching feature in the schema, so the query is written per feature. Remove "
                "the ```sql blocks."
            )

    if sql_mode in ("pinned", "seed") and not (summary_sql and detail_sql):
        raise RuleParseError(
            f"sql_mode is '{sql_mode}' but the rule carries no SQL. Add ```sql summary and "
            f"```sql detail blocks, or set sql_mode: authored."
        )

    return AnomalyRule(
        rule_id=rule_id,
        title=meta.get("title") or title,
        category=meta.get("category", "Uncategorised"),
        severity=_validate("severity", meta.get("severity", "medium").lower(), SEVERITIES, rule_id),
        entity=meta.get("entity", "row"),
        method=_validate("method", meta.get("method", "rule").lower(), METHODS, rule_id),
        sql_mode=sql_mode,
        status=_validate("status", meta.get("status", "active").lower(), STATUSES, rule_id),
        # READ FROM THE FILE, not hardcoded, so a rule can say where it came from. Everything in
        # data_anomalies.md omits it and is therefore "declared", exactly as before; only the
        # Scout's accepted proposals in anomalies/discovered.md carry `source: discovered`, and
        # that label is what keeps their findings out of the headline numbers downstream.
        source=_validate("source", meta.get("source", "declared").lower(), SOURCES, rule_id),
        expands_over=expands_over,
        tags=tuple(t.strip() for t in meta.get("tags", "").split(",") if t.strip()),
        body=_strip_sql_fences(body),
        summary_sql=summary_sql,
        detail_sql=detail_sql,
        params=params,
        raw=block,
        source_file=os.path.basename(path),
        line_no=line_no,
    )


def load_rules() -> tuple[list[AnomalyRule], list[str]]:
    """Every declared rule, plus a list of human-readable problems.

    Returns errors rather than raising on the first one: a file with three mistakes should
    report all three, not force three edit-run cycles.

    A malformed rule is NEVER silently skipped. It is absent from the returned list AND named
    in the errors, and the caller is expected to surface that - a rule the operator believes is
    running but is not is the worst possible failure for a data-quality tool.
    """
    rules: list[AnomalyRule] = []
    errors: list[str] = []
    seen: dict[str, str] = {}

    for path in _domain_files():
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: cannot be read ({exc})")
            continue

        matches = list(_RULE_HEADING.finditer(text))
        errors.extend(_malformed_headings(text, path, {m.group(1).strip() for m in matches}))
        if not matches:
            log.info("load: %s contains no '## RULE <id> - <title>' headings", os.path.basename(path))
            continue

        for i, m in enumerate(matches):
            rule_id = m.group(1).strip()
            title = m.group(2).strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[m.start():end].strip()
            line_no = text.count("\n", 0, m.start()) + 1
            where = f"{os.path.basename(path)}:{line_no} [{rule_id}]"

            if rule_id in seen:
                errors.append(
                    f"{where}: duplicate rule id - already defined in {seen[rule_id]}. "
                    f"Ids must be unique; the catalog is keyed on them."
                )
                continue
            try:
                rules.append(_build_rule(rule_id, title, block, path, line_no))
                seen[rule_id] = where
            except RuleParseError as exc:
                errors.append(f"{where}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, never crash the whole load
                errors.append(f"{where}: unexpected problem - {exc}")

    active = sum(1 for r in rules if r.runnable)
    log.info(
        "load: %d rule(s) from %d file(s) - %d active, %d draft/disabled, %d error(s)",
        len(rules), len(_domain_files()), active, len(rules) - active, len(errors),
    )
    return rules, errors


# Any "## RULE ..." line, however malformed. The strict pattern above decides what PARSES; this
# one decides what was INTENDED, and the difference between them is what gets reported.
_RULE_ATTEMPT = re.compile(r"^##\s+RULE\b.*$", re.MULTILINE)
# A trailing number in an id, so the next free one can be suggested: DQ-D26 -> ("DQ-D", 26).
_ID_TAIL = re.compile(r"^(.*?)(\d+)$")


def _next_free_id(existing: set[str]) -> str:
    """The next unused id in the largest existing family, for the error message.

    Suggested, never assigned. The catalog is KEYED on the id: an auto-assigned one would shift
    whenever rules were reordered or deleted, orphaning every probe compiled against the old
    value and silently forcing a full recompile. Better that a person chooses it once and it
    never moves.
    """
    groups: dict[str, int] = {}
    for rule_id in existing:
        m = _ID_TAIL.match(rule_id)
        if m:
            groups[m.group(1)] = max(groups.get(m.group(1), 0), int(m.group(2)))
    if not groups:
        return ""
    prefix = max(groups, key=lambda p: sum(1 for r in existing if r.startswith(p)))
    width = max(
        (len(m.group(2)) for r in existing if (m := _ID_TAIL.match(r)) and r.startswith(prefix)),
        default=2,
    )
    return f"{prefix}{groups[prefix] + 1:0{width}d}"


def _malformed_headings(text: str, path: str, parsed: set[str]) -> list[str]:
    """Report every '## RULE' line the strict pattern did NOT accept.

    THE SILENT MISS THIS PREVENTS. A heading without an id - "## RULE Negative crew size" -
    matched nothing, so the rule was skipped with no error, no warning and no mention in the
    summary count. Someone writes an anomaly, saves, compiles, and nothing happens: they now
    believe a check is running that does not exist. The CLI's own documentation calls that the
    worst failure mode a data-quality tool has, and the loader had it.

    A near-miss is reported rather than guessed at, because guessing is worse than stopping:
    "## RULE DQ-D27 Negative crew size" (no separator) DOES parse, as id "DQ" with the title
    "D27 Negative crew size". Silently filing a rule under the wrong id is how a probe ends up
    compiled against a rule nobody can find.
    """
    out: list[str] = []
    for m in _RULE_ATTEMPT.finditer(text or ""):
        line = m.group(0).strip()
        if _RULE_HEADING.match(line):
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        suggestion = _next_free_id(parsed)
        out.append(
            f"{os.path.basename(path)}:{line_no}: {line[:70]!r} is not a usable rule heading, "
            f"so this rule WOULD BE SILENTLY IGNORED. Write it as: "
            f"## RULE <id> - <title>"
            + (f"   (for example: ## RULE {suggestion} - ...)" if suggestion else "")
            + ". The id is how the engine tracks this check across runs and names it in the "
            "report, so it must be written explicitly and must never change afterwards."
        )
    return out
