"""Render domain/generic_probes.md against the live schema. No LLM anywhere in this file.

One template becomes as many probes as the schema has matching features: one per declared
foreign key, one per grain marker, one per date pair, and so on. On this database that is over
a hundred working probes for zero tokens.

WHY THESE ARE DETERMINISTIC AND THE DECLARED RULES ARE NOT
----------------------------------------------------------
A generic probe asks a question with a single correct answer that the schema itself already
states: this column declares a foreign key, so a value with no parent is wrong. There is no
business judgement in it, so there is nothing for a model to add - and a model asked to write
it would only introduce a chance of getting it wrong.

The declared rules in data_anomalies.md are the opposite: what counts as an anomaly is a
business decision, expressed in prose, and resolving that against a live schema is exactly what
the agents are for. That is the division of labour - not a shortcut.

The set of things these cover is handed to the SQL Author as "already covered - do not
re-express", which is how "sequenced after the deterministic rules, not duplicating them" is
enforced mechanically rather than by hoping a prompt behaves.
"""
from __future__ import annotations

import os
import re

from app.observability import get_logger
from app.rules.schema_index import SchemaIndex, Table, load_index
from app.rules.spec import AnomalyRule
from app.utils import extract_sql_blocks

log = get_logger()

_DOMAIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "domain"
)
_TEMPLATE_FILE = os.path.join(_DOMAIN_DIR, "generic_probes.md")

_TEMPLATE_HEADING = re.compile(
    r"^##\s+TEMPLATE\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*[—–:-]\s*(.+?)\s*$", re.MULTILINE
)
_META_LINE = re.compile(r"^\s*[-*]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Date-column pairings, longest suffix first so "_start_date"/"_end_date" is preferred over the
# shorter "_start"/"_end" (which would leave a stem ending in "_date" and match nothing).
_DATE_PAIRS: tuple[tuple[str, str], ...] = (
    ("_start_date", "_end_date"),
    ("_start_date", "_finish_date"),
    ("_on_date", "_off_date"),
    ("_start", "_finish"),
    ("_start", "_end"),
    ("_from", "_to"),
    ("_on", "_off"),
)
# Prefix form, for camelCase names like startDate / endDate.
_DATE_PREFIX_PAIRS: tuple[tuple[str, str], ...] = (("start", "end"), ("start", "finish"))

# A date column recording something PLANNED is supposed to be in the future, so it is excluded
# from the future-date probe. Matching on the name is a heuristic and will occasionally be
# wrong in both directions - which is why triage exists.
_PLANNED_MARKERS = (
    "ex_", "expect", "target", "planned", "plan_", "committed", "forecast", "earl_",
    "_req_", "req_date", "due", "next_", "estimate", "sched",
)


class TemplateError(Exception):
    """A generic template that cannot be used."""


class Template:
    def __init__(self, family: str, title: str, meta: dict[str, str],
                 body: str, summary_sql: str, detail_sql: str) -> None:
        self.family = family
        self.title = title
        self.meta = meta
        self.body = body
        self.summary_sql = summary_sql
        self.detail_sql = detail_sql
        self.applies_to = meta.get("applies_to", "")

    def render(self, rule_id: str, values: dict[str, str]) -> tuple[str, str]:
        """Substitute placeholders into both queries.

        An unresolved placeholder raises rather than rendering a query containing a literal
        "{{...}}": that would reach the database as a syntax error whose message points at the
        braces instead of at the template that forgot to supply the value.
        """
        subs = dict(values)
        subs["rule_id"] = rule_id

        def one(sql: str, which: str) -> str:
            missing: list[str] = []

            def repl(m: re.Match) -> str:
                name = m.group(1).lower()
                if name not in subs:
                    missing.append(name)
                    return m.group(0)
                return subs[name]

            out = _PLACEHOLDER.sub(repl, sql)
            if missing:
                raise TemplateError(
                    f"{self.family} {which}: no value for {{{{{'}}, {{'.join(sorted(set(missing)))}}}}}"
                )
            return out

        return one(self.summary_sql, "summary"), one(self.detail_sql, "detail")


def load_templates() -> list[Template]:
    if not os.path.exists(_TEMPLATE_FILE):
        log.warning("generic: %s not found - no generic probes", _TEMPLATE_FILE)
        return []
    with open(_TEMPLATE_FILE, encoding="utf-8") as fh:
        text = fh.read()

    out: list[Template] = []
    matches = list(_TEMPLATE_HEADING.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start():end]
        lines = block.splitlines()[1:]

        meta: dict[str, str] = {}
        j = 0
        while j < len(lines):
            if not lines[j].strip():
                j += 1
                continue
            mm = _META_LINE.match(lines[j])
            if not mm:
                break
            meta[mm.group(1).lower()] = mm.group(2).strip()
            j += 1
        body = "\n".join(lines[j:])

        blocks = extract_sql_blocks(body)
        if not blocks.get("summary") or not blocks.get("detail"):
            log.warning("generic: template %s has no summary/detail pair - skipped", m.group(1))
            continue
        prose = re.sub(r"```[ \t]*sql[^\n]*\n.*?```", "", body,
                       flags=re.IGNORECASE | re.DOTALL).strip()
        out.append(
            Template(m.group(1), m.group(2).strip(), meta, prose,
                     blocks["summary"], blocks["detail"])
        )
    log.info("generic: %d template(s) loaded", len(out))
    return out


# ── Feature extraction: what in THIS schema each template applies to ────────────

def _q(name: str) -> str:
    """The column spelled for substitution into `[{{placeholder}}]` in a template.

    The templates already wrap every column placeholder in brackets, so the BARE name goes in.
    A name that itself contains a ']' would break out of the bracket; doubling it is the T-SQL
    escape, and it costs nothing to be correct here.
    """
    return name.replace("]", "]]")


def _date_pairs(table: Table) -> list[tuple[str, str]]:
    """Start/end date columns that genuinely belong together, by name."""
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


def _actual_date_columns(table: Table) -> list[str]:
    return [
        c.name for c in table.date_columns
        if not any(m in c.name.lower() for m in _PLANNED_MARKERS)
    ]


def _features(index: SchemaIndex, applies_to: str) -> list[dict[str, str]]:
    """Every place in this schema where one template applies, as placeholder values."""
    out: list[dict[str, str]] = []

    if applies_to == "foreign_key":
        for table, fk in index.foreign_keys:
            entity = table.entity_column()
            parent = index.get(fk.parent_table)
            if not entity or parent is None:
                continue
            out.append({
                "child_table": table.name, "child_column": _q(fk.child_column),
                "parent_table": fk.parent_table, "parent_column": _q(fk.parent_column),
                "entity_column": _q(entity),
                "_label": f"{table.name}.{fk.child_column} -> {fk.parent_table}",
            })

    elif applies_to == "duplicate_key":
        for table, key in index.duplicate_keys:
            if table.column(key) is None:
                # The marker names a column the schema does not list, which means the grain
                # line was parsed wrongly. Generating a probe on it would fail at the database
                # with a confusing "invalid column" error; skipping it is honest.
                log.warning("generic: grain key %s.%s not found - skipped", table.name, key)
                continue
            out.append({
                "table": table.name, "key_column": _q(key),
                "_label": f"{table.name} per {key}",
            })

    elif applies_to == "date_pair":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for start, end in _date_pairs(table):
                out.append({
                    "table": table.name, "start_column": _q(start), "end_column": _q(end),
                    "entity_column": _q(entity),
                    "_label": f"{table.name}.{start} > {end}",
                })

    elif applies_to == "future_date":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for col in _actual_date_columns(table):
                out.append({
                    "table": table.name, "column": _q(col), "entity_column": _q(entity),
                    "_label": f"{table.name}.{col} in the future",
                })

    elif applies_to == "numeric_range":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for col, stat in table.stats.items():
                bounds = stat.bounds()
                if bounds is None or table.column(col) is None:
                    continue
                lo, hi = bounds
                out.append({
                    "table": table.name, "column": _q(col), "entity_column": _q(entity),
                    "lower_bound": f"{lo:g}", "upper_bound": f"{hi:g}",
                    "_label": f"{table.name}.{col} outside {lo:g}..{hi:g}",
                })

    elif applies_to == "text_numeric":
        for table in index.tables.values():
            entity = table.entity_column()
            if not entity:
                continue
            for col, stat in table.stats.items():
                # Only columns where SOME values parse. Where none do, the column holds codes
                # and the name-based guess was wrong - a probe there would report 100% of the
                # table as anomalous, which is noise, not a finding.
                if stat.text_is_codes or not stat.text_bad or table.column(col) is None:
                    continue
                out.append({
                    "table": table.name, "column": _q(col), "entity_column": _q(entity),
                    "_label": f"{table.name}.{col} ({stat.text_bad}/{stat.text_total} unparseable)",
                })

    else:
        log.warning("generic: unknown applies_to '%s' - template skipped", applies_to)
    return out


# ── Generation ─────────────────────────────────────────────────────────────────

def generate(index: SchemaIndex | None = None) -> list[AnomalyRule]:
    """Every generic probe this schema supports, as AnomalyRule objects with source='generic'.

    They are `pinned` by construction: the SQL is fully determined by the schema, so there is
    nothing for an Author to decide and sending them to one would only add cost and risk.
    """
    if index is None:
        index = load_index()
    templates = load_templates()
    rules: list[AnomalyRule] = []

    for tpl in templates:
        features = _features(index, tpl.applies_to)
        made = 0
        for n, values in enumerate(features, 1):
            rule_id = f"{tpl.family}-{n:03d}"
            label = values.pop("_label", "")
            try:
                summary_sql, detail_sql = tpl.render(rule_id, values)
            except TemplateError as exc:
                log.warning("generic: %s - %s", rule_id, exc)
                continue
            rules.append(
                AnomalyRule(
                    rule_id=rule_id,
                    title=f"{tpl.title}: {label}" if label else tpl.title,
                    category=tpl.meta.get("category", "Structural"),
                    severity=tpl.meta.get("severity", "medium").lower(),
                    entity=tpl.meta.get("entity", "row"),
                    method=tpl.meta.get("method", "rule").lower(),
                    sql_mode="pinned",
                    status="active",
                    source="generic",
                    tags=("generic", tpl.family.lower()),
                    body=tpl.body,
                    summary_sql=summary_sql,
                    detail_sql=detail_sql,
                    params=dict(values),
                    # The rendered SQL IS the identity: regenerate after a schema change and a
                    # probe whose SQL is unchanged keeps its hash, so it is not recompiled.
                    raw=f"{tpl.family}\n{summary_sql}\n{detail_sql}",
                    source_file="generic_probes.md",
                )
            )
            made += 1
        log.info("generic: %-16s %3d probe(s) from %s", tpl.family, made, tpl.applies_to)

    log.info("generic: %d probe(s) generated, 0 LLM calls", len(rules))
    return rules


def coverage_summary(rules: list[AnomalyRule]) -> str:
    """One line per family, for the prompt that tells the SQL Author what is already covered.

    Deliberately a SUMMARY rather than the full list: a hundred rule ids would cost real tokens
    on every author call and tell the model less than one sentence per family does.
    """
    by_family: dict[str, int] = {}
    for r in rules:
        family = r.rule_id.rsplit("-", 1)[0]
        by_family[family] = by_family.get(family, 0) + 1
    if not by_family:
        return ""
    lines = [
        "ALREADY COVERED by deterministic probes - do NOT write a rule that re-expresses any "
        "of these; they run first and their findings are reported separately:"
    ]
    for family, count in sorted(by_family.items()):
        lines.append(f"- {family}: {count} probe(s)")
    return "\n".join(lines)
