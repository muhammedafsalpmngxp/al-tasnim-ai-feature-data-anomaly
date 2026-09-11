"""Read-only-intent MS SQL connection via pyodbc.

This engine never issues writes; combined with the Validator (SELECT-only) that keeps the
database safe even when the configured login is privileged.

Two things here are specific to an ANOMALY workload rather than a chatbot one:

  * a per-call `timeout` override, because a SUMMARY query returning one row and a DETAIL query
    scanning a fact table are different problems and must not share one deadline;
  * optional READ UNCOMMITTED, because these are scanning queries and an anomaly report is
    never worth blocking production writers for. It is issued as its own statement on the
    connection by THIS module - it is never part of the SQL the Validator inspects, so it
    cannot weaken the SELECT-only gate.
"""
from __future__ import annotations

import datetime
import struct

import pyodbc

from app.config import settings
from app.observability import get_logger

log = get_logger()

# pyodbc has no built-in decoder for SQL Server's `datetimeoffset` type (ODBC type code -155,
# SQL_SS_TIMESTAMPOFFSET) - fetching ANY column of this type raises
# "ODBC SQL type -155 is not yet supported" regardless of the value, even when it is NULL for
# every row. No SQL can avoid it; it is a driver gap, not a query problem, and an anomaly probe
# that happens to select such a column would fail for a reason no rewrite could fix.
#
# The fix is the converter Microsoft's own ODBC samples document for this type: the driver hands
# back raw fixed-layout bytes, and this unpacks them into a timezone-aware datetime.
_SQL_SS_TIMESTAMPOFFSET = -155


def _decode_datetimeoffset(raw: bytes) -> datetime.datetime:
    year, month, day, hour, minute, second, frac_100ns, tz_hour, tz_minute = struct.unpack(
        "<6hI2h", raw
    )
    return datetime.datetime(
        year, month, day, hour, minute, second, frac_100ns // 1000,
        tzinfo=datetime.timezone(datetime.timedelta(hours=tz_hour, minutes=tz_minute)),
    )


# Preferred driver order when DB_DRIVER is not pinned in .env.
_DRIVER_CANDIDATES = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
]


def _pick_driver() -> str:
    if settings.db_driver:
        return settings.db_driver
    installed = {d.strip() for d in pyodbc.drivers()}
    for cand in _DRIVER_CANDIDATES:
        if cand in installed:
            return cand
    if installed:
        return sorted(installed)[-1]
    raise RuntimeError(
        "No ODBC driver found. Install 'ODBC Driver 18 for SQL Server' (or 17) - see README."
    )


def _connection_string() -> str:
    driver = _pick_driver()
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={settings.db_server},{settings.db_port};"
        f"DATABASE={settings.db_name};"
        f"UID={settings.db_user};"
        f"PWD={settings.db_password};"
        f"Encrypt={settings.db_encrypt};"
        f"TrustServerCertificate={settings.db_trust_cert};"
    )


def get_connection(timeout: int | None = None, read_uncommitted: bool | None = None):
    """Open a fresh autocommit connection.

    `timeout` defaults to ANOMALY_QUERY_TIMEOUT. Pass ANOMALY_DETAIL_TIMEOUT explicitly for a
    detail scan - inheriting the summary deadline would turn every large rule into a timeout
    instead of a finding.

    `read_uncommitted` defaults to ANOMALY_READ_UNCOMMITTED. Setting the isolation level here,
    on the connection, is what keeps it out of the validated SQL: the Validator only ever sees
    the single SELECT, so this cannot be used to smuggle a second statement past it.

    Converters are registered per-connection (not via module-level pyodbc.add_output_converter)
    because every call opens a brand-new connection - there is no pool to configure once.
    """
    secs = settings.query_timeout if timeout is None else timeout
    conn = pyodbc.connect(_connection_string(), autocommit=True, timeout=secs)
    conn.timeout = secs
    conn.add_output_converter(_SQL_SS_TIMESTAMPOFFSET, _decode_datetimeoffset)

    want_ru = settings.read_uncommitted if read_uncommitted is None else read_uncommitted
    if want_ru:
        try:
            conn.cursor().execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        except Exception as exc:  # noqa: BLE001 - a courtesy setting must never block a run
            log.warning("db: could not set READ UNCOMMITTED (%s) - continuing at default", exc)
    return conn


def ping() -> tuple[bool, str]:
    """Health check used at startup. Returns (ok, message)."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME(), @@VERSION")
        row = cur.fetchone()
        return True, f"Connected to {row[0]}"
    except Exception as exc:  # noqa: BLE001 - surface any driver/network error to the caller
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
