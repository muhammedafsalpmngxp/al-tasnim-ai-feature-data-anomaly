"""Read .cache/schema.txt and .cache/numeric_hints.txt back into structured facts.

The generic probe generator needs the schema as DATA, not as a prompt string. Rather than
re-query the database, this parses the artefacts introspection already wrote - so the probes are
generated from exactly the same picture the agents are shown, and the two can never disagree
about what exists.

Everything here is derived at runtime from those two files. No table or column name appears
below; point the app at another database and the index follows it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app.observability import get_logger

log = get_logger()

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
_SCHEMA_PATH = os.path.join(_CACHE_DIR, "schema.txt")
_NUMERIC_PATH = os.path.join(_CACHE_DIR, "numeric_hints.txt")

# "TABLE schema.table"
_TABLE_RE = re.compile(r"^TABLE\s+(\S+)\s*$")
# "  - col type[(n)][ NOT NULL][ PK]" - the name may be [bracketed] when it needs quoting.
_COLUMN_RE = re.compile(
    r"^\s+-\s+(\[[^\]]+\]|\S+)\s+([A-Za-z_]+)(?:\(([^)]*)\))?(\s+NOT NULL)?(\s+PK)?\s*$"
)
# "  FK: child_col -> schema.table.parent_col"
_FK_RE = re.compile(r"^\s+FK:\s+(\S+)\s*->\s*(\S+)\.(\w+)\s*$")
# "  MANY ROWS PER key - ...". The key may be [bracketed] when it needs quoting: this
# database has one literally named "PDO Well ID", and a bare \S+ would capture only "PDO".
_DUP_RE = re.compile(r"MANY ROWS PER\s+(\[[^\]]+\]|\S+)")

# "core.revenue  (21,566 rows)"
_NUM_TABLE_RE = re.compile(r"^(\S+)\s+\(([\d,]+)\s+rows\)\s*$")
# "  - col: min 0, max 0.3, avg 0.01, stdev 0.03, nulls 0% | <scale prose>"
_NUM_COL_RE = re.compile(
    r"^\s+-\s+(\S+):\s+min\s+(\S+?),\s+max\s+(\S+?),\s+avg\s+(\S+?),\s+stdev\s+(\S+?),"
    r"\s+nulls\s+(<?>?\d+)%\s*\|\s*(.+?)\s*$"
)
# The COMPACT form, "  - well_id: 628..37625", written for a column whose full statistics carry
# nothing actionable (see _is_full_detail in db/introspect.py).
#
# ⚠ THIS READER AND THAT WRITER MUST BE CHANGED TOGETHER. Adding the compact form to the writer
# without adding it here left every plain-number column invisible to the index - the table count
# with statistics fell from 70 to 13 and nothing failed, because the ratio columns that the
# generic probes actually need happened to still match the full pattern. A silent two-thirds
# loss of the index is exactly the kind of regression this engine exists to catch in other
# people's data, so eval/test_rules.py now asserts the two formats round-trip.
_NUM_COMPACT_RE = re.compile(r"^\s+-\s+(\S+):\s+(-?[\d.eE+]+)\.\.(-?[\d.eE+]+)\s*$")
# "  - col (text): 183 of 642 values do NOT parse ..."
_TXT_BAD_RE = re.compile(r"^\s+-\s+(\S+)\s+\(text\):\s+(\d+)\s+of\s+(\d+)\s+values do NOT parse")
# "  - col (text): only 3 of 997 values parse as a number (0%) - ... NOT a quantity"
#
# ⚠ THIS PATTERN AND THE WRITER IN db/introspect.py MUST CHANGE TOGETHER. The reader silently
# ignores a line it cannot match, so a reworded verdict does not fail - it makes every affected
# column vanish from the index, and the probes over them disappear with it. eval/test_rules.py
# round-trips both forms for exactly this reason.
_TXT_NONE_RE = re.compile(
    r"^\s+-\s+(\S+)\s+\(text\):\s+only\s+\d+\s+of\s+(\d+)\s+values parse"
)

_DATE_TYPES = frozenset(("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"))


def _bare(name: str) -> str:
    """Strip the display brackets schema.txt adds to a name that needs quoting."""
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# Types SQL Server cannot compare, sort or group. A probe keyed on one of these is
# syntactically valid and fails at execution every time, so they are never an entity key.
_UNSORTABLE = re.compile(r"^\s*(text|ntext|image)\s*$", re.IGNORECASE)


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True
    is_pk: bool = False

    @property
    def is_date(self) -> bool:
        return self.data_type.lower() in _DATE_TYPES


@dataclass
class ForeignKey:
    child_column: str
    parent_table: str
    parent_column: str


@dataclass
class NumericStat:
    """What was MEASURED for one column. `scale` is the classifier's prose verdict."""

    lo: float | None = None
    hi: float | None = None
    avg: float | None = None
    stdev: float | None = None
    null_pct: int = 0
    scale: str = ""
    # Text columns only: how many values fail to parse as a number, out of how many non-blank.
    text_bad: int = 0
    text_total: int = 0
    # True when NOTHING parses - the column holds codes, and the name-based guess that it holds
    # a quantity was simply wrong. Such a column must never get a range or cast probe.
    text_is_codes: bool = False

    @property
    def is_ratio(self) -> bool:
        """A proportion of some whole, per the classifier - the only kind with natural bounds."""
        s = self.scale.upper()
        return "FRACTION_1" in s or "PERCENT_100" in s or s.startswith("PROPORTION")

    def bounds(self) -> tuple[float, float] | None:
        """The range this column should occupy, derived from what it actually holds.

        Chosen on the AVERAGE, not the maximum. The maximum is precisely the value most likely
        to be corrupt - picking bounds from it would let one bad row widen the range until that
        row looks legal, which is the opposite of what a range check is for. A column averaging
        0.26 is a 0-1 fraction even when a stray row reads 66.7; that row is the finding.
        """
        if not self.is_ratio or self.avg is None:
            return None
        if self.avg <= 1.0:
            return (0.0, 1.0)
        if self.avg <= 100.0:
            return (0.0, 100.0)
        return None


@dataclass
class Table:
    name: str  # schema.table
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    duplicate_key: str | None = None
    row_count: int | None = None
    stats: dict[str, NumericStat] = field(default_factory=dict)

    @property
    def schema(self) -> str:
        return self.name.partition(".")[0]

    @property
    def pk_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_pk]

    @property
    def date_columns(self) -> list[Column]:
        return [c for c in self.columns if c.is_date]

    def column(self, name: str) -> Column | None:
        lower = name.lower()
        return next((c for c in self.columns if c.name.lower() == lower), None)

    def entity_column(self) -> str | None:
        """A stable per-row identifier for `entity_key`, so a finding names something real.

        Preference order: a single-column primary key, then the first *_id column, then the
        first NOT NULL column. Returns None when a table offers nothing usable - better to skip
        generating a probe than to emit findings nobody can trace back to a record.

        A `text`, `ntext` or `image` column is never usable here, whatever its name. SQL Server
        cannot compare or sort those types at all, and the probe contract requires DETAIL to be
        ordered - so a probe keyed on one parses, validates, is approved by the reviewer, and
        then fails at the database every single run. Observed on dbo.mapping_master, whose
        Activity_ID is `text`: it was chosen for ending in "_id", and the probe never ran.
        """
        usable = [c for c in self.columns if not _UNSORTABLE.match(c.data_type or "")]
        pks = [p for p in self.pk_columns if any(c.name == p for c in usable)]
        if len(pks) == 1:
            return pks[0]
        # A bare "id" is checked BEFORE the *_id suffix: "id".endswith("_id") is False, so a
        # table whose row identity is plainly `id` would otherwise fall through to the first
        # FOREIGN key that happens to end in _id - labelling every finding with somebody
        # else's identifier.
        for c in usable:
            if c.name.lower() == "id":
                return c.name
        for c in usable:
            if c.name.lower().endswith("_id"):
                return c.name
        for c in usable:
            if not c.nullable:
                return c.name
        return usable[0].name if usable else None


class SchemaIndex:
    """Every structural fact the generic probes are built from."""

    def __init__(self, tables: dict[str, Table]) -> None:
        self.tables = tables

    def __len__(self) -> int:
        return len(self.tables)

    def get(self, name: str) -> Table | None:
        return self.tables.get(name)

    @property
    def foreign_keys(self) -> list[tuple[Table, ForeignKey]]:
        return [(t, fk) for t in self.tables.values() for fk in t.foreign_keys]

    @property
    def duplicate_keys(self) -> list[tuple[Table, str]]:
        return [(t, t.duplicate_key) for t in self.tables.values() if t.duplicate_key]


def _parse_schema(text: str) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    current: Table | None = None
    for line in text.splitlines():
        m = _TABLE_RE.match(line)
        if m:
            current = Table(name=m.group(1))
            tables[current.name] = current
            continue
        if current is None:
            continue

        m = _COLUMN_RE.match(line)
        if m:
            current.columns.append(
                Column(
                    name=_bare(m.group(1)),
                    data_type=m.group(2),
                    nullable=not m.group(4),
                    is_pk=bool(m.group(5)),
                )
            )
            continue

        m = _FK_RE.match(line)
        if m:
            current.foreign_keys.append(
                ForeignKey(
                    child_column=_bare(m.group(1)),
                    parent_table=m.group(2),
                    parent_column=m.group(3),
                )
            )
            continue

        m = _DUP_RE.search(line)
        if m:
            current.duplicate_key = _bare(m.group(1))
    return tables


def _parse_numeric(text: str, tables: dict[str, Table]) -> None:
    """Fold measured statistics onto the tables, in place.

    A table absent from numeric_hints.txt simply has no stats - it was empty, too large to
    profile, or had no numeric columns. Callers must treat "no stats" as "scale unknown" and
    generate nothing, never as a default.
    """
    current: Table | None = None
    for line in text.splitlines():
        m = _NUM_TABLE_RE.match(line)
        if m:
            current = tables.get(m.group(1))
            if current is not None:
                current.row_count = int(m.group(2).replace(",", ""))
            continue
        if current is None:
            continue

        m = _NUM_COL_RE.match(line)
        if m:
            # "<1%" / ">99%" are emitted where rounding would otherwise claim 0% or 100% and
            # contradict the statistics beside it, so strip the marker before converting.
            current.stats[m.group(1)] = NumericStat(
                lo=_num(m.group(2)), hi=_num(m.group(3)),
                avg=_num(m.group(4)), stdev=_num(m.group(5)),
                null_pct=int(m.group(6).lstrip("<>")), scale=m.group(7),
            )
            continue

        m = _NUM_COMPACT_RE.match(line)
        if m:
            # Range only. `scale` stays empty, so is_ratio is False and bounds() returns None -
            # which is correct: the writer chose the compact form precisely because this column
            # has no meaningful scale, and inventing one here would be worse than having none.
            current.stats[m.group(1)] = NumericStat(lo=_num(m.group(2)), hi=_num(m.group(3)))
            continue

        m = _TXT_BAD_RE.match(line)
        if m:
            current.stats[m.group(1)] = NumericStat(
                text_bad=int(m.group(2)), text_total=int(m.group(3))
            )
            continue

        m = _TXT_NONE_RE.match(line)
        if m:
            total = int(m.group(2))
            current.stats[m.group(1)] = NumericStat(
                text_bad=total, text_total=total, text_is_codes=True
            )


def load_index(schema_text: str | None = None, numeric_text: str | None = None) -> SchemaIndex:
    """Build the index from the cached artefacts (or from text supplied directly, for tests).

    Raises FileNotFoundError when schema.txt is absent: generating probes against no schema
    would silently produce nothing, and a silent empty result is the failure mode this whole
    engine exists to prevent.
    """
    if schema_text is None:
        if not os.path.exists(_SCHEMA_PATH):
            raise FileNotFoundError(
                "schema.txt not found. Run: python -m app.cli introspect"
            )
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            schema_text = fh.read()

    if numeric_text is None and os.path.exists(_NUMERIC_PATH):
        with open(_NUMERIC_PATH, encoding="utf-8") as fh:
            numeric_text = fh.read()

    tables = _parse_schema(schema_text)
    if numeric_text:
        _parse_numeric(numeric_text, tables)
    else:
        log.warning(
            "schema index: numeric_hints.txt is missing - range and cast probes cannot be "
            "generated, because the scale of every numeric column is unknown"
        )

    log.info(
        "schema index: %d tables, %d foreign keys, %d grain markers, %d tables with statistics",
        len(tables),
        sum(len(t.foreign_keys) for t in tables.values()),
        sum(1 for t in tables.values() if t.duplicate_key),
        sum(1 for t in tables.values() if t.stats),
    )
    return SchemaIndex(tables)
