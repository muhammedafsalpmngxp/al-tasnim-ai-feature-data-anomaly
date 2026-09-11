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
import json
import os
import queue
import threading
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


@app.get("/api/status", dependencies=[Depends(require_api_key)])
def status() -> dict:
    """Everything a dashboard needs to render before any run: what exists and what is current."""
    from app.rules import catalog as catalog_store
    from app.runner import history

    catalog = catalog_store.load()
    runs = history()
    return {
        "database": {"name": settings.db_name, "server": settings.db_server},
        "schemas": list(settings.allowed_schemas),
        "models": {"main": settings.active_model, "fast": settings.fast_model},
        "catalog": {
            "compiled_at": catalog.compiled_at,
            "total": len(catalog.probes),
            "active": len(catalog.active),
            "failed": len(catalog.failed),
            "not_applicable": len(catalog.not_applicable),
        },
        "runs": len(runs),
        "latest_run": runs[0].to_json() if runs else None,
        "busy": _BUSY.locked(),
    }


@app.get("/api/rules", dependencies=[Depends(require_api_key)])
def rules(source: str | None = Query(default=None)) -> dict:
    """Every rule, with its compiled state. The control surface, as the engine sees it."""
    from app.rules import catalog as catalog_store
    from app.rules.generic import generate as generate_generic
    from app.rules.loader import load_rules

    declared, errors = load_rules()
    all_rules = list(declared)
    if settings.generic_probes:
        try:
            known = {r.rule_id for r in all_rules}
            all_rules += [r for r in generate_generic() if r.rule_id not in known]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"generic probes unavailable: {exc}")

    catalog = catalog_store.load()
    items = []
    for rule in all_rules:
        if source and rule.source != source:
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
    from app.rules.generic import generate as generate_generic
    from app.rules.loader import load_rules

    declared, _ = load_rules()
    found = next((r for r in declared if r.rule_id == rule_id), None)
    if found is None and settings.generic_probes:
        try:
            found = next((r for r in generate_generic() if r.rule_id == rule_id), None)
        except Exception:  # noqa: BLE001
            found = None
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
    from app.runner import history, load_results

    recorded = history()
    if not recorded:
        raise HTTPException(status_code=404, detail="No run has been recorded yet")
    results = load_results(recorded[0].run_id)
    if results is None:
        raise HTTPException(status_code=404, detail="The latest run has no stored results")
    return results


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_api_key)])
def run_detail(run_id: str) -> dict:
    from app.runner import load_results

    results = load_results(run_id)
    if results is None:
        raise HTTPException(status_code=404, detail=f"No stored results for run {run_id!r}")
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

    def emit(event: str, data: Any) -> None:
        events.put((event, data))

    def worker() -> None:
        try:
            work(emit)
        except Exception as exc:  # noqa: BLE001 - the client must be told, not left hanging
            log.warning("api: %s failed - %s: %s", name, type(exc).__name__, exc)
            events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
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
