"""State passed between the compile graph's nodes.

ONE RULE PER GRAPH INVOCATION. The driver (app/compiler.py) runs this graph once per rule
rather than threading every rule through a single graph, and that is the decision everything
else here depends on:

  * each rule gets its OWN retry budgets, so a pathological rule cannot spend the budget that
    the next twenty rules need;
  * a rule that fails fails alone - it is recorded as `failed` with its error and the compile
    carries on, which is the only acceptable behaviour for a tool whose whole purpose is to
    report problems honestly;
  * the state stays small enough to read in a log line.

WHAT IS DELIBERATELY *NOT* HERE
-------------------------------
The full DETAIL result set. The executor fetches at most `sample_rows + 1` rows at compile
time and nothing else ever holds more. A compile that accumulated real findings would blow
both the context window and memory, and it has no reason to: compiling decides whether the SQL
is RIGHT, not what the data says today.
"""
from __future__ import annotations

from typing import Any, TypedDict


class CompileState(TypedDict, total=False):
    # ── Input: the rule being compiled (seeded by the driver, never modified) ──
    rule_id: str
    title: str
    body: str                     # the prose: what is wrong / why / how to detect / do NOT flag
    category: str
    severity: str
    entity: str
    method: str                   # rule | statistical | rollup
    sql_mode: str                 # pinned | seed | authored
    source: str                   # declared | generic
    tolerance: str                # the rule's `tolerance:` value, or "" when it declared none
    # Every non-reserved metadata key the rule declared. For a GENERIC probe these are the
    # schema features it was rendered against, which is how its tables are known exactly
    # without asking a model to guess at something already certain.
    params: dict[str, str]
    rule_hash: str
    structure_fingerprint: str
    # One structural hash per table in the database. The catalog writer folds the entries for
    # THIS probe's tables into its stored table_fingerprint, so staleness can later be judged
    # against the tables it reads rather than against the whole database.
    table_signatures: dict[str, str]

    # Seed SQL from the markdown. For `pinned` this IS the final SQL; for `seed` it is a
    # starting point; for `authored` both are empty.
    seed_summary_sql: str
    seed_detail_sql: str

    # ── Shared reference material (identical for every rule in one compile) ──
    # Held per-rule anyway because a node must never reach outside its state for the facts it
    # reasons about - that is what makes a single rule reproducible from its state alone.
    schema: str                   # the FULL schema block
    value_hints: str
    numeric_hints: str
    patterns: str                 # the ## PATTERNS section of data_anomalies.md
    coverage: str                 # what the generic probes already cover

    # ── Family rules (expands_over) ──
    # A rule applied to every matching feature in the schema. `placeholders` lists the tokens
    # the authored query must use in place of literal names; the templates are what get reused
    # across the other features, while summary_sql/detail_sql above always hold the runnable,
    # substituted form that the gates and the reviewer actually judge.
    expands_over: str
    placeholders: tuple[str, ...]
    summary_sql_template: str
    detail_sql_template: str

    # ── Grounding ──
    tables: list[str]             # the tables this rule actually concerns
    grounding_note: str
    applicable: bool              # False => this database cannot express the rule at all
    not_applicable_reason: str
    # The pruned schema actually shown to the Author and the Verifier. Separate from `schema`
    # on purpose: the Verifier must judge against the SAME block the Author wrote from, and
    # keeping both means a prune that went wrong is visible rather than silent.
    schema_block: str
    hint_block: str               # value + numeric hints, sliced to `tables`

    # ── SQL Author ──
    summary_sql: str
    detail_sql: str
    # Normalised SQL already attempted. Re-emitting one is pointless: identical SQL returns
    # identical data, so it cannot earn a different verdict - it only burns a cycle.
    tried_sql: list[str]

    # ── Validator (deterministic, pre-execution) ──
    validation_error: str
    # MECHANICAL rewrites used: a safety rejection, a broken static contract, or a database
    # error. Funded by MAX_SQL_RETRIES.
    retry_count: int

    # ── Executor (deterministic) ──
    exec_error: str
    summary_columns: list[str]
    summary_row: list[Any]        # the first row; exactly one is expected, by contract
    # How many rows SUMMARY actually returned, counted up to 2. The contract requires exactly
    # one, and the count is what proves it: without it a query that returned fifty rows is
    # indistinguishable from a correct one, because only the first is ever read.
    summary_row_count: int
    detail_columns: list[str]
    detail_rows: list[list[Any]]  # at most sample_rows - never the real result
    detail_truncated: bool
    # Always False during a compile: both halves are executed so both can be contract-checked
    # (see executor.py). The field exists because the detection run, which DOES skip DETAIL on
    # a zero count, reports the same outcome shape.
    detail_skipped: bool

    # ── Sanity gate (deterministic) ──
    contract_error: str           # HARD: the report has nowhere to put this output
    concerns: list[str]           # ADVISORY: handed to the Verifier to adjudicate
    advice: list[str]             # non-blocking notes (a missing recommended column)

    # ── Rule Verifier ──
    verify_ok: bool
    verify_feedback: str
    # Every rejection this reviewer has issued for this rule, in order. Shown back to it so a
    # second review cannot demand the opposite of the first - see verifier.py.
    feedback_history: list[str]
    # SEMANTIC rewrites used. Deliberately a SEPARATE counter from retry_count: sharing one
    # budget lets a couple of syntax errors early in a rule leave the Verifier with zero
    # rewrites, so it rejects and is overruled in the same breath - the worst of both.
    verify_retry_count: int
    verifier_note: str
    threshold_note: str           # where an `auto` threshold came from; required when used

    # ── Outcome ──
    status: str                   # active | failed | not_applicable
    error: str                    # populated when status is failed
    llm_calls: int
    # The finished CompiledProbe, assembled by catalog_writer. Typed as Any rather than
    # importing the dataclass: state.py is imported by every node, and a node that only reads
    # a couple of string fields should not drag the rules package in behind it.
    probe: Any
