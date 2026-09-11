"""HTTP API entry point.  python serve.py"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        # More than one worker means more than one copy of the schema and the catalog in
        # memory. Raise deliberately.
        workers=settings.api_workers if not settings.api_reload else 1,
    )
