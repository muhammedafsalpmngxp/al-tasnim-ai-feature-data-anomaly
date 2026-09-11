"""Stream a probe's findings to disk instead of holding them in memory.

WHY A SPOOL FILE AT ALL
-----------------------
A detection run has two consumers of the same rows and they want opposite things:

  Word  wants the worst few thousand, formatted, in memory - a document is read by a person.
  Excel wants EVERY row, up to a million - a spreadsheet is filtered by a person.

Accumulating the Excel set in memory so Word can take a slice of it is what makes a
data-quality tool fall over on exactly the database that needed it most: the run holds every
finding of every rule at once, and a single rule with two hundred thousand orphan rows is
enough to end it.

So the runner writes each rule's rows straight through to its own file as they are fetched,
keeps only the Word cap in memory, and the Excel builder streams that file back out. Peak
memory is then one fetch batch per concurrent probe, whatever the rules find.

ONE FILE PER RULE, NOT ONE SHARED FILE. Probes run concurrently, and a shared handle would
need a lock that serialises exactly the part that is slow. Per-rule files need no coordination
at all, and a rule that fails mid-stream damages only its own.

The format is JSON Lines: one array per row. It round-trips numbers as numbers (so Excel gets
a right-aligned figure rather than text that looks like one), it needs no schema header, and a
truncated final line from an interrupted run is skipped on read rather than corrupting the
file.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import shutil
import uuid
from typing import Any, Iterator

from app.observability import get_logger

log = get_logger()

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
_RUNS_DIR = os.path.join(_CACHE_DIR, "runs")


def jsonable(value: Any) -> Any:
    """One place decides how a database value becomes a spooled value.

    int/float/bool/None survive as themselves so Excel receives real numbers. Everything else
    becomes a string HERE rather than at write time, so the Word table and the Excel sheet can
    never disagree about how a date or a decimal is rendered.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        # float() would silently lose precision on a money-scaled decimal; str keeps what the
        # database actually stored.
        return str(value)
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def new_run_id() -> str:
    """A sortable, unique id: the local timestamp plus a short random suffix.

    Sortable because runs are listed newest-first and a lexical sort must match chronology.
    Suffixed because two runs started in the same second must not share a directory.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def run_dir(run_id: str) -> str:
    path = os.path.join(_RUNS_DIR, run_id)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(rule_id: str) -> str:
    """A rule id as a filename. Rule ids are author-supplied, so they are not trusted here."""
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in rule_id]
    return "".join(keep)[:80] or "rule"


class Spool:
    """Append-only writer for one rule's findings."""

    def __init__(self, run_id: str, rule_id: str, columns: list[str]) -> None:
        self.path = os.path.join(run_dir(run_id), f"{_safe_name(rule_id)}.jsonl")
        self.columns = columns
        self.rows_written = 0
        self._fh = open(self.path, "w", encoding="utf-8")

    def write(self, row: list[Any]) -> None:
        self._fh.write(json.dumps([jsonable(v) for v in row], ensure_ascii=False))
        self._fh.write("\n")
        self.rows_written += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001 - closing a spool must never fail a run
            pass

    def __enter__(self) -> "Spool":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def read_rows(path: str) -> Iterator[list[Any]]:
    """Stream a spool file back, one row at a time.

    A malformed final line is skipped rather than raised: it means the run was interrupted
    mid-write, and every complete row before it is still perfectly good evidence.
    """
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                log.warning("spool: skipping an incomplete row in %s", os.path.basename(path))
                return


def cleanup(run_id: str) -> None:
    """Delete one run's spool directory.

    Called after the reports are written: the spool is an intermediate, and the .docx and .xlsx
    are the artefacts worth keeping. Failure is logged, never raised - a leftover temp
    directory is untidy, while a run that reports failure after writing both reports is a lie.
    """
    path = os.path.join(_RUNS_DIR, run_id)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("spool: could not remove %s (%s)", path, exc)
