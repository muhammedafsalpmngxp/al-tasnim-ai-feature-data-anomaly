"""Structured logging. JSON in production (DQ_LOG_JSON=true), human-readable locally."""
from __future__ import annotations

import logging
import sys

import structlog

from app.config import get_settings

_configured = False


def configure(force: bool = False) -> None:
    global _configured  # noqa: PLW0603
    if _configured and not force:
        return
    s = get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if s.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str):  # noqa: ANN201
    configure()
    return structlog.get_logger(name)
