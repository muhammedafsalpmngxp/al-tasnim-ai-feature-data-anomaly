"""Which database this process is pointed at, and the cache filenames that follow from it.

ONE DEFINITION OF "THIS DATABASE", USED BY EVERYTHING THAT CACHES.
-----------------------------------------------------------------
Two separate things are keyed on database identity: the compiled catalog and the three
rendered description files (schema.txt and the two hint files). They were keyed
independently - the catalog grew its own `_slug()` while the caches used fixed filenames - and
the consequence was the failure this module exists to prevent: a catalog correctly kept per
database, sitting beside a schema.txt describing a DIFFERENT one. Every probe then looked
current (its own fingerprint matched) while the schema block shown to the author and the
reviewer belonged to somewhere else.

So the slug lives here, once, and both consumers import it. If they disagree about identity
they disagree about everything, and that is a bug no fingerprint can catch.

IDENTITY IS SERVER + PORT + NAME, NOT NAME.
Two servers can host a database with the same name - a restored copy, dev beside production -
and the hint files hold REAL VALUES read out of the data. Sharing them across two systems means
probes calibrated against the wrong scale. The readable part of a filename is for whoever opens
.cache and wants to know what they are looking at; the 8-hex digest is what actually
distinguishes them.
"""
from __future__ import annotations

import hashlib
import os
import re

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)

# The digest length. Eight hex characters is 4 billion values over a handful of databases per
# installation - a collision is not a practical concern, and a longer digest makes every
# filename in .cache unreadable at a glance, which is the whole point of the readable half.
_DIGEST_CHARS = 8
_READABLE_CHARS = 40


def cache_dir() -> str:
    return _CACHE_DIR


def current_database() -> dict[str, str]:
    """Which database this process is pointed at, as a catalog or a cache records it."""
    from app.config import settings

    return {"name": settings.db_name, "server": settings.db_server}


def identity() -> str:
    """The full identity string the digest is taken over. Lowercased, so case cannot fork it."""
    from app.config import settings

    return f"{settings.db_server}:{settings.db_port}/{settings.db_name}".lower()


def digest() -> str:
    return hashlib.sha256(identity().encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def slug(database: dict[str, str] | None = None) -> str:
    """A filename-safe, human-readable, COLLISION-FREE key for one database.

    `database` is accepted only so a caller holding a recorded identity can render the same
    slug for it; the digest always describes the CONFIGURED database, because that is what the
    caller is asking about when it asks for a path.
    """
    name = (database or current_database()).get("name") or "db"
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:_READABLE_CHARS]
    return f"{readable or 'db'}-{digest()}"


# Matches ONLY a per-database cache name: the slug always ends in the 8-hex identity digest.
# Deliberately not "anything with an extra dot" - a hand-made backup like
# `schema.before-grain-fix.txt` sitting in the same directory must not be mistaken for a
# migrated cache and silently cancel the one-off upgrade path below.
def _per_database_name(stem: str, ext: str) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(stem)}\.[A-Za-z0-9_.-]+-[0-9a-f]{{{_DIGEST_CHARS}}}\.{re.escape(ext)}$"
    )


def cache_path(stem: str, ext: str) -> str:
    """Where THIS database's copy of a cache file is written: <stem>.<slug>.<ext>."""
    return os.path.join(_CACHE_DIR, f"{stem}.{slug()}.{ext}")


def legacy_cache_path(stem: str, ext: str) -> str:
    """The pre-split filename, ONLY while no database has a copy of its own yet.

    ⚠ FOR THE CATALOG ONLY. read_cache() above deliberately does NOT use this - see the note
    there for the production failure that removed it from the description files. The difference
    is not a preference:

      the CATALOG   every probe carries its own db-identity fingerprint, compared LOCALLY with
                    no network call, so a catalog adopted by the wrong database is judged stale
                    deterministically and rebuilt. Safe.
      a DESCRIPTION file has no per-record guard. Whether it belongs to this database can only
                    be decided by a metadata query, and a query can time out. Not safe.

    An installation that compiled before the split has a catalog holding real, paid-for work;
    discarding it on upgrade charges the operator a full LLM rebuild for installing a new build.

    THE MOMENT ANY PER-DATABASE COPY EXISTS, THE OLD FILE IS SUPERSEDED and never read again.
    Without that condition it would be offered to the second database too, and the third, each
    time producing a confident log line about a description written for somewhere else.
    """
    legacy = os.path.join(_CACHE_DIR, f"{stem}.{ext}")
    if not os.path.exists(legacy):
        return ""
    pattern = _per_database_name(stem, ext)
    try:
        if any(pattern.match(name) for name in os.listdir(_CACHE_DIR)):
            return ""
    except OSError:
        return ""
    return legacy


def read_cache(stem: str, ext: str) -> str:
    """THIS database's cached text, by name. "" when absent - never another database's.

    DELIBERATELY DOES NOT FALL BACK TO THE PRE-SPLIT FILE, and that is the whole point.

    It did, to spare an existing installation one re-introspection on upgrade, on the reasoning
    that adopting the wrong database's file was harmless because database identity is folded
    into the fingerprint - so a mismatch would be detected and the file rebuilt. That reasoning
    had a hole, and it was found in production within a day: the fingerprint check is a NETWORK
    QUERY, and on a slow or permission-limited server it times out. build_schema_text() then
    takes its "drift check failed - using the cached schema" path and returns the adopted file
    anyway. Observed exactly that: pointed at AlTasnimBI, the engine described it using
    TrialDB_Test's 46 tables and began authoring SQL against tables it does not have.

    A guard that only holds while the network is healthy is not a guard. An unsuffixed file
    cannot be attributed to any database, so it is never adopted; a suffixed one can only
    belong to the database named in it, which makes attribution structural and free. The cost
    of getting this right is one metadata pass per database - no LLM calls - which is precisely
    what SHOULD happen when the engine is pointed somewhere new.

    The catalog keeps its own legacy fallback, and that remains safe for a different reason:
    every probe in it carries its own db-identity fingerprint, compared locally with no network
    call, so an adopted catalog is judged stale deterministically.
    """
    path = cache_path(stem, ext)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_cache(stem: str, ext: str, text: str) -> str:
    """Write this database's copy and return the path. Always the per-database name."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = cache_path(stem, ext)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
