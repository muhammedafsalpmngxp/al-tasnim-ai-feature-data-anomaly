# LLM Model Selection — Cost vs. Capability

The direct question: **which OpenAI model should generate the report?** The honest answer
is **not one model** — the four LLM jobs in this feature have different reasoning demands
and wildly different call volumes, and pricing one model for all four either overpays for
the cheap jobs or underpowers the one job that actually needs reasoning. This document sets
the tiered strategy and, importantly, **how to keep it correct as OpenAI's lineup changes**
— my own knowledge of current model names/pricing is dated (see caveat below), so the
design never hardcodes a model name in code; it hardcodes a *role*, and the model behind
each role is one config value.

---

## 1. The four jobs, and what each actually needs

| Job | Runs per report | Input size | Reasoning needed | Cost driver |
|---|---|---|---|---|
| **Narrate** — 2 sentences per finding | once per finding (∼50 today, hundreds once the full ~1,093-check catalogue is built) | one finding + ≤5 evidence rows (~1–2K tokens) | **low** — summarise structured data in plain language | **volume** — this is 90%+ of all LLM calls in a run |
| **Correlate** — group findings into root-cause incidents | once per report | the whole finding set (~50–300 findings, ~20–40K tokens) | **medium-high** — cross-referencing many findings to spot a shared cause (the `DUP-002` + `PLC-003` + `REF-007` = one root cause example) | one large call, not many small ones |
| **Agentic investigate** — bounded tool-loop per flagged conflict | ~15–25 times per report (docs/04 §3.4) | one finding + tool results, iteratively | **medium** — decide the next query from the last result; tool-calling required | moderate — several calls per investigation, but only for CRITICAL/HIGH master-data conflicts |
| **Executive summary** | once per report | the incidents + top findings (~5–10K tokens) | **medium** — the one artefact a manager actually reads first | one call, small in isolation |

**The architectural point:** narration is where the token volume lives, and it is also the
job needing the least reasoning — the numbers are already computed and validated by SQL
(Phase 1/2); the model's whole job is turning `{"check_id": "BIZ-107", "affected_count":
14003, ...}` into one clear sentence, with the number injected back in afterward and
checked against the evidence (docs/02 §7 — "no invented numbers"). That is squarely a job
for the **cheapest capable tier**, not the flagship. Correlation and the executive summary
are the opposite: they run once, so even a much pricier per-token rate barely moves the
total bill, while the reasoning quality directly determines whether the report's headline
insight is useful or generic.

---

## 2. The recommendation: tiered, not a single model

```
DQ_LLM_MODEL_NARRATE     = <cheapest tier that reliably follows a JSON schema>
DQ_LLM_MODEL_AGENT       = <mid tier with strong tool-calling>
DQ_LLM_MODEL_CORRELATE   = <top tier, or a stronger reasoning-tuned model>
DQ_LLM_MODEL_SUMMARY     = <same as CORRELATE — it reads the correlator's own output>
```

Four env vars, each a plain model-name string, each independently swappable. **No file in
`app/sentinel/llm/` ever contains a literal model name** — every call reads
`get_settings().dq_llm_model_narrate` (etc.), so upgrading a tier when OpenAI ships a new
model is a one-line `.env` change, not a code change or a redeploy of logic.

### Why tiering, concretely, for this workload

- At ~50 findings today (and design headroom to ~1,093 once the full check catalogue is
  built), narration is the call that scales with the database, not with the report's
  sophistication. Pricing that job at the flagship rate multiplies the cost by however many
  findings exist — pricing it at the cheap tier keeps the bill roughly flat regardless of
  how many checks the catalogue grows to.
- Correlation and the summary are exactly the two jobs the business actually reads and where
  reasoning quality is visible — a manager notices if the "root cause" section reads as
  generic filler versus actually connecting three findings the reader wouldn't have
  connected themselves. Paying for a better model here is cheap in absolute terms (one call)
  and highest-leverage in outcome.
- The agentic tool-loop (docs/04) sits in the middle: it needs to reliably call
  `run_query`/`describe_table` and interpret results well enough to pick a sensible next
  step, but each individual step is narrow (interpret one query's rows), so a mid-tier model
  with solid function-calling is normally sufficient — escalate only if investigation
  quality with the mid-tier proves weak in testing (an empirical call, not an assumption).

### What I'd set each tier to, and the honest caveat

My training data goes to **January 2026**; today in this session is **September 2026** —
eight months of OpenAI releases I have no visibility into. I am not going to assert a
specific current model name with false confidence. What I can say reliably:

- OpenAI publishes **cost-effective "mini"/"nano"-class variants alongside every flagship
  release**, aimed at exactly this narration-style, structured, high-volume job — that
  pattern has held across every generation I have knowledge of, and whichever is the
  current cheap tier at implementation time is the right choice for `DQ_LLM_MODEL_NARRATE`.
- For `DQ_LLM_MODEL_CORRELATE`/`DQ_LLM_MODEL_SUMMARY`, the current **flagship general
  model** (not a narrowly math/code-focused reasoning variant — this job is qualitative
  cross-referencing of business findings, not symbolic reasoning) is the right pick.
- For `DQ_LLM_MODEL_AGENT`, the **same mid/cheap tier as narration** is a reasonable
  starting point given moderate reasoning + tool-calling need; move up only if the agentic
  layer's investigation quality (docs/04 §3) is measurably weak in the golden-set tests.

**Action before this ships:** check `platform.openai.com/docs/models` for the current
lineup and current per-token pricing, and set the four env vars accordingly. This is a
five-minute config step, not a design decision — the code below has zero dependency on
which names you choose.

---

## 3. Cost control that doesn't depend on picking the "right" model

Model choice is one lever. These matter as much or more, and are already load-bearing in
the design regardless of which models are configured:

| Control | Where | Effect |
|---|---|---|
| Narration batched by family, not per-call-per-finding | `llm/narrate.py` | fewer, larger calls instead of many tiny ones — most providers price a large fraction of cost in per-request overhead, not only tokens |
| `DQ_LLM_MAX_TOKENS_PER_BATCH` | already in config (`02-FEATURE-PLAN.md` §7) | a hard ceiling per run regardless of finding count |
| `DQ_AGENT_MAX_TOOL_CALLS` / `DQ_AGENT_MAX_INVESTIGATIONS_PER_RUN` | already in config (`04-AGENTIC-LIVE-ANALYSIS.md` §3.2) | bounds the most expensive, most variable-cost job |
| PII excluded from every payload | `docs/02` §7 | smaller payloads, and a compliance requirement independent of cost |
| Deterministic fallback | `DQ_LLM_ENABLED=false` | the report still builds correctly with zero LLM cost — verified today (§5 below) |
| Token/cost recorded on every run | `dq.run.llm_tokens`, `llm_cost_usd` (already in the `Run` model) | cost is visible per report, not discovered at the end of a billing cycle |

---

## 4. Configuration added

```ini
# --- LLM model tiers (see docs/05-LLM-MODEL-SELECTION.md) ---
# Verify current names/pricing at platform.openai.com/docs/models before setting these.
DQ_LLM_MODEL_NARRATE=gpt-4o-mini
DQ_LLM_MODEL_AGENT=gpt-4o-mini
DQ_LLM_MODEL_CORRELATE=gpt-4o
DQ_LLM_MODEL_SUMMARY=gpt-4o

DQ_LLM_TEMPERATURE=0.1
DQ_LLM_MAX_RETRIES=2
DQ_LLM_TIMEOUT_SECONDS=60
```

The defaults above name the last models I have direct knowledge of as a *working baseline
so the system is runnable out of the box* — not a claim that they are OpenAI's current
best-value tier in September 2026. Treat the `.env` values, not this file, as the source of
truth once someone checks the live pricing page.

---

## 5. What was actually verified today

No `OPENAI_API_KEY` is configured in this environment (checked directly), so no live model
comparison could be run. What **was** verified:

- The narration/correlation/summary code paths all run end-to-end with `DQ_LLM_ENABLED=false`
  — the Excel and Word reports build correctly from the deterministic findings alone, with
  a visible banner noting enrichment was skipped. This is the fallback path, not a stub —
  it is the same code a real run takes when the LLM step is present but returns nothing
  usable.
- The citation-validation logic (`llm/validate.py`) is unit-tested against fabricated
  model output containing an invented number, and correctly rejects it — this runs with no
  network access and no API key, so it is verified regardless of which model ends up
  configured.

The live call path (`llm/client.py`) is written and ready; add a key to `.env` and set
`DQ_LLM_ENABLED=true` to exercise it for real.
