"""Tests for `app/api/jobs.py` -- the background job runner behind `POST /api/runs`.

Uses a fake in place of `Orchestrator` (same style as `FakeSource`/`FakeScope` in
test_suggest_tools.py): it replicates the ONE sequence that matters for this module's own
logic -- create the run row, then fire progress events, then finish -- without opening a
real pyodbc connection or calling OpenAI. No live database or API key needed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.store import FindingsStore
from app.domain.models import Run, RunStatus

import app.api.jobs as jobs_mod
from app.api.jobs import ConcurrencyLimitError, JobManager


@dataclass
class _FakeOutcome:
    run_id: str
    status: RunStatus
    error: str | None = None


class _FakeOrchestrator:
    """Stands in for `Orchestrator`: same __init__(progress=)/.run()/.close() shape."""

    behavior = "success"   # class-level, overridden per test via subclassing/monkeypatch
    delay = 0.0

    def __init__(self, progress=None) -> None:
        self.progress = progress
        self.store = FindingsStore(path=type(self).store_path)

    def run(self, *, tables, triggered_by, dry_run, enrich, generate_reports):
        run_id = FindingsStore.new_run_id()
        self.store.create_run(
            Run(run_id=run_id, started_at=datetime.now(timezone.utc), triggered_by=triggered_by)
        )
        if self.progress:
            self.progress("checks", "started", {})
        if type(self).delay:
            time.sleep(type(self).delay)
        if type(self).behavior == "fail":
            self.store.finish_run(run_id, RunStatus.FAILED, error_text="boom")
            return _FakeOutcome(run_id=run_id, status=RunStatus.FAILED, error="boom")
        self.store.finish_run(run_id, RunStatus.COMPLETED)
        return _FakeOutcome(run_id=run_id, status=RunStatus.COMPLETED)

    def close(self) -> None:
        self.store.close()


def _install_fake(monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path, *, behavior="success", delay=0.0):
    fake_cls = type("_FakeOrchestrator", (_FakeOrchestrator,), {
        "store_path": tmp_store_path, "behavior": behavior, "delay": delay,
    })
    monkeypatch.setattr(jobs_mod, "Orchestrator", fake_cls)
    # jobs.py's own lookup_store must point at the same temp file, not the real
    # (Settings-derived) store path -- see FindingsStore() call inside JobManager._run.
    real_store_cls = jobs_mod.FindingsStore

    class _PathedStore(real_store_cls):
        def __init__(self, *a, **kw):
            kw.setdefault("path", tmp_store_path)
            super().__init__(*a, **kw)

    monkeypatch.setattr(jobs_mod, "FindingsStore", _PathedStore)
    return fake_cls


def _mgr(max_concurrent: int = 2) -> JobManager:
    mgr = JobManager.__new__(JobManager)
    mgr._settings = SimpleNamespace(dq_max_concurrent_runs=max_concurrent)
    mgr._lock = jobs_mod.threading.Lock()
    mgr._jobs = {}
    mgr._active = 0
    return mgr


def _wait_until_settled(mgr: JobManager, job_id: str, timeout: float = 5.0):
    """Poll by elapsed wall-clock time, not a fixed iteration count -- a fixed count (e.g.
    50 x 0.02s = 1s) is exactly the kind of budget that looks generous in isolation and
    then flakes under load (the full suite, a slow CI box), because the real constraint is
    wall-clock time, not iterations. 5s is far more than any of these fake, in-memory,
    no-sleep-by-default runs should ever need.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = mgr.get(job_id)
        if job.status != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running after {timeout}s")


def test_a_successful_run_reaches_completed_with_its_real_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path,
):
    _install_fake(monkeypatch, tmp_store_path)
    mgr = _mgr()
    job = mgr.start_run(tables=None, triggered_by="test", enrich=False, generate_reports=False)
    final = _wait_until_settled(mgr, job.job_id)
    assert final.status == "completed"
    assert final.run_id is not None
    assert final.run_id.startswith("run_")
    assert final.error is None


def test_a_failing_run_is_reported_as_failed_with_its_error(
    monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path,
):
    _install_fake(monkeypatch, tmp_store_path, behavior="fail")
    mgr = _mgr()
    job = mgr.start_run(tables=None, triggered_by="test", enrich=False, generate_reports=False)
    final = _wait_until_settled(mgr, job.job_id)
    assert final.status == "failed"
    assert final.error == "boom"


def test_run_id_is_found_via_its_own_marker_not_the_newest_row_in_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path,
):
    """Regression guard for the exact race the `triggered_by` marker exists to avoid:
    if job B's run row is created (and would sort as "newest") before job A's progress
    callback fires, naively taking `list_runs(limit=1)[0]` would hand job A the WRONG
    run_id. Two runs with the same human label makes this concrete.
    """
    _install_fake(monkeypatch, tmp_store_path, delay=0.15)
    mgr = _mgr(max_concurrent=2)
    job_a = mgr.start_run(tables=None, triggered_by="same-label", enrich=False, generate_reports=False)
    time.sleep(0.03)  # job_a's row now exists; its progress callback has already run once
    job_b = mgr.start_run(tables=None, triggered_by="same-label", enrich=False, generate_reports=False)
    final_a = _wait_until_settled(mgr, job_a.job_id)
    final_b = _wait_until_settled(mgr, job_b.job_id)
    assert final_a.run_id is not None and final_b.run_id is not None
    assert final_a.run_id != final_b.run_id, "both jobs resolved to the same run_id"


def test_concurrency_limit_is_enforced(monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path):
    _install_fake(monkeypatch, tmp_store_path, delay=0.3)
    mgr = _mgr(max_concurrent=1)
    mgr.start_run(tables=None, triggered_by="first", enrich=False, generate_reports=False)
    with pytest.raises(ConcurrencyLimitError):
        mgr.start_run(tables=None, triggered_by="second", enrich=False, generate_reports=False)


def test_slot_is_released_after_completion_so_a_later_run_can_start(
    monkeypatch: pytest.MonkeyPatch, tmp_store_path: Path,
):
    _install_fake(monkeypatch, tmp_store_path)
    mgr = _mgr(max_concurrent=1)
    first = mgr.start_run(tables=None, triggered_by="first", enrich=False, generate_reports=False)
    _wait_until_settled(mgr, first.job_id)
    # the slot must be free now -- this must NOT raise
    mgr.start_run(tables=None, triggered_by="second", enrich=False, generate_reports=False)


def test_unknown_job_id_returns_none():
    mgr = _mgr()
    assert mgr.get("job_does_not_exist") is None
