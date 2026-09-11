"""Drive a detection run, and keep a record of every run that has happened.

THE HISTORY IS THE POINT OF KEEPING AN INDEX. A single score is nearly meaningless - 82 out of
100 is neither good nor bad on its own. What a data owner actually needs is the direction of
travel: was it 74 last week? Did the number move because the data improved, or because a check
stopped running? So every run appends a small record, and the reports it produced stay on disk
beside it.

The index holds counts and paths only - never findings. It is read on every API call and must
stay small no matter how long the system has been in service.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from app.graph.build import build_run_graph
from app.llm import get_usage_report, start_usage_tracking
from app.observability import get_logger
from app.report import spool as spool_store

log = get_logger()

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
_INDEX_PATH = os.path.join(_CACHE_DIR, "runs.json")
# One file per run holding the findings SUMMARY - the ranked rules, the coverage gaps and the
# executive summary. Kept apart from the index because the index is read on every API call and
# must stay tiny, while this is read only when somebody opens that particular run.
#
# It holds the per-RULE aggregates, never the per-RECORD findings: those live in the Excel
# workbook, which is the right tool for them. That keeps this file small however bad the data.
_RESULTS_DIR = os.path.join(_CACHE_DIR, "run_results")

# How many runs the index keeps. Enough to show a trend over months of daily runs, bounded so
# the file stays small and cheap to read on every request.
MAX_HISTORY = 200


@dataclass
class RunSummary:
    """One row of the history. Small by design - no findings, ever."""

    run_id: str
    started_at: str
    seconds: float
    score: float
    totals: dict[str, Any] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    report_paths: dict[str, str] = field(default_factory=dict)
    llm_calls: int = 0
    error: str = ""

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id, "started_at": self.started_at, "seconds": self.seconds,
            "score": self.score, "totals": self.totals, "by_severity": self.by_severity,
            "report_paths": self.report_paths, "llm_calls": self.llm_calls,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RunSummary":
        return cls(
            run_id=data.get("run_id", ""),
            started_at=data.get("started_at", ""),
            seconds=float(data.get("seconds") or 0),
            score=float(data.get("score") or 0),
            totals=data.get("totals") or {},
            by_severity=data.get("by_severity") or {},
            report_paths=data.get("report_paths") or {},
            llm_calls=int(data.get("llm_calls") or 0),
            error=data.get("error", ""),
        )


def history() -> list[RunSummary]:
    """Every recorded run, newest first. Never raises - a broken index is not a broken run."""
    if not os.path.exists(_INDEX_PATH):
        return []
    try:
        with open(_INDEX_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("runs: the history index could not be read (%s)", exc)
        return []
    runs = [RunSummary.from_json(r) for r in (data.get("runs") or [])]
    return sorted(runs, key=lambda r: r.started_at, reverse=True)


def _record(summary: RunSummary) -> None:
    """Append to the history, atomically, keeping the most recent MAX_HISTORY runs."""
    runs = [r for r in history() if r.run_id != summary.run_id]
    runs.insert(0, summary)
    runs = runs[:MAX_HISTORY]
    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = _INDEX_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"runs": [r.to_json() for r in runs]}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _INDEX_PATH)
    except OSError as exc:
        # The reports are already written and the findings are already real. Failing the run
        # over its bookkeeping would discard work that succeeded.
        log.warning("runs: the history index could not be updated (%s)", exc)


def _save_results(run_id: str, final: dict, summary: RunSummary) -> None:
    """Persist one run's findings summary so a UI can show it without re-running anything."""
    os.makedirs(_RESULTS_DIR, exist_ok=True)
    payload = {
        "run_id": run_id,
        "started_at": summary.started_at,
        "seconds": summary.seconds,
        "score": summary.score,
        "score_basis": final.get("score_basis", ""),
        "summary": final.get("summary", ""),
        "totals": final.get("totals") or {},
        "by_severity": final.get("by_severity") or {},
        "by_category": final.get("by_category") or [],
        "ranked": final.get("ranked") or [],
        "empty_scope": final.get("empty_scope") or [],
        "not_running": final.get("not_running") or [],
        "report_paths": summary.report_paths,
        "catalog_stale": bool(final.get("catalog_stale")),
        "catalog_note": final.get("catalog_note", ""),
        "failed": [
            {"rule_id": r.rule_id, "error": r.error}
            for r in (final.get("results") or []) if not r.ok
        ],
        "error": summary.error,
    }
    try:
        with open(os.path.join(_RESULTS_DIR, f"{run_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    except OSError as exc:
        log.warning("runs: the result file could not be written (%s)", exc)


def load_results(run_id: str) -> dict | None:
    """One run's stored findings summary, or None when it is not on disk."""
    path = os.path.join(_RESULTS_DIR, f"{run_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("runs: %s could not be read (%s)", run_id, exc)
        return None


# The run graph's nodes, in the order they execute. Used only to label progress events, so a
# caller watching a long run is told which stage it is in rather than being left to guess.
RUN_STAGES: tuple[str, ...] = (
    "catalog_loader", "probe_runner", "scorer", "summarizer", "report_builder",
)


def run_detection(
    only: list[str] | None = None,
    formats: list[str] | None = None,
    on_progress=None,
) -> tuple[dict, RunSummary]:
    """Execute every active probe and write the reports. Returns (final state, history row).

    `on_progress(step, total, stage)` is called as each stage completes, for a CLI bar or an
    SSE stream. When it is given the graph is STREAMED rather than invoked: a detection run can
    take minutes against a large database, and a caller with no feedback cannot tell a slow
    scan from a hung one.
    """
    started = time.perf_counter()
    start_usage_tracking()
    run_id = spool_store.new_run_id()
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    log.info("probe: starting run %s", run_id)

    state = {
        "run_id": run_id,
        "only": list(only or []),
        "formats": list(formats or []),
        "results": [],
        "ranked": [],
        "not_running": [],
        "empty_scope": [],
        "llm_calls": 0,
    }

    error = ""
    try:
        graph = build_run_graph()
        if on_progress is None:
            final = graph.invoke(state)
        else:
            # stream_mode="values" yields the COMPLETE state after each node, so the last one
            # is the final state - no reassembling partial updates, which is where a streamed
            # run would otherwise quietly diverge from an invoked one.
            final = state
            step = 0
            for snapshot in graph.stream(state, stream_mode="values"):
                final = snapshot
                stage = RUN_STAGES[step] if step < len(RUN_STAGES) else ""
                on_progress(step, len(RUN_STAGES), stage)
                step += 1
            on_progress(len(RUN_STAGES), len(RUN_STAGES), "done")
    except Exception as exc:  # noqa: BLE001 - a run must always produce a record of itself
        log.warning("probe: the run failed - %s: %s", type(exc).__name__, exc)
        # The spool is deliberately NOT cleaned up here: whatever was gathered before the
        # failure is the only copy, and it is worth more than a tidy cache directory.
        error = f"{type(exc).__name__}: {exc}"
        final = dict(state)

    final["seconds"] = time.perf_counter() - started
    summary = RunSummary(
        run_id=run_id,
        started_at=started_at,
        seconds=round(final["seconds"], 1),
        score=float(final.get("score") or 0),
        totals=final.get("totals") or {},
        by_severity=final.get("by_severity") or {},
        report_paths=final.get("report_paths") or {},
        llm_calls=int(final.get("llm_calls") or 0),
        error=error or str(final.get("report_error") or ""),
    )
    _record(summary)
    _save_results(run_id, final, summary)

    final["usage"] = get_usage_report()
    log.info(
        "probe: run %s finished in %.1fs - score %.1f, %d LLM call(s)",
        run_id, final["seconds"], summary.score, summary.llm_calls,
    )
    return final, summary
