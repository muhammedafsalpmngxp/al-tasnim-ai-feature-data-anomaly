"""Self-checks that need no database and no LLM.

    python -m eval.test_rules        (from the backend/ directory)

Plain asserts rather than pytest, so this runs in any environment that can run the app itself.
Three groups:

  1. NO BUSINESS SQL IN PYTHON - the invariant that makes this engine database-agnostic. It is
     easy to state and easy to break accidentally, so it is enforced rather than promised.
  2. RULE PARSING - the loader either understands a rule or reports exactly what is wrong.
     Includes the negative cases, because a parser that silently drops a malformed rule is
     worse than one that crashes: the operator believes a check is running when it is not.
  3. CONTRACT - every pinned probe selects the aliases the report needs.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_BACKEND, "app")

_failures: list[str] = []
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        _failures.append(message)


# ── 0. Every module parses and imports ─────────────────────────────────────────

def test_every_module_parses() -> None:
    """Catch a broken module before a 25-minute compile does.

    THIS TEST EXISTS BECAUSE THE SUITE MISSED ONE. Everything below imports the rule loader and
    the report helpers, so a syntax error in a GRAPH NODE - the half of the system that only
    runs during a compile - passed every check and then aborted `compile` on the first import.
    A test suite that reports "all passed" while a module cannot be imported is worse than no
    suite, because it is trusted.

    Parsing is separated from importing on purpose: parsing needs nothing installed, so it runs
    everywhere and pinpoints the file and line. Importing additionally catches a bad name at
    module scope, but needs the third-party packages, so it is skipped when they are absent
    rather than reported as a failure.
    """
    import ast
    import importlib
    import pathlib

    root = pathlib.Path(_BACKEND)
    modules = [
        f for f in sorted((root / "app").rglob("*.py")) if "__pycache__" not in str(f)
    ]
    check(bool(modules), "no modules found to check")

    for f in modules:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            check(False, f"{f.relative_to(root)}:{exc.lineno} does not parse - {exc.msg}")

    try:
        import pyodbc  # noqa: F401
    except Exception:  # noqa: BLE001 - dependency absent; parsing above already ran
        print("         (import check skipped - pyodbc not installed)")
        return

    for f in modules:
        if f.name == "__init__.py":
            continue
        name = ".".join(f.relative_to(root).with_suffix("").parts)
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            check(False, f"{name} cannot be imported - {type(exc).__name__}: {exc}")


# ── 1. No business SQL in Python ───────────────────────────────────────────────
# Catalogue queries are legitimate: they describe the ENGINE, are identical on every database,
# and carry no business knowledge. Anything else naming a table is a leak of domain knowledge
# into code, which is what makes an app single-database.
_CATALOGUE_OK = re.compile(
    r"^(information_schema|sys)\.", re.IGNORECASE
)
# Files allowed to contain catalogue SQL at all.
_SQL_ALLOWED_FILES = {"introspect.py", "connection.py"}
# FROM/JOIN <name>, where <name> is a bare or schema-qualified identifier. Deliberately also
# matches unqualified names so a stray `FROM well_master` is caught.
_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)
# Identifiers that are Python, not SQL - these appear in ordinary prose and code.
_NOT_A_TABLE = frozenset(
    ("import", "the", "a", "an", "this", "that", "it", "them", "here", "which", "what",
     "one", "two", "both", "each", "every", "its", "their", "typing", "dataclasses",
     "functools", "collections", "concurrent", "app", "langchain_core", "langchain_openai",
     "langchain_ollama", "langgraph", "rich", "docx", "openpyxl", "pyodbc", "dotenv",
     "__future__", "datetime", "hashlib", "logging", "os", "re", "sys", "json", "io",
     "threading", "time", "struct", "argparse", "abc", "enum", "pathlib", "uuid", "csv",
     "fastapi", "pydantic", "starlette", "uvicorn", "langfuse", "nothing", "anything",
     "something", "whatever", "where", "when", "there", "now", "then", "outside", "inside")
)


def _sql_strings(source: str) -> list[str]:
    """Triple-quoted strings that look like SQL. Docstrings full of prose are skipped."""
    out = []
    for m in re.finditer(r'("""|\'\'\')(.*?)\1', source, re.DOTALL):
        body = m.group(2)
        if re.search(r"\bSELECT\b", body, re.IGNORECASE) and re.search(
            r"\bFROM\b", body, re.IGNORECASE
        ):
            out.append(body)
    return out


def test_no_business_sql() -> None:
    for root, _dirs, files in os.walk(_APP):
        if "__pycache__" in root:
            continue
        for name in sorted(f for f in files if f.endswith(".py")):
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            rel = os.path.relpath(path, _BACKEND)

            for block in _sql_strings(source):
                for ref in _FROM_JOIN.findall(block):
                    low = ref.lower().rstrip(".")
                    if low in _NOT_A_TABLE or _CATALOGUE_OK.match(low):
                        continue
                    # A CTE name defined in the same block is not a table reference.
                    if re.search(rf"\b{re.escape(ref)}\s+AS\s*\(", block, re.IGNORECASE):
                        continue
                    check(
                        name in _SQL_ALLOWED_FILES,
                        f"{rel}: SQL references '{ref}', which is not a catalogue object. "
                        f"All business SQL belongs in domain/*.md, never in Python.",
                    )


def test_render_version_does_not_invalidate_catalog() -> None:
    """A presentation change must never force a full LLM recompile.

    structure_fingerprint() guards the compiled catalog; _live_fingerprint() guards the rendered
    files. The RENDER VERSION belongs only to the second: a probe's SQL depends on which tables
    and columns exist, never on how they were printed into a prompt.

    Including it cost a 25-minute, ~150-call recompile four times in a row, and a run that
    refused to execute 148 of 166 probes. This asserts the separation rather than trusting a
    comment to hold.
    """
    # Read the SOURCE rather than importing the module: app.db.introspect pulls in pyodbc, and a
    # check this important must run in any environment, including one without a database driver.
    import ast

    path = os.path.join(_APP, "db", "introspect.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    bodies = {
        node.name: ast.get_source_segment(open(path, encoding="utf-8").read(), node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_structure_signature", "_live_fingerprint")
    }
    check(len(bodies) == 2, f"expected both fingerprint functions, found {sorted(bodies)}")
    if len(bodies) != 2:
        return
    structure_src = bodies["_structure_signature"]
    live_src = bodies["_live_fingerprint"]

    code_only = [
        line for line in structure_src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    check(
        not any("_RENDER_VERSION" in line for line in code_only),
        "_structure_signature folds in _RENDER_VERSION, so every rendering tweak invalidates "
        "the whole compiled catalog and demands a full LLM recompile.",
    )
    check(
        "_RENDER_VERSION" in live_src,
        "_live_fingerprint must fold in _RENDER_VERSION - schema.txt and the hint files ARE "
        "the rendering, so a renderer change really does make them stale.",
    )
    # The signals that MUST invalidate a compiled probe, because each can make its SQL wrong.
    for signal in ("allowed_schemas", "excluded_tables", "excluded_columns", "db_name"):
        check(
            signal in structure_src,
            f"_structure_signature no longer folds in {signal}; a probe could keep running "
            f"against a table that setting now hides, or against a different database.",
        )


def test_numeric_hints_round_trip() -> None:
    """Every line db/introspect.py can WRITE, rules/schema_index.py must be able to READ.

    THIS TEST EXISTS BECAUSE THE TWO DRIFTED. A token-saving change added a compact
    "col: lo..hi" line to the writer and did not add it to the reader, so every plain-number
    column became invisible to the index - tables with statistics fell from 70 to 13. Nothing
    failed and nothing was logged as wrong, because the ratio columns the generic probes
    actually consume happened to still match the old pattern.

    A silent two-thirds loss of an internal index is precisely the class of defect this engine
    exists to find in other people's data. Asserting the round trip costs nothing and makes the
    coupling explicit.
    """
    from app.rules.schema_index import load_index

    sample = "\n".join([
        "NUMERIC HINTS (measured):",
        "demo.table_one  (1,234 rows)",
        "  - plain_id: 16..21581",
        "  - negative_measure: min -66, max 16092, avg 27.75, stdev 258.2, nulls 0% | plain number",
        "  - a_fraction: min 0, max 1, avg 0.26, stdev 0.43, nulls <1% | FRACTION_1 (0-1) - x100",
        "  - a_percentage: min 0, max 100, avg 50, stdev 52.2, nulls >99% | PERCENT_100 (0-100)",
        "  - some_text (text): 183 of 642 values do NOT parse as a number - TRY_CAST required",
        "  - code_text (text): only 0 of 17264 values parse as a number (0%) - this column "
        "holds codes, labels or free text, NOT a quantity. Do not range-check or cast-check it.",
    ])
    schema = "\n".join([
        "TABLE demo.table_one",
        "  - plain_id int NOT NULL PK",
        "  - negative_measure decimal",
        "  - a_fraction decimal",
        "  - a_percentage decimal",
        "  - some_text nvarchar(50)",
        "  - code_text nvarchar(50)",
        "",
    ])

    index = load_index(schema_text=schema, numeric_text=sample)
    table = index.get("demo.table_one")
    check(table is not None, "the sample table did not parse")
    if table is None:
        return

    check(table.row_count == 1234, f"row count not parsed (got {table.row_count})")
    check(
        len(table.stats) == 6,
        f"expected statistics for all 6 columns, parsed {len(table.stats)}: "
        f"{sorted(table.stats)}",
    )

    compact = table.stats.get("plain_id")
    check(compact is not None, "the COMPACT 'col: lo..hi' form did not parse")
    if compact:
        check(compact.lo == 16 and compact.hi == 21581, "compact bounds wrong")
        check(not compact.is_ratio, "a compact line must carry no scale")
        check(compact.bounds() is None, "a column with no measured scale must offer no bounds")

    fraction = table.stats.get("a_fraction")
    check(fraction is not None and fraction.is_ratio, "FRACTION_1 not recognised as a ratio")
    if fraction:
        check(fraction.bounds() == (0.0, 1.0), f"fraction bounds wrong: {fraction.bounds()}")

    percentage = table.stats.get("a_percentage")
    check(percentage is not None, "a '>99%' null rate broke the full-detail pattern")
    if percentage:
        check(percentage.bounds() == (0.0, 100.0), "percentage bounds wrong")

    negative = table.stats.get("negative_measure")
    check(negative is not None and negative.lo == -66, "a negative minimum did not parse")

    text_bad = table.stats.get("some_text")
    check(text_bad is not None and text_bad.text_bad == 183, "partial cast failure not parsed")
    check(text_bad is not None and not text_bad.text_is_codes,
          "a partial failure must not be marked codes-only")

    codes = table.stats.get("code_text")
    check(codes is not None and codes.text_is_codes,
          "a column where NOTHING parses must be marked codes-only, or a cast probe is "
          "generated that reports 100% of the table as anomalous")


# ── 2. Rule parsing ────────────────────────────────────────────────────────────

def test_rules_parse() -> None:
    from app.rules.loader import load_patterns, load_rules

    rules, errors = load_rules()
    check(not errors, "domain rule files failed to parse: " + "; ".join(errors))
    check(bool(rules), "no rules were parsed from domain/data_anomalies.md")
    check(bool(load_patterns()), "domain/data_anomalies.md has no ## PATTERNS section")

    ids = [r.rule_id for r in rules]
    check(len(ids) == len(set(ids)), f"duplicate rule ids: {ids}")

    for r in rules:
        check(bool(r.title.strip()), f"{r.rule_id}: empty title")
        check(bool(r.body.strip()), f"{r.rule_id}: no prose body - the Verifier needs the intent")
        check(
            "```" not in r.body,
            f"{r.rule_id}: body still contains a fenced block; SQL must be stripped from the "
            f"prose or it is sent to the model twice",
        )
        check(len(r.rule_hash) == 16, f"{r.rule_id}: rule_hash is not stable")


def test_parser_rejects_bad_rules() -> None:
    """The negative cases. A parser that accepts these would run the wrong query silently."""
    from app.rules.loader import RuleParseError, _build_rule

    def build(block: str):
        return _build_rule("X-1", "T", block, "test.md", 1)

    # Only one half of the contract.
    try:
        build("## RULE X-1 - T\n- severity: low\n\nProse\n\n```sql summary\nSELECT 1\n```\n")
        check(False, "a rule with SUMMARY but no DETAIL was accepted")
    except RuleParseError:
        check(True, "")

    # A placeholder with no metadata line defining it.
    try:
        build(
            "## RULE X-1 - T\n- severity: low\n\nProse\n\n"
            "```sql summary\nSELECT {{nope}}\n```\n\n```sql detail\nSELECT 1\n```\n"
        )
        check(False, "an undefined {{placeholder}} was accepted")
    except RuleParseError:
        check(True, "")

    # 'auto' cannot be substituted into SQL - it means "derive it in the query".
    try:
        build(
            "## RULE X-1 - T\n- severity: low\n- tolerance: auto\n\nProse\n\n"
            "```sql summary\nSELECT {{tolerance}}\n```\n\n```sql detail\nSELECT 1\n```\n"
        )
        check(False, "{{tolerance}} was substituted even though tolerance is 'auto'")
    except RuleParseError:
        check(True, "")

    # An invalid enum must be named, not silently defaulted.
    try:
        build("## RULE X-1 - T\n- severity: catastrophic\n\nProse\n")
        check(False, "an invalid severity was accepted")
    except RuleParseError:
        check(True, "")

    # Metadata must survive the heading line (the bug this file was written to catch).
    r = build("## RULE X-1 - T\n- severity: critical\n- method: rollup\n\nProse\n")
    check(r.severity == "critical", f"metadata after the heading was lost (got {r.severity})")
    check(r.method == "rollup", f"metadata after the heading was lost (got {r.method})")


# ── 3. Probe contract ──────────────────────────────────────────────────────────
# The literal-blanking and comment-stripping helpers these tests used to carry now live in
# app/rules/contract.py, where the compile stage uses the SAME code. Keeping a private copy
# here is precisely how the two bars drifted apart in the first place.

def test_pinned_sql_meets_contract() -> None:
    """Hand-written SQL is held to EXACTLY the bar authored SQL is held to.

    Delegates to app.rules.contract.static_problems() rather than restating the requirements.
    These checks used to live here as their own copy, which meant markdown SQL and model-written
    SQL were judged by two implementations that could drift - and the model-written half, the
    one no person reviews, was the half with the weaker checks.
    """
    from app.rules.contract import static_problems
    from app.rules.loader import load_rules

    rules, _ = load_rules()
    for r in rules:
        if not r.has_sql:
            continue
        for problem in static_problems(r.summary_sql, r.detail_sql, r.rule_id):
            check(False, f"{r.rule_id}: {problem}")
        check(True, "")  # count a rule that passed cleanly


def test_static_contract_reads_only_executable_sql() -> None:
    """A comment is prose. Regression test for a bug that cost a real rule its whole budget.

    An author explained a weighting as "equal-weighted (1/N)" in a comment; the division guard
    read it as a division, rejected the probe three times, and recorded a correct rule as
    failed. The opposite direction is worse and is checked too: an ORDER BY that appears only
    inside a comment must NOT satisfy the ordering requirement, or the report would present an
    arbitrary sample of rows as though they were the worst ones.
    """
    from app.rules.contract import static_problems

    summary = (
        "-- equal-weighted (1/N): each child counts the same\n"
        "SELECT 'X-1' AS rule_id, COUNT(*) AS scope_total, SUM(f) AS anomaly_count,\n"
        "       100.0 * SUM(f) / NULLIF(COUNT(*), 0) AS anomaly_pct\n"
        "FROM t"
    )
    detail = "SELECT k AS entity_key, v AS evidence_value FROM t ORDER BY v DESC"

    check(
        not static_problems(summary, detail, "X-1"),
        "a division inside a comment was reported as an unguarded division",
    )
    check(
        any("ORDER BY" in p for p in static_problems(
            summary, "SELECT k AS entity_key, v AS evidence_value FROM t -- ORDER BY v", "X-1"
        )),
        "an ORDER BY that exists only in a comment was accepted as real ordering",
    )
    check(
        not any("TOP" in p for p in static_problems(
            summary,
            "/* SELECT TOP 10 */ SELECT k AS entity_key, v AS evidence_value FROM t ORDER BY v",
            "X-1",
        )),
        "a TOP inside a block comment was reported as a real TOP",
    )
    check(
        any("NULLIF" in p for p in static_problems(
            summary.replace("/ NULLIF(COUNT(*), 0)", "/ COUNT(*)"), detail, "X-1"
        )),
        "a genuinely unguarded division was not caught",
    )
    check(
        any("rule_id" in p for p in static_problems(summary, detail, "X-2")),
        "a SUMMARY carrying the WRONG rule_id literal was accepted",
    )


def test_prompts_state_the_enforced_contract() -> None:
    """Every alias the code enforces must be named in the prompt that asks for it.

    This is the alignment guarantee. An agent penalised for omitting something it was never
    asked for cannot succeed, and it fails in the most expensive way available: it burns its
    whole retry budget rewriting a query without ever being told what is actually wrong.
    """
    from app.graph.prompts import ANOMALY_SQL_AUTHOR_SYSTEM, PROBE_CONTRACT
    from app.rules.spec import DETAIL_REQUIRED, EVIDENCE_PREFIX, SUMMARY_REQUIRED

    for column in (*SUMMARY_REQUIRED, *DETAIL_REQUIRED, EVIDENCE_PREFIX):
        check(
            column in PROBE_CONTRACT,
            f"the probe contract enforces {column!r} but PROBE_CONTRACT never names it",
        )
    check(PROBE_CONTRACT in ANOMALY_SQL_AUTHOR_SYSTEM,
          "the SQL Author's prompt does not include the probe contract")
    for phrase in ("ORDER BY", "NULLIF", "TOP"):
        check(phrase in PROBE_CONTRACT,
              f"the contract enforces {phrase} but the prompt never mentions it")
    # The tagged fences the author must emit are what utils.extract_sql_blocks() keys on. A
    # prompt asking for a different tag would produce replies nothing downstream can read.
    for fence in ("```sql summary", "```sql detail"):
        check(fence in ANOMALY_SQL_AUTHOR_SYSTEM,
              f"the SQL Author is never told to tag its output {fence!r}")


def test_eval_bands_catch_regressions() -> None:
    """The regression harness must actually FAIL on a regression.

    A suite that cannot fail is worse than no suite: it reports green forever and everyone
    stops looking. compare() is pure, so each failure mode it exists to catch is exercised here
    directly - no database, no LLM.
    """
    from eval.run_eval import Outcome, compare

    base = {"scope_total": 100_000, "anomaly_count": 200, "share": 0.002}

    def verdict(now: dict, baseline: dict | None = None) -> Outcome:
        out = Outcome()
        compare("X-1", now, base if baseline is None else baseline, out)
        return out

    # The data moving a little is NOT a regression - this is the case that decides whether
    # anyone keeps the suite switched on.
    steady = verdict({"scope_total": 101_500, "anomaly_count": 240, "share": 0.00236})
    check(len(steady.passed) == 1, f"normal daily drift was reported as a failure: {steady.failed}")

    # A join that stopped matching. No tolerance for this one, ever - but it is a REGRESSION
    # only when the probe used to work; otherwise it is a standing coverage gap. Both fail.
    empty = verdict({"scope_total": 0, "anomaly_count": 0, "share": 0.0})
    check(
        any("examines\n" not in why and "NO records" in why for _, why in empty.failed),
        "a probe that used to examine rows and now examines none was not reported",
    )
    standing = verdict(
        {"scope_total": 0, "anomaly_count": 0, "share": 0.0},
        {"scope_total": 0, "anomaly_count": 0, "share": 0.0},
    )
    check(
        not standing.failed and len(standing.gaps) == 1,
        "a probe that never examined anything should be a coverage gap, not a regression",
    )
    check(
        not standing.passed,
        "a probe that examines nothing must NEVER be reported as passing",
    )

    # An inverted condition.
    inverted = verdict({"scope_total": 100_000, "anomaly_count": 99_000, "share": 0.99})
    check(
        any("anomaly share moved" in why for _, why in inverted.failed),
        "an inverted condition (0.2% -> 99%) was not caught",
    )

    # Counting at two different grains - an impossible result.
    impossible = verdict({"scope_total": 100, "anomaly_count": 500, "share": 5.0})
    check(
        any("impossible" in why for _, why in impossible.failed),
        "anomaly_count exceeding scope_total was not caught",
    )

    # Scope halving looks like a broken join, not like deleted rows, at these volumes.
    shrunk = verdict({"scope_total": 20_000, "anomaly_count": 40, "share": 0.002})
    check(
        any("scope_total fell" in why for _, why in shrunk.failed),
        "a collapse in scope_total was not caught",
    )

    # A probe that found nothing at baseline and now flags a real share.
    appeared = verdict(
        {"scope_total": 100_000, "anomaly_count": 30_000, "share": 0.3},
        {"scope_total": 100_000, "anomaly_count": 0, "share": 0.0},
    )
    check(
        any("found nothing at baseline" in why for _, why in appeared.failed),
        "a probe going from zero findings to 30% was not caught",
    )

    # Tiny counts must NOT be compared as ratios: 1 -> 3 findings is data, not a defect.
    tiny = verdict(
        {"scope_total": 100_000, "anomaly_count": 3, "share": 0.00003},
        {"scope_total": 100_000, "anomaly_count": 1, "share": 0.00001},
    )
    check(len(tiny.passed) == 1, f"a 1->3 move in a tiny count was reported as a failure: {tiny.failed}")

    # A probe that failed to execute is a failure, not a silent pass.
    broken = verdict({"error": "Invalid column name 'foo'"})
    check(len(broken.failed) == 1, "a probe that errored was not reported as failed")


def test_zero_scope_probe_is_never_stored_active() -> None:
    """A probe that examined nothing must never reach the catalog as `active`.

    Regression test for a real failure. Asked to review a probe whose SUMMARY returned
    scope_total = 0, the reviewer APPROVED it and explained - correctly - that the rule could
    not be implemented against this schema. Correct diagnosis, wrong verdict: the probe was
    stored active and would then have reported "0 examined, 0 anomalies" on every run, which
    every reader downstream takes as a clean bill of health for a subject nobody is checking.

    The guard is deterministic on purpose. The query ran, the database said it matched no
    rows, and that is decidable without asking a model - so it must not depend on one.
    """
    from app.graph.nodes.catalog_writer import _status

    approved_but_empty = {
        "applicable": True,
        "summary_sql": "SELECT 1 AS rule_id",
        "detail_sql": "SELECT 1 AS entity_key",
        "summary_columns": ["rule_id", "scope_total", "anomaly_count"],
        "summary_row": ["X-1", 0, 0],
        "verify_ok": True,
        "verifier_note": "cannot be implemented against the provided schema",
    }
    status, reason = _status(approved_but_empty)
    check(
        status != "active",
        "a probe that examined 0 records was stored as ACTIVE - it would report 'clean' forever",
    )
    check(status == "not_applicable", f"expected not_applicable for a zero-scope probe, got {status}")
    check(bool(reason.strip()), "a retired probe must carry a reason a person can act on")

    # The same probe with real scope is fine - the guard must not reject working probes.
    working = dict(approved_but_empty, summary_row=["X-1", 5000, 12])
    check(_status(working)[0] == "active", "a probe with real scope was wrongly rejected")

    # A probe with no summary at all must not be mistaken for a zero-scope one.
    unknown = dict(approved_but_empty, summary_columns=[], summary_row=[])
    check(
        _status(unknown)[0] == "active",
        "a probe whose scope is UNKNOWN was treated as if it had examined nothing",
    )


def test_verifier_prompt_offers_the_not_applicable_verdict() -> None:
    """The reviewer must have the vocabulary its instructions assume.

    It is told never to approve a probe that examines nothing, and never to reject a rule no
    query can fix. Those two instructions are only satisfiable together if it has a third
    verdict available, so the JSON template has to offer one.
    """
    from app.graph.prompts import RULE_VERIFIER_SYSTEM

    check("not_applicable" in RULE_VERIFIER_SYSTEM,
          "the verifier is never told it can return not_applicable")
    check('"not_applicable"' in RULE_VERIFIER_SYSTEM,
          "the verifier's JSON template does not include a not_applicable field")
    check("scope_total = 0" in RULE_VERIFIER_SYSTEM,
          "the verifier is not warned that a zero scope is never a clean result")


def test_no_rule_carries_sql() -> None:
    """The invariant that replaced domain/generic_probes.md.

    Every anomaly is now described in business language in data_anomalies.md and the SQL is
    written by an agent against the live schema. SQL typed into a rule would be correct only
    against the schema as it stood the day it was written - exactly the guarantee that decays,
    and the reason the structural templates were retired.

    The ## PATTERNS section is deliberately exempt: it holds worked SHAPES shown to the author,
    names no table or column, and is not a rule.
    """
    from app.rules.loader import load_rules

    check(
        not os.path.exists(os.path.join(_BACKEND, "domain", "generic_probes.md")),
        "domain/generic_probes.md is back - structural SQL belongs in a rule's prose now",
    )
    check(
        not os.path.exists(os.path.join(_APP, "rules", "generic.py")),
        "app/rules/generic.py is back - enumeration lives in app/rules/expand.py, without SQL",
    )

    rules, _errors = load_rules()
    for rule in rules:
        check(
            not rule.has_sql,
            f"{rule.rule_id} carries SQL. Describe the anomaly in words and let the agent "
            f"write the query against the live schema.",
        )


def test_families_expand_without_sql() -> None:
    """A family rule must produce one concrete rule per schema feature, and carry no SQL.

    This is what replaced the 143 template-rendered probes, so a silent failure here is a
    silent loss of most of the engine's coverage.
    """
    from app.rules.expand import PLACEHOLDERS, tokens_for
    from app.rules.loader import EXPANDS_OVER

    for kind in EXPANDS_OVER:
        check(kind in PLACEHOLDERS, f"{kind} is a declarable feature with no placeholders")
        check(
            "rule_id" in tokens_for(kind),
            f"{kind} does not require {{{{rule_id}}}} - every member would report under the "
            f"id of whichever one happened to be authored",
        )


def test_staleness_is_judged_per_probe() -> None:
    """Regression: staleness must be decided against the tables a probe READS.

    The whole-database fingerprint alone marked all 149 compiled probes stale whenever any
    column changed anywhere, which made both honest responses to a stale catalog unusable: a
    run that refuses stale SQL would refuse everything, and one that recompiles would recompile
    everything at full LLM price.
    """
    from app.db.introspect import missing_tables, probe_fingerprint
    from app.rules.catalog import structure_moved
    from app.rules.spec import CompiledProbe

    signatures = {"well.well_master": "aaa", "ref.cluster": "bbb", "other.untouched": "ccc"}

    def probe(**kw) -> CompiledProbe:
        return CompiledProbe(rule_id="T-1", summary_sql="x", detail_sql="y", **kw)

    # Identical input, any spelling or order, must give the same hash - otherwise a probe would
    # look stale purely because Grounding listed its tables differently this time.
    check(
        probe_fingerprint(["well.well_master", "ref.cluster"], signatures)
        == probe_fingerprint(["REF.Cluster", " well.well_master "], signatures),
        "probe_fingerprint is not stable across table-name case and ordering",
    )
    check(
        probe_fingerprint([], signatures) == "",
        "an unknown table list must yield no fingerprint, so the caller falls back",
    )

    unchanged = probe(
        tables=("well.well_master",), structure_fingerprint="old-whole-database-hash"
    )
    unchanged.table_fingerprint = probe_fingerprint(unchanged.tables, signatures)
    moved, why = structure_moved(unchanged, "a-different-whole-database-hash", signatures)
    check(
        not moved,
        f"a probe whose own tables are unchanged was still called stale ({why}) - this is the "
        "regression that forced full recompiles over an unrelated column",
    )

    changed = probe(tables=("well.well_master",), table_fingerprint="stale-hash")
    moved, _ = structure_moved(changed, "", signatures)
    check(moved, "a probe whose table structure changed was not detected as stale")

    # A renamed or dropped table must be named, not left to fail later at the database with an
    # "Invalid object name" the report cannot explain.
    ghost = probe(tables=("well.well_master", "well.renamed_away"), table_fingerprint="any")
    moved, why = structure_moved(ghost, "", signatures)
    check(moved, "a probe reading a table that no longer exists was not detected")
    check(
        "well.renamed_away" in why,
        f"the reason does not name the missing table, so a reader cannot act on it: {why!r}",
    )
    check(
        missing_tables(["well.well_master", "gone.table"], signatures) == ["gone.table"],
        "missing_tables did not identify exactly the table that is absent",
    )

    # An entry written before per-table hashes existed must still be judged, by the blunt
    # whole-database signal. Treating "I cannot tell" as "nothing changed" would silently run
    # stale SQL, which is the one outcome this whole mechanism exists to prevent.
    legacy = probe(tables=("well.well_master",), structure_fingerprint="old")
    moved, _ = structure_moved(legacy, "new", signatures)
    check(moved, "a probe with no table fingerprint was not falling back to the whole-database test")


def test_a_stale_catalog_is_never_silently_executed() -> None:
    """Regression: ANOMALY_AUTO_COMPILE was defined in config and documented in the catalog
    loader's own docstring, but never read anywhere. Every run executed stale SQL and reported
    its numbers as findings, with only a warning banner to say so.
    """
    from app.config import settings

    with open(os.path.join(_APP, "graph", "nodes", "catalog_loader.py"), encoding="utf-8") as fh:
        source = fh.read()
    check(
        "settings.auto_compile" in source,
        "catalog_loader does not read settings.auto_compile - stale probes run regardless, "
        "which is exactly the bug this test exists to prevent",
    )
    check(
        "auto_compile_max_rules" in source,
        "catalog_loader does not bound the automatic recompile, so a schema-wide change would "
        "start an unbounded LLM spend inside a detection run",
    )
    check(
        hasattr(settings, "auto_compile") and hasattr(settings, "auto_compile_max_rules"),
        "the auto-compile settings are missing from config",
    )


def main() -> int:
    tests = [
        ("every module parses and imports", test_every_module_parses),
        ("no business SQL in Python", test_no_business_sql),
        ("numeric hints round-trip", test_numeric_hints_round_trip),
        ("render version does not invalidate the catalog",
         test_render_version_does_not_invalidate_catalog),
        ("rules parse", test_rules_parse),
        ("parser rejects bad rules", test_parser_rejects_bad_rules),
        ("pinned SQL meets the contract", test_pinned_sql_meets_contract),
        ("static contract ignores comments", test_static_contract_reads_only_executable_sql),
        ("prompts state the enforced contract", test_prompts_state_the_enforced_contract),
        ("eval bands catch regressions", test_eval_bands_catch_regressions),
        ("zero-scope probe never stored active", test_zero_scope_probe_is_never_stored_active),
        ("verifier can say not applicable", test_verifier_prompt_offers_the_not_applicable_verdict),
        ("no rule carries SQL", test_no_rule_carries_sql),
        ("families expand without SQL", test_families_expand_without_sql),
        ("staleness is judged per probe", test_staleness_is_judged_per_probe),
        ("a stale catalog is never silently executed", test_a_stale_catalog_is_never_silently_executed),
    ]
    for name, fn in tests:
        before = len(_failures)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a crashing test is a failing test
            _failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
        status = "FAIL" if len(_failures) > before else "ok"
        print(f"  [{status:>4}] {name}")

    print(f"\n{_checks} checks run")
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  x {f}")
        return 1
    print("All passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
