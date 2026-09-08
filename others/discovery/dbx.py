"""Shared DB access for the discovery/exploration scripts.

This is a THIN WRAPPER over the product's own connection, scope and config modules
(app.config, app.db.source, app.sentinel.scope) -- it duplicates nothing. Before this
rewrite, dbx.py had its own copy of the connection-string logic and its own copy of the
ALLOWED_SCHEMAS/EXCLUDED_TABLES parsing, reading the CHAT feature's env vars rather than
the Sentinel's DQ_* ones. That meant an exploration script and the product engine could
disagree about what was in scope. There is now exactly one implementation of each concern;
these scripts get it by importing the app, not by re-deriving it.

Practical effect: connect() returns a read-only-verified SourceDatabase from the product's
own pool (with the MAXDOP bound and the statement guard applied to every query), and
ALLOWED_SCHEMAS / is_table_excluded / is_column_excluded reflect DQ_ALLOWED_SCHEMAS /
DQ_EXCLUDED_TABLES / DQ_EXCLUDED_COLUMNS -- the Sentinel's own scope, not the chat
feature's. That is a deliberate change: the old chat-feature scope hid
dbo.activity_master_mapping and the wbs schema, which business rule Sec 3 requires
(docs/01-DISCOVERY-FINDINGS.md Sec 2).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# backend/scripts/discovery/dbx.py -> backend/  (so `import app.*` resolves without
# needing PYTHONPATH set manually -- matches how these scripts have always been run:
# `cd backend/scripts/discovery && python 01_inventory.py`)
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.source import SourceDatabase  # noqa: E402
from app.sentinel.scope import Scope  # noqa: E402

ROOT = _BACKEND_ROOT.parent
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

_settings = get_settings()
_scope = Scope(_settings)

# Preserved for scripts that inspect these directly (e.g. printing scope in a report).
# Sourced from DQ_ALLOWED_SCHEMAS / DQ_EXCLUDED_TABLES / DQ_EXCLUDED_COLUMNS -- the same
# values the orchestrator uses -- not a second, independently-maintained copy.
ALLOWED_SCHEMAS = sorted(_scope.allowed_schemas)
EXCLUDED_TABLES = sorted(_scope._excluded_tables)  # noqa: SLF001 (read-only introspection)
EXCLUDED_COLUMNS = sorted(_scope._excluded_columns)  # noqa: SLF001


def is_table_excluded(schema: str, table: str) -> bool:
    """True if the Sentinel's scope hides this table (wrong schema OR an exclusion match)."""
    return not _scope.table_allowed(schema, table)


def is_column_excluded(schema: str, table: str, column: str) -> bool:
    return not _scope.column_allowed(schema, table, column)


def connect(timeout: int | None = None) -> SourceDatabase:
    """Return a read-only-verified connection from the product's own pool.

    Every query run through it passes the same statement guard and MAXDOP bound the
    orchestrator's checks use -- an exploration script gets the same safety, for free.
    """
    src = SourceDatabase(_settings)
    info = src.assert_read_only_account()
    print(
        f"[ok] connected to {info.get('db_name')} as {info.get('login_name')} "
        "(read-only verified)",
        file=sys.stderr,
    )
    return src


def rows(cn: SourceDatabase, sql: str, params=()) -> list[dict]:
    return cn.rows(sql, params)


def one(cn: SourceDatabase, sql: str, params=()) -> dict | None:
    return cn.one(sql, params)


def scalar(cn: SourceDatabase, sql: str, params=()):
    return cn.scalar(sql, params)


def save(name: str, obj) -> Path:
    p = OUT / name
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    print(f"[saved] {p.relative_to(ROOT)}", file=sys.stderr)
    return p
