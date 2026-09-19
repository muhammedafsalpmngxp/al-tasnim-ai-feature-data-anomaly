"""HTTP API for the anomaly engine.

WHAT THE API IS AND IS NOT
--------------------------
It is a thin shell over app/compiler.py and app/runner.py. It holds no business logic, makes
no decisions the CLI does not also make, and stores nothing of its own - so the UI and the
command line can never disagree about what the engine did.

LONG OPERATIONS ARE STREAMED, NOT AWAITED. A compile can take twenty minutes and a run several
minutes against a large database. An endpoint that blocks for that long is indistinguishable
from one that has hung, so both stream Server-Sent Events as the work progresses.

ONE AT A TIME. Compiling and running both hammer the same source database, and two concurrent
runs would double the load for no benefit while producing two half-speed reports. A lock
refuses the second with a clear message instead of quietly queueing it.

THE WORK RUNS IN A THREAD, NOT THE EVENT LOOP. pyodbc and the LLM client are both blocking;
running them inline would freeze every other request, including /api/health.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import queue
import threading
import time
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from app.config import settings
from app.observability import get_logger, setup_logging

log = get_logger()

# Only one heavy operation at a time. Not a queue: a caller told "busy, try again" can decide
# what to do, while a caller silently queued behind a twenty-minute compile cannot.
_BUSY = threading.Lock()

# WHAT THE RUNNING JOB IS DOING, READABLE BY SOMEBODY WHO IS NOT HOLDING THE STREAM.
#
# The SSE stream reaches the caller who STARTED the job. That is one browser tab, and a compile
# runs for half an hour - long enough that the tab gets reloaded, closed, opened on another
# machine, or simply left while the operator goes to lunch. Every one of those loses the stream
# while the job keeps running, and the UI could then only say "another operation is in
# progress": no bar, no percentage, no clock, and indistinguishable from a hang.
#
# So the same progress events are recorded here as well, and /api/status hands the snapshot to
# anyone who asks. IN MEMORY ON PURPOSE - it describes a thread inside this process, so a
# restart that loses it has lost the job it described too, and a snapshot that outlived its
# work would be a lie on the dashboard.
_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] | None = None


def _job_started(name: str) -> None:
    global _JOB
    with _JOB_LOCK:
        _JOB = {
            "job": name,
            "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "_monotonic": time.monotonic(),
            "done": 0,
            "total": 0,
            "label": "",
        }


def _job_progress(name: str, data: dict) -> None:
    """Fold one progress event into the snapshot.

    A compile counts RULES and a run counts STAGES, so the numbers arrive under different keys.
    They are normalised to done/total/label HERE rather than in the client, so a second client
    cannot end up rendering the same job differently from the first.
    """
    with _JOB_LOCK:
        if _JOB is None or _JOB.get("job") != name:
            return
        if name == "compile":
            _JOB["done"] = int(data.get("done") or 0)
            _JOB["label"] = str(data.get("rule_id") or "")
        else:
            _JOB["done"] = int(data.get("step") or 0)
            _JOB["label"] = str(data.get("stage") or "")
        _JOB["total"] = int(data.get("total") or 0)


def _job_finished() -> None:
    global _JOB
    with _JOB_LOCK:
        _JOB = None


def _job_snapshot() -> dict | None:
    """The running job with a SERVER-COMPUTED elapsed time, or None when nothing is running.

    Elapsed seconds are sent rather than a start time alone because the client's clock is not
    this machine's: a browser a few minutes out of step would otherwise show a compile that
    started in the future, or one that has apparently been running for hours.
    """
    with _JOB_LOCK:
        if _JOB is None:
            return None
        snapshot = {k: v for k, v in _JOB.items() if not k.startswith("_")}
        snapshot["elapsed"] = round(time.monotonic() - _JOB["_monotonic"], 1)
        return snapshot

app = FastAPI(
    title="Data Quality & Anomaly Sentinel",
    version="1.0.0",
    description="Agentic anomaly detection over a Microsoft SQL Server database.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    setup_logging(console=True)
    log.info("api: listening on %s:%s", settings.api_host, settings.api_port)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Blank API_KEY disables auth entirely, which is the local-development default."""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ── Read-only endpoints ─────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    """Liveness plus database reachability. Deliberately unauthenticated, for monitoring."""
    from app.db.connection import ping

    ok, message = ping()
    return {"ok": ok, "database": message, "model": settings.active_model}


def _rules_by_id() -> dict:
    """Every rule the loader can see, by id. Never raises - a status call must always answer."""
    try:
        from app.rules.expand import expand_families
        from app.rules.loader import load_rules

        rules, _ = load_rules()
        rules, _ = expand_families(rules)
        return {r.rule_id: r for r in rules}
    except Exception:  # noqa: BLE001 - a broken rule file must not blank the whole dashboard
        return {}


def _probe_counts(catalog, rules_by_id: dict) -> tuple[int, int]:
    """(trusted, on trial) - probes that will actually RUN, split by whether they are scored.

    COUNTED FROM THE RULE, NOT THE PROBE. `catalog.active` means "this SQL built", which is
    true of three different things a header must not add together:

      a trusted rule    runs, and its findings are in the score          -> trusted
      a rule on trial   runs, and its findings are in no headline figure -> on trial
      a REFUSED rule    does not run at all, but its probe is still on
                        file until the next compile prunes it           -> neither

    That last case is why this is not simply a subtraction. Refusing an accepted rule left its
    probe in the catalog, so the header went UP by one - advertising a check that had just been
    switched off as an established one.
    """
    trusted = on_trial = 0
    for probe in catalog.active:
        rule = rules_by_id.get(probe.rule_id)
        # No rule at all: an orphaned probe the next compile will prune. Counted as trusted
        # because that is what it was, and under-reporting coverage is the worse error.
        if rule is None:
            trusted += 1
        elif not rule.runnable:
            continue
        elif rule.scored:
            trusted += 1
        else:
            on_trial += 1
    return trusted, on_trial


@app.get("/api/status", dependencies=[Depends(require_api_key)])
def status() -> dict:
    """Everything a dashboard needs to render before any run: what exists and what is current."""
    from app.rules import catalog as catalog_store
    from app.runner import history, history_for_current_database

    catalog = catalog_store.load()
    runs = history()
    # Scoped to this database, exactly as /api/runs/latest is. The two describe the same thing
    # and a dashboard that read one for its headline and the other for its status line would
    # contradict itself the moment DB_NAME moved.
    mine, other_database_runs = history_for_current_database()
    _trusted, _on_trial_count = _probe_counts(catalog, _rules_by_id())
    return {
        "database": {"name": settings.db_name, "server": settings.db_server},
        "schemas": list(settings.allowed_schemas),
        "models": {"main": settings.active_model, "fast": settings.fast_model},
        "catalog": {
            "compiled_at": catalog.compiled_at,
            "total": len(catalog.probes),
            # SPLIT BY TRUST, not just by whether the SQL compiled - see _probe_counts. The
            # count beside the score must describe the same set of checks the score came from.
            "active": _trusted,
            "on_trial": _on_trial_count,
            "failed": len(catalog.failed),
            "not_applicable": len(catalog.not_applicable),
        },
        "runs": len(runs),
        "runs_here": len(mine),
        "runs_elsewhere": len(other_database_runs),
        "latest_run": mine[0].to_json() if mine else None,
        "busy": _BUSY.locked(),
        "job": _job_snapshot(),
    }


@app.get("/api/rules", dependencies=[Depends(require_api_key)])
def rules(source: str | None = Query(default=None)) -> dict:
    """Every rule, with its compiled state. The control surface, as the engine sees it."""
    from app.rules import catalog as catalog_store
    from app.rules.expand import expand_families
    from app.rules.loader import load_rules

    declared, errors = load_rules()
    declared, notes = expand_families(declared)
    errors += notes
    all_rules = list(declared)

    catalog = catalog_store.load()
    items = []
    for rule in all_rules:
        if source and rule.source != source:
            continue
        # A REFUSED PROPOSAL IS NOT A RULE OF THIS SYSTEM. It is kept on file only so the Scout
        # cannot raise the same idea again, and it is already listed - with the reason it was
        # refused - under Refused on the Discover screen. Showing it here too would grow the
        # rule list with things nobody wants checked, and a reader scanning for what the engine
        # actually does would have to filter them out by eye on every visit.
        if rule.status == "rejected":
            continue
        probe = catalog.get(rule.rule_id)
        items.append({
            "rule_id": rule.rule_id,
            "title": rule.title,
            "category": rule.category,
            "severity": rule.severity,
            "entity": rule.entity,
            "method": rule.method,
            "sql_mode": rule.sql_mode,
            "source": rule.source,
            "status": rule.status,
            "compiled_status": probe.status if probe else "not compiled",
            "compiled_at": probe.compiled_at if probe else "",
            "tables": list(probe.tables) if probe else [],
            "error": probe.error if probe else "",
            "llm_calls": probe.llm_calls if probe else 0,
        })
    items.sort(key=lambda i: i["rule_id"])
    return {"rules": items, "errors": errors, "total": len(items)}


@app.get("/api/rules/{rule_id}", dependencies=[Depends(require_api_key)])
def rule_detail(rule_id: str) -> dict:
    """One rule's prose AND the exact SQL that will run for it.

    Exposed on purpose. The probes decide what the report says, so being able to read precisely
    what executes - without a database or a model - is what makes a finding auditable rather
    than something the reader has to take on trust.
    """
    from app.rules import catalog as catalog_store
    from app.rules.expand import expand_families
    from app.rules.loader import load_rules

    declared, _ = load_rules()
    declared, _notes = expand_families(declared)
    found = next((r for r in declared if r.rule_id == rule_id), None)
    probe = catalog_store.load().get(rule_id)
    if found is None and probe is None:
        raise HTTPException(status_code=404, detail=f"No rule {rule_id!r}")

    return {
        "rule_id": rule_id,
        "title": getattr(found, "title", ""),
        "category": getattr(found, "category", ""),
        "severity": getattr(found, "severity", ""),
        "method": getattr(found, "method", ""),
        "sql_mode": getattr(found, "sql_mode", ""),
        "source": getattr(found, "source", ""),
        "body": getattr(found, "body", ""),
        "compiled": probe.to_json() if probe else None,
    }


@app.get("/api/runs", dependencies=[Depends(require_api_key)])
def runs(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    from app.runner import history

    return {"runs": [r.to_json() for r in history()[:limit]]}


@app.get("/api/runs/latest", dependencies=[Depends(require_api_key)])
def latest_run() -> dict:
    """The newest run MADE AGAINST THE DATABASE THIS PROCESS IS POINTED AT.

    Not simply the newest run. Change DB_NAME and the previous database's last run is still the
    newest row in the index - it would be served as the dashboard's headline score, its findings
    and its summary, describing a database nobody is looking at any more, with nothing on screen
    to say so. A dashboard that confidently reports the wrong system is worse than an empty one,
    so an unmatched history is answered with "nothing here yet" and the reason.
    """
    from app.runner import current_database, history_for_current_database, load_results

    mine, others = history_for_current_database()
    if not mine and not others:
        raise HTTPException(status_code=404, detail="No run has been recorded yet")
    if not mine:
        database = current_database()
        raise HTTPException(status_code=404, detail=(
            f"No run has been recorded against {database.get('name') or 'this database'} yet. "
            f"The history holds {len(others)} run(s) from another database or from before "
            "runs recorded which database they measured."
        ))
    results = load_results(mine[0].run_id)
    if results is None:
        raise HTTPException(status_code=404, detail="The latest run has no stored results")
    results.setdefault("database", mine[0].database)
    return results


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_api_key)])
def run_detail(run_id: str) -> dict:
    """One run by id - INCLUDING one from another database, deliberately.

    Old runs stay readable: they are a record of what was true, and deleting the past because
    the connection string moved would destroy the only evidence of it. What they must not do is
    masquerade as current, so the database each one measured travels with it and the UI says so.
    """
    from app.runner import history, load_results

    results = load_results(run_id)
    if results is None:
        raise HTTPException(status_code=404, detail=f"No stored results for run {run_id!r}")
    if not results.get("database"):
        # Written before the results file carried it; the index row may still know.
        row = next((r for r in history() if r.run_id == run_id), None)
        results["database"] = row.database if row else {}
    return results


@app.get("/api/runs/{run_id}/report/{fmt}", dependencies=[Depends(require_api_key)])
def download_report(run_id: str, fmt: str):
    """Serve a generated report file.

    The path comes from the run's OWN record, never from the URL, and is confirmed to sit
    inside the reports directory before it is served. A caller must not be able to turn a
    download endpoint into a way to read arbitrary files off the server.
    """
    from app.report.format import reports_dir
    from app.runner import history

    if fmt not in ("xlsx", "docx"):
        raise HTTPException(status_code=400, detail="Format must be xlsx or docx")
    run = next((r for r in history() if r.run_id == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id!r}")
    path = (run.report_paths or {}).get(fmt, "")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"That run has no {fmt} report")
    root = os.path.realpath(reports_dir())
    if os.path.commonpath([os.path.realpath(path), root]) != root:
        raise HTTPException(status_code=400, detail="Refusing to serve a file outside reports/")

    media = {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[fmt]
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


# ── Streaming operations ────────────────────────────────────────────────────────

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream_job(work, name: str):
    """Run a blocking job in a thread, relaying its progress events as SSE.

    A queue bridges the two worlds: the worker thread pushes events, the event loop drains
    them. `None` is the sentinel meaning the worker has finished, so the loop never has to
    poll a thread to find out whether it is still alive.
    """
    if not _BUSY.acquire(blocking=False):
        async def busy() -> Iterator[str]:
            yield _sse("error", {"message": (
                "Another compile or run is already in progress. These operations both load the "
                "source database heavily, so only one runs at a time."
            )})
        return StreamingResponse(busy(), media_type="text/event-stream")

    events: queue.Queue = queue.Queue()
    _job_started(name)

    def emit(event: str, data: Any) -> None:
        # Recorded BEFORE it is queued. The queue is drained by whoever holds the stream, and
        # if nobody does - a closed tab - it is never drained at all; the snapshot has to be
        # updated on the producing side or it would freeze exactly when it matters most.
        if event == "progress" and isinstance(data, dict):
            _job_progress(name, data)
        events.put((event, data))

    def worker() -> None:
        from app.rules.lockfile import CompileLockError

        try:
            work(emit)
        except CompileLockError as exc:
            # Another PROCESS holds the catalog lock - a CLI compile, typically, which _BUSY
            # above cannot see. Nothing ran and nothing is broken, so it is reported in the
            # lock's own words rather than as "CompileLockError: ...", which reads to an
            # operator like a defect in the engine.
            log.info("api: %s refused - %s", name, exc)
            events.put(("error", {"message": str(exc), "busy": True}))
        except Exception as exc:  # noqa: BLE001 - the client must be told, not left hanging
            log.warning("api: %s failed - %s: %s", name, type(exc).__name__, exc)
            events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            # Ordered: the snapshot is cleared BEFORE the lock is released, so /api/status can
            # never answer "not busy" while still describing a running job.
            _job_finished()
            events.put(None)
            _BUSY.release()

    threading.Thread(target=worker, name=f"api-{name}", daemon=True).start()

    async def generate():
        yield _sse("started", {"job": name})
        while True:
            item = await asyncio.to_thread(events.get)
            if item is None:
                break
            event, data = item
            yield _sse(event, data)
        yield _sse("end", {"job": name})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # Without this an intermediate proxy may buffer the whole stream and deliver it at the
        # end, which is exactly the blocking behaviour streaming exists to avoid.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/compile", dependencies=[Depends(require_api_key)])
async def compile_endpoint(
    rule: list[str] | None = Query(default=None),
    force: bool = Query(default=False),
    retry_failed: bool = Query(default=False),
):
    """Compile the rules into probes, streaming progress. The expensive operation."""
    from app.compiler import compile_rules

    def work(emit) -> None:
        def progress(done: int, total: int, rule_id: str) -> None:
            emit("progress", {"done": done, "total": total, "rule_id": rule_id})

        _catalog, report, errors = compile_rules(
            only=rule, force=force, retry_failed=retry_failed, progress=progress
        )
        emit("result", {
            "compiled": report.compiled, "reused": report.reused, "failed": report.failed,
            "not_applicable": report.not_applicable, "removed": report.removed,
            "llm_calls": report.llm_calls, "seconds": round(report.seconds, 1),
            "stopped_early": report.stopped_early, "usage": report.usage, "errors": errors,
        })

    return await _stream_job(work, "compile")


@app.post("/api/run", dependencies=[Depends(require_api_key)])
async def run_endpoint(
    rule: list[str] | None = Query(default=None),
    format: list[str] | None = Query(default=None),
):
    """Execute the compiled probes and write the reports, streaming stage progress."""
    from app.runner import run_detection

    def work(emit) -> None:
        def progress(step: int, total: int, stage: str) -> None:
            emit("progress", {"step": step, "total": total, "stage": stage})

        final, summary = run_detection(only=rule, formats=format, on_progress=progress)
        emit("result", {
            "run_id": summary.run_id,
            "score": summary.score,
            "totals": summary.totals,
            "by_severity": summary.by_severity,
            "summary": final.get("summary", ""),
            "ranked": final.get("ranked") or [],
            "empty_scope": final.get("empty_scope") or [],
            "not_running": final.get("not_running") or [],
            "report_paths": summary.report_paths,
            "seconds": summary.seconds,
            "error": summary.error,
        })

    return await _stream_job(work, "run")


# ── Discovery: propose new anomalies, and record what the operator decides ──────
#
# THE PROPOSING IS A STREAMED JOB; THE DECIDING IS NOT. Running the Scout spends a model call
# and takes tens of seconds, so it streams like a compile or a run and shares the same lock -
# two at once would double the spend for one answer. Accept and Reject spend nothing and touch
# no database: they edit one file, so they are ordinary requests that return at once.
#
# NOTHING HERE REACHES THE CATALOG. A proposal becomes a rule only when a person accepts it, and
# becomes SQL only when the ordinary compile writes it - through the same author, the same
# contract checks and the same reviewer as every hand-written rule.

@app.get("/api/discoveries", dependencies=[Depends(require_api_key)])
def discoveries() -> dict:
    """Everything the Discover screen renders: pending, accepted, rejected, and what was cut."""
    from app.discovery import state as discovery_state

    return discovery_state()


@app.post("/api/discover", dependencies=[Depends(require_api_key)])
async def discover_endpoint(max_proposals: int = Query(default=8, ge=1, le=20)):
    """Run the Scout over this database and write its proposals, streaming stage progress."""
    from app.discovery import discover

    def work(emit) -> None:
        def progress(step: int, total: int, stage: str) -> None:
            emit("progress", {"step": step, "total": total, "stage": stage})

        emit("result", discover(max_proposals=max_proposals, progress=progress))

    return await _stream_job(work, "discover")


@app.post("/api/discoveries/{proposal_hash}/accept", dependencies=[Depends(require_api_key)])
def accept_discovery(proposal_hash: str, status: str = Query(default="probation")) -> dict:
    """Admit one proposal as a rule, on trial by default.

    `probation` runs it while counting it towards nothing on the dashboard, so it can be judged
    on what it actually finds rather than on how it reads. Compile afterwards to give it SQL.
    """
    from app.discovery import accept

    if status not in ("probation", "active"):
        raise HTTPException(status_code=400, detail="status must be probation or active")
    decided = accept(proposal_hash, status=status)
    if not decided:
        raise HTTPException(status_code=404, detail="That proposal is no longer pending")
    return decided


@app.post("/api/discoveries/{proposal_hash}/reject", dependencies=[Depends(require_api_key)])
def reject_discovery(proposal_hash: str, reason: str = Query(default="")) -> dict:
    """Refuse one proposal permanently, so the Scout cannot raise it again."""
    from app.discovery import reject

    decided = reject(proposal_hash, reason=reason)
    if not decided:
        raise HTTPException(status_code=404, detail="That proposal is no longer pending")
    return decided


@app.post("/api/discovered/{rule_id}/status", dependencies=[Depends(require_api_key)])
def set_discovered_status(
    rule_id: str,
    status: str = Query(...),
    reason: str = Query(default=""),
) -> dict:
    """Promote, refuse after accepting, or restore. All three are one status change.

    Nothing is deleted: what was decided, and when, is the point of the file. The existing
    engine does the rest - a rule that stops being runnable stops running on the next detection
    run, and its probe is pruned from the catalog on the next compile.
    """
    from app.discovery import set_status

    try:
        changed = set_status(rule_id, status, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail=f"No discovered rule {rule_id!r}")
    return {"rule_id": rule_id, "status": status}


# ── The built UI ────────────────────────────────────────────────────────────────
#
# Serving the frontend from the API is what makes a production deployment ONE process on ONE
# origin. It is not a convenience:
#
#   * CORS stops existing. In development Vite proxies /api, so the browser sees one origin
#     and the question never arises. Split across two servers in production it does arise, and
#     the usual answer - CORS_ORIGINS=* - hands every site on the internet a logged-in caller's
#     browser as a proxy to this API.
#   * Node is not needed on the server. `npm run build` runs on a build machine; what ships is
#     a directory of static files.
#   * The API key never has to reach a separate web server's configuration.
#
# Mounted LAST, on purpose. Every /api route above is already registered, so this catch-all
# cannot shadow one. If the directory is absent - a backend-only or CLI-only deployment, or a
# checkout where the UI was never built - nothing is mounted and the API serves as before.
_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "..", "frontend", "dist")
_UI_DIR = os.path.normpath(_UI_DIR)

if os.path.isdir(_UI_DIR) and os.path.isfile(os.path.join(_UI_DIR, "index.html")):
    from fastapi.staticfiles import StaticFiles

    # html=True serves index.html at "/" - which is the whole UI. Its tabs are React state,
    # not URL routes, so there are no deep links to fall back for.
    #
    # If the UI is ever given real routes (react-router, or pushState on the tabs), THIS MOUNT
    # MUST CHANGE: Starlette answers an unknown path with 404 and does not fall back to
    # index.html, so /runs would 404 on reload while working perfectly when clicked. Handle it
    # then with an explicit catch-all returning index.html, registered after every /api route.
    app.mount("/", StaticFiles(directory=_UI_DIR, html=True), name="ui")
    log.info("api: serving the built UI from %s", _UI_DIR)
else:
    log.info(
        "api: no built UI at %s - serving the API only. Build it with: "
        "cd frontend && npm install && npm run build", _UI_DIR,
    )
