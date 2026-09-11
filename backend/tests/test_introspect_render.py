"""Tests for `app/sentinel/introspect.py` -- the compile-time agent's knowledge base
(schema.txt / hint_data.txt / fingerprints.json).

Two of these are regressions for bugs found the first time this was actually run against
AlTasnimBI, not from reasoning about the code:

  * `well.task_daily` disappeared from hint_data.txt ENTIRELY, with no error surfaced
    short of reading the log -- its `time_stamp` column is `datetimeoffset` (ODBC type
    -155), and a bare MIN()/MAX() on it crashed the whole table's profiling query. Same
    crash this project already fixed once in generator.py/normaliser.py; introspect.py
    had reintroduced it.
  * `dbo.activity_task_plan.supervisor_email` leaked into hint_data.txt on the first real
    run -- not a bug in the PII filter itself, but in the PII *declaration*: three sibling
    P6-family tables had this column declared as PII, a fourth did not. Covered by the
    `column_semantics.yaml` fix separately; the test here locks in that the FILTER
    correctly hides whatever `pii.is_pii()` reports, so a future declaration gap fails
    loudly here rather than silently in the rendered file.

No live database: fakes stand in for SourceDatabase/Scope, matching the same doubles
pattern used in test_grain_measurement.py.
"""
from __future__ import annotations

import re

from app.sentinel.introspect import (
    ColumnProfile,
    KnowledgeBase,
    TableProfile,
    render_hint_data,
    render_schema_text,
)
from app.sentinel.schema_snapshot import ColumnSnapshot, ForeignKeyRef, GrainCandidate, TableSnapshot
from app.sentinel.scope import TableRef

_BRACKET = re.compile(r"\[([^\]]+)\]")


def _snap(table: str, rows: int, columns: dict[str, str], **kw) -> TableSnapshot:
    ref = TableRef.parse(table)
    snap = TableSnapshot(ref=ref, row_count=rows, **kw)
    for i, (name, dtype) in enumerate(columns.items()):
        snap.columns[name] = ColumnSnapshot(
            name=name, data_type=dtype, is_nullable=True, max_length=0, ordinal=i
        )
    return snap


# ------------------------------------------------------------------- render_schema_text
def test_declared_grain_line_states_the_measured_ratio():
    snap = _snap("well.task_daily", 107_484, {"well_id": "int", "task_code": "nvarchar"})
    snap.declared_grain = "well_id, task_code"
    snap.declared_rows_per_key = 3.03
    text = render_schema_text({"well.task_daily": snap})
    assert "GRAIN (declared, confirmed): well_id, task_code -- measured 3.03 rows/key" in text


def test_unconfirmed_candidates_are_listed_not_chosen():
    snap = _snap("wbs.WBS_master", 81_846, {"WBS_Code": "varchar", "Plant_Code": "int"})
    snap.grain_candidates = (
        GrainCandidate(columns=("WBS_Code",), rows_per_value=1.0),
        GrainCandidate(columns=("Plant_Code",), rows_per_value=11692.29),
    )
    text = render_schema_text({"wbs.WBS_master": snap})
    assert "GRAIN: UNCONFIRMED" in text
    assert "WBS_Code -> 1.0 rows/value" in text
    assert "Plant_Code -> 11692.29 rows/value  <- MANY ROWS PER THIS KEY" in text
    # the unique one must not ALSO be flagged as "many rows per key"
    assert "WBS_Code -> 1.0 rows/value  <- MANY ROWS PER THIS KEY" not in text


def test_primary_key_column_is_marked():
    snap = _snap("bridge.crew_employee", 10_302, {"pk_id": "int"}, primary_key=("pk_id",))
    text = render_schema_text({"bridge.crew_employee": snap})
    assert "- pk_id int NULL PK" in text


def test_nullable_foreign_key_is_flagged_as_possibly_unlinked():
    snap = _snap("well.task_daily", 100, {"crew_type_id": "int"})
    snap.foreign_keys = (
        ForeignKeyRef(column="crew_type_id", target_table="ref.crew_type",
                     target_column="crew_type_id", nullable=True),
    )
    text = render_schema_text({"well.task_daily": snap})
    assert "FK: crew_type_id -> ref.crew_type.crew_type_id (nullable" in text


def test_a_table_with_neither_declared_nor_candidate_grain_states_neither():
    """An empty/skipped-large table has nothing to say about grain -- the renderer
    must not fabricate a line for it."""
    snap = _snap("dbo.activity_task_plan", 17_930_484, {"id": "int"})
    text = render_schema_text({"dbo.activity_task_plan": snap})
    assert "GRAIN" not in text


# --------------------------------------------------------------------- render_hint_data
def test_hint_data_formats_null_distinct_and_range():
    profile = TableProfile(
        row_count=159,
        columns={"crew_type_id": ColumnProfile(null_pct=0.0, distinct=159,
                                               min_value="118", max_value="344")},
    )
    text = render_hint_data({}, {"ref.crew_type": profile})
    assert "crew_type_id: 0.0% null · 159 distinct · range [118 .. 344]" in text


def test_hint_data_includes_lookup_values_line_only_when_present():
    with_values = TableProfile(row_count=5, columns={}, lookup_values=["MWS0602", "MWS0603"])
    without = TableProfile(row_count=50_000, columns={})
    text = render_hint_data({}, {"ref.crew_type": with_values, "well.well_master": without})
    assert "VALUES: MWS0602; MWS0603" in text
    # a table with no lookup_values must not print an empty "VALUES:" line
    well_block = text.split("TABLE well.well_master")[1]
    assert "VALUES:" not in well_block


# --------------------------------------------------- regression: datetimeoffset crash
def test_a_datetimeoffset_column_uses_convert_not_a_bare_min_max():
    """Found live: well.task_daily.time_stamp is `datetimeoffset` (ODBC type -155). A
    bare MIN()/MAX() on it crashed the ENTIRE table's profile query, and the table
    silently disappeared from hint_data.txt with nothing but a log line to explain why.
    """
    class RecordingSource:
        def one(self, sql, params=()):
            self.sql = sql
            return {"__n": 1, "time_stamp__null": 1}

    class SmallScope:
        large_table_row_limit = 2_000_000

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.src = RecordingSource()
    kb.scope = SmallScope()
    snap = _snap("well.task_daily", 1, {"time_stamp": "datetimeoffset"})

    kb._profile_one(snap, ["time_stamp"])

    assert "CONVERT(varchar(30), MIN([time_stamp]), 121)" in kb.src.sql
    assert "CONVERT(varchar(30), MAX([time_stamp]), 121)" in kb.src.sql
    assert "MIN([time_stamp]))" not in kb.src.sql.replace("CONVERT(varchar(30), MIN([time_stamp]), 121))", "")


def test_uncountable_and_skip_distinct_columns_never_reach_the_select_list():
    """A `text` column must never appear in a COUNT(DISTINCT ...), and a table over the
    large-row threshold must skip DISTINCT/MIN/MAX for every column, not just some."""
    class RecordingSource:
        def one(self, sql, params=()):
            self.sql = sql
            return {"__n": 10, "notes__null": 0, "big__null": 0}

    class HugeScope:
        large_table_row_limit = 2_000_000

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.src = RecordingSource()
    kb.scope = HugeScope()
    snap = _snap("dbo.mapping_master", 3_000_000, {"notes": "text", "big": "int"})

    kb._profile_one(snap, ["notes", "big"])

    assert "DISTINCT" not in kb.src.sql  # skip_distinct kicked in for the whole table


# ------------------------------------------------------------------ regression: PII
def test_visible_columns_excludes_whatever_pii_reports_regardless_of_table():
    """The filter itself must hide anything `pii.is_pii()` says is PII -- this is what
    makes a future declaration gap (like the one found live on
    dbo.activity_task_plan.supervisor_email) fail here first, rather than only in the
    rendered file after a human happens to grep for it.
    """
    from app.sentinel import pii

    class AlwaysScope:
        def column_allowed(self, schema, table, column):
            return True

    class FakeSpecPiiEverything:
        def is_pii(self, table, column):
            return column == "supervisor_email"

    pii._spec.cache_clear()
    try:
        import app.sentinel.introspect as introspect_mod

        original = introspect_mod.pii.is_pii
        introspect_mod.pii.is_pii = FakeSpecPiiEverything().is_pii
        try:
            kb = KnowledgeBase.__new__(KnowledgeBase)
            kb.scope = AlwaysScope()
            snap = _snap(
                "dbo.activity_task_plan", 100,
                {"well_id": "int", "supervisor_email": "nvarchar"},
            )
            visible = kb._visible_columns(snap)
            assert "supervisor_email" not in visible
            assert "well_id" in visible
        finally:
            introspect_mod.pii.is_pii = original
    finally:
        pii._spec.cache_clear()
