"""Structured logging.

Two renderers, one switch (`DQ_LOG_JSON`):
  * false (default, local) -- `rich` renders every record: a clock column, a coloured
    level, and rich's own highlighter colouring the numbers/ids/paths inside the
    key=value pairs, plus full-colour tracebacks on an exception.
  * true (production/CI) -- one JSON object per line, no colour, no boxes.

Logs go to **stderr**; the CLI's own tables and rules go to **stdout** (`app/cli.py`
holds that console). That split is what lets `python -m app.cli run > report.txt` keep
the report output in the file while the live progress log still streams to the terminal.
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.config import get_settings

_configured = False
_console = None


def log_console():
    """The single shared stderr `rich.Console` used by every log record.

    One instance, not one per call: two Consoles writing the same stream interleave
    mid-line when a log record lands while another is still rendering.
    """
    global _console  # noqa: PLW0603
    if _console is None:
        from rich.console import Console

        _console = Console(stderr=True)
    return _console


def configure(force: bool = False) -> None:
    global _configured  # noqa: PLW0603
    if _configured and not force:
        return
    s = get_settings()
    level = getattr(logging, s.log_level.upper(), logging.INFO)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if s.log_json:
        logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
        processors.append(structlog.stdlib.add_log_level)
        processors.append(structlog.processors.TimeStamper(fmt="iso", utc=True))
        processors.append(structlog.processors.JSONRenderer())
    else:
        from rich.logging import RichHandler

        logging.basicConfig(
            format="%(message)s",
            level=level,
            force=True,
            handlers=[
                RichHandler(
                    console=log_console(),
                    show_time=True,
                    show_level=True,
                    show_path=False,
                    rich_tracebacks=True,
                    markup=False,          # an event name containing [] must not be parsed as markup
                    log_time_format="%H:%M:%S",
                    omit_repeated_times=False,
                )
            ],
        )
        # RichHandler supplies the timestamp AND level columns, so structlog adds neither
        # (no TimeStamper, no add_log_level above) -- otherwise every line carries the
        # level twice. colors=False because structlog's ANSI escapes would print
        # literally through RichHandler, whose own highlighter already colours values.
        processors.append(structlog.dev.ConsoleRenderer(colors=False, pad_event_to=30))

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
