"""Runs the existing `Orchestrator` on a background thread and tracks its progress.

`Orchestrator.run()` is synchronous -- it opens a live pyodbc connection and makes
blocking OpenAI calls, and a full run takes minutes (~5-6 for a full LLM-enriched run).
An API request handler cannot call it inline without blocking the whole server, so each
"generate report" request starts it on its own thread and returns a `job_id`
immediately; the frontend polls `GET /api/jobs/{job_id}` for progress.

This module does not reimplement or fork any run logic -- it calls the exact same
`Orchestrator.run()` that `app/cli.py`'s `run` command calls, and reads results back
through `FindingsStore`, exactly like the CLI's `cmd_run` does. The only thing supplied
here that the CLI doesn't use is the `progress` callback `Orchestrator.__init__` already
accepts.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.db.store import FindingsStore
from app.logging import get_logger
from app.sentinel.orchestrator import Orchestrator

log = get_logger(__name__)


@dataclass(slots=True)
class JobState:
    job_id: str
    status: str = "running"  # running | completed | failed
    run_id: str | None = None
    phase: str | None = None
    phase_state: str | None = None
    phase_detail: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "run_id": self.run_id,
            "phase": self.phase,
            "phase_state": self.phase_state,
            "phase_detail": self.phase_detail,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class ConcurrencyLimitError(RuntimeError):
    pass


class JobManager:
    """Process-wide, in-memory job tracker -- one instance per API process.

    Bounded by `Settings.dq_max_concurrent_runs`, a knob already declared in config.py
    for exactly this purpose ("API (Phase 3; unused in Phase 1)") and left unused until
    now. Each run is tagged with a unique `triggered_by` marker so its real `run_id` (only
    known once `Orchestrator.run()` creates the row) can be looked up unambiguously even
    if more than one run is in flight at once -- never by assuming "the newest row in the
    store", which would be wrong under concurrency.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._lock = threading.Lock()
        self._jobs: dict[str, JobState] = {}
        self._active = 0

    def list_jobs(self) -> list[JobState]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start_run(
        self,
        *,
        tables: list[str] | None,
        triggered_by: str,
        enrich: bool,
        generate_reports: bool,
    ) -> JobState:
        with self._lock:
            if self._active >= self._settings.dq_max_concurrent_runs:
                raise ConcurrencyLimitError(
                    f"{self._active} run(s) already in progress "
                    f"(limit DQ_MAX_CONCURRENT_RUNS={self._settings.dq_max_concurrent_runs})"
                )
            self._active += 1

        job = JobState(job_id=f"job_{uuid.uuid4().hex[:12]}")
        with self._lock:
            self._jobs[job.job_id] = job

        # A human-readable label (who/what triggered this) PLUS a unique suffix -- the
        # suffix is what makes the run_id lookup below correct even if two runs with the
        # same label are in flight at once; the label itself still ends up in the run's
        # own `triggered_by` column for display, unmodified apart from that suffix.
        marker = f"{triggered_by}#{job.job_id[-8:]}"
        thread = threading.Thread(
            target=self._run,
            args=(job, tables, marker, enrich, generate_reports),
            daemon=True,
            name=f"dq-run-{job.job_id}",
        )
        thread.start()
        return job

    def _run(
        self,
        job: JobState,
        tables: list[str] | None,
        marker: str,
        enrich: bool,
        generate_reports: bool,
    ) -> None:
        lookup_store = FindingsStore()

        def _progress(phase: str, state: str, data: dict[str, Any]) -> None:
            job.phase, job.phase_state, job.phase_detail = phase, state, data
            job.events.append({"phase": phase, "state": state, "data": data, "ts": time.time()})
            if job.run_id is None:
                # By the time the first phase event fires, Orchestrator.run() has already
                # called store.create_run() -- find that row by the unique marker we
                # tagged it with, not "the most recent run", which would race under
                # concurrent jobs.
                for row in lookup_store.list_runs(limit=20):
                    if row.get("triggered_by") == marker:
                        job.run_id = row["run_id"]
                        break

        # `job.run_id` is deliberately NOT tracked as a local here -- `_progress` above
        # mutates it directly, live, throughout the run (that's what lets a poller show a
        # run_id before the run finishes), so re-assigning it from a value captured before
        # the run started would stomp that with a stale None on any exception path.
        # `status`/`error` have no such live mutator, so they're safe to compute locally
        # and publish onto `job` only once, at the very end.
        status, error = "failed", None
        try:
            orch = Orchestrator(progress=_progress)
            try:
                outcome = orch.run(
                    tables=tables,
                    triggered_by=marker,
                    dry_run=False,
                    enrich=enrich,
                    generate_reports=generate_reports,
                )
            finally:
                orch.close()
            job.run_id = job.run_id or outcome.run_id
            status = "completed" if outcome.status.value == "completed" else "failed"
            error = outcome.error
        except Exception as exc:  # noqa: BLE001 -- surfaced to the API caller, never swallowed
            log.error("api.run_failed", job_id=job.job_id, error=str(exc)[:500])
            error = str(exc)[:1000]
        finally:
            lookup_store.close()
            # Free the concurrency slot BEFORE publishing the final status: otherwise an
            # observer that polls `job.status` and sees it leave "running" could call
            # `start_run()` a moment before `_active` is actually decremented here, and
            # get a spurious `ConcurrencyLimitError` even though the run genuinely finished.
            with self._lock:
                self._active -= 1
            job.error = error
            job.finished_at = time.time()
            job.status = status  # set LAST: this is what callers poll to detect "done"


_MANAGER: JobManager | None = None


def get_job_manager() -> JobManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = JobManager()
    return _MANAGER
