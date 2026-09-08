"""The check framework: registry, context, and the mandatory-declaration guard.

Every check -- generated or hand-written -- goes through one path: register a function
with `@check(...)`, and the decorator refuses to register it if `grain` or `baseline` is
missing. That is not a style preference; it is what makes the two mistakes documented in
docs/01c (wrong baseline, wrong grain) structurally impossible to reintroduce silently.
A check that forgets to declare either fails at IMPORT time, not at report time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.db.source import SourceDatabase
from app.domain.models import Baseline, CheckResult, Finding, Grain
from app.logging import get_logger
from app.sentinel.normalise.normaliser import NormalisedSource
from app.sentinel.normalise.spec import NormalisationSpec
from app.sentinel.scope import Scope

log = get_logger(__name__)


class CheckRegistrationError(ValueError):
    """Raised at import time when a check is declared incompletely."""


@dataclass(slots=True)
class CheckContext:
    """Everything a check needs. Nothing a check needs comes from anywhere else --
    no check opens its own connection or reads its own config file, so every check runs
    against the SAME normalised sources, the SAME scope, and the SAME as-of date.
    """

    source: SourceDatabase
    scope: Scope
    spec: NormalisationSpec
    sources: dict[str, NormalisedSource]
    as_of_date: str  # ISO date, read live from the database at run start -- never today()

    def normalised(self, table: str) -> NormalisedSource | None:
        return self.sources.get(table)


@dataclass(slots=True)
class CheckOutcome:
    result: CheckResult
    findings: list[Finding] = field(default_factory=list)


CheckFn = Callable[[CheckContext], CheckOutcome]


@dataclass(slots=True)
class RegisteredCheck:
    id: str
    family: str
    grain: Grain
    baseline: Baseline
    severity: str
    title: str
    fn: CheckFn
    business_rule_ref: str | None = None
    source: str = "hand"  # 'hand' | 'generated'

    def run(self, ctx: CheckContext) -> CheckOutcome:
        try:
            outcome = self.fn(ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("check.errored", check_id=self.id, error=str(exc)[:300])
            return CheckOutcome(
                result=CheckResult(
                    check_id=self.id, family=self.family, status="error",
                    grain=self.grain.describe(), baseline=self.baseline.value,
                    error_text=str(exc)[:500],
                )
            )
        return outcome


_REGISTRY: dict[str, RegisteredCheck] = {}


def _validate_and_store(entry: RegisteredCheck) -> None:
    """The one path every check -- decorated or generated -- passes through. Raises at
    registration time (import time for hand-written checks; generator run time for
    generated ones, which happens once during orchestrator start-up) rather than letting
    a bad declaration reach a live run.
    """
    if entry.grain is None:
        raise CheckRegistrationError(f"check {entry.id!r}: grain is required")
    if entry.baseline is None:
        raise CheckRegistrationError(f"check {entry.id!r}: baseline is required")
    if not entry.baseline.enabled:
        raise CheckRegistrationError(f"check {entry.id!r}: {entry.baseline.disabled_reason}")
    if entry.id in _REGISTRY:
        raise CheckRegistrationError(f"check id {entry.id!r} is already registered")
    _REGISTRY[entry.id] = entry


def register_check(
    *,
    id: str,  # noqa: A002
    family: str,
    grain: Grain,
    baseline: Baseline,
    fn: CheckFn,
    severity: str = "high",
    title: str = "",
    business_rule_ref: str | None = None,
    source: str = "hand",
) -> None:
    """Programmatic registration -- used by the generator, which builds an unknown-at-
    import-time number of checks from config/column_semantics.yaml. Shares every guard
    with the decorator below; there is no separate, looser path for generated checks.
    """
    _validate_and_store(
        RegisteredCheck(
            id=id, family=family, grain=grain, baseline=baseline, severity=severity,
            title=title or id, fn=fn, business_rule_ref=business_rule_ref, source=source,
        )
    )


def check(
    *,
    id: str,  # noqa: A002
    family: str,
    grain: Grain,
    baseline: Baseline,
    severity: str = "high",
    title: str = "",
    business_rule_ref: str | None = None,
    source: str = "hand",
) -> Callable[[CheckFn], CheckFn]:
    """Decorator that registers a check function. Fails loudly, at import time, if the
    two mandatory declarations are missing or if `baseline` is disabled (Baseline.COMMITTED
    -- the business has not confirmed what committed_start/committed_end mean, so no check
    may use it as a yardstick; see docs/01c and Baseline.disabled_reason).
    """

    def wrap(fn: CheckFn) -> CheckFn:
        register_check(
            id=id, family=family, grain=grain, baseline=baseline, severity=severity,
            title=title or fn.__doc__ or id, fn=fn,
            business_rule_ref=business_rule_ref, source=source,
        )
        return fn

    return wrap


def all_checks() -> list[RegisteredCheck]:
    return sorted(_REGISTRY.values(), key=lambda c: c.id)


def get_check(check_id: str) -> RegisteredCheck | None:
    return _REGISTRY.get(check_id)


def registry_size() -> int:
    return len(_REGISTRY)


def clear_generated_checks() -> None:
    """Remove all `source == "generated"` entries.

    The registry is process-global, but a long-lived API server calls Orchestrator.run()
    repeatedly without restarting -- generated checks must be rebuilt fresh each run (a
    schema change between runs must be picked up), so the orchestrator clears and
    re-registers them at the start of every run's checks phase. Hand-written checks
    register once via decorators at module import time (Python caches imports) and are
    deliberately NOT touched here.
    """
    for cid in [c.id for c in _REGISTRY.values() if c.source == "generated"]:
        del _REGISTRY[cid]


def _clear_registry_for_tests() -> None:
    """Test-only: the registry is process-global so re-importing check modules in the
    same pytest session would otherwise raise 'already registered'.
    """
    _REGISTRY.clear()
