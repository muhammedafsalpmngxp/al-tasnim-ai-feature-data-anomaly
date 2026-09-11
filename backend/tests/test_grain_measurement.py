"""Regression tests for `SchemaSnapshotter.measure_grain` -- three real bugs, each found
by running the previous version live against AlTasnimBI, not by reasoning about it.

  v1 (first-id-column):     called well.task_daily clean at 1.0. Real grain: 3.03.
  v2 (worst-ratio-wins):    flagged well_master.well_type_id as "67.8x duplication" --
                            a low-cardinality DIMENSION, not a repeated business key.
  v3 candidate bug:          wbs.WBS_master has no PK and no `*id`-suffixed column at all
                            (only WBS_Code/Activity_code/...) -- an id-only suffix rule
                            silently produced zero candidates for exactly the kind of
                            table the agent most needs a grain for.

The fix settled on: a DECLARED grain (config) is measured for confirmation, never
re-guessed. An UNDECLARED table gets every candidate PUBLISHED, ranked, with no winner
picked -- because no naming rule can tell a dimension column from a business key.

No live database: a fake source returns canned COUNT(*)/COUNT(DISTINCT) pairs and records
every statement, so each scenario is reproduced exactly rather than approximated.
"""
from __future__ import annotations

import re

from app.sentinel.schema_snapshot import (
    ColumnSnapshot,
    SchemaSnapshotter,
    TableSnapshot,
)
from app.sentinel.scope import TableRef


_BRACKET_TOKEN = re.compile(r"\[([^\]]+)\]")


class FakeSource:
    """`one()` answers a scripted (n, distinct) pair, keyed by the exact tuple of column
    names in the SELECT list (order preserved, duplicates from ISNULL(CAST(...)) collapsed) --
    parsed out of the generated SQL rather than matched by substring, so a composite probe
    can never be confused with one of its own member columns probed alone. Only the part
    BEFORE " FROM " is parsed, so the `[schema].[table]` in the FROM clause never counts as
    a column. `rows()` is unused by measure_grain directly. Recording every call lets a test
    assert not just the RESULT but which -- and how many -- probes actually ran.
    """

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, int]]):
        self.answers = answers  # e.g. {("well_id", "task_code"): (107_484, 35_444)}
        self.calls: list[str] = []

    def one(self, sql: str, params=()):  # noqa: ANN001, ANN201
        self.calls.append(sql)
        select_list = sql.split(" FROM ")[0]
        seen: list[str] = []
        for tok in _BRACKET_TOKEN.findall(select_list):
            if tok not in seen:
                seen.append(tok)
        key = tuple(seen)
        if key in self.answers:
            n, d = self.answers[key]
            return {"n": n, "d": d}
        raise AssertionError(
            f"FakeSource has no scripted answer for columns {key}\nfull SQL:\n{sql}"
        )

    def rows(self, sql: str, params=()):  # noqa: ANN001, ANN201
        return []

    def table_row_counts(self):  # noqa: ANN201
        return {}


class FakeScope:
    large_table_row_limit = 2_000_000


def _snapshot(table: str, row_count: int, columns: dict[str, str], *, pk: tuple = ()) -> TableSnapshot:
    ref = TableRef.parse(table)
    snap = TableSnapshot(ref=ref, row_count=row_count, primary_key=pk)
    for i, (name, dtype) in enumerate(columns.items()):
        snap.columns[name] = ColumnSnapshot(
            name=name, data_type=dtype, is_nullable=True, max_length=0, ordinal=i
        )
    return snap


def _snapshotter(source: FakeSource) -> SchemaSnapshotter:
    s = SchemaSnapshotter.__new__(SchemaSnapshotter)
    s.src = source
    s.scope = FakeScope()
    s._row_counts = {}
    return s


# ------------------------------------------------------------------- declared grain
def test_a_declared_grain_is_measured_not_reguessed():
    """The config-declared grain is authoritative -- it is confirmed by a real COUNT,
    but candidate columns are never even probed."""
    class FakeSpec:
        def semantics_for(self, table):
            class Sem:
                class grain:
                    keys = ["well_id", "task_code"]
            return Sem()

    snap = _snapshot("well.task_daily", 107_484, {"well_id": "int", "task_code": "nvarchar"})
    src = FakeSource({("well_id", "task_code"): (107_484, 35_444)})
    ss = _snapshotter(src)
    ss._row_counts = {"well.task_daily": 107_484}
    ss.measure_grain({"well.task_daily": snap}, spec=FakeSpec())

    assert snap.declared_grain == "well_id, task_code"
    assert snap.declared_rows_per_key == round(107_484 / 35_444, 2)
    assert snap.grain_is_confirmed is True
    assert snap.grain_candidates == ()  # never probed -- config already answered this


# ------------------------------------------------------- v1 regression: first-id-column
def test_the_first_id_column_is_not_the_only_candidate_considered():
    """v1's bug: `well.task_daily` has a surrogate `id` alongside the real business key.
    Probing only the first *id column and stopping would find `id` -> 1.0 and never even
    look at (well_id, task_code) -> 3.03, which is the actual grain.
    """
    snap = _snapshot(
        "well.task_daily", 107_484,
        {"id": "int", "well_id": "int", "task_code": "nvarchar"},
    )
    src = FakeSource({
        ("id", "well_id", "task_code"): (107_484, 107_484),  # 3-way: unique (id alone would be)
        ("id", "well_id"): (107_484, 107_484),                # 2-way with the surrogate: unique
        ("well_id", "task_code"): (107_484, 35_444),          # the REAL key: repeats 3.03x
        ("id",): (107_484, 107_484),                          # surrogate alone: unique
        ("well_id",): (107_484, 500),
        ("task_code",): (107_484, 35_444),
    })
    ss = _snapshotter(src)
    ss._row_counts = {"well.task_daily": 107_484}
    ss.measure_grain({"well.task_daily": snap}, spec=None)

    described = {c.columns: c.rows_per_value for c in snap.grain_candidates}
    assert ("well_id", "task_code") in described, (
        "the real repeating key must be probed and published, not shadowed by a "
        "unique surrogate id column"
    )
    assert described[("well_id", "task_code")] == round(107_484 / 35_444, 2)


# --------------------------------------------------- v2 regression: worst-ratio-wins
def test_a_low_cardinality_dimension_is_not_reported_as_the_grain():
    """v2's bug: picking the candidate with the WORST (highest) ratio flagged
    `well_type_id` (a ~12-value dimension) as "67.8x duplication" on a table that is
    genuinely one row per well. The fix: publish every candidate, ranked closest-to-
    unique FIRST, and let a human or config decide which one is the real key -- never
    pick "the worst" as if high-ratio automatically means "the grain".
    """
    snap = _snapshot(
        "well.well_master", 814, {"well_id": "int", "well_type_id": "int"}
    )
    src = FakeSource({
        ("well_id", "well_type_id"): (814, 814),  # composite: still unique (well_id alone is)
        ("well_id",): (814, 814),                  # the real grain: unique
        ("well_type_id",): (814, 12),               # a dimension: naturally low-cardinality
    })
    ss = _snapshotter(src)
    ss._row_counts = {"well.well_master": 814}
    ss.measure_grain({"well.well_master": snap}, spec=None)

    # Both are published as candidates -- neither is silently dropped or crowned...
    described = {c.columns: c.rows_per_value for c in snap.grain_candidates}
    assert described[("well_id",)] == 1.0
    assert described[("well_type_id",)] == round(814 / 12, 2)
    # ...but the genuinely unique candidate(s) sort ahead of the low-cardinality
    # dimension, so a reader sees the real key before the false lead. (Two candidates
    # that are BOTH fully unique -- here, well_id alone and the well_id+well_type_id
    # composite -- may tie for first; that tie is fine. What must never happen is the
    # dimension, at 67.8x, sorting ahead of either unique one.)
    assert snap.grain_candidates[0].is_unique is True
    assert snap.grain_candidates[-1].columns == ("well_type_id",)


# --------------------------------------------- v3 regression: id-only suffix rule
def test_code_suffixed_columns_are_candidates_when_there_is_no_id_column_at_all():
    """v3's bug: `wbs.WBS_master` has no PK and no column ending in `id` -- only
    WBS_Code/Activity_code/Cluster_code/Plant_Code. An id-only suffix rule produced ZERO
    candidates for this table, silently. Live-corrected count: WBS_Code alone measures
    1.00 (the real key) and is not shadowed by that omission.
    """
    snap = _snapshot(
        "wbs.WBS_master", 81_846,
        {"WBS_Code": "varchar", "Plant_Code": "int", "Cluster_code": "int",
         "Activity_code": "varchar"},
    )
    src = FakeSource({
        ("WBS_Code", "Plant_Code", "Cluster_code", "Activity_code"): (81_846, 81_846),
        ("WBS_Code", "Plant_Code"): (81_846, 81_846),
        ("Plant_Code", "Cluster_code"): (81_846, 70),
        ("Cluster_code", "Activity_code"): (81_846, 142),
        ("WBS_Code",): (81_846, 81_846),
        ("Plant_Code",): (81_846, 7),
    })
    ss = _snapshotter(src)
    ss._row_counts = {"wbs.WBS_master": 81_846}
    ss.measure_grain({"wbs.WBS_master": snap}, spec=None)

    assert snap.grain_candidates, "a *_code-only table must still produce candidates"
    assert ("WBS_Code",) in {c.columns for c in snap.grain_candidates}


# ------------------------------------------------------------------ size / type limits
def test_a_table_over_the_large_row_limit_is_skipped_not_scanned():
    """dbo.activity_task_plan: 17.9M rows. A multi-column COUNT(DISTINCT ...) there
    measured 10.7s live -- unaffordable on every introspect run, so it is skipped and the
    skip is visible (empty candidates), not silently estimated."""
    snap = _snapshot("dbo.activity_task_plan", 17_930_484, {"id": "int"})
    src = FakeSource({})  # must NOT be queried at all
    ss = _snapshotter(src)
    ss._row_counts = {"dbo.activity_task_plan": 17_930_484}
    ss.measure_grain({"dbo.activity_task_plan": snap}, spec=None)

    assert snap.grain_candidates == ()
    assert src.calls == []


def test_an_uncountable_column_type_does_not_crash_the_sweep():
    """dbo.mapping_master: COUNT(DISTINCT ...) on a `text` column raised SQL error 8117
    live. A `text`-typed candidate must be excluded before it is ever probed."""
    snap = _snapshot(
        "dbo.mapping_master", 642, {"row_id": "int", "notes": "text"}
    )
    src = FakeSource({("row_id",): (642, 642)})
    ss = _snapshotter(src)
    ss._row_counts = {"dbo.mapping_master": 642}
    ss.measure_grain({"dbo.mapping_master": snap}, spec=None)  # must not raise

    probed_columns = {c.columns for c in snap.grain_candidates}
    assert ("notes",) not in probed_columns


def test_small_tables_are_not_probed_at_all():
    """Below min_rows, a repeated key cannot distort a figure enough to matter, and
    these are the lookup tables anyway -- so no probe should even run."""
    snap = _snapshot("ref.tiny", 5, {"code": "varchar"})
    src = FakeSource({})  # must NOT be queried
    ss = _snapshotter(src)
    ss._row_counts = {"ref.tiny": 5}
    ss.measure_grain({"ref.tiny": snap}, spec=None, min_rows=100)

    assert snap.grain_candidates == ()
    assert src.calls == []
