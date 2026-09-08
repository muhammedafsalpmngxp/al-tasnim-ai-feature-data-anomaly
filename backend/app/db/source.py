"""Read-only access to AlTasnimBI.

Three independent layers of protection, in order of strength:

1.  The account itself. Measured 2026-09-07: BIuser is `db_datareader` only —
    CREATE SCHEMA 0, CREATE TABLE 0, INSERT 0. It *cannot* write. This is the real
    guarantee; the two below are belt-and-braces.
2.  `ApplicationIntent=ReadOnly` on the connection string.
3.  `ReadOnlyGuard`, which rejects any statement that is not a bare SELECT/WITH before it
    reaches the driver.

Nothing in this module may ever write. Findings go to `app.db.store` (SQLite).
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterator, Sequence

import pyodbc
import structlog

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

_PREFERRED_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
)

# Statements that must never reach a production database from this feature.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|DENY"
    r"|EXEC|EXECUTE|SP_|XP_|BULK|OPENROWSET|OPENDATASOURCE|RECONFIGURE|SHUTDOWN"
    r"|BACKUP|RESTORE|WRITETEXT|UPDATETEXT|INTO\s+#|INTO\s+\[?dbo)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class ReadOnlyViolation(RuntimeError):
    """Raised when a statement that could modify data is attempted."""


def _strip_noise(sql: str) -> str:
    """Remove comments and string literals so keyword scanning can't be fooled.

    Literals are blanked because a legitimate `WHERE name = 'DROP TABLE'` must not trip
    the guard, while `; DROP TABLE x --` appended outside a literal must.
    """
    s = _BLOCK_COMMENT.sub(" ", sql)
    s = _LINE_COMMENT.sub(" ", s)
    s = _STRING_LITERAL.sub("''", s)
    return s


class ReadOnlyGuard:
    """Validates SQL before execution. Static, no state, easy to unit-test."""

    @staticmethod
    def validate(sql: str) -> None:
        if not sql or not sql.strip():
            raise ReadOnlyViolation("empty statement")
        stripped = _strip_noise(sql)
        if not _ALLOWED_START.match(stripped):
            head = stripped.strip()[:60].replace("\n", " ")
            raise ReadOnlyViolation(
                f"statement must begin with SELECT or WITH, got: {head!r}"
            )
        if hit := _FORBIDDEN.search(stripped):
            raise ReadOnlyViolation(
                f"forbidden keyword {hit.group(0).upper()!r} in a read-only statement"
            )
        # A second statement after a semicolon is how a guard gets bypassed.
        body = stripped.strip().rstrip(";")
        if ";" in body:
            raise ReadOnlyViolation("multiple statements are not allowed")


@dataclass(slots=True)
class QueryStats:
    queries: int = 0
    rows: int = 0
    millis: int = 0


class SourceDatabase:
    """Pooled read-only reader for the source database.

    pyodbc connections are not thread-safe, so each caller checks one out of a small pool
    via `cursor()`. `MAXDOP` and a query timeout bound the cost of any single check.
    """

    def __init__(self, settings: Settings | None = None, pool_size: int = 4) -> None:
        self._s = settings or get_settings()
        self._pool: list[pyodbc.Connection] = []
        self._lock = Lock()
        self._pool_size = pool_size
        self._driver: str | None = None
        self.stats = QueryStats()

    # ------------------------------------------------------------------ connection
    def _connection_string(self, driver: str) -> str:
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={self._s.db_server},{self._s.db_port};"
            f"DATABASE={self._s.db_name};"
            f"UID={self._s.db_user};PWD={self._s.db_password};"
            # Driver 18 defaults Encrypt=yes; the server certificate is self-signed.
            "Encrypt=yes;TrustServerCertificate=yes;"
            # Declared intent, on top of the account already being db_datareader.
            "ApplicationIntent=ReadOnly;"
            f"Connection Timeout={self._s.db_connect_timeout};"
        )

    def _connect(self) -> pyodbc.Connection:
        available = set(pyodbc.drivers())
        last: Exception | None = None
        for driver in _PREFERRED_DRIVERS:
            if driver not in available:
                continue
            try:
                cn = pyodbc.connect(
                    self._connection_string(driver),
                    timeout=self._s.db_connect_timeout,
                    readonly=True,
                )
                cn.timeout = self._s.db_query_timeout
                if self._driver != driver:
                    self._driver = driver
                    log.info("source.connected", driver=driver, database=self._s.db_name)
                return cn
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("source.driver_failed", driver=driver, error=str(exc)[:200])
        raise ConnectionError(
            f"could not connect to {self._s.db_server}:{self._s.db_port}/"
            f"{self._s.db_name}: {last}"
        )

    @contextmanager
    def cursor(self) -> Iterator[pyodbc.Cursor]:
        with self._lock:
            cn = self._pool.pop() if self._pool else None
        if cn is None:
            cn = self._connect()
        cur = None
        try:
            cur = cn.cursor()
            yield cur
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001, S110
                    pass
            with self._lock:
                if len(self._pool) < self._pool_size:
                    self._pool.append(cn)
                else:
                    try:
                        cn.close()
                    except Exception:  # noqa: BLE001, S110
                        pass

    def close(self) -> None:
        with self._lock:
            while self._pool:
                try:
                    self._pool.pop().close()
                except Exception:  # noqa: BLE001, S110
                    pass

    # ----------------------------------------------------------------------- query
    def _decorate(self, sql: str) -> str:
        """Append the MAXDOP bound. Skipped when the caller already set OPTION(...)."""
        if self._s.db_maxdop and "OPTION" not in sql.upper():
            return f"{sql.rstrip().rstrip(';')}\nOPTION (MAXDOP {self._s.db_maxdop})"
        return sql

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a validated SELECT and return a list of dicts."""
        ReadOnlyGuard.validate(sql)
        started = time.perf_counter()
        with self.cursor() as cur:
            cur.execute(self._decorate(sql), *params)
            columns = [d[0] for d in cur.description or []]
            out = [dict(zip(columns, r)) for r in cur.fetchall()]
        elapsed = int((time.perf_counter() - started) * 1000)
        self.stats.queries += 1
        self.stats.rows += len(out)
        self.stats.millis += elapsed
        if elapsed > 10_000:
            log.warning("source.slow_query", ms=elapsed, sql=sql.strip()[:160])
        return out

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rs = self.rows(sql, params)
        return rs[0] if rs else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    # ------------------------------------------------------------------- metadata
    def server_info(self) -> dict[str, Any]:
        return self.one(
            """
            SELECT DB_NAME() AS db_name,
                   SUSER_SNAME() AS login_name,
                   CAST(SERVERPROPERTY('ProductVersion') AS varchar(64)) AS product_version,
                   CAST(SERVERPROPERTY('Collation') AS varchar(128)) AS collation,
                   CAST(GETDATE() AS datetime2(0)) AS server_now,
                   CAST(GETDATE() AS date) AS server_today,
                   IS_MEMBER('db_owner') AS is_db_owner,
                   IS_MEMBER('db_datawriter') AS is_datawriter,
                   IS_MEMBER('db_datareader') AS is_datareader,
                   HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','INSERT') AS can_insert,
                   HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE') AS can_create_table
            """
        ) or {}

    def assert_read_only_account(self) -> dict[str, Any]:
        """Fail loudly if the account can write.

        The design assumes a reader. If someone points this at a privileged account, that
        assumption is broken and the run must not proceed silently.
        """
        info = self.server_info()
        writable = [
            name
            for name in ("is_db_owner", "is_datawriter", "can_insert", "can_create_table")
            if info.get(name)
        ]
        if writable:
            raise ReadOnlyViolation(
                "the source account has write permissions "
                f"({', '.join(writable)}) — the Sentinel requires a read-only account. "
                "Point DB_USER at a db_datareader login."
            )
        log.info(
            "source.account_verified_read_only",
            login=info.get("login_name"),
            database=info.get("db_name"),
        )
        return info

    def table_row_counts(self) -> dict[str, int]:
        """Fast approximate row counts from sys.partitions (no table scans)."""
        rs = self.rows(
            """
            SELECT s.name AS [schema], o.name AS [table], ISNULL(SUM(p.rows), 0) AS approx_rows
            FROM sys.objects o
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            LEFT JOIN sys.partitions p
                   ON p.object_id = o.object_id AND p.index_id IN (0, 1)
            WHERE o.type = 'U'
            GROUP BY s.name, o.name
            """
        )
        return {f"{r['schema']}.{r['table']}": int(r["approx_rows"]) for r in rs}
