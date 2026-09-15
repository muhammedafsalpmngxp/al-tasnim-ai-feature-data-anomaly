"""HTTP API entry point.  python serve.py

Serves the API and, when frontend/dist exists, the built UI on the same port - so a production
deployment is one process on one origin, with no CORS and no Node on the server.
"""
import sys

import uvicorn

from app.config import settings

# WHY MORE THAN ONE WORKER IS REFUSED RATHER THAN ALLOWED.
#
# app/api/main.py serialises compiles and runs with a threading.Lock, which is a lock within
# ONE process. uvicorn workers are separate processes, so a second worker does not see it: two
# compiles can then run at once, both rewrite .cache/anomaly_catalog.json, and the catalog that
# survives is whichever finished last - a silently half-compiled catalog that still looks
# healthy. The engine would then report on checks that were never really compiled, which is the
# one failure a data-quality tool must not have.
#
# The workload does not want more workers anyway. This API is one operator clicking Compile or
# Run; the work is a long database-and-model job on a background thread, not concurrent request
# handling. Scaling it means a bigger database, not more copies of this process.
#
# Refused loudly, at startup. Silently forcing it back to 1 would leave an operator believing
# they were running four workers, and the setting would look like it worked.
_MULTI_WORKER_REFUSAL = (
    f"API_WORKERS={settings.api_workers} is not supported.\n\n"
    "Compiles and runs are serialised by a lock held inside a single process. Separate "
    "uvicorn workers do not share it, so two compiles could write .cache/anomaly_catalog.json "
    "at the same time and leave a half-compiled catalog that still reports as healthy.\n\n"
    "Set API_WORKERS=1 in .env. This service is one operator running long background jobs, "
    "not a high-concurrency request handler - extra workers buy nothing."
)

if __name__ == "__main__":
    if settings.api_workers > 1:
        print(_MULTI_WORKER_REFUSAL, file=sys.stderr)
        raise SystemExit(2)

    uvicorn.run(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        workers=1,
    )
