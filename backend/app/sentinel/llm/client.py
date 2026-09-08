"""LLM provider client -- OpenAI, structured output, retries, cost tracking.

Every call goes through here. No file outside this module ever imports the `openai`
package directly, and no file anywhere in the codebase names a specific model string --
callers pass a *role* (`Role.NARRATE`, `Role.CORRELATE`, ...) and this module resolves it
to whichever model is configured for that role (docs/05-LLM-MODEL-SELECTION.md). Swapping
a model when OpenAI ships a new one is a `.env` edit, never a code change.

Degrades cleanly: if `DQ_LLM_ENABLED=false`, or no API key is configured, or every retry
fails, `LLMClient.enabled` is False and every call returns `LLMResult(ok=False, ...)`
instead of raising -- the reporting layer already knows how to build a correct report
without narration (docs/02 §7: "Layer 1+2 alone produce a correct report").
"""
from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)


class Role(str, enum.Enum):
    """Which job this call is for -- resolves to a model via config, never a literal name."""

    NARRATE = "narrate"      # per-finding explanation. Cheapest tier, highest call volume.
    AGENT = "agent"          # bounded tool-loop investigation (docs/04).
    CORRELATE = "correlate"  # group findings into incidents. Runs once per report.
    SUMMARY = "summary"      # executive summary. Runs once per report.


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class LLMResult:
    ok: bool
    data: dict[str, Any] | None = None
    raw_text: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    error: str | None = None
    attempts: int = 0


class LLMClient:
    """Thin wrapper around the OpenAI SDK. Constructed once per run, reused across calls
    so usage totals accumulate for the run's cost report (`dq.run.llm_tokens`).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self.total_usage = LLMUsage()
        self.calls_made = 0
        self.calls_failed = 0
        self._client = None
        self._init_error: str | None = None
        # Reasoning-family models (confirmed live 2026-09-08: gpt-5-mini) reject the
        # `temperature` parameter outright with a 400. Detected and cached per model,
        # not hardcoded by name -- a name-based check ("if model.startswith('gpt-5')")
        # would need updating for every future reasoning model; catching the actual
        # error once and remembering it works for any of them, present or future.
        self._no_temperature_models: set[str] = set()

        if not self._s.dq_llm_enabled:
            self._init_error = "DQ_LLM_ENABLED=false"
        elif not self._s.openai_api_key:
            self._init_error = "no OPENAI_API_KEY configured"
        elif self._s.llm_provider != "openai":
            self._init_error = f"unsupported LLM_PROVIDER={self._s.llm_provider!r}"
        else:
            try:
                from openai import OpenAI  # local import: package optional at runtime

                self._client = OpenAI(
                    api_key=self._s.openai_api_key,
                    timeout=self._s.dq_llm_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"client init failed: {exc}"

        if self._init_error:
            log.warning("llm.disabled", reason=self._init_error)
        else:
            log.info("llm.ready", provider=self._s.llm_provider)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def disabled_reason(self) -> str | None:
        return self._init_error

    def _model_for(self, role: Role) -> str:
        """OPENAI_MODEL is the single source of truth: an empty per-role override falls
        back to it. Set a DQ_LLM_MODEL_* var only to run one specific job on a different
        model than the rest (docs/05) -- most setups need none of them.
        """
        override = {
            Role.NARRATE: self._s.dq_llm_model_narrate,
            Role.AGENT: self._s.dq_llm_model_agent,
            Role.CORRELATE: self._s.dq_llm_model_correlate,
            Role.SUMMARY: self._s.dq_llm_model_summary,
        }[role]
        return override or self._s.openai_model

    # ------------------------------------------------------------------------- calls
    def structured(
        self,
        role: Role,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str = "response",
    ) -> LLMResult:
        """One call, constrained to `json_schema` via the Responses API's structured
        output. Retries on transient failure up to DQ_LLM_MAX_RETRIES; never raises.
        """
        if not self.enabled:
            return LLMResult(ok=False, error=f"LLM disabled: {self._init_error}")

        model = self._model_for(role)
        last_error = ""
        for attempt in range(1, self._s.dq_llm_max_retries + 2):
            kwargs: dict[str, Any] = dict(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": json_schema,
                        "strict": True,
                    }
                },
            )
            if model not in self._no_temperature_models:
                kwargs["temperature"] = self._s.dq_llm_temperature
            try:
                resp = self._client.responses.create(**kwargs)
                text = getattr(resp, "output_text", "") or ""
                usage = getattr(resp, "usage", None)
                u = LLMUsage(
                    prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                )
                self.total_usage.prompt_tokens += u.prompt_tokens
                self.total_usage.completion_tokens += u.completion_tokens
                self.calls_made += 1
                parsed = json.loads(text) if text else None
                if parsed is None:
                    raise ValueError("empty response body")
                return LLMResult(
                    ok=True, data=parsed, raw_text=text, usage=u, model=model,
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if (
                    model not in self._no_temperature_models
                    and "temperature" in msg
                    and "not supported" in msg
                ):
                    self._no_temperature_models.add(model)
                    log.info("llm.temperature_unsupported", model=model)
                    continue  # retry this same attempt immediately, temperature omitted
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "llm.call_failed", role=role.value, model=model,
                    attempt=attempt, error=last_error[:200],
                )
                if attempt <= self._s.dq_llm_max_retries:
                    time.sleep(min(2 ** attempt, 8))

        self.calls_failed += 1
        return LLMResult(ok=False, error=last_error, model=model, attempts=self._s.dq_llm_max_retries + 1)

    def raw_with_tools(self, role: Role, *, input_messages: list[Any], tools: list[dict[str, Any]]):
        """One round trip for a tool-calling loop -- returns the SDK's raw response object
        (which may contain `function_call` items the caller must dispatch) rather than a
        parsed `LLMResult`, because a tool-loop caller (suggest.py) needs the full output,
        not a single structured JSON payload. Applies the same temperature self-healing as
        `structured()`. Raises on failure (unlike `structured()`) -- the caller is a bounded
        loop that already treats any exception as "end the session", so there is no
        separate degraded-result path to construct here.
        """
        model = self._model_for(role)
        kwargs: dict[str, Any] = {"model": model, "input": input_messages, "tools": tools}
        if model not in self._no_temperature_models:
            kwargs["temperature"] = self._s.dq_llm_temperature
        try:
            resp = self._client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if model not in self._no_temperature_models and "temperature" in msg and "not supported" in msg:
                self._no_temperature_models.add(model)
                log.info("llm.temperature_unsupported", model=model)
                kwargs.pop("temperature", None)
                resp = self._client.responses.create(**kwargs)
            else:
                raise
        usage = getattr(resp, "usage", None)
        self.total_usage.prompt_tokens += getattr(usage, "input_tokens", 0) or 0
        self.total_usage.completion_tokens += getattr(usage, "output_tokens", 0) or 0
        self.calls_made += 1
        return resp

    def usage_summary(self) -> dict[str, Any]:
        return {
            "calls_made": self.calls_made,
            "calls_failed": self.calls_failed,
            "prompt_tokens": self.total_usage.prompt_tokens,
            "completion_tokens": self.total_usage.completion_tokens,
            "total_tokens": self.total_usage.total,
        }
