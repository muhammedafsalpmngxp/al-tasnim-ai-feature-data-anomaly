"""Tests for the check registry itself -- registration guards, not live-DB behaviour.

These are what make the two mistakes in docs/01c (wrong baseline, wrong grain)
structurally impossible to reintroduce: a check that omits either must fail here, at
registration time, in a test that runs with no database at all.
"""
from __future__ import annotations

import pytest

from app.domain.models import Baseline, CheckResult, Grain
from app.sentinel.checks.base import (
    CheckContext,
    CheckOutcome,
    CheckRegistrationError,
    _clear_registry_for_tests,
    all_checks,
    check,
    clear_generated_checks,
    get_check,
    register_check,
    registry_size,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The registry is process-global; each test starts and ends with it empty so tests
    cannot see each other's registrations or leak into the real app's checks.
    """
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


def _noop(ctx: CheckContext) -> CheckOutcome:
    return CheckOutcome(result=CheckResult(check_id="X", family="X", status="pass"))


# --------------------------------------------------------------------- mandatory fields
def test_grain_is_required():
    with pytest.raises(CheckRegistrationError, match="grain is required"):
        register_check(
            id="T-1", family="T", grain=None, baseline=Baseline.NONE, fn=_noop,  # type: ignore[arg-type]
        )


def test_baseline_is_required():
    with pytest.raises(CheckRegistrationError, match="baseline is required"):
        register_check(
            id="T-1", family="T", grain=Grain.row(), baseline=None, fn=_noop,  # type: ignore[arg-type]
        )


def test_committed_baseline_is_rejected_at_registration():
    """The business has not confirmed what committed_start/committed_end mean (docs/01c);
    no check may use Baseline.COMMITTED as its yardstick, and this must fail loudly at
    registration, not silently produce a wrong finding later.
    """
    with pytest.raises(CheckRegistrationError, match="not confirmed"):
        register_check(
            id="T-1", family="T", grain=Grain.row(), baseline=Baseline.COMMITTED, fn=_noop,
        )


def test_duplicate_id_is_rejected():
    register_check(id="T-1", family="T", grain=Grain.row(), baseline=Baseline.NONE, fn=_noop)
    with pytest.raises(CheckRegistrationError, match="already registered"):
        register_check(id="T-1", family="T", grain=Grain.row(), baseline=Baseline.NONE, fn=_noop)


def test_decorator_and_programmatic_registration_share_the_same_guard():
    """@check(...) and register_check(...) must enforce identically -- there is no looser
    path for generated checks (docs/01e Section 5.2 -- generated checks pass through the
    SAME grain/baseline validation as hand-written ones).
    """
    with pytest.raises(CheckRegistrationError, match="baseline is required"):
        @check(id="T-1", family="T", grain=Grain.row(), baseline=None)  # type: ignore[arg-type]
        def _bad(ctx: CheckContext) -> CheckOutcome:
            return CheckOutcome(result=CheckResult(check_id="T-1", family="T", status="pass"))


# ------------------------------------------------------------------------------ registry
def test_valid_check_registers_and_is_retrievable():
    @check(id="T-1", family="T", grain=Grain.row(), baseline=Baseline.NONE, severity="high")
    def my_check(ctx: CheckContext) -> CheckOutcome:
        return CheckOutcome(result=CheckResult(check_id="T-1", family="T", status="pass"))

    assert registry_size() == 1
    entry = get_check("T-1")
    assert entry is not None
    assert entry.family == "T"
    assert entry.severity == "high"
    assert entry.source == "hand"


def test_all_checks_sorted_by_id():
    register_check(id="Z-1", family="Z", grain=Grain.row(), baseline=Baseline.NONE, fn=_noop)
    register_check(id="A-1", family="A", grain=Grain.row(), baseline=Baseline.NONE, fn=_noop)
    ids = [c.id for c in all_checks()]
    assert ids == sorted(ids)


def test_clear_generated_checks_only_removes_generated():
    register_check(id="HAND-1", family="H", grain=Grain.row(), baseline=Baseline.NONE,
                    fn=_noop, source="hand")
    register_check(id="GEN-1", family="G", grain=Grain.row(), baseline=Baseline.NONE,
                    fn=_noop, source="generated")
    assert registry_size() == 2
    clear_generated_checks()
    assert registry_size() == 1
    assert get_check("HAND-1") is not None
    assert get_check("GEN-1") is None


def test_generated_checks_can_be_rebuilt_after_clearing():
    """A long-lived process (an API server) calls the checks phase repeatedly; generated
    checks must be re-registerable on the second run without hitting 'already registered'.
    """
    register_check(id="GEN-1", family="G", grain=Grain.row(), baseline=Baseline.NONE,
                    fn=_noop, source="generated")
    clear_generated_checks()
    register_check(id="GEN-1", family="G", grain=Grain.row(), baseline=Baseline.NONE,
                    fn=_noop, source="generated")  # must not raise
    assert registry_size() == 1


# ------------------------------------------------------------------------- error capture
def test_a_check_that_raises_is_captured_as_an_error_result_not_a_crash():
    def boom(ctx: CheckContext) -> CheckOutcome:
        raise RuntimeError("simulated SQL failure")

    register_check(id="T-1", family="T", grain=Grain.row(), baseline=Baseline.NONE, fn=boom)
    entry = get_check("T-1")
    outcome = entry.run(ctx=None)  # type: ignore[arg-type]
    assert outcome.result.status == "error"
    assert "simulated SQL failure" in (outcome.result.error_text or "")
    assert outcome.findings == []
