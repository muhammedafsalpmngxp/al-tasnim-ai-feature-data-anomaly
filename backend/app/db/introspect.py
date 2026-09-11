"""Build a compact, LLM-friendly description of the live database.

Three artefacts are produced, all cached under .cache/ and all rebuilt automatically when the
database changes:

    schema.txt         tables, columns (+PK markers), declared foreign keys, grain warnings
    value_hints.txt    real coded values from small lookup tables (TEXT columns)
    numeric_hints.txt  observed range/scale/null-rate of NUMERIC columns          <- new here

NOTHING IN THIS MODULE NAMES A TABLE OR COLUMN. Only the schemas listed in ALLOWED_SCHEMAS are
read; EXCLUDED_TABLES / EXCLUDED_COLUMNS and a built-in secret-name filter remove the rest.
Point the app at a different database and everything below follows it with no code change.

WHY numeric_hints.txt EXISTS (and has no counterpart in a chatbot)
------------------------------------------------------------------
An anomaly probe constantly compares a measured value against a threshold, and the single most
damaging mistake it can make is getting the SCALE wrong - writing `progress < 100` against a
column that actually stores 0.0-1.0 flags every row in the table, and writing `progress < 1`
against a 0-100 column flags none. Both look like a working query. Neither is detectable from
INFORMATION_SCHEMA, because `decimal` says nothing about the range a column actually holds.

So the scale is MEASURED, once, and handed to the agents as fact. The same single scan also
yields the null rate, the observed bounds that the generic range probes use instead of assumed
ones, and a standard deviation - which is what lets a rule express a self-calibrating threshold
(`> AVG + 2*STDEV`) rather than an invented constant.

TWO FINGERPRINTS, DELIBERATELY
------------------------------
`_live_fingerprint()`  structure + keys + row counts + database identity.
    Guards schema.txt / value_hints.txt / numeric_hints.txt, because all three describe DATA
    that changes when rows are inserted or deleted.

`structure_fingerprint()`  the same MINUS row counts.
    Guards the compiled anomaly catalog. This split is the whole reason a catalog survives:
    row counts change every day as data loads, so a catalog keyed on the full fingerprint would
    recompile - at full LLM cost - every single day, which is exactly what the catalog exists
    to avoid. A probe's SQL depends on the STRUCTURE, not on how many rows are currently in it.
"""
from __future__ import annotations

import hashlib
import os
import re

from app.config import settings
from app.db.connection import get_connection
from app.observability import get_logger

log = get_logger()

# Substrings that mark a column as secret - never exposed to the LLM. Generic credential
# wording, not tied to any schema. Applies on top of EXCLUDED_COLUMNS and needs no config.
SECRET_COLUMN_MARKERS = (
    "password", "secret", "token", "pwd", "apikey", "api_key", "credential", "private_key",
)

# Bumped whenever _render() changes the TEXT it emits for an unchanged database. The cache is
# keyed on a fingerprint of the live STRUCTURE, so without this a rendering change would keep
# serving the old cached text forever - the database has not changed, so nothing else notices.
_RENDER_VERSION = "3-anomaly-honest-null-pct"

# T-SQL reserved keywords. A column whose NAME is one of these MUST be written [bracketed] or
# the query fails to parse - and the error is misleading: SQL Server reports "Incorrect syntax
# near the keyword 'plan'" (42000), NOT "Invalid column name" (42S22), so it reads like the
# column does not exist when in fact it does.
#
# The list is conservative on purpose: `status`, `time`, `system` and `session` LOOK reserved
# but SQL Server accepts them bare, and bracketing every column would add noise to every prompt.
_TSQL_RESERVED = frozenset("""
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE
CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS
CONTAINSTABLE CONTINUE CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_USER CURSOR DATABASE DBCC DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT
DISTRIBUTED DOUBLE DROP DUMP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH
FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL FUNCTION GOTO GRANT GROUP HAVING
HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY
KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL NULLIF OF OFF OFFSETS ON
OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN
PRECISION PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE
SCHEMA SECURITYAUDIT SELECT SESSION_USER SET SETUSER SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE
TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL UNION
UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH
WRITETEXT
""".split())

# A bare (unbracketed) T-SQL identifier may only be a letter/underscore followed by letters,
# digits, underscore, $ or #. Any other shape must be bracketed - a SECOND, independent reason
# from the reserved-word list above (a column name containing a space fails identically).
_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def quote_column(col: str) -> str:
    """The form the SQL Author must copy: [bracketed] whenever the bare name would not parse.

    Applied to the column DECLARATION lines only. FK lines are left bare deliberately, because
    app/graph/sqlcheck.py parses them with a \\w+ pattern that brackets would not match.
    """
    if col.upper() in _TSQL_RESERVED or not _BARE_IDENTIFIER.match(col):
        return f"[{col}]"
    return col


_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
_SCHEMA_PATH = os.path.join(_CACHE_DIR, "schema.txt")
_FINGERPRINT_PATH = os.path.join(_CACHE_DIR, "schema.fingerprint")
_VALUE_HINTS_PATH = os.path.join(_CACHE_DIR, "value_hints.txt")
_NUMERIC_HINTS_PATH = os.path.join(_CACHE_DIR, "numeric_hints.txt")


# ── Visibility filters ─────────────────────────────────────────────────────────

def _is_secret(col: str) -> bool:
    lc = col.lower()
    return any(m in lc for m in SECRET_COLUMN_MARKERS)


def _is_excluded(schema: str, table: str) -> bool:
    """True when EXCLUDED_TABLES hides this table.

    Matches "schema.table" (exact - the safe form) or a bare "table" name, which hides that name
    in every allowed schema. An excluded table also disappears from the primary keys, foreign
    keys and both hint files, because each is built from the surviving table list.
    """
    if not settings.excluded_tables:
        return False
    excluded = settings.excluded_tables
    return f"{schema}.{table}".lower() in excluded or table.lower() in excluded


def _is_excluded_column(schema: str, table: str, column: str) -> bool:
    """True when EXCLUDED_COLUMNS hides this column.

    Matches "schema.table.column" (exact), "table.column" (that table in any schema), or a bare
    "column" name (that name in every table). Runs on top of SECRET_COLUMN_MARKERS.
    """
    if not settings.excluded_columns:
        return False
    excluded = settings.excluded_columns
    return (
        f"{schema}.{table}.{column}".lower() in excluded
        or f"{table}.{column}".lower() in excluded
        or column.lower() in excluded
    )


# ── Catalogue reads ────────────────────────────────────────────────────────────

def _fetch_columns(cur) -> dict[str, list[tuple]]:
    """schema.table -> [(column, data_type, max_length, is_nullable), ...] in ordinal order."""
    placeholders = ",".join("?" for _ in settings.allowed_schemas)
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """,
        *settings.allowed_schemas,
    )
    tables: dict[str, list[tuple]] = {}
    for sch, tbl, col, dtype, maxlen, nullable, _ in cur.fetchall():
        if _is_excluded(sch, tbl) or _is_secret(col) or _is_excluded_column(sch, tbl, col):
            continue
        tables.setdefault(f"{sch}.{tbl}", []).append((col, dtype, maxlen, nullable))
    return tables


def _fetch_primary_keys(cur) -> dict[str, set[str]]:
    """schema.table -> set of primary-key column names."""
    cur.execute(
        """
        SELECT sch.name, t.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c   ON c.object_id  = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables  t   ON t.object_id  = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_primary_key = 1
        """
    )
    pks: dict[str, set[str]] = {}
    for sch, tbl, col in cur.fetchall():
        # A hidden PK column is dropped HERE rather than later: pks feeds both the " PK" marker
        # and the duplicate-key scan, either of which would otherwise name an invisible column.
        if _is_excluded(sch, tbl) or _is_excluded_column(sch, tbl, col):
            continue
        pks.setdefault(f"{sch}.{tbl}", set()).add(col)
    return pks


def _fetch_foreign_keys(cur, visible: set[str]) -> dict[str, list[str]]:
    """Declared FKs, restricted so BOTH endpoints are visible (no dangling references).

    These lines are load-bearing twice over in this app: they render into schema.txt for the
    agents, and app/rules/generic.py turns each one into an orphan-row probe with no LLM at all.
    """
    cur.execute(
        """
        SELECT sch.name, t.name, c.name, rsch.name, rt.name, rc.name
        FROM sys.foreign_key_columns fkc
        JOIN sys.tables  t    ON t.object_id  = fkc.parent_object_id
        JOIN sys.schemas sch  ON sch.schema_id = t.schema_id
        JOIN sys.columns c    ON c.object_id  = fkc.parent_object_id
                             AND c.column_id  = fkc.parent_column_id
        JOIN sys.tables  rt   ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rsch ON rsch.schema_id = rt.schema_id
        JOIN sys.columns rc   ON rc.object_id  = fkc.referenced_object_id
                             AND rc.column_id  = fkc.referenced_column_id
        """
    )
    fks: dict[str, list[str]] = {}
    for fs, ft, fc, ts, tt, tc in cur.fetchall():
        frm, to = f"{fs}.{ft}", f"{ts}.{tt}"
        if frm not in visible or to not in visible:
            continue
        # Either END being hidden makes the relationship unusable - a join nobody can write.
        if _is_excluded_column(fs, ft, fc) or _is_excluded_column(ts, tt, tc):
            continue
        fks.setdefault(frm, []).append(f"{fc} -> {to}.{tc}")
    return fks


def _row_counts(cur) -> list[tuple]:
    """Approximate row count per table, from catalogue metadata - not a table scan.

    ONE query, effectively instant no matter how much data the tables hold. Used to notice that
    data changed and to decide which tables are small enough to sample, never as a reported
    figure.

    TWO SOURCES, tried in order, because the first needs a permission a read-only reporting
    login is routinely not granted:

      sys.dm_db_partition_stats  a DMV; requires VIEW DATABASE STATE (or on Azure SQL,
                                 VIEW DATABASE PERFORMANCE STATE). Preferred - it reflects
                                 in-flight changes slightly sooner.
      sys.partitions             a CATALOG VIEW; visible to anyone who can already see the
                                 table itself, which by definition this login can. Same
                                 approximate count, from the same storage metadata.

    Falling back matters more than it looks. Without any row count: the "too large to profile"
    guard in build_numeric_hints() silently never fires, so a hundred-million-row table would
    be fully scanned; and _live_fingerprint() degrades to structure-only, so neither hint file
    would ever notice that the DATA changed. Both failures are invisible.
    """
    placeholders = ",".join("?" for _ in settings.allowed_schemas)
    attempts = (
        ("sys.dm_db_partition_stats", "p.row_count"),
        ("sys.partitions", "p.rows"),
    )
    last_exc: Exception | None = None
    for source, column in attempts:
        try:
            cur.execute(
                f"""
                SELECT s.name, t.name, SUM({column})
                FROM {source} p
                JOIN sys.tables  t ON t.object_id = p.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE p.index_id IN (0, 1) AND LOWER(s.name) IN ({placeholders})
                GROUP BY s.name, t.name
                ORDER BY s.name, t.name
                """,
                *settings.allowed_schemas,
            )
            rows = cur.fetchall()
            if source != attempts[0][0]:
                log.info("schema: row counts read from %s (the DMV was not permitted)", source)
            return rows
        except Exception as exc:  # noqa: BLE001 - try the next source before giving up
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("no row-count source available")


def _key_signature(cur) -> list[tuple]:
    """Everything a table's structure can change WITHOUT a column being added, dropped, renamed
    or retyped: primary keys, foreign keys, CHECK constraints, DEFAULT values, and non-PK unique
    indexes.

    None of these appear in INFORMATION_SCHEMA.COLUMNS, so without this a DBA could drop a
    foreign key or add a CHECK constraint and the caches would never notice - the rendered
    schema would keep describing relationships that no longer exist, and the generic probes
    would keep checking an orphan relationship that is no longer declared.

    All metadata-only reads; no table is scanned.
    """
    placeholders = ",".join("?" for _ in settings.allowed_schemas)
    schemas = settings.allowed_schemas
    rows: list[tuple] = []

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c   ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables  t   ON t.object_id = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_primary_key = 1 AND LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("PK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name, rsch.name, rt.name, rc.name
        FROM sys.foreign_key_columns fkc
        JOIN sys.tables  t    ON t.object_id = fkc.parent_object_id
        JOIN sys.schemas sch  ON sch.schema_id = t.schema_id
        JOIN sys.columns c    ON c.object_id = fkc.parent_object_id
                             AND c.column_id = fkc.parent_column_id
        JOIN sys.tables  rt   ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rsch ON rsch.schema_id = rt.schema_id
        JOIN sys.columns rc   ON rc.object_id = fkc.referenced_object_id
                             AND rc.column_id = fkc.referenced_column_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("FK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, cc.name, cc.definition
        FROM sys.check_constraints cc
        JOIN sys.tables  t   ON t.object_id = cc.parent_object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, cc.name
        """,
        *schemas,
    )
    rows += [("CHECK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name, dc.definition
        FROM sys.default_constraints dc
        JOIN sys.tables  t   ON t.object_id = dc.parent_object_id
        JOIN sys.columns c   ON c.object_id = dc.parent_object_id
                            AND c.column_id = dc.parent_column_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("DEFAULT", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, i.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c   ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables  t   ON t.object_id = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, i.name, ic.key_ordinal
        """,
        *schemas,
    )
    rows += [("UNIQUE", *r) for r in cur.fetchall()]

    return rows


# ── Grain detection ────────────────────────────────────────────────────────────

# Below this many rows a repeated key cannot distort an answer enough to be worth a scan, and
# these are the lookup tables anyway. Keeps rebuild time flat as the fact tables grow.
_DUP_CHECK_MIN_ROWS = 100


def _detect_duplicate_keys(
    cur,
    tables: dict[str, list[tuple]],
    pks: dict[str, set[str]],
    row_counts: dict[str, int] | None = None,
) -> dict[str, str]:
    """table -> the key column that repeats, for tables holding many rows per entity.

    Measured from the live data, not assumed. Forgetting to de-duplicate such a table is the
    single most damaging SQL mistake in this kind of database, and in an anomaly probe it is
    worse than wrong - it inflates the anomaly COUNT, which is the headline number in the
    report. Surfacing it in schema.txt means the agents learn it from the database itself.

    Only the COUNT(DISTINCT ...) is a scan; the total comes free from catalogue statistics, and
    small tables are skipped entirely.
    """
    row_counts = row_counts or {}
    found: dict[str, str] = {}
    for table, cols in tables.items():
        total = row_counts.get(table)
        if total is not None and total < _DUP_CHECK_MIN_ROWS:
            continue

        tpk = pks.get(table, set())
        # A SINGLE-column primary key is unique by definition - the database enforces it, so
        # scanning to confirm would tell us nothing. Only tables where the entity key is a
        # guess (no PK, or a composite one whose parts individually repeat) need a scan.
        if len(tpk) == 1:
            continue

        names = [c[0] for c in cols]
        candidates = sorted(tpk) or [c for c in names if c.lower().endswith("id")]
        if not candidates:
            continue
        key = candidates[0]
        sch, _, tbl = table.partition(".")
        try:
            if total is None:
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT [{key}]) FROM [{sch}].[{tbl}]")
                total, distinct = cur.fetchone()
            else:
                cur.execute(f"SELECT COUNT(DISTINCT [{key}]) FROM [{sch}].[{tbl}]")
                distinct = cur.fetchone()[0]
        except Exception:  # noqa: BLE001 - a table we cannot count must not break introspection
            continue
        if total and distinct and total > distinct:
            found[table] = key
    return found


# ── Rendering ──────────────────────────────────────────────────────────────────

def _render(
    tables: dict[str, list[tuple]],
    pks: dict[str, set[str]],
    fks: dict[str, list[str]],
    dup_keys: dict[str, str] | None = None,
) -> str:
    dup_keys = dup_keys or {}
    lines: list[str] = []
    for table, cols in tables.items():
        lines.append(f"TABLE {table}")
        tpk = pks.get(table, set())
        for col, dtype, maxlen, nullable in cols:
            typ = dtype + (f"({maxlen})" if maxlen and maxlen > 0 else "")
            null = "" if nullable == "YES" else " NOT NULL"
            pk = " PK" if col in tpk else ""
            # quote_column() is for DISPLAY only. `col` stays raw for the PK comparison above,
            # and every other consumer keeps the bare name.
            lines.append(f"  - {quote_column(col)} {typ}{null}{pk}")
        if table in dup_keys:
            # quote_column() here too, for the same reason as the declarations above: this
            # database has a key literally named "PDO Well ID". Rendered bare it reads as
            # three words, and app/rules/generic.py - which builds a real probe from this
            # marker - would emit [PDO] and fail on a column that does not exist.
            key = quote_column(dup_keys[table])
            lines.append(
                f"  MANY ROWS PER {key} - de-duplicate (COUNT(DISTINCT ...) or "
                f"GROUP BY) before counting anything per-{key}"
            )
        for rel in fks.get(table, []):
            lines.append(f"  FK: {rel}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Fingerprints ───────────────────────────────────────────────────────────────

def _structure_signature(cur, h: hashlib._Hash) -> None:
    """Fold everything STRUCTURAL into `h`: columns, keys, constraints, settings, db identity.

    Shared by both fingerprints so they can never disagree about what "structure" means.

    The database IDENTITY is folded in, not just its shape: two databases can have an IDENTICAL
    structure (a restored copy, or dev vs production). For schema.txt that is harmless, but the
    hint files hold REAL VALUES read out of the data, and carrying those across to a different
    system means probes calibrated against the wrong scale and thresholds.
    """
    placeholders = ",".join("?" for _ in settings.allowed_schemas)
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
        """,
        *settings.allowed_schemas,
    )
    # The catalogue query is NOT filtered by the exclusion settings, so editing one of them
    # would leave the hash unchanged and the cache would keep serving tables you just hid.
    # Fold the settings in explicitly so a change rebuilds.
    h.update(("!schemas=" + ",".join(sorted(settings.allowed_schemas))).encode("utf-8"))
    h.update(("!excluded=" + ",".join(sorted(settings.excluded_tables))).encode("utf-8"))
    h.update(("!excluded_cols=" + ",".join(sorted(settings.excluded_columns))).encode("utf-8"))
    h.update(("!render=" + _RENDER_VERSION).encode("utf-8"))
    h.update(f"!db={settings.db_server}:{settings.db_port}/{settings.db_name}".encode("utf-8"))
    h.update(b"\n")
    for row in cur.fetchall():
        h.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
        h.update(b"\n")

    # Keys/constraints live in separate catalogues, so a key changing alone would otherwise
    # leave the hash - and the cache - untouched.
    try:
        for row in _key_signature(cur):
            h.update(("$" + "|".join("" if v is None else str(v) for v in row)).encode("utf-8"))
            h.update(b"\n")
    except Exception as exc:  # noqa: BLE001
        log.info("schema: key/constraint signal unavailable (%s) - tracking columns only", exc)


def _live_fingerprint(cur) -> str:
    """Structure + row counts. Guards schema.txt and both hint files.

    Row counts are included here because all three artefacts describe DATA: the grain warnings,
    the sampled lookup values and the observed numeric ranges all go stale when rows change,
    even though every column stayed the same.
    """
    h = hashlib.sha256()
    _structure_signature(cur, h)
    # Row counts need VIEW DATABASE STATE. A login without it still gets structure-only drift
    # detection rather than a failed startup.
    try:
        for row in _row_counts(cur):
            h.update(("#" + "|".join(str(v) for v in row)).encode("utf-8"))
            h.update(b"\n")
    except Exception as exc:  # noqa: BLE001
        log.info("schema: row-count signal unavailable (%s) - tracking structure only", exc)
    return h.hexdigest()


def structure_fingerprint(cur=None) -> str:
    """Structure ONLY - no row counts. Guards the compiled anomaly catalog.

    THIS SPLIT IS WHY A CATALOG SURVIVES. Row counts change every day as data loads. A catalog
    keyed on _live_fingerprint() would therefore be invalidated daily and recompiled at full
    LLM cost - defeating the entire point of compiling probes once. A probe's SQL depends on
    the structure it queries, not on how many rows are in it today.

    Pass an open cursor to reuse a connection; omit it and one is opened and closed here.
    """
    if cur is not None:
        h = hashlib.sha256()
        _structure_signature(cur, h)
        return h.hexdigest()

    conn = get_connection()
    try:
        h = hashlib.sha256()
        _structure_signature(conn.cursor(), h)
        return h.hexdigest()
    finally:
        conn.close()


def _read_fingerprint() -> str:
    try:
        with open(_FINGERPRINT_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _invalidate_hints() -> None:
    """Drop the hint caches so they are rebuilt from the live database on next use.

    Called when the schema fingerprint moves: both hint files describe the same structure, so a
    new table would otherwise appear in schema.txt but never in the hints.
    """
    for path in (_VALUE_HINTS_PATH, _NUMERIC_HINTS_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("schema: %s invalidated, it will be rebuilt", os.path.basename(path))
        except OSError as exc:  # noqa: BLE001 - cache cleanup must never break startup
            log.warning("schema: could not invalidate %s (%s)", os.path.basename(path), exc)


# ── schema.txt ─────────────────────────────────────────────────────────────────

def build_schema_text(use_cache: bool = True) -> str:
    """Introspect and return the schema block (cached to .cache/schema.txt).

    With use_cache=True the cache is used ONLY if the live database still matches what it was
    built from. If a table or column is added the fingerprint differs and it rebuilds itself on
    the next run - no one has to remember to run the refresh command.
    """
    cached = ""
    if use_cache and os.path.exists(_SCHEMA_PATH):
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            cached = fh.read().strip()

    conn = get_connection()
    try:
        cur = conn.cursor()
        if cached:
            try:
                live = _live_fingerprint(cur)
            except Exception as exc:  # noqa: BLE001 - a drift check must never break startup
                log.warning("schema: drift check failed (%s) - using the cached schema", exc)
                return cached
            if live and live == _read_fingerprint():
                return cached
            log.warning("schema: the database changed since the cache was built - rebuilding")
            _invalidate_hints()

        tables = _fetch_columns(cur)
        visible = set(tables.keys())
        pks = _fetch_primary_keys(cur)
        fks = _fetch_foreign_keys(cur, visible)
        try:
            sizes = {f"{s}.{t}": (n or 0) for s, t, n in _row_counts(cur)}
        except Exception:  # noqa: BLE001 - fall back to counting per table
            sizes = {}
        dup_keys = _detect_duplicate_keys(cur, tables, pks, sizes)
        text = _render(tables, pks, fks, dup_keys)
        fingerprint = _live_fingerprint(cur)
    finally:
        conn.close()

    # Never cache an empty schema (e.g. a wrong ALLOWED_SCHEMAS) - that would silently degrade
    # every later run into "no tables exist".
    if not text.strip():
        return text
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_SCHEMA_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(_FINGERPRINT_PATH, "w", encoding="utf-8") as fh:
        fh.write(fingerprint)
    log.info("schema: rebuilt from the live database (%d tables)", len(text.split("\nTABLE ")))
    return text


# ── value_hints.txt ────────────────────────────────────────────────────────────
# Real coded values from small lookup tables, so probes filter and decode using ACTUAL values
# instead of guessing them. A lookup table is recognised by SHAPE - few rows, and a
# name/description-ish text column - so this follows whatever database is configured.

_HINT_COL_SUFFIXES = ("name", "code", "description", "desc", "label", "status", "type")
_HINT_MAX_TABLES = 80
_HINT_MAX_VALUES = 30
# A lookup table is small by definition. Above this it is transactional data, not a code list.
_HINT_MAX_ROWS = 50


def _is_hint_column(col: str) -> bool:
    lc = col.lower()
    return lc.endswith(_HINT_COL_SUFFIXES) or lc in ("code", "name")


def build_value_hints(use_cache: bool = True) -> str:
    if use_cache and os.path.exists(_VALUE_HINTS_PATH):
        with open(_VALUE_HINTS_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    if not settings.allowed_schemas:
        return ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in settings.allowed_schemas)
        cur.execute(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """,
            *settings.allowed_schemas,
        )
        table_cols: dict[tuple[str, str], list[str]] = {}
        for sch, tbl, col in cur.fetchall():
            if _is_excluded(sch, tbl) or _is_secret(col) or _is_excluded_column(sch, tbl, col):
                continue
            if _is_hint_column(col):
                table_cols.setdefault((sch, tbl), []).append(col)

        lines = [
            "VALUE HINTS (real coded values from the lookup tables - filter, join and decode "
            "using these ACTUAL values rather than guessing them):"
        ]
        # Sizes come from catalogue statistics in ONE query, so a large transactional table is
        # skipped WITHOUT being queried at all.
        try:
            sizes = {f"{s}.{t}": (n or 0) for s, t, n in _row_counts(cur)}
        except Exception:  # noqa: BLE001
            sizes = {}

        for (sch, tbl), cols in list(table_cols.items())[:_HINT_MAX_TABLES]:
            known = sizes.get(f"{sch}.{tbl}")
            if known is not None and known > _HINT_MAX_ROWS:
                continue  # transactional data, not a code list
            col_sql = ", ".join(f"[{c}]" for c in cols)
            try:
                if known is None:
                    cur.execute(f"SELECT COUNT(*) FROM [{sch}].[{tbl}]")
                    if (cur.fetchone()[0] or 0) > _HINT_MAX_ROWS:
                        continue
                cur.execute(
                    f"SELECT DISTINCT TOP {_HINT_MAX_VALUES} {col_sql} "
                    f"FROM [{sch}].[{tbl}] ORDER BY {col_sql}"
                )
                rows = cur.fetchall()
            except Exception:  # noqa: BLE001 - a bad/empty table must not break hint building
                continue
            if not rows:
                continue
            vals = ["|".join("" if v is None else str(v).strip() for v in r) for r in rows]
            lines.append(f"- {sch}.{tbl} ({', '.join(cols)}): " + "; ".join(vals))
        text = "\n".join(lines) if len(lines) > 1 else ""
    finally:
        conn.close()

    if not text.strip():
        return text
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_VALUE_HINTS_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


# ── numeric_hints.txt ──────────────────────────────────────────────────────────
# The measured range, scale and null rate of every numeric column. See the module docstring for
# why an anomaly engine cannot work without this and a chatbot can.

_NUMERIC_TYPES = frozenset(
    ("int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money",
     "smallmoney")
)
_TEXT_TYPES = frozenset(("varchar", "nvarchar", "char", "nchar"))

# ⚠ These are matched as WHOLE NAME TOKENS, never as substrings. Substring matching looks
# obviously fine and is quietly wrong: "ratio" is a substring of "du-ratio-n", so every
# `duration` column in this database was classified as a 0-100 percentage. That produced range
# probes with 0..100 bounds over day counts - and on a column reaching 16,420 it would have
# reported most of the table as out of range. A whole-token match costs nothing and removes the
# entire class of error.
_RATIO_MARKERS = frozenset(
    ("pct", "percent", "percentage", "progress", "ratio", "share", "weightage", "weight",
     "complete", "completion", "util", "utilisation", "utilization")
)
# Tokens meaning "this text column ought to parse as a number". A non-numeric value in one is a
# data-quality finding in its own right.
_NUMERIC_TEXT_MARKERS = frozenset(
    ("norm", "norms", "duration", "qty", "quantity", "amount", "length", "progress", "percent",
     "count", "days", "hours", "rate", "weight", "value", "number", "no", "num", "size", "total")
)
# ...except where another token says the "number" is an identifier, not a quantity. A phone
# number legitimately holds spaces, '+' and '-', so casting it is meaningless and a probe over
# it would report ordinary data as broken.
_NOT_A_QUANTITY = frozenset(
    ("phone", "mobile", "contact", "fax", "tel", "telephone", "account", "invoice", "po",
     "serial", "ref", "reference", "doc", "document", "form", "batch", "version")
)

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _name_tokens(col: str) -> set[str]:
    """Lowercased word tokens of a column name, splitting on punctuation AND camelCase.

    "actual_progress" -> {actual, progress};  "startDate" -> {start, date};
    "duration" -> {duration}, which is NOT {ratio}.
    """
    return {t for t in re.split(r"[^A-Za-z0-9]+", _CAMEL_SPLIT.sub("_", col).lower()) if t}
# One aggregate pass per table is cheap, but above this many rows it is a real scan on a fact
# table. Such tables are reported as "not profiled" rather than silently skipped, so the agents
# know the scale is unknown instead of assuming a default.
_NUMERIC_MAX_ROWS = 20_000_000
_NUMERIC_MAX_COLS_PER_QUERY = 40


# Name shapes that make a column an IDENTIFIER, never a measurement. Checked before the ratio
# markers, because an identifier routinely contains one: `progress_id` is a primary key, not a
# progress, and reporting it as "PERCENT_100 but MAX=99589 exceeds 100" is noise that would
# also generate a bogus range probe over a key column.
_KEY_TOKENS = frozenset(("id", "key", "code", "no", "num", "uid", "guid", "ref"))


def _is_key_like(col: str, pk_columns: set[str] | None = None) -> bool:
    if pk_columns and col in pk_columns:
        return True
    tokens = _name_tokens(col)
    # The LAST token decides: "id_verified" is not an identifier, "well_id" is.
    last = re.split(r"[^A-Za-z0-9]+", _CAMEL_SPLIT.sub("_", col).lower())[-1]
    return last in _KEY_TOKENS or tokens == {"id"}


def _null_pct(nulls: int, total: int) -> str:
    """Null rate, never rounded to a figure that contradicts the other statistics.

    Plain rounding reports "nulls 100%" for a column that is 99.6% null - and that column
    still has a min, a max and an average sitting beside the claim that it is empty. A reader
    (or an agent) resolves the contradiction by disbelieving one of them, and either choice is
    wrong. 100% and 0% are now reserved for the exact cases, and everything else is clamped
    into 1..99 with a "<1%" / ">99%" marker where it belongs.
    """
    if total <= 0:
        return "0%"
    if nulls <= 0:
        return "0%"
    if nulls >= total:
        return "100%"
    pct = 100.0 * nulls / total
    if pct < 1:
        return "<1%"
    if pct > 99:
        return ">99%"
    return f"{pct:.0f}%"


def _classify_scale(col: str, lo, hi, pk_columns: set[str] | None = None) -> str:
    """Name the observed scale so a probe compares against the right bound.

    Reports what was MEASURED and flags what contradicts the column's own name. It never
    rewrites a value or decides an anomaly - that is the rules' job.
    """
    if lo is None or hi is None:
        return "no data"
    # An identifier is never a proportion, however it is named.
    ratio_ish = bool(_name_tokens(col) & _RATIO_MARKERS) and not _is_key_like(col, pk_columns)
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return "unknown"
    if ratio_ish:
        if hi_f > 100:
            return f"PERCENT_100 but MAX={hi_f:g} EXCEEDS 100 - out-of-range values present"
        if lo_f < 0:
            return f"proportion but MIN={lo_f:g} is NEGATIVE - out-of-range values present"
        if hi_f <= 1.0:
            return "FRACTION_1 (0-1) - multiply by 100 to report a percentage"
        return "PERCENT_100 (0-100) - already a percentage, do NOT multiply"
    return "plain number"


def build_numeric_hints(use_cache: bool = True) -> str:
    """Measured range/scale/null-rate per numeric column, plus text columns that fail to parse.

    One aggregate query per table computes every numeric column in a single pass, so the cost
    is one scan per table - paid once, then cached until the structure or the data changes.
    """
    if use_cache and os.path.exists(_NUMERIC_HINTS_PATH):
        with open(_NUMERIC_HINTS_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    if not settings.allowed_schemas:
        return ""

    conn = get_connection(timeout=settings.detail_timeout)
    try:
        cur = conn.cursor()
        tables = _fetch_columns(cur)
        # Needed so a primary key is never classified as a measurement, however it is named.
        pks = _fetch_primary_keys(cur)
        try:
            sizes = {f"{s}.{t}": (n or 0) for s, t, n in _row_counts(cur)}
        except Exception:  # noqa: BLE001
            sizes = {}

        lines = [
            "NUMERIC HINTS (MEASURED from the live data - use these to pick the correct scale "
            "and real bounds; never assume a decimal column is 0-100 or 0-1):"
        ]
        skipped: list[str] = []

        for table, cols in tables.items():
            sch, _, tbl = table.partition(".")
            total = sizes.get(table)
            if total is not None and total > _NUMERIC_MAX_ROWS:
                skipped.append(f"{table} ({total:,} rows)")
                continue

            numeric = [c for c, dt, *_ in cols if dt.lower() in _NUMERIC_TYPES]
            textish = [
                c for c, dt, *_ in cols
                if dt.lower() in _TEXT_TYPES
                and (_name_tokens(c) & _NUMERIC_TEXT_MARKERS)
                and not (_name_tokens(c) & _NOT_A_QUANTITY)
            ]
            if not numeric and not textish:
                continue

            # Cap the projection: a very wide table would otherwise build a statement with
            # hundreds of aggregates, which is slower to compile than it is to run.
            numeric = numeric[:_NUMERIC_MAX_COLS_PER_QUERY]
            textish = textish[:_NUMERIC_MAX_COLS_PER_QUERY]

            parts = ["COUNT(*) AS _rows"]
            for c in numeric:
                q = f"[{c}]"
                # CAST to float so int columns still yield a usable AVG/STDEV, and so mixed
                # precisions across columns cannot overflow a shared decimal type.
                parts += [
                    f"MIN(CAST({q} AS float))", f"MAX(CAST({q} AS float))",
                    f"AVG(CAST({q} AS float))", f"STDEV(CAST({q} AS float))", f"COUNT({q})",
                ]
            for c in textish:
                q = f"[{c}]"
                parts += [
                    f"COUNT({q})",
                    f"SUM(CASE WHEN {q} IS NOT NULL AND TRY_CAST({q} AS float) IS NULL "
                    f"THEN 1 ELSE 0 END)",
                ]

            try:
                cur.execute(f"SELECT {', '.join(parts)} FROM [{sch}].[{tbl}]")
                row = list(cur.fetchone())
            except Exception as exc:  # noqa: BLE001 - one bad table must not stop profiling
                log.info("numeric hints: skipped %s (%s)", table, exc)
                continue

            n_rows = row[0] or 0
            if not n_rows:
                continue
            i = 1
            out: list[str] = []
            tpk = pks.get(table, set())
            for c in numeric:
                lo, hi, avg, sd, non_null = row[i : i + 5]
                i += 5
                nulls = n_rows - (non_null or 0)
                scale = _classify_scale(c, lo, hi, tpk)
                bits = [
                    f"min {lo:g}" if lo is not None else "min -",
                    f"max {hi:g}" if hi is not None else "max -",
                    f"avg {avg:.4g}" if avg is not None else "avg -",
                    f"stdev {sd:.4g}" if sd is not None else "stdev -",
                    f"nulls {_null_pct(nulls, n_rows)}",
                ]
                out.append(f"  - {c}: {', '.join(bits)} | {scale}")
            for c in textish:
                non_null, bad = row[i : i + 2]
                i += 2
                non_null, bad = non_null or 0, bad or 0
                if bad and bad == non_null:
                    # EVERY value fails. That is not a data-quality defect - it is a column
                    # that holds codes, and the name-based guess that it holds a quantity was
                    # simply wrong. Saying so keeps a cast probe from being generated over it,
                    # which would otherwise report 100% of the table as anomalous.
                    out.append(
                        f"  - {c} (text): NONE of {non_null} values parse as a number - this "
                        f"column holds codes/labels, NOT a quantity. Do not range-check it."
                    )
                elif bad:
                    out.append(
                        f"  - {c} (text): {bad} of {non_null} values do NOT parse as a number "
                        f"- TRY_CAST is required, and the failures are themselves a finding"
                    )
                elif non_null:
                    out.append(
                        f"  - {c} (text): all {non_null} values parse as a number "
                        f"- use TRY_CAST before comparing"
                    )
            if out:
                lines.append(f"{table}  ({n_rows:,} rows)")
                lines.extend(out)

        if skipped:
            lines.append(
                "NOT PROFILED (too large to scan; the scale of their numeric columns is "
                "UNKNOWN - do not assume one): " + "; ".join(skipped)
            )
        text = "\n".join(lines) if len(lines) > 1 else ""
    finally:
        conn.close()

    if not text.strip():
        return text
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_NUMERIC_HINTS_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


# ── Entry point ────────────────────────────────────────────────────────────────

def refresh(verbose: bool = True) -> tuple[str, str, str]:
    """Force a full re-introspection, overwriting every cache.

    Use after the database or its schema changes - the cached files are otherwise reused and
    would describe the OLD database. Note this does NOT delete the compiled anomaly catalog:
    the catalog carries its own structure-only fingerprint and decides for itself whether it is
    stale, so a data-only reload never triggers an expensive recompile.
    """
    for path in (_SCHEMA_PATH, _VALUE_HINTS_PATH, _NUMERIC_HINTS_PATH, _FINGERPRINT_PATH):
        if os.path.exists(path):
            os.remove(path)
            if verbose:
                print(f"  removed {os.path.basename(path)}")

    schema = build_schema_text(use_cache=False)
    values = build_value_hints(use_cache=False)
    numbers = build_numeric_hints(use_cache=False)

    if verbose:
        tables = schema.count("\nTABLE ") + (1 if schema.startswith("TABLE ") else 0)
        fks = schema.count("\n  FK: ")
        dups = schema.count("MANY ROWS PER ")
        print(f"  schema.txt        : {tables} tables, {fks} foreign keys, "
              f"{dups} grain warnings, {len(schema):,} chars")
        print(f"  value_hints.txt   : {len(values):,} chars")
        print(f"  numeric_hints.txt : {len(numbers):,} chars")
        if not schema.strip():
            print("  WARNING: empty schema - check DB_NAME / ALLOWED_SCHEMAS in .env")
    return schema, values, numbers


if __name__ == "__main__":
    # Run with:  python -m app.db.introspect      (from the backend/ directory)
    from app.db.connection import ping

    print(
        f"Introspecting {settings.db_name} on {settings.db_server} "
        f"(schemas: {', '.join(settings.allowed_schemas) or '(none set!)'})"
    )
    ok, message = ping()
    if not ok:
        raise SystemExit(f"Cannot connect: {message}")
    print(f"  {message}")
    refresh()
    print("Done.")
