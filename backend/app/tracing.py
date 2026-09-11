"""Optional Langfuse tracing. Completely inert unless LANGFUSE_PUBLIC_KEY is set.

WHY THIS MODULE EXISTS
----------------------
Two places need to know whether tracing is on: runtime.py (attaches the handler to the graph, so
one trace covers a whole compile or run) and llm.py (names each LLM span after the agent that made the
call). Putting the decision here means they cannot disagree, and there is exactly ONE place to
turn tracing off or swap the implementation.

THE KILL SWITCH
---------------
Blank LANGFUSE_PUBLIC_KEY (or LANGFUSE_SECRET_KEY) => callbacks() returns [] and flush() does
nothing, so every call path is identical to the pre-Langfuse code. This mirrors conventions the
app already uses: a blank API_KEY disables auth, a blank OPENAI_FAST_MODEL falls back to the
main model. Turning tracing off is `LANGFUSE_PUBLIC_KEY=` and a restart — no code to
comment out, nothing to revert.

The SDK also honours LANGFUSE_TRACING_ENABLED=false as a second, independent switch (verified in
the installed SDK: it reads that variable and disables the tracer when it is "false"). Either one
is sufficient.

WHY EVERY FAILURE IS SWALLOWED
------------------------------
Observability must never be the reason a compile or a detection run fails.
  * The langfuse/langchain packages are optional: a missing install degrades to "tracing off",
    never an ImportError that stops the CLI or server booting.
  * Initialisation is tried ONCE per process and the outcome cached, so a misconfigured host does
    not re-raise on every rule.
  * The SDK batches sends on a background thread and its docs state "SDK errors are caught and
    logged", so a stopped Langfuse container cannot block or fail a run.

⚠ TRACES CONTAIN THE FULL PROMPT AND RESPONSE of every LLM call — for this pipeline that includes
the schema, the business rules, and sampled anomaly rows. Only ever point this at a trusted,
access-controlled instance.
"""
from __future__ import annotations

import threading

from app.config import settings
from app.observability import get_logger

log = get_logger()

# Resolved once per process, behind a lock: a run executes on an asyncio worker thread (see
# runtime.py), so several threads can call callbacks() simultaneously on a busy server and must
# not each build their own handler.
_lock = threading.Lock()
_handler = None      # the CallbackHandler once built, else None
_resolved = False    # True after the single initialisation attempt, success or failure


def enabled() -> bool:
    """The single source of truth for "is tracing on". Cheap; safe to call per LLM call."""
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def _handler_or_none():
    global _handler, _resolved
    if _resolved:
        return _handler
    with _lock:
        if _resolved:  # another thread finished while we waited
            return _handler
        try:
            # Imported here, not at module scope, so an environment without langfuse installed
            # still boots normally with tracing simply off.
            from langfuse.langchain import CallbackHandler

            # The SDK reads LANGFUSE_PUBLIC_KEY / _SECRET_KEY / _BASE_URL from the environment.
            # app.config calls load_dotenv() at import time, so they are already in os.environ
            # by the time this runs — no need to pass them explicitly.
            _handler = CallbackHandler()
            log.info(
                "langfuse: tracing ENABLED -> %s",
                settings.langfuse_base_url or "(SDK default host)",
            )
        except Exception:  # noqa: BLE001 - tracing must never break startup or a turn
            log.exception("langfuse: could not initialise - continuing WITHOUT tracing")
            _handler = None
        _resolved = True
    return _handler


def callbacks() -> list:
    """Callbacks to merge into a LangChain/LangGraph config. Empty list means tracing is off.

    Returning a list (rather than the handler itself) lets callers do the merge without knowing
    whether tracing is on: an empty list means they leave their config untouched.
    """
    if not enabled():
        return []
    handler = _handler_or_none()
    return [handler] if handler is not None else []


def flush() -> None:
    """Best-effort: push queued traces before the process exits. Never raises.

    The SDK sends in batches on a background thread, so without this the last few traces of a
    long-running server can be lost when it is stopped.
    """
    if not enabled() or _handler is None:
        return
    try:
        from langfuse import get_client

        get_client().flush()
        log.info("langfuse: flushed pending traces")
    except Exception:  # noqa: BLE001
        log.warning("langfuse: flush failed - a few traces may be lost", exc_info=True)
