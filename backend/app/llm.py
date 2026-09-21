"""Configurable LLM client shared by every agent node.

Provider is chosen by LLM_PROVIDER in .env:
  - "openai" / "open" (default) -> OpenAI (OPENAI_MODEL) via OPENAI_API_KEY
  - "local"                     -> Ollama (LLM_MODEL) via OLLAMA_BASE_URL

`fast=True` selects the cheap tier (OPENAI_FAST_MODEL / OLLAMA_FAST_MODEL) when one is set;
otherwise it falls back to the main model, so enabling it is purely opt-in and an unset fast
model can never break a node.

Every node calls chat() here, so switching provider or model tier is a pure config change.
That single funnel is also what makes per-run token accounting possible without touching any
node, prompt, or graph code.
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.observability import get_logger


def _is_openai() -> bool:
    return settings.llm_provider.lower() in ("open", "openai")


# -- Which agent ran on which model --------------------------------------------------------
# Every node passes its prompt constant from app.graph.prompts unchanged, so the calling agent
# is recognised here by OBJECT IDENTITY. This keeps the model trace in one place: no node,
# prompt or graph code has to change to appear correctly in the log and the usage table.
_PROMPT_LABELS: dict[int, str] | None = None
# Which domain generation the cache above was built from. The prompts carrying business rules
# are REASSIGNED when the domain files are reloaded (app.graph.prompts.reload_domain), which
# creates new string objects and makes every cached id stale - so a cache built once would
# silently label every agent "agent" from the first reload onwards. Rebuilding when the
# generation moves keeps the identity trick honest without asking any node to do anything.
_LABELS_GENERATION = -1


def _agent_label(system: str, label: str = "") -> str:
    """Which agent made this call.

    AN EXPLICIT LABEL WINS, and it has to be available for prompts the identity trick cannot
    see. Recognising an agent by `id(system)` works only while every node passes a prompt
    CONSTANT, and the Scout's system prompt is BUILT PER CALL - scout_system(max_proposals)
    formats a template, so it is a new string object every time and can never match a cached
    id. The Scout therefore appeared in the usage table and the log as the anonymous "agent",
    which is precisely the wrong answer for the one node whose whole cost is a single call.
    """
    global _PROMPT_LABELS, _LABELS_GENERATION
    if label:
        return label
    try:
        from app.graph import prompts as p  # deferred: avoids an import cycle

        generation = p.DOMAIN_GENERATION
    except Exception:  # noqa: BLE001 - logging must never break a request
        return "agent"

    if _PROMPT_LABELS is None or generation != _LABELS_GENERATION:
        try:
            _PROMPT_LABELS = {
                id(p.GROUNDING_SYSTEM): "grounding",
                id(p.ANOMALY_SQL_AUTHOR_SYSTEM): "sql_author",
                id(p.RULE_VERIFIER_SYSTEM): "verifier",
                id(p.SUMMARIZER_SYSTEM): "summarizer",
            }
            _LABELS_GENERATION = generation
        except Exception:  # noqa: BLE001 - logging must never break a request
            _PROMPT_LABELS = {}
    return (_PROMPT_LABELS or {}).get(id(system), "agent")


def active_model_for(fast: bool) -> str:
    """The model name a call at this tier will actually use."""
    return settings.fast_model if fast else settings.active_model


def _log_call(system: str, fast: bool, elapsed: float, label: str = "") -> None:
    try:
        model = active_model_for(fast)
        # Only label it "fast" when a DISTINCT fast model is really configured - otherwise the
        # log would claim a cheap tier that does not exist.
        tier = " (fast)" if fast and model != settings.active_model else ""
        get_logger().info("llm: %s -> %s%s in %.1fs", _agent_label(system, label), model, tier, elapsed)
    except Exception:  # noqa: BLE001
        pass


# -- Per-run token accounting ---------------------------------------------------------------
# Counts come from the provider's own response (`usage_metadata`), never estimated locally.
#
# Thread-local, because a compile or a run executes inside one worker thread. That gives one
# isolated accumulator with no locking. The corollary matters: whoever starts tracking must
# also READ the report FROM THE SAME THREAD. Reading it from an event loop after
# `await asyncio.to_thread(...)` returns would silently see an empty list.
_usage = threading.local()


def start_usage_tracking() -> None:
    """Begin (or restart) accounting on the current thread.

    Clearing is required, not merely tidy: asyncio's default executor REUSES worker threads, so
    without this a run would inherit the previous run's counts on the same thread.
    """
    _usage.calls = []


def _record_usage(system: str, fast: bool, resp, label: str = "") -> None:
    """Append one call's usage. Never raises - accounting must not break a run."""
    try:
        calls = getattr(_usage, "calls", None)
        if calls is None:
            return  # tracking not started on this thread (CLI one-off, eval script)
        # Ollama does not always populate usage_metadata, so this is best-effort: a call that
        # reports nothing contributes 0 rather than breaking the table.
        usage = getattr(resp, "usage_metadata", None) or {}
        calls.append(
            {
                "agent": _agent_label(system, label),
                "model": active_model_for(fast),
                "input": usage.get("input_tokens") or 0,
                "output": usage.get("output_tokens") or 0,
            }
        )
    except Exception:  # noqa: BLE001
        pass


def get_usage_report() -> list[dict]:
    """One aggregated row per agent for the current thread, in first-call order.

    Only agents that actually ran appear - the deterministic nodes (loader, validator, executor,
    sanity gate, scorer, report) never call an LLM, so they are absent by construction rather
    than listed as zero.
    """
    by_agent: dict[str, dict] = {}
    for call in getattr(_usage, "calls", []) or []:
        row = by_agent.setdefault(
            call["agent"],
            {"agent": call["agent"], "model": call["model"], "calls": 0, "input": 0, "output": 0},
        )
        row["calls"] += 1
        row["input"] += call["input"]
        row["output"] += call["output"]
        # A retry can land on a different model than the first attempt. Show that rather than
        # implying one model handled them all.
        if call["model"] not in row["model"].split(", "):
            row["model"] += ", " + call["model"]
    return list(by_agent.values())


def usage_call_count() -> int:
    """Total LLM calls on this thread - the figure ANOMALY_MAX_COMPILE_CALLS bounds.

    Read by the compile loop before each rule so one pathological rule cannot burn the budget.
    """
    return len(getattr(_usage, "calls", []) or [])


@lru_cache(maxsize=32)
def get_llm(temperature: float | None = 0.0, fast: bool = False):
    """A cached chat model for the configured provider.

    temperature=None builds the client with no temperature override - the fallback for models
    that only accept their default. fast=True selects the cheap tier.
    """
    if _is_openai():
        from langchain_openai import ChatOpenAI

        kwargs: dict = {
            "model": settings.fast_model if fast else settings.openai_model,
            "api_key": settings.openai_api_key,
            # Without a timeout a stalled provider request holds its worker thread forever, and
            # each thread here also holds a DB connection.
            "timeout": settings.llm_timeout,
            "max_retries": settings.llm_max_retries,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return ChatOpenAI(**kwargs)

    from langchain_ollama import ChatOllama

    # ChatOllama has no `timeout` field - passing one is silently dropped by pydantic, so the
    # timeout has to reach the underlying httpx client instead.
    kwargs = {
        "model": settings.fast_model if fast else settings.llm_model,
        "base_url": settings.ollama_base_url,
        "num_ctx": settings.ollama_num_ctx,
        "client_kwargs": {"timeout": settings.llm_timeout},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOllama(**kwargs)


def chat_structured(
    system: str,
    user: str,
    schema: type,
    temperature: float = 0.0,
    fast: bool = False,
    label: str = "",
):
    """One-shot call that returns an INSTANCE of `schema`, or None if that was not possible.

    WHY THIS EXISTS. A node that needs a verdict used to ask for JSON in the prompt, receive
    free text, and parse it. That parse fails on anything the prompt did not anticipate - a
    fenced code block, a trailing comma, a sentence of commentary before the object - and the
    nodes here treat an unreadable verdict as a REJECTION, because approving on a failed parse
    would let a bad probe through. So a formatting slip by the model costs a rewrite cycle and
    is recorded as if the reviewer had objected to the SQL.

    Structured output removes that class of failure: the provider is given the schema and
    constrains its own decoding to match, so the reply either satisfies the schema or the call
    raises. Verified against the configured model with a prompt explicitly demanding markdown
    and commentary - it still returned a clean object.

    RETURNS None RATHER THAN RAISING when the provider cannot do this at all (an older local
    model, say). The caller then falls back to asking for text and parsing it, so enabling this
    can never make a node worse than it was - only better where it is supported.
    """
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    from app.tracing import enabled as _tracing_on

    cfg = {"run_name": _agent_label(system, label)} if _tracing_on() else None
    start = time.perf_counter()

    # include_raw=True RETURNS THE ENVELOPE AS WELL AS THE PARSED OBJECT, and that is the whole
    # reason it is asked for here.
    #
    # Without it, with_structured_output hands back only the parsed instance - the token counts
    # live on the response it was parsed from, and that response is discarded before this
    # function sees it. Every call down this path therefore cost real money and reported none
    # of it. The Scout made that visible: its single model call logged "0 LLM call(s)" and
    # contributed nothing to the usage table, and as more nodes move to structured output the
    # gap between the usage report and the actual bill would only widen.
    #
    # The shape becomes {"raw": AIMessage, "parsed": obj | None, "parsing_error": exc | None},
    # and a parse failure arrives as a VALUE rather than an exception - so the failure handling
    # below has to cover both.
    def _call(temp, include_raw: bool):
        model = get_llm(temp, fast).with_structured_output(schema, include_raw=include_raw)
        return model.invoke(messages, config=cfg)

    def _attempt(temp):
        """The call, degrading to the plain shape if this provider cannot do include_raw.

        Not every integration accepts the argument. Losing token accounting is a far smaller
        loss than losing structured output altogether, so an unsupported argument falls back to
        exactly the behaviour this function had before - never to the text path.
        """
        try:
            return _call(temp, True), True
        except TypeError as exc:
            get_logger().debug("llm: include_raw unsupported (%s) - usage will not be recorded", exc)
            return _call(temp, False), False

    try:
        envelope, has_raw = _attempt(temperature)
    except Exception as exc:  # noqa: BLE001
        # Same temperature quirk the text path handles: some reasoning models reject a
        # non-default value and must be called without one.
        if _is_openai() and "temperature" in str(exc).lower():
            try:
                envelope, has_raw = _attempt(None)
            except Exception as inner:  # noqa: BLE001
                get_logger().warning("llm: structured output unavailable (%s) - falling back", inner)
                return None
        else:
            get_logger().warning("llm: structured output unavailable (%s) - falling back", exc)
            return None

    _log_call(system, fast, time.perf_counter() - start, label)

    if not has_raw:
        return envelope

    raw = (envelope or {}).get("raw")
    parsed = (envelope or {}).get("parsed")
    error = (envelope or {}).get("parsing_error")

    # RECORDED EVEN WHEN THE PARSE FAILED. The tokens were spent either way, and a failed
    # structured call is precisely the expensive case worth seeing in the usage table - it is
    # about to be retried down the text path, so the run pays for it twice.
    if raw is not None:
        _record_usage(system, fast, raw, label)

    if error is not None or parsed is None:
        get_logger().warning("llm: structured output could not be parsed (%s) - falling back", error)
        return None
    return parsed


def chat(system: str, user: str, temperature: float = 0.0, fast: bool = False,
         label: str = "") -> str:
    """One-shot system+user call, returning the raw text content."""
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    # Names the span after the agent making the call, so a trace reads
    # "grounding / sql_author / verifier / summarizer" instead of identical provider entries.
    # Deferred import: app.tracing imports app.config, and importing it at module scope would
    # add a cycle risk for no benefit.
    from app.tracing import enabled as _tracing_on

    # None when tracing is off, which is exactly invoke()'s default - so the disabled path is
    # byte-identical to the pre-Langfuse call.
    cfg = {"run_name": _agent_label(system, label)} if _tracing_on() else None
    start = time.perf_counter()
    try:
        resp = get_llm(temperature, fast).invoke(messages, config=cfg)
    except Exception as exc:  # noqa: BLE001
        # Some OpenAI reasoning models reject a non-default temperature - retry without it.
        if _is_openai() and "temperature" in str(exc).lower():
            resp = get_llm(None, fast).invoke(messages, config=cfg)
        else:
            raise
    _log_call(system, fast, time.perf_counter() - start, label)
    _record_usage(system, fast, resp, label)
    return resp.content if isinstance(resp.content, str) else str(resp.content)
