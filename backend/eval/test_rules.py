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


def test_generic_templates_parse() -> None:
    """The generic templates must keep their {{placeholders}} - they are filled from the
    schema at generation time, not from rule metadata."""
    path = os.path.join(_BACKEND, "domain", "generic_probes.md")
    check(os.path.exists(path), "domain/generic_probes.md is missing")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    families = re.findall(r"^##\s+TEMPLATE\s+(\S+)", text, re.MULTILINE)
    check(bool(families), "generic_probes.md declares no '## TEMPLATE <id>' sections")
    for fam in families:
        block = text.split(f"## TEMPLATE {fam}")[1].split("\n## ")[0]
        check("applies_to:" in block, f"{fam}: no 'applies_to:' line - nothing to iterate over")
        check("```sql summary" in block, f"{fam}: no ```sql summary block")
        check("```sql detail" in block, f"{fam}: no ```sql detail block")
        check("{{rule_id}}" in block, f"{fam}: does not use {{{{rule_id}}}}")


def main() -> int:
    tests = [
        ("no business SQL in Python", test_no_business_sql),
        ("rules parse", test_rules_parse),
        ("parser rejects bad rules", test_parser_rejects_bad_rules),
        ("pinned SQL meets the contract", test_pinned_sql_meets_contract),
        ("static contract ignores comments", test_static_contract_reads_only_executable_sql),
        ("prompts state the enforced contract", test_prompts_state_the_enforced_contract),
        ("eval bands catch regressions", test_eval_bands_catch_regressions),
        ("zero-scope probe never stored active", test_zero_scope_probe_is_never_stored_active),
        ("verifier can say not applicable", test_verifier_prompt_offers_the_not_applicable_verdict),
        ("generic templates parse", test_generic_templates_parse),
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
