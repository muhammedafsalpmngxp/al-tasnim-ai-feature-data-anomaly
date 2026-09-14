"""Enumerate the schema features a family rule applies to. No SQL, no LLM, no naming guesses.

WHAT THIS FILE IS FOR
---------------------
A rule in data_anomalies.md that declares `expands_over: foreign_key` is written once and
applied to every foreign key the database declares. Something has to walk the schema and
produce that list - "here are the 36 foreign keys, with their child and parent columns" - and
that is the whole job of this module.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not hold SQL. generic.py, which this replaces, carried six SQL templates rendered from
domain/generic_probes.md; under the new design the query is written by the SQL Author from the
rule's prose, once per family, and substituted across the features listed here. Keeping SQL out
of Python is also an invariant the test suite enforces.

It does not guess from column NAMES. That distinction matters more than it sounds: every
feature below is something the database STATES (a declared foreign key, a primary key, a
measured scale, a grain marker) rather than something inferred from spelling. The one place
the old code did infer from spelling - deciding which date columns hold actuals - is left
unimplemented here on purpose. See `future_date` below.

A FEATURE IS A SET OF PLACEHOLDER VALUES. The SQL Author writes one query using the placeholder
names listed for each feature, and it is then substituted per feature. So `{{child_table}}`
means whatever this particular foreign key's child table is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from app.observability import get_logger
from app.rules.schema_index import SchemaIndex, Table, load_index
from app.rules.spec import AnomalyRule

log = get_logger()

# Every feature kind a rule may expand over, and the placeholders it supplies. The SQL Author is
# shown this list for the family it is writing, so it knows exactly which tokens it may use.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "foreign_key": ("child_table", "child_column", "parent_table", "parent_column",
                    "entity_column"),
    # No entity_column: the duplicated key IS the identity of the thing being reported, so the
    # query names the group rather than a row inside it.
    "duplicate_key": ("table", "key_column"),
    "date_pair": ("table", "start_column", "end_column", "entity_column"),
    "future_date": ("table", "column", "entity_column"),
    "numeric_range": ("table", "column", "lower_bound", "upper_bound", "entity_column"),
    "text_numeric": ("table", "column", "entity_column"),
}

# Start/end suffix pairs, longest first so "_start_date"/"_end_date" wins over "_start"/"_end"
# (which would leave a stem ending in "_date" and match nothing).
#
# This IS a naming convention, and therefore a weaker signal than the rest of this module. It is
# kept because pairing two date columns is not something the schema declares anywhere, and
# because it fails SAFELY: an unrecognised convention yields no pair and therefore no probe - a
# visible gap in coverage - rather than a wrong pair and a wrong finding. Every one of the 15
# pairs it currently produces was checked by hand and is correct.
_DATE_PAIRS: tuple[tuple[str, str], ...] = (
    ("_start_date", "_end_date"),
    ("_start_date", "_finish_date"),
    ("_on_date", "_off_date"),
    ("_start", "_finish"),
    ("_start", "_end"),
    ("_from", "_to"),
    ("_on", "_off"),
)
_DATE_PREFIX_PAIRS: tuple[tuple[str, str], ...] = (("start", "end"), ("start", "finish"))

# {{token}} as the SQL Author is told to write it.
_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class Feature:
    """One place in this schema where a family rule applies.

    `values` are substituted into the authored SQL; `label` is what the report shows, so a
    finding reads "GEN-FK-004 - well.task_daily.crew_id -> ref.crew_type" rather than an
    opaque number.
    """

    values: dict[str, str] = field(default_factory=dict)
    label: str = ""


def _escape(name: str) -> str:
    """A column name as it is substituted into `[{{placeholder}}]`.

    The authored SQL brackets its placeholders, so the BARE name goes in. Doubling a ']' is
    T-SQL's own escape - several columns here really are spelled `[PDO Well ID]` - and a name
    containing one would otherwise break out of the brackets.
    """
    return str(name).replace("]", "]]")


def _date_pairs(table: Table) -> list[tuple[str, str]]:
    """Start/end date columns that genuinely belong together, by naming convention."""
    names = {c.name.lower(): c.name for c in table.date_columns}
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    for lower, actual in sorted(names.items()):
        if lower in used:
            continue
        for a_suf, b_suf in _DATE_PAIRS:
            if not lower.endswith(a_suf):
                continue
            partner = lower[: -len(a_suf)] + b_suf
            if partner in names and partner not in used:
                pairs.append((actual, names[partner]))
                used.update({lower, partner})
                break
        else:
            for a_pre, b_pre in _DATE_PREFIX_PAIRS:
                if lower.startswith(a_pre):
                    partner = b_pre + lower[len(a_pre):]
                    if partner in names and partner not in used:
                        pairs.append((actual, names[partner]))
                        used.update({lower, partner})
                        break
    return pairs


def features_for(kind: str, index: SchemaIndex | None = None) -> list[Feature]:
    """Every place in this schema where a family rule of `kind` applies."""
    if kind not in PLACEHOLDERS:
        log.warning("expand: unknown feature kind '%s' - no probes", kind)
        return []
    if index is None:
        index = load_index()
    out: list[Feature] = []

    if kind == "foreign_key":
        for table, fk in index.foreign_keys:
            entity = table.entity_column()
            if not entity or index.get(fk.parent_table) is None:
                continue
            out.append(Feature(
                values={
                    "child_table": table.name, "child_column": _escape(fk.child_column),
                    "parent_table": fk.parent_table,
                    "parent_column": _escape(fk.parent_column),
                    "entity_column": _escape(entity),
                },
                label=f"{table.name}.{fk.child_column} -> {fk.parent_table}",
            ))

    elif kind == "duplicate_key":
        for table, key in index.duplicate_keys:
            if table.column(key) is None:
                # The marker names a column the schema does not list, so the grain line was
                # parsed wrongly. A probe on it would fail at the database with a confusing
                # "invalid column"; skipping it is honest.
                log.warning("expand: grain key %s.%s not found - skipped", table.name, key)
                continue
            out.append(Feature(
                values={"table": table.name, "key_column": _escape(key)},
                label=f"{table.name} per {key}",
            ))

    elif kind == "date_pair":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for start, end in _date_pairs(table):
                out.append(Feature(
                    values={"table": table.name, "start_column": _escape(start),
                            "end_column": _escape(end), "entity_column": _escape(entity)},
                    label=f"{table.name}.{start} > {end}",
                ))

    elif kind == "future_date":
        # NOT IMPLEMENTED, and the empty list is the point.
        #
        # This family needs to know which date columns record an OUTCOME rather than an
        # intention, and the schema does not say: both are `date`. The old code guessed from
        # the column name against a hardcoded English word-list, and got it wrong at scale -
        # `well.task_daily.endDate` was read as an actual and reported 9,198 records as
        # defects, while sitting beside an `actual_end` column measured at 0.00% future values.
        #
        # Rather than carry that guess forward, this returns nothing until the actual-date
        # columns are DECLARED - the natural home being the column dictionary in
        # business_rules.md §2, which already exists to state what a column means and which
        # outranks any inference. Until then DQ-G04 stays draft and the gap is visible here,
        # in code, instead of being hidden inside a word-list that looks authoritative.
        log.info(
            "expand: future_date has no declared source of actual-date columns yet - no "
            "probes. Declare them in business_rules.md rather than inferring from names."
        )

    elif kind == "numeric_range":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for column, stat in table.stats.items():
                bounds = stat.bounds()
                if bounds is None or table.column(column) is None:
                    continue
                low, high = bounds
                out.append(Feature(
                    values={"table": table.name, "column": _escape(column),
                            "entity_column": _escape(entity),
                            "lower_bound": f"{low:g}", "upper_bound": f"{high:g}"},
                    label=f"{table.name}.{column} outside {low:g}..{high:g}",
                ))

    elif kind == "text_numeric":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for column, stat in table.stats.items():
                # Only columns where SOME values parse. Where none do, the column holds codes
                # and a probe would report 100% of the table as anomalous - noise, not a
                # finding.
                if stat.text_is_codes or not stat.text_bad or table.column(column) is None:
                    continue
                out.append(Feature(
                    values={"table": table.name, "column": _escape(column),
                            "entity_column": _escape(entity)},
                    label=f"{table.name}.{column} ({stat.text_bad}/{stat.text_total} unparseable)",
                ))

    log.info("expand: %-14s %3d feature(s)", kind, len(out))
    return out


def substitute(sql: str, values: dict[str, str]) -> str:
    """Fill `{{token}}` placeholders from one feature's values.

    An UNKNOWN token is left exactly as it is rather than blanked. It then fails the safety
    gate as an obvious literal `{{...}}`, which names the problem; blanking it would produce
    syntactically valid SQL reading `FROM  ` or `[]`, and the database error that followed
    would point at a comma instead of at the token nobody supplied.
    """
    def replace_token(match: re.Match) -> str:
        return values.get(match.group(1).lower(), match.group(0))

    return _TOKEN.sub(replace_token, sql or "")


def unfilled_tokens(sql: str) -> list[str]:
    """Tokens still present after substitution - the check that a template was fully applied."""
    return sorted({m.group(1) for m in _TOKEN.finditer(sql or "")})


def expand_families(
    rules: list[AnomalyRule], index: SchemaIndex | None = None
) -> tuple[list[AnomalyRule], list[str]]:
    """Turn every family rule into one concrete rule per matching schema feature.

    Returns (rules with families replaced by their expansions, notes). The family rule itself
    is NEVER returned: it is a description, not a probe, and compiling it would ask an agent to
    write one query for thirty-six different foreign keys.

    EXPANDS AT LOAD TIME, NOT COMPILE TIME, and that is a deliberate choice. Everything
    downstream - the catalog, the pruning of probes whose rule has gone, the API's rule list,
    the report's titles - works from "the set of rules that exist". Expanding later would make
    thirty-six probes that no rule accounts for, and the first full compile would prune every
    one of them as orphaned.
    """
    if index is None:
        index = load_index()

    out: list[AnomalyRule] = []
    notes: list[str] = []
    for rule in rules:
        if not rule.is_family:
            out.append(rule)
            continue

        features = features_for(rule.expands_over, index)
        if not features:
            notes.append(
                f"{rule.rule_id} expands over {rule.expands_over}, which matched nothing in "
                "this schema - no probe was created for it"
            )
            continue

        for n, feature in enumerate(features, 1):
            out.append(replace(
                rule,
                rule_id=f"{rule.rule_id}-{n:03d}",
                title=f"{rule.title}: {feature.label}" if feature.label else rule.title,
                family_id=rule.rule_id,
                source="expanded",
                sql_mode="authored",
                params={**rule.params, **feature.values},
                # The feature is part of this probe's identity, so a foreign key repointed at a
                # different column invalidates ITS probe and leaves the other thirty-five
                # alone. Sorted so an incidental reordering is not mistaken for a change.
                raw=rule.raw + "\n" + "\n".join(
                    f"{k}={v}" for k, v in sorted(feature.values.items())
                ),
            ))
        log.info(
            "expand: %s -> %d probe(s) over %s", rule.rule_id, len(features), rule.expands_over
        )
    return out, notes


def coverage_summary(kinds: list[str]) -> str:
    """One line per family, telling the SQL Author what the structural rules already cover.

    Without this an authored rule can restate a structural one and the same record is reported
    twice, under two ids, as though they were separate problems.
    """
    if not kinds:
        return ""
    lines = [
        "ALREADY COVERED by the structural rules - do NOT write a rule that re-expresses any "
        "of these; they are applied across the whole schema and reported separately:"
    ]
    lines += [f"- {kind.replace('_', ' ')}" for kind in sorted(set(kinds))]
    return "\n".join(lines)
