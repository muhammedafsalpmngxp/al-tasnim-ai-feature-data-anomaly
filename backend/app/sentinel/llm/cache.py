"""Narration reuse across runs.

The data is LIVE: between two runs an anomaly can be fixed, partly fixed, grow, or appear
for the first time. So the cache key is not the check id -- it is a fingerprint of
everything the narration actually asserts. If any of it moves, the key moves, and the
finding is narrated again.

    finding unchanged        -> same key  -> reuse, no API call
    partly fixed (33 -> 23)  -> title/affected_count/evidence all change -> re-narrate
    fully fixed              -> the finding does not exist this run -> nothing to narrate
    brand new                -> no prior key -> narrate
    model changed            -> key includes the model -> re-narrate everything
    business rules edited    -> key includes the prompt fingerprint -> re-narrate everything

That last pair matters as much as the data ones. A narration written by a different model,
or under a different version of BUSINESS_RULES.md, is not interchangeable with one written
now -- reusing it would put two different authors' text in one report and quietly ignore a
rule change the user just made.

Reuse is safe on the citation rule too: a reused narration was validated against evidence
that is byte-identical to this run's, because the evidence IS part of the key.
"""
from __future__ import annotations

import hashlib
from typing import Any

# Fields whose value the narration text directly depends on. `evidence` and
# `affected_count` are the load-bearing ones -- they are the numbers the prose quotes and
# that `validate.py` checks it against.
_KEYED_FIELDS = (
    "check_id",
    "severity",
    "finding_class",
    "title",
    "affected_count",
    "grain",
    "baseline",
    "business_rule_ref",
    "owner",
    "why_it_matters",
    "entity_id",
    "well_id",
    "evidence",
)


def prompt_fingerprint(system_prompt: str) -> str:
    """Short hash of the narrate system prompt -- which embeds GUARD_RULES and the whole
    of BUSINESS_RULES.md, so editing either invalidates every cached narration."""
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]


def narration_key(finding: dict[str, Any], *, model: str, prompt_fp: str) -> str:
    """Stable fingerprint of one finding's narration inputs."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt_fp.encode("utf-8"))
    for field in _KEYED_FIELDS:
        h.update(b"\x00")
        value = finding.get(field)
        # str() on the stored value: `evidence` arrives as the raw JSON text SQLite holds,
        # so identical evidence produces an identical key without re-serialising it (a
        # re-serialisation could reorder keys and cause a spurious miss).
        h.update(b"" if value is None else str(value).encode("utf-8"))
    return h.hexdigest()
