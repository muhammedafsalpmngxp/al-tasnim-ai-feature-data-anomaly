"""Observability - file logging plus an optional colour console view of the pipeline.

Every compile and every detection run writes its route, generated SQL, retries, DB errors, row
counts and latency to logs/anomaly.log. `setup_logging(console=True)` additionally renders those
same records to the terminal with rich, one colour-coded line per agent, so the orchestration is
watchable while it runs.

The agent is inferred from the message text, deliberately: it keeps all of this in this module
and means no node, prompt or graph code has to change to stay traceable.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import settings

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "anomaly.log")
_configured = False
_console_attached = False


def _max_line_chars() -> int:
    """Console line cap. 0 (default) = never truncate, so the full SQL and the full verifier
    feedback are always visible. Set LOG_CONSOLE_MAX_CHARS to shorten them again."""
    try:
        return max(0, int(os.getenv("LOG_CONSOLE_MAX_CHARS", "0").strip() or 0))
    except (TypeError, ValueError):
        return 0


# (match, agent label, colour). First match wins, so order matters: put the most specific
# prefixes above the generic ones.
_AGENT_STYLES: list[tuple[str, str, str]] = [
    ("RULE:", "Rule", "bold white"),
    ("llm:", "LLM", "dim"),
    ("load:", "Rule Loader", "white"),
    ("generic:", "Generic Probes", "blue"),
    ("ground:", "Grounding", "cyan"),
    ("SQL[", "SQL Author", "yellow"),
    ("validation ok", "Validator", "green"),
    ("validation rejected", "Validator", "red"),
    ("contract ok", "Contract", "green"),
    ("contract failed", "Contract", "red"),
    ("exec ok", "Executor", "green"),
    ("exec error", "Executor", "red"),
    ("sanity:", "Sanity Gate", "magenta"),
    ("verify: ok", "Verifier", "green"),
    ("verify: rejected", "Verifier", "red"),
    ("verify:", "Verifier", "yellow"),
    ("catalog:", "Catalog", "blue"),
    ("probe:", "Probe Runner", "cyan"),
    ("score:", "Scorer", "magenta"),
    ("summary:", "Summarizer", "cyan"),
    ("report:", "Report", "bold green"),
    ("schema:", "Schema", "dim cyan"),
]


def _style_for(message: str) -> tuple[str, str]:
    for match, label, colour in _AGENT_STYLES:
        if message.startswith(match) or match in message[:24]:
            return label, colour
    return "", ""


class _RichConsoleHandler(logging.Handler):
    """Renders pipeline records as one colour-coded line each.

    Deliberately best-effort: a failure to PRINT a log line must never fail the run that
    produced it, so every path here swallows its own errors.
    """

    def __init__(self) -> None:
        super().__init__()
        from rich.console import Console

        self._console = Console(stderr=False, soft_wrap=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            label, colour = _style_for(message)
            cap = _max_line_chars()
            if cap and len(message) > cap:
                message = message[:cap] + " ..."
            if record.levelno >= logging.WARNING and not colour:
                colour = "red" if record.levelno >= logging.ERROR else "yellow"
            prefix = f"[{colour or 'white'}]{label or record.levelname.lower():<14}[/]" if label or colour else ""
            self._console.print(f"{prefix} {message}", highlight=False, markup=True)
        except Exception:  # noqa: BLE001 - printing a log line must never break a run
            pass


def setup_logging(console: bool | None = None) -> logging.Logger:
    """Configure the root application logger once. Safe to call repeatedly.

    `console` defaults to LOG_CONSOLE in .env. The file handler is always attached: a run that
    produced no terminal output must still be reconstructable afterwards from logs/anomaly.log.
    """
    global _configured, _console_attached

    logger = logging.getLogger("anomaly")
    if not _configured:
        logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        logger.propagate = False
        os.makedirs(_LOG_DIR, exist_ok=True)
        # Rotating, because a compile logs every generated query in full: an unbounded file
        # would grow without limit on a scheduled deployment.
        handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        _configured = True

    want_console = (
        console
        if console is not None
        else os.getenv("LOG_CONSOLE", "true").strip().lower() in ("1", "true", "yes", "on")
    )
    if want_console and not _console_attached:
        try:
            logger.addHandler(_RichConsoleHandler())
            _console_attached = True
        except Exception:  # noqa: BLE001 - rich missing or a non-tty must not break logging
            pass
    elif console is False and _console_attached:
        # An EXPLICIT console=False must be able to take the handler back off again, not merely
        # decline to add one. Any module reaching get_logger() during import attaches it first
        # (get_logger falls back to the LOG_CONSOLE default), so by the time a script asks for
        # a quiet logger the handler is already there - and a request for quiet output that
        # silently does nothing is worse than no option at all. This is what lets
        # eval/run_eval.py emit a clean pass/fail report that CI can read.
        for handler in [h for h in logger.handlers if isinstance(h, _RichConsoleHandler)]:
            logger.removeHandler(handler)
        _console_attached = False

    return logger


def get_logger() -> logging.Logger:
    """The application logger, configuring it on first use.

    Every module calls this rather than logging.getLogger() directly, so a script that forgot to
    call setup_logging() still gets a working, file-backed logger instead of silence.
    """
    if not _configured:
        setup_logging()
    return logging.getLogger("anomaly")
