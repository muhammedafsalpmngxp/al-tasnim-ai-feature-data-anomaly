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
        and node.name in ("_structure_signature", "_live_fingerprint", "table_signatures")
    }
    check(len(bodies) == 3, f"expected the three fingerprint functions, found {sorted(bodies)}")
    if len(bodies) < 2:
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
    # The per-table signatures are what the PER-PROBE staleness check uses, so the same rule
    # applies there - and removing it from the global fingerprint alone left the bug reachable
    # through that door, with the extra hazard that the answer then depended on which process
    # asked.
    sig_src = bodies.get("table_signatures", "")
    check(bool(sig_src), "table_signatures not found in introspect.py")
    sig_code = [
        line for line in sig_src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    check(
        not any("_RENDER_VERSION" in line for line in sig_code),
        "table_signatures folds in _RENDER_VERSION, so a presentation tweak marks every probe "
        "stale - and a server started before the edit disagrees with a CLI run started after it.",
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


def test_switched_off_rules_never_run() -> None:
    """A rule set to `disabled` or `draft` must not produce findings. Two layers.

    THIS IS THE BUG THIS TEST WAS WRITTEN FOR. DQ-D01 and DQ-D02 were marked `disabled`
    precisely because they double-report with a structural family, and both still ran - only
    the PROBE's status was ever consulted, never the RULE's. The flagged-record count and the
    score therefore carried duplicates, which for a data-quality tool is the worst defect
    available: its own numbers were wrong.

    Checked at the layer that decides, so it holds however the caller reaches it.
    """
    from app.rules.catalog import runnable_reason
    from app.rules.spec import AnomalyRule, CompiledProbe

    def probe(status: str = "active") -> CompiledProbe:
        return CompiledProbe(rule_id="X-1", summary_sql="s", detail_sql="d", status=status)

    def rule(status: str) -> AnomalyRule:
        return AnomalyRule(rule_id="X-1", title="T", status=status)

    check(runnable_reason(probe(), rule("active")) == "",
          "an active rule with a compiled probe must be allowed to run")

    for status in ("disabled", "draft"):
        reason = runnable_reason(probe(), rule(status))
        check(bool(reason), f"a {status} rule's probe was allowed to run")
        check(status in reason,
              f"the reason for not running a {status} rule must name its status, so the report "
              f"can say why; got {reason!r}")

    check(bool(runnable_reason(probe("failed"), rule("active"))),
          "a failed probe must not run even when its rule is active")
    check(bool(runnable_reason(probe("not_applicable"), rule("active"))),
          "a not_applicable probe must not run even when its rule is active")
    check(runnable_reason(probe(), None) == "",
          "a probe whose rule was deleted falls back to its own status")


def test_pruning_uses_runnable_ids_only() -> None:
    """Switching a rule off must eventually REMOVE its probe, not merely block it each run."""
    import ast

    path = os.path.join(_APP, "compiler.py")
    source = open(path, encoding="utf-8").read()
    calls = [
        ast.get_source_segment(source, node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "prune_removed"
    ]
    check(bool(calls), "no prune_removed call found in compiler.py")
    for call in calls:
        check(
            "for r in runnable" in (call or ""),
            "prune_removed is passed every rule id, including disabled and draft ones, so a "
            "switched-off rule keeps its compiled probe in the catalog for ever. Pass the "
            f"runnable ids instead. Found: {call}",
        )


def test_expansion_skips_empty_tables() -> None:
    """A family must not generate a probe over a table with no rows.

    Such a probe cannot find anything and does not fail - it reports a clean result, or a zero
    scope that then has to be explained away. Ten were generated against one empty table here.

    An UNKNOWN row count is deliberately kept: unknown is not empty, and skipping a real check
    on a guess is the more expensive mistake.
    """
    from app.rules.expand import Feature, _drop_empty_tables
    from app.rules.schema_index import load_index

    schema = "\n".join([
        "TABLE demo.populated  -- 1,234 rows",
        "  - id int NOT NULL PK",
        "  - other_id int",
        "",
        "TABLE demo.empty_one  -- EMPTY: 0 rows. Nothing can be found here.",
        "  - id int NOT NULL PK",
        "",
        "TABLE demo.unknown_size",
        "  - id int NOT NULL PK",
        "",
    ])
    index = load_index(schema_text=schema, numeric_text="")

    check(index.get("demo.populated").row_count == 1234,
          "a row count on the TABLE line must be read back by the index")
    check(index.get("demo.empty_one").row_count == 0,
          "an EMPTY marker must be read back as zero rows, or the filter below cannot fire")
    check(index.get("demo.unknown_size").row_count is None,
          "a table with no stated count must read as unknown, never as zero")

    features = [
        Feature(values={"table": "demo.populated"}, label="kept"),
        Feature(values={"table": "demo.empty_one"}, label="dropped"),
        Feature(values={"table": "demo.unknown_size"}, label="kept-unknown"),
        Feature(values={"child_table": "demo.empty_one",
                        "parent_table": "demo.populated"}, label="dropped-child"),
        Feature(values={"child_table": "demo.populated",
                        "parent_table": "demo.empty_one"}, label="dropped-parent"),
    ]
    kept, dropped = _drop_empty_tables(features, index)
    kept_labels = {f.label for f in kept}

    check(kept_labels == {"kept", "kept-unknown"},
          f"wrong features survived the empty-table filter: {sorted(kept_labels)}")
    check(len(dropped) == 3, f"expected 3 dropped features, got {len(dropped)}")
    check(all(t == "demo.empty_one" for t, _ in dropped),
          "the dropped features must name the empty table, so the skip is explainable")


def test_schema_block_states_row_counts() -> None:
    """The row count must reach the Author and the Verifier, not just Grounding.

    It lived only in the index built for Grounding, so once Grounding named an empty table
    nothing downstream could catch it. The renderer now states it on the TABLE line and the
    prompts tell both agents to read it - assert all three, because any one alone is useless.
    """
    import ast

    path = os.path.join(_APP, "db", "introspect.py")
    source = open(path, encoding="utf-8").read()
    render = next(
        (ast.get_source_segment(source, n) for n in ast.walk(ast.parse(source))
         if isinstance(n, ast.FunctionDef) and n.name == "_render"),
        "",
    )
    check(bool(render), "_render not found in introspect.py")
    check("EMPTY" in render and "rows" in render,
          "_render must mark empty tables and state row counts on the TABLE line")

    from app.graph import prompts

    check("EMPTY" in prompts.GROUNDING_SYSTEM,
          "GROUNDING_SYSTEM must tell the model never to scope a rule on an empty table")
    check("EMPTY" in prompts.ANOMALY_SQL_AUTHOR_SYSTEM,
          "the SQL Author checklist must include the scope-size check, so a grounding mistake "
          "is still caught before it reaches the database")
    check("NOT APPLICABLE" in prompts.RULE_VERIFIER_SYSTEM.upper(),
          "RULE_VERIFIER_SYSTEM must separate 'the table is empty' (not applicable) from "
          "'the predicate matches nothing' (reject), or it burns rewrites on unfixable probes")


def test_a_dedup_that_reduces_nothing_is_refused() -> None:
    """A probe claiming a per-entity grain must actually have one.

    THE INCIDENT: a probe wrote a textbook de-duplication -

        ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY t.updated_at DESC) ... WHERE row_num = 1

    - which reduced nothing, because `t.id` is the row's OWN key: every partition held one row.
    Its scope came back as 110,181 against a table of 110,184 rows holding ~35,749 things. The
    Verifier read the shape and approved it ("correctly selects one latest row per task"), and
    the sample rows happened to be distinct so the grain check passed too. Three rules on one
    table counted things while three counted updates, and no percentage was comparable.

    This tests the OUTCOME rather than the mechanism on purpose: checking HOW a probe
    de-duplicates can always be satisfied by writing something that looks right.
    """
    from app.rules.contract import dedup_problems

    repeated = (
        "TABLE well.task_daily  -- 110,184 rows\n"
        "  - [id]  int NOT NULL  PK\n"
        "  MANY ROWS PER task_code - de-duplicate (COUNT(DISTINCT ...))\n"
        "\nTABLE ref.uom  -- 12 rows\n  - [id]  int NOT NULL  PK\n"
    )
    for scope in (110181, 110184):
        check(
            bool(dedup_problems({"scope_total": scope}, repeated)),
            f"a scope of {scope:,} against a 110,184-row table marked MANY ROWS PER is accepted "
            f"- the de-duplication reduced nothing and every percentage is against the wrong "
            f"denominator",
        )
    for scope in (35749, 32461):
        check(
            not dedup_problems({"scope_total": scope}, repeated),
            f"a genuinely de-duplicated scope of {scope:,} is refused",
        )

    # A table the schema does NOT mark as repeated: one row IS one thing, so a scope equal to
    # the row count is correct and must never be refused.
    one_per_thing = "TABLE well.well_master  -- 841 rows\n  - [well_id]  int NOT NULL  PK\n"
    check(
        not dedup_problems({"scope_total": 841}, one_per_thing),
        "a probe examining every row of a table that holds one row per entity is refused - that "
        "is what a correct well-level check looks like",
    )
    check(
        not dedup_problems({"scope_total": 0}, repeated),
        "an empty scope is handled by the scope_total=0 check, not by this one",
    )
    check(
        not dedup_problems({"scope_total": 110181}, ""),
        "judged without a schema block - there is nothing to compare the scope against",
    )


def test_a_malformed_rule_heading_is_never_silently_ignored() -> None:
    """Writing an anomaly and having nothing happen is the worst outcome this file allows.

    A heading without an id - "## RULE Negative crew size" - matched nothing, so the rule was
    skipped with no error, no warning, and no mention in the count. Someone adds a check, saves,
    compiles, and believes it is running when it does not exist. The CLI's own help calls that
    the worst failure mode a data-quality tool has.

    A near-miss is REPORTED, never guessed at, because guessing is worse: before this,
    "## RULE DQ-D27 Negative crew size" (separator forgotten) parsed happily by splitting on the
    id's own hyphen, giving id "DQ" and title "D27 Negative crew size" - a rule filed under a
    name nobody could find.
    """
    from app.rules.loader import _RULE_HEADING, _malformed_headings, _next_free_id

    valid = [
        "## RULE DQ-D27 - Negative crew size",
        "## RULE DQ-D27 — Negative crew size",     # em dash, as Word produces
        "## RULE DQ-D27 – Negative crew size",     # en dash
        "## RULE DQ-D27: Negative crew size",
        "## RULE DQ-D27 : Negative crew size",
    ]
    for line in valid:
        m = _RULE_HEADING.match(line)
        check(bool(m), f"a valid heading is rejected: {line!r}")
        if m:
            check(
                m.group(1) == "DQ-D27" and m.group(2) == "Negative crew size",
                f"heading parsed to the wrong id/title: {line!r} -> {m.groups() if m else ()}",
            )

    for line in ("## RULE DQ-D27 Negative crew size", "## RULE Negative crew size"):
        check(
            not _RULE_HEADING.match(line),
            f"{line!r} parses, so a rule is filed under a wrong or missing id",
        )
        reported = _malformed_headings(line, "data_anomalies.md", {"DQ-D25", "DQ-D26"})
        check(
            bool(reported),
            f"{line!r} is neither parsed NOR reported - the rule vanishes silently",
        )
        check(
            "SILENTLY IGNORED" in reported[0] and "## RULE <id> - <title>" in reported[0],
            "the error does not say what happened or how to fix it",
        )

    check(
        _next_free_id({"DQ-A01", "DQ-D25", "DQ-D26", "DQ-B04"}) == "DQ-D27",
        "the suggested next id is wrong - it must continue the largest existing family",
    )
    check(_next_free_id(set()) == "", "an empty catalog must suggest nothing rather than guess")

    # And the real file must still be clean.
    from app.rules.loader import load_rules

    _rules, errors = load_rules()
    check(not errors, "domain/data_anomalies.md now reports heading errors: " + "; ".join(errors))


def test_two_rules_measuring_the_same_thing_are_reported() -> None:
    """Nothing but the catalog-as-a-whole can notice that two rules have converged.

    THE INCIDENT: a rule meaning "pegging exists but no deadline can be computed" lost its scope
    filter during a recompile and became equivalent to "the expected rig-on date is missing" -
    same table, same condition, same (absent) scope. Both reported the same 61 wells in one
    report, under two ids, and every total that summed them was overstated by those 61.

    The Verifier cannot catch this: it reviews ONE rule and has never seen the other.

    The signature must include the SCOPE, and that is not a detail. Two rules legitimately share
    a condition while examining different populations - "the expected rig-on date is missing"
    and "the rig arrived but none was ever planned" flag the same absent date, the second only
    among wells where the rig arrived. Comparing conditions alone called those duplicates too.
    """
    from app.rules.contract import probe_signature

    def summary(table: str, flag: str, scope: str = "") -> str:
        where = f"    WHERE {scope}\n" if scope else ""
        return (
            "WITH scoped AS (\n"
            f"    SELECT w.id, CASE WHEN {flag} THEN 1 ELSE 0 END AS is_anomaly\n"
            f"    FROM {table} AS w\n" + where + ")\n"
            "SELECT 'DQ-X' AS rule_id, COUNT(*) AS scope_total,\n"
            "       SUM(is_anomaly) AS anomaly_count FROM scoped;"
        )

    same_a = summary("well.well_master", "w.ex_rig_on_date IS NULL")
    # Same measurement, different aliases, different formatting, different id literal.
    same_b = (
        summary("well.well_master", "wm.ex_rig_on_date IS NULL")
        .replace("'DQ-X'", "'DQ-Y'").replace(" AS w\n", " AS wm\n").replace("w.id", "wm.id")
    )
    check(
        probe_signature(same_a) == probe_signature(same_b),
        "two probes measuring the same thing get different signatures because of aliases or "
        "formatting - the duplicate that shipped would still ship",
    )

    scoped = summary("well.well_master", "w.ex_rig_on_date IS NULL", "w.rig_on_date IS NOT NULL")
    check(
        probe_signature(same_a) != probe_signature(scoped),
        "a rule examining a NARROWER population is called a duplicate of one examining all of "
        "it - that accuses two correct rules of being the same, which is how a warning like "
        "this gets switched off",
    )
    other = summary("well.well_master", "w.ex_rig_off_date IS NULL")
    check(
        probe_signature(same_a) != probe_signature(other),
        "two different conditions on one table collide",
    )
    check(
        probe_signature("SELECT 1") == "",
        "a probe with nothing comparable must return no signature rather than a partial one - a "
        "FALSE duplicate is worse than a missed one",
    )

    import ast

    path = os.path.join(_APP, "rules", "catalog.py")
    source = open(path, encoding="utf-8").read()
    body = ast.get_source_segment(source, next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "duplicate_probes"
    ))
    check(
        'probe.source == "expanded"' in body,
        "structural family members are compared against each other. They share a shape BY "
        "CONSTRUCTION and their features are already deduplicated, so this reports nine groups "
        "of false duplicates - and a warning that cries wolf gets ignored",
    )


def test_author_sees_every_outstanding_objection() -> None:
    """A rewrite must not be able to fix one objection by reintroducing another.

    THE BUG: the feedback builder was an if/elif chain, so exactly one message ever reached the
    author - while the state it reads goes to deliberate trouble to keep the reviewer's semantic
    instruction alive across attempts, for precisely this reason. A rule rejected on MEANING,
    rewritten, and then tripping a mechanical check saw only the mechanical error. It fixed
    that, quietly reinstated what the reviewer had refused, and was rejected again on review -
    two attempts spent alternating between two objections, satisfying neither, and the rule
    recorded as failed with its budget spent.

    Adding two hard checks in FRONT of the reviewer (grain, saturation) turned that from an
    occasional collision into a likely one, because both fire after the reviewer has spoken.
    """
    from app.graph.nodes.sql_author import _feedback_sections

    base = {"summary_sql": "SELECT 1", "detail_sql": "SELECT 2"}
    semantic = "Derive the threshold from the data, do not invent a multiplier."

    alone = "\n".join(_feedback_sections({**base, "verify_feedback": semantic}))
    check(semantic in alone, "a verifier rejection on its own never reaches the author")
    check(
        "YOUR PREVIOUS ATTEMPT WAS" in alone,
        "the author is not shown the SQL it is being asked to change",
    )

    for mechanical, label in (
        ("validation_error", "the validator"),
        ("exec_error", "the database"),
        ("contract_error", "the contract/grain checks"),
    ):
        both = "\n".join(
            _feedback_sections({**base, mechanical: "something broke", "verify_feedback": semantic})
        )
        check(
            "something broke" in both,
            f"a rejection from {label} does not reach the author",
        )
        check(
            semantic in both,
            f"when {label} rejects a rewrite, the reviewer's standing objection is DROPPED - "
            f"the author will fix the mechanical fault and reinstate what was already refused",
        )
        check(
            both.index("something broke") < both.index(semantic),
            "the mechanical fault must come first: a query that cannot run cannot be reviewed, "
            "so that is what has to be fixed before the semantic objection can even be tested",
        )
        # AND THE AUTHOR MUST BE TOLD WHICH ONE WINS.
        #
        # A probe correctly scoped to 35,796 tasks was told by the reviewer to key on the row
        # `id` instead. It complied, the scope became the full 110,181 rows, the mechanical
        # check refused it three times, and the rule failed - having had the right answer on
        # attempt two. Showing both objections without ranking them is what kept it chasing an
        # instruction the code would never accept.
        check(
            "THE REQUIREMENT ABOVE WINS" in both,
            "the author is shown two objections and not told which takes precedence. When the "
            "reviewer's judgement contradicts a mechanical check, the author will follow the "
            "reviewer and be refused until its budget runs out",
        )
        check(
            "disregard that part of the review" in both,
            "the author is never given permission to ignore a review that cannot be satisfied, "
            "so a mistaken objection is terminal rather than recoverable",
        )

    from app.graph.prompts import RULE_VERIFIER_SYSTEM

    check(
        "MECHANICAL CHECKS WILL REFUSE" in RULE_VERIFIER_SYSTEM,
        "the verifier is not told that its recommendations are themselves checked - so it will "
        "keep demanding rewrites the contract refuses, at a full attempt each",
    )
    check(
        "unique per ROW" in RULE_VERIFIER_SYSTEM,
        "the verifier is not warned against naming a row-unique key as an entity identity, "
        "which is the exact advice that cost DQ-D27 its entire retry budget",
    )


def test_detail_rows_are_one_per_entity() -> None:
    """A findings row must name one thing to fix, and name it once.

    THE INCIDENT: the task data keeps one record per update. Four probes read it without
    resolving to the current record, so DETAIL returned the same task once per historical
    update - 43,534 rows for 10,860 distinct tasks, and 75-79% duplication on three more
    checks. The report's headline counted history rows, and the workbook handed someone four
    copies of every row to fix. A fifth probe used a positional row number as its key, so its
    findings could not be traced to anything at all.

    Every one of those rules says in plain words to take the most recent record per task.
    Nothing checked that it had happened, and the evidence was already in hand: the sample rows
    the executor fetches during compilation. This is decided from those, with no model call.
    """
    from app.rules.contract import grain_problems

    cols = ["entity_key", "entity_label", "evidence_x"]

    repeated = [["T1", "a", 1], ["T1", "a", 1], ["T1", "a", 1], ["T2", "b", 2], ["T2", "b", 2],
                ["T3", "c", 3]]
    check(
        bool(grain_problems(cols, repeated)),
        "a DETAIL repeating the same entity_key is accepted - the grain bug that inflated a "
        "report fourfold would ship again",
    )

    positional = [[i, "x", i] for i in range(1, 9)]
    check(
        bool(grain_problems(cols, positional)),
        "entity_key as a positional row number (1, 2, 3 ...) is accepted, so a finding can name "
        "a position in a result set instead of a record someone can fix",
    )

    clean = [["A", "a", 1], ["B", "b", 2], ["C", "c", 3], ["D", "d", 4], ["E", "e", 5],
             ["F", "f", 6]]
    check(
        not grain_problems(cols, clean),
        "a correct DETAIL is being rejected - false rejections cost a full authoring cycle each",
    )
    check(
        not grain_problems(cols, [["T1", "a", 1], ["T1", "a", 1]]),
        "judging grain from two sample rows: too little evidence, and a wrong rejection here is "
        "more expensive than the duplicate it would catch",
    )
    check(
        not grain_problems(["evidence_x"], clean),
        "grain judged without an entity_key column - there is nothing to judge against",
    )

    # THE CASE THAT ACTUALLY SHIPPED, and which the two checks above do not see: the keys were
    # all DISTINCT record ids, and everything a reader looks at repeated. Reproduced at the
    # measured ratio - 25 sample rows carrying 10 distinct findings.
    from app.rules.contract import grain_concerns

    history = [[1000 + i, "TASK-%d" % (i // 4), 1.1, "same sentence"] for i in range(25)]
    graingy = "TABLE x.y  -- 9 rows\n  MANY ROWS PER task_code - de-duplicate\n"
    check(
        bool(grain_problems(cols + ["evidence_t"], history, False, graingy)),
        "DETAIL repeating one record under distinct ids is accepted when the SCHEMA itself marks "
        "the table as MANY ROWS PER entity - this is the 43,534-rows-for-10,860-tasks bug",
    )

    # THE REJECTION MUST NAME WHAT REPEATS, not just count it.
    #
    # The first version reported only the arithmetic - "25 rows reduce to 10" - and left the
    # author to work out WHICH column identified the thing. Three rules then burned three
    # attempts each without ever reaching the reviewer, while rules that happened to guess the
    # right column passed first time. That difference was guesswork, not capability, and the
    # sample already held the answer.
    message = " ".join(grain_problems(cols + ["evidence_t"], history, False, graingy))
    check(
        "TASK-0" in message,
        "the rejection does not quote the record that repeats, so the author must guess which "
        "column to group on - exactly what made three rules fail three attempts each",
    )
    check(
        "task_code" in message,
        "the rejection does not pass on the grain marker the SCHEMA already states, which names "
        "the very key the author needs to partition by",
    )
    check(
        "ROW_NUMBER" in message and "PARTITION BY" in message,
        "the rejection says to de-duplicate but not how - the author is left to invent the "
        "mechanism while its retry budget runs out",
    )
    check(
        not grain_problems(cols + ["evidence_t"], history, False, ""),
        "repetition is REFUSED without the schema marker to justify it. A hundred records "
        "pointing at one missing parent legitimately look alike; only the reviewer can tell, so "
        "without the marker this must be a concern rather than a refusal",
    )
    check(
        bool(grain_concerns(cols + ["evidence_t"], history)),
        "unexplained repetition does not even reach the reviewer as a concern",
    )
    # The measured separation: correct probes returned 25 distinct rows from 25, broken ones 5
    # to 15. Anything in between must not sit near the line.
    distinct25 = [[i, "TASK-%d" % i, i, "t%d" % i] for i in range(25)]
    check(
        not grain_problems(cols + ["evidence_t"], distinct25, False, graingy)
        and not grain_concerns(cols + ["evidence_t"], distinct25),
        "a probe returning 25 distinct findings from 25 rows is flagged - that is what EVERY "
        "correct probe in the measured report looked like",
    )


def test_a_saturated_scope_is_refused_above_the_floor() -> None:
    """scope_total must be a denominator, not the anomalies counted twice.

    Three checks in one report read "N of N (100.00%)", one of which had a real denominator the
    week before (373 of 422) and had since collapsed onto its own failures. A percentage that is
    100% by construction tells a business reader that everything is broken, about a population
    defined as the broken things.

    The floor matters as much as the rule: a small population genuinely can be entirely bad -
    the same check legitimately reported 5 of 5 - so below it this stays an advisory for the
    reviewer rather than a refusal.
    """
    from app.config import settings
    from app.rules.contract import sanity_concerns, saturation_problems

    big = settings.saturation_floor + 1000
    check(
        bool(saturation_problems({"scope_total": big, "anomaly_count": big})),
        f"a probe flagging all {big} records in its scope is stored as though it had measured "
        f"something",
    )
    check(
        not saturation_problems({"scope_total": 5, "anomaly_count": 5}),
        "a genuinely small all-bad population is refused - 5 of 5 was a real finding",
    )
    check(
        bool(sanity_concerns({"scope_total": 5, "anomaly_count": 5})),
        "below the floor it must still reach the reviewer as an advisory, or it is simply hidden",
    )
    check(
        not saturation_problems({"scope_total": big, "anomaly_count": big - 1}),
        "a probe with a real denominator is being refused",
    )

    import ast

    path = os.path.join(_APP, "graph", "nodes", "sanity_gate.py")
    body = ast.get_source_segment(
        open(path, encoding="utf-8").read(),
        next(n for n in ast.walk(ast.parse(open(path, encoding="utf-8").read()))
             if isinstance(n, ast.FunctionDef) and n.name == "sanity_gate_node"),
    )
    for fn in ("grain_problems", "saturation_problems"):
        check(
            fn in body and body.index(fn) < body.index("# ── ADVISORY"),
            f"{fn} is not enforced as a HARD contract check - it was advisory in effect once "
            f"already, and a report shipped with the consequences",
        )


def test_summary_is_told_which_counts_are_unconfirmed() -> None:
    """The executive summary must not quote a saturated count as plain fact.

    The finding's own section carried the caveat; the summary did not, and stated "6,435 tasks
    cannot be traced to an activity" where 6,435 was also everything that check examined. Most
    readers read only the summary.
    """
    import ast

    path = os.path.join(_APP, "graph", "nodes", "summarizer.py")
    source = open(path, encoding="utf-8").read()
    body = ast.get_source_segment(source, next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "summarizer_node"
    ))
    check(
        'row["anomaly_count"] == row["scope_total"]' in body,
        "the summarizer never identifies which findings flagged their whole scope",
    )
    check(
        "UNCONFIRMED" in body,
        "the summarizer is not told to qualify a count with no denominator, so it will present "
        "one beside measured percentages as the same kind of number",
    )


def test_reference_prune_never_starves_a_rule() -> None:
    """Cutting the reference material must not remove what a rule is ABOUT.

    business_rules.md is cut per rule, because a rule about milestone dates does not need the
    task-to-work-breakdown mapping. That saving is only legitimate while three things hold, and
    all three have already been got wrong once:

      1. A SECTION NAMING ONE OF THE RULE'S OWN TAGS IS ALWAYS KEPT. Word-frequency overlap is
         diluted by long prose: when the weightage rollup rule was rewritten into plain business
         language its overlap with the section DEFINING weightage fell to 3%, and that section
         would have been dropped from the one rule that cannot be written without it.
      2. THE AUTHOR AND THE VERIFIER SEE THE SAME TEXT. The reviewer judges the SQL against the
         rules; showing it a definition the author never saw makes it reject correct work.
      3. A WORKED EXAMPLE IS CUT ONLY WHERE few_shots.md ITSELF SAYS IT MAY BE. That file is
         never scored: scoring it against business language dropped "Scope is what you EXAMINED"
         for 55 of 60 rules, because it teaches probe craft in the engine's vocabulary while the
         rules are written in the business's. Each example declares `applies:` instead, and a
         universal one must survive even the emptiest condition set.
    """
    from app.graph import prompts
    from app.graph.context import prune_reference, split_sections
    from app.rules.loader import load_rules

    rules, _errors = load_rules()
    check(bool(rules), "no rules to check the prune against")

    big = [
        (h, b) for h, b in split_sections(prompts.BUSINESS_RULES)
        if h and len(b) > settings_keep_below()
    ]
    check(bool(big), "no prunable sections - the prune cannot be exercised by this test")

    for rule in rules:
        text = " ".join([rule.title, rule.category, rule.entity, rule.method, rule.body])
        tags = tuple(rule.tags or ())
        pruned = prune_reference(prompts.BUSINESS_RULES, text, tags, "test")

        # (1) every section naming one of this rule's declared tags survives
        for heading, body in big:
            named = any(
                tag.strip().lower() in (heading + "\n" + body).lower()
                for tag in tags if len(tag.strip()) > 2
            )
            if named:
                check(
                    body in pruned,
                    f"{rule.rule_id} declares tag(s) {tags} and the section '{heading[:40]}' "
                    f"names one of them, but the prune dropped it",
                )

        # (2) the reviewer reads exactly what the author read
        check(
            prompts.author_system(text, tags).count(pruned.strip()[:400])
            == prompts.verifier_system(text, tags).count(pruned.strip()[:400])
            == 1,
            f"{rule.rule_id}: the author and the verifier are shown different business rules",
        )

        # (3) a worked example is cut only where the FILE says it may be, and a universal one
        #     survives even the emptiest possible condition set
        minimal = prompts.author_system(text, tags, {"always"})
        for heading, body in split_sections(prompts.FEW_SHOTS):
            if not heading:
                continue
            declared = re.search(r"^\s*[-*]\s*applies\s*:\s*(\S+)", body, re.I | re.M)
            if declared is None or declared.group(1).strip().lower() == "always":
                check(
                    body.strip()[:200] in minimal,
                    f"{rule.rule_id}: the worked example '{heading[:45]}' is universal "
                    f"(applies: always, or undeclared) but was cut anyway",
                )


def settings_keep_below() -> int:
    from app.config import settings

    return settings.reference_keep_below


def test_family_clones_must_prove_they_execute() -> None:
    """A cloned probe is never stored active on the strength of textual substitution alone.

    THE INCIDENT: a foreign-key template selected the child entity key and the child foreign
    key separately. On one of thirty-six features they were the same column, so the clone
    selected it twice and SQL Server rejected the statement. Both static guards passed - no
    unfilled token, no foreign column - and the probe was filed `active`. It failed in an
    unattended run instead, as an ODBC error nobody could attribute to a rule.

    The executor's docstring already states the principle for authored probes: a query that is
    never executed is never checked. Clones are the majority of the catalog, so they are where
    it matters most.
    """
    import ast

    path = os.path.join(_APP, "compiler.py")
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)

    check(
        any(isinstance(n, ast.FunctionDef) and n.name == "_smoke_test" for n in ast.walk(tree)),
        "compiler._smoke_test is gone - nothing executes a cloned query before storing it",
    )

    body = next(
        (ast.get_source_segment(source, n) for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "compile_rules"),
        "",
    )
    check(
        "_smoke_test(probe)" in body,
        "the clone path never calls _smoke_test, so a query that cannot run is stored active "
        "and fails later in an unattended run",
    )
    check(
        body.index("_smoke_test(probe)") < body.index(
            'report.compiled if probe.status == "active"'
        ),
        "the smoke test must run BEFORE the probe's status decides where it is recorded, or a "
        "clone that cannot execute is still counted as compiled",
    )

    # IT MUST RUN THE SUMMARY ONLY. Executing both halves turned a compile into an outage: a
    # foreign-key DETAIL scan across a 124,000-row table took 223 seconds, the next hit the
    # 600-second timeout, and the one after dropped the connection entirely after 936s. The
    # summary is sufficient - every fault this catches is rejected when the statement is parsed,
    # before a row is read - and it takes the SHORT timeout for the same reason.
    smoke = next(
        (ast.get_source_segment(source, n) for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_smoke_test"),
        "",
    )
    check(
        '"detail_sql": ""' in smoke,
        "the smoke test executes the DETAIL query. On a large database that is a full scan per "
        "clone, and forty of them timed out and killed the connection - while proving nothing "
        "the summary does not already prove",
    )
    check(
        '"smoke_test": True' in smoke,
        "the smoke test does not identify itself to the executor, so it inherits the ten-minute "
        "detail timeout to check a one-row aggregate",
    )

    executor = open(os.path.join(_APP, "graph", "nodes", "executor.py"), encoding="utf-8").read()
    check(
        "smoke_test" in executor and "query_timeout if smoke else" in executor,
        "the executor ignores the smoke-test flag, so the short timeout is never applied",
    )
    check(
        'if not (detail_sql or "").strip():' in executor,
        "the executor runs a blank DETAIL rather than skipping it",
    )


def test_every_reader_of_schema_txt_survives_a_comment_on_the_table_line() -> None:
    """Whatever _render() writes after a table name, every reader must still see the table.

    THE INCIDENT: adding "  -- 10,302 rows" to the TABLE line broke context.split_blocks(),
    whose pattern was anchored to end-of-line. Nothing raised. The pruner simply saw 5 tables
    instead of 75, fell under ANOMALY_SCHEMA_PRUNE_ABOVE_TABLES, and returned the FULL schema
    on every author and verifier call - roughly 18,000 tokens each, for a whole compile.

    A silent loss of an optimisation is worse than a crash: the run still succeeds, the report
    is still right, and the only symptom is the bill. So this asserts the two readers agree
    with each other on REAL rendered text, not on a regex spelling.
    """
    from app.graph import context
    from app.rules import schema_index

    rendered = (
        "TABLE dbo.plain\n"
        "  - id  int NOT NULL  PK\n"
        "\n"
        "TABLE dbo.counted  -- 10,302 rows\n"
        "  - id  int NOT NULL  PK\n"
        "\n"
        "TABLE dbo.blank  -- EMPTY: 0 rows. Nothing can be found here.\n"
        "  - id  int NOT NULL  PK\n"
    )
    expected = {"dbo.plain", "dbo.counted", "dbo.blank"}

    blocks = set(context.split_blocks(rendered))
    check(
        blocks == expected,
        f"context.split_blocks() lost tables to the comment on the TABLE line: got {sorted(blocks)}",
    )

    indexed = set(schema_index.load_index(rendered, "").tables)
    check(
        indexed == expected,
        f"schema_index.load_index() lost tables to the comment: got {sorted(indexed)}",
    )
    check(
        blocks == indexed,
        "the pruner and the schema index disagree about which tables exist - one of them is "
        "reading a form of the TABLE line the other does not write",
    )

    listed = context.table_index(rendered).splitlines()
    check(
        len(listed) == len(expected),
        f"the grounding index dropped tables: {len(listed)} line(s) for {len(expected)} tables",
    )


def test_family_retries_another_member_before_giving_up() -> None:
    """One rejected representative must not write off its whole family.

    THE INCIDENT: a compile recorded 54 failures, of which only FOUR were distinct problems.
    DQ-G01-001 was rejected and took 35 siblings with it; DQ-G05-001 was inapplicable on one
    column and took 15 more, on columns that were perfectly measurable. The report then read as
    though the database had 54 faults.

    A family is one sentence of business prose applied to many schema features. The features
    differ - different tables, different columns - so the next member is a genuinely DIFFERENT
    query, not a retry of the rejected one. That is why it is worth another call, and why an
    ordinary retry budget is the wrong instrument.

    Bounded by ANOMALY_FAMILY_AUTHOR_ATTEMPTS, because a family this database cannot express at
    all must not cost one call per feature to discover that.
    """
    import ast

    path = os.path.join(_APP, "compiler.py")
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)

    body = next(
        (ast.get_source_segment(source, n) for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "compile_rules"),
        "",
    )
    check(bool(body), "compile_rules not found in compiler.py")

    check(
        "family_author_attempts" in body,
        "the compile loop never consults ANOMALY_FAMILY_AUTHOR_ATTEMPTS, so a family whose "
        "chosen author fails still writes off every one of its members",
    )
    check(
        "family_attempts" in body,
        "no per-family attempt counter - either the family gets one try (the original bug) or "
        "it gets one call per feature (the opposite, and expensive)",
    )
    check(
        "authors[rule.family_id] = rule.rule_id" in body,
        "nothing promotes another member to author, so a failed representative is terminal",
    )

    from app.config import settings

    check(
        settings.family_author_attempts >= 1,
        f"family_author_attempts must be at least 1, got {settings.family_author_attempts}",
    )
    check(
        settings.family_author_attempts <= 10,
        "family_author_attempts is high enough that an inexpressible family would cost a call "
        f"per feature to rule out; got {settings.family_author_attempts}",
    )


def test_compile_progress_is_reportable() -> None:
    """A compile runs for tens of minutes, so its progress has to be observable.

    Not cosmetic. The last full compile took 2,305 seconds with no output any interface could
    show, which is indistinguishable from a hang - and the honest response to a hang is to kill
    it, losing the whole run.
    """
    import ast

    path = os.path.join(_APP, "compiler.py")
    source = open(path, encoding="utf-8").read()
    body = next(
        (ast.get_source_segment(source, n) for n in ast.walk(ast.parse(source))
         if isinstance(n, ast.FunctionDef) and n.name == "compile_rules"),
        "",
    )
    check("progress" in body, "compile_rules exposes no progress callback")
    check(
        "progress(index - 1, total, rule.rule_id)" in body,
        "the progress callback must report position, total and the rule being worked on - a "
        "bare percentage cannot tell an operator WHICH rule is slow",
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
    from app.graph.prompts import PROBE_PATTERNS
    from app.rules.loader import load_rules

    rules, errors = load_rules()
    check(not errors, "domain rule files failed to parse: " + "; ".join(errors))
    check(bool(rules), "no rules were parsed from domain/data_anomalies.md")
    # The probe shapes moved out of the markdown and into prompts.py, so that the file a
    # business owner edits holds business language only. Assert they still reach the author.
    check(bool(PROBE_PATTERNS.strip()), "PROBE_PATTERNS is empty - the SQL Author is shown "
          "no probe shape at all")

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

    The probe SHAPES are exempt and no longer live here at all: they are in prompts.py as
    PROBE_PATTERNS, name no table or column, and are not rules.
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
        ("switched-off rules never run", test_switched_off_rules_never_run),
        ("pruning uses runnable ids only", test_pruning_uses_runnable_ids_only),
        ("expansion skips empty tables", test_expansion_skips_empty_tables),
        ("schema block states row counts", test_schema_block_states_row_counts),
        ("family clones must prove they execute", test_family_clones_must_prove_they_execute),
        ("reference prune never starves a rule", test_reference_prune_never_starves_a_rule),
        ("a dedup that reduces nothing is refused", test_a_dedup_that_reduces_nothing_is_refused),
        ("a malformed rule heading is never silently ignored",
         test_a_malformed_rule_heading_is_never_silently_ignored),
        ("two rules measuring the same thing are reported",
         test_two_rules_measuring_the_same_thing_are_reported),
        ("author sees every outstanding objection", test_author_sees_every_outstanding_objection),
        ("detail rows are one per entity", test_detail_rows_are_one_per_entity),
        ("a saturated scope is refused above the floor",
         test_a_saturated_scope_is_refused_above_the_floor),
        ("summary is told which counts are unconfirmed",
         test_summary_is_told_which_counts_are_unconfirmed),
        (
            "every schema.txt reader survives a comment on the TABLE line",
            test_every_reader_of_schema_txt_survives_a_comment_on_the_table_line,
        ),
        ("family retries another member", test_family_retries_another_member_before_giving_up),
        ("compile progress is reportable", test_compile_progress_is_reportable),
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
