"""Tier 2 -- the suggestion agent (2026-09-08 design discussion).

Answers "what happens past the ~51 fixed checks": a bounded, read-only agent explores the
live schema, forms a hypothesis, VERIFIES it with its own query, and proposes a candidate
new check. Nothing it produces can ever reach a report on its own -- see the three-tier
model this implements:

  Tier 1  Fixed checks (business_rules.py, generator.py)  -- the ONLY thing that ever
          produces a number in a report. Reviewed, reproducible, unchanged by this module.
  Tier 2  THIS MODULE -- proposes candidates for a human to review. A suggestion sits in
          the `suggestion` table with status='pending' until a person approves or rejects
          it. Approval does not wire anything into the check registry automatically --
          it emits reviewable Python boilerplate (using the SAME @check(...) pattern as
          every hand-written check) that a person must read, and manually move into a real
          module, before it ever runs as part of a report.
  Tier 3  The docs/04 investigation agent (deepens an EXISTING finding) -- not this, and
          not built yet.

This is deliberately the ONLY place in the codebase where an LLM decides what SQL to run.
Every query it writes goes through the exact same `ReadOnlyGuard` as everywhere else
(`tools.py`), is row-capped and time-boxed regardless of the query's own shape, and is
logged verbatim to `agent_trace` for a human to audit -- see `tools.py` for the guards.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.db.source import SourceDatabase
from app.db.store import FindingsStore
from app.logging import get_logger
from app.sentinel.checks.base import all_checks
from app.sentinel.llm.client import LLMClient, Role
from app.sentinel.llm.prompts import suggest_system_prompt, suggest_user_prompt
from app.sentinel.llm.schemas import EXPLORATION_TOOLS
from app.sentinel.llm.tools import ExplorationTools
from app.sentinel.scope import Scope

log = get_logger(__name__)

_TOOL_NAMES = {"list_tables", "describe_table", "run_query", "propose_check"}

# Confirmed live 2026-09-08: a session proposed "rig-on date earlier than expected" with
# severity_guess=high -- its OWN hypothesis text correctly called this an acceleration
# (business rule §5: early is good, never a defect), but the metadata contradicted the
# prose. The prompt now warns against this explicitly (see suggest_system_prompt), but a
# prompt instruction is not enforcement -- this is the same lesson as every guard
# elsewhere in this codebase: real safety lives in code, not in wording a model might not
# follow. A hypothesis using any of these words is refused before it is even persisted,
# regardless of the severity or SQL attached to it.
_EARLY_IS_GOOD_MARKERS = (
    "earlier than", "ahead of schedule", "acceleration", "accelerat", "sooner than",
    "before its expected", "before the expected", "before its target", "before the target",
)


@dataclass(slots=True)
class SuggestionOutcome:
    skipped: bool = False
    skip_reason: str = ""
    session_id: str = ""
    proposals_made: int = 0
    tool_calls_used: int = 0
    rounds_used: int = 0
    stopped_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


def run_suggestion_session(
    store: FindingsStore,
    source: SourceDatabase,
    scope: Scope | None = None,
    client: LLMClient | None = None,
    *,
    max_calls: int = 20,
    max_rows_per_query: int = 50,
    max_seconds_total: int = 120,
    max_rounds: int = 12,
) -> SuggestionOutcome:
    """Run one bounded exploration session. Degrades exactly like the enrichment layer:
    if the LLM client is disabled, this returns immediately with `skipped=True` -- there
    is no partial or fabricated result.
    """
    client = client or LLMClient()
    if not client.enabled:
        log.info("suggest.skipped", reason=client.disabled_reason)
        return SuggestionOutcome(skipped=True, skip_reason=client.disabled_reason or "disabled")

    scope = scope or Scope()
    session_id = f"suggest_{uuid.uuid4().hex[:10]}"
    tools = ExplorationTools(
        source, scope, max_calls=max_calls, max_rows_per_query=max_rows_per_query,
        max_seconds_total=max_seconds_total,
    )
    existing_titles = [c.title for c in all_checks()]
    history: list[Any] = [
        {"role": "system", "content": suggest_system_prompt(existing_titles)},
        {"role": "user", "content": suggest_user_prompt()},
    ]

    outcome = SuggestionOutcome(session_id=session_id)
    stopped_reason = "max_rounds reached"

    for round_no in range(1, max_rounds + 1):
        if tools.budget_exhausted:
            stopped_reason = "tool budget exhausted"
            break
        try:
            resp = client.raw_with_tools(Role.AGENT, input_messages=history, tools=EXPLORATION_TOOLS)
        except Exception as exc:  # noqa: BLE001
            log.error("suggest.round_failed", round=round_no, error=str(exc)[:300])
            stopped_reason = f"API error: {str(exc)[:200]}"
            break

        history.extend(resp.output)
        calls = [item for item in resp.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            stopped_reason = "agent concluded (no further tool calls)"
            break

        for call in calls:
            if call.name not in _TOOL_NAMES:
                output = f"error: unknown tool {call.name!r}"
            else:
                try:
                    args = json.loads(call.arguments) if call.arguments else {}
                except (TypeError, ValueError):
                    args = {}
                if call.name == "list_tables":
                    output = tools.list_tables()
                elif call.name == "describe_table":
                    output = tools.describe_table(args.get("table", ""))
                elif call.name == "run_query":
                    output = tools.run_query(args.get("sql", ""))
                else:  # propose_check
                    recorded, output = _handle_proposal(store, tools, args)
                    if recorded:
                        outcome.proposals_made += 1
            history.append({
                "type": "function_call_output", "call_id": call.call_id, "output": output,
            })
        outcome.rounds_used = round_no

    outcome.tool_calls_used = tools.calls_used
    outcome.stopped_reason = stopped_reason
    outcome.usage = client.usage_summary()
    store.add_agent_trace(session_id, tools.trace)
    log.info(
        "suggest.complete", session_id=session_id, proposals=outcome.proposals_made,
        tool_calls=outcome.tool_calls_used, rounds=outcome.rounds_used,
        stopped=stopped_reason, tokens=outcome.usage.get("total_tokens", 0),
    )
    return outcome


def generate_boilerplate(suggestion: dict[str, Any]) -> str:
    """Render an APPROVED suggestion as a reviewable Python module using the exact same
    `@check(...)` pattern as every hand-written check. Saved with a `.py.suggested`
    extension by the caller -- not `.py` -- so it is structurally impossible for this to
    be imported and registered without a person deliberately renaming it after reading
    it. This is the human-in-the-loop gate: the agent drafts, a person decides.
    """
    check_id = f"SUG-{suggestion['suggestion_id']:04d}"
    fn_name = f"sug_{suggestion['suggestion_id']:04d}_{suggestion['family'].lower()}"
    sql = suggestion["sql_text"].strip()
    return f'''"""AGENT-PROPOSED CHECK -- suggestion #{suggestion['suggestion_id']}, NOT YET REVIEWED.

Generated {suggestion.get('created_at', '')} by the Tier 2 suggestion agent
(app/sentinel/llm/suggest.py) after bounded, read-only exploration and one independent
verification run ({suggestion.get('test_row_count')} rows on last re-run).

Hypothesis, in the agent's own words:
    {suggestion['hypothesis']}

DO NOT import or register this file as-is. Before this can run as part of a report:
  1. Read the SQL below and confirm it is actually correct -- the agent's exploration was
     bounded and capped; verify the logic against the real business rules yourself.
  2. Confirm the GRAIN this check should run at (docs/01c) -- the agent does not know
     your table grain declarations; `grain=Grain.row()` below is a placeholder, not a
     verified choice. Get it from `column_semantics.yaml` for the real table(s) involved.
  3. Confirm the BASELINE (docs/01c) if this check compares two dates/values -- the agent
     cannot know which column is authoritative; `baseline=Baseline.NONE` below is a
     placeholder.
  4. Set `business_rule_ref` if this maps to a section of BUSINESS_RULES.md.
  5. Rename this file to end in `.py` and move it into `business_rules.py` (or a new
     module imported the same way) once you are satisfied.
"""
from __future__ import annotations

from app.domain.models import Baseline, CheckResult, Finding, FindingClass, Grain, Severity
from app.sentinel.checks.base import CheckContext, CheckOutcome, check


@check(
    id="{check_id}", family="{suggestion['family']}",
    grain=Grain.row(),          # TODO: verify -- see docstring above
    baseline=Baseline.NONE,     # TODO: verify -- see docstring above
    severity="{suggestion['severity_guess']}",
    business_rule_ref=None,     # TODO: set if this maps to a BUSINESS_RULES.md section
    title="{suggestion['title']}",
)
def {fn_name}(ctx: CheckContext) -> CheckOutcome:
    # TODO: review this query. It was proposed and verified once by the suggestion
    # agent, not by a human, and not against the normalised source (ctx.normalised(...))
    # the way every other check in this codebase is -- consider rewriting it to use
    # ctx.normalised("{suggestion.get('table_ref', 'schema.table')}").at_grain() once you
    # have confirmed the correct grain, rather than querying the raw table directly.
    row = ctx.source.one(f"""
        SELECT COUNT(*) AS n FROM (
{sql}
        ) AS x
    """) or {{}}
    n = int(row.get("n") or 0)
    result = CheckResult(check_id="{check_id}", family="{suggestion['family']}",
                          status="fail" if n else "pass", violations=n,
                          grain="row", baseline=Baseline.NONE.value)
    if not n:
        return CheckOutcome(result=result)
    finding = Finding(
        check_id="{check_id}", family="{suggestion['family']}",
        severity=Severity("{suggestion['severity_guess']}"),
        finding_class=FindingClass.DEFECT,  # TODO: confirm -- VIOLATION/GAP may fit better
        title=f"{{n}} rows: {suggestion['title']}",
        entity_type="table", entity_id="{suggestion.get('table_ref', '')}",
        entity_label="{suggestion.get('table_ref', '')}",
        affected_count=n, grain="row", baseline="none",
        why_it_matters="{suggestion['hypothesis'].replace(chr(34), chr(39))}",
        evidence=[{{"count": n}}],
    )
    return CheckOutcome(result=result, findings=[finding])
'''


def _handle_proposal(
    store: FindingsStore, tools: ExplorationTools, args: dict[str, Any]
) -> tuple[bool, str]:
    """Persist one propose_check call, unless it is refused outright (see
    `_EARLY_IS_GOOD_MARKERS`). Independently re-executes the proposed SQL (structured, for
    the review record) rather than trusting the agent's own run_query preview -- the human
    reviewing this suggestion sees a result this code just verified a second time, not one
    the agent merely claims it saw.

    Returns `(recorded, message)` -- `recorded` is False for a missing-field error or an
    outright refusal, so the caller's `proposals_made` count reflects what actually
    entered the review queue, not how many times the model called the tool.
    """
    required = ("title", "family", "severity_guess", "hypothesis", "sql_text", "table_ref")
    missing = [k for k in required if not args.get(k)]
    if missing:
        return False, f"error: propose_check missing required field(s): {missing}"

    combined_text = f"{args['title']} {args['hypothesis']}".lower()
    hit = next((m for m in _EARLY_IS_GOOD_MARKERS if m in combined_text), None)
    if hit:
        log.warning("suggest.refused_early_is_good", title=args["title"], marker=hit)
        return False, (
            f"REFUSED, not recorded: this describes an EARLY/accelerated outcome "
            f"(matched {hit!r}), which business rule §5 defines as good news, never a "
            "defect, regardless of severity. Do not propose this or anything like it."
        )

    test = tools.test_execute(args["sql_text"])
    row_count, sample = (test[0], test[1]) if test else (None, None)
    suggestion_id = store.add_suggestion(
        title=args["title"], family=args["family"], severity_guess=args["severity_guess"],
        hypothesis=args["hypothesis"], sql_text=args["sql_text"], table_ref=args["table_ref"],
        test_row_count=row_count, test_sample=sample,
    )
    log.info("suggest.proposed", suggestion_id=suggestion_id, title=args["title"])
    if test is None:
        return True, (
            f"recorded as suggestion #{suggestion_id}, but the SQL failed to re-run "
            "independently -- it will need correction before a human can approve it"
        )
    return True, f"recorded as suggestion #{suggestion_id} ({row_count} rows on independent re-run)"
