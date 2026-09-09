"""FastAPI app entrypoint.

Run with:  python -m app.api.main          (dev, auto-detects a free behaviour via uvicorn.run)
       or:  uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8001   (equivalent, standard)

Reads `API_HOST` / `API_PORT` / `API_CORS_ORIGINS` from the same `.env` / `Settings`
(`app/config.py`) the rest of the app already uses -- no separate config surface.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging import get_logger

from .routes import router

log = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Data Quality & Anomaly Sentinel API",
        description=(
            "Read-mostly API over the Sentinel's findings store, plus one action "
            "endpoint (POST /api/runs) that triggers a live scan + LLM enrichment + "
            "Excel/Word report generation."
        ),
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins or ["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )
