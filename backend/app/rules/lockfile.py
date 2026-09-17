"""Cross-process guard on the compiled catalog.

WHY THIS EXISTS
---------------
`.cache/anomaly_catalog.json` has exactly one writer - `compiler.compile_rules()` - and that
write is atomic (tmp + os.replace), so the file can never be observed half-written. What it
CAN be is silently overwritten: two compiles both read the catalog, both write it, and the one
that finishes second discards everything the first compiled. Nothing fails, nothing is logged,
and the surviving catalog looks perfectly healthy while missing probes nobody knows are gone.

The API already serialises its own callers with a threading.Lock, but that lock lives inside
ONE process. It cannot see `python -m app.cli compile` typed in a terminal, which is exactly
how a rebuild was lost in practice. This guard is held by whoever is compiling, wherever they
started it from.

THE LOCKFILE'S EXISTENCE IS NOT THE LOCK
-----------------------------------------
The file is created once and then left on disk for ever. Whether a compile is actually running
is decided by an OS-level exclusive lock held on the OPEN DESCRIPTOR, never by the file being
present, and never by how old it is.

That distinction is the whole design, and it is what makes stale locks a non-problem:

  * a compile legitimately takes 25-40 minutes, so ANY age-based timeout risks declaring a live
    compile dead and letting a second one destroy its work - a worse bug than the one being
    fixed;
  * the kernel drops the lock when the owning process dies, however it dies - clean exit,
    unhandled exception, kill, or the machine losing power. There is no case left for a
    heuristic to cover;
  * PID liveness checks were considered and rejected. A PID is only meaningful on the host that
    recorded it, Windows recycles PIDs (so a dead compile's 8124 becomes a live unrelated
    process and the lock is refused for ever), and querying another process can fail on
    permissions alone.

`pid`, `hostname` and `started_at` ARE written into the file, but only so a refusal can name
who holds the lock. Nothing here ever makes a decision from them.

THE FILE IS NEVER DELETED, DELIBERATELY. Unlinking it on release opens a race that the OS lock
would otherwise have closed: a second process can open the path, have the file unlinked from
under it, and then lock an inode no longer reachable by name - while a third process creates a
fresh file at that path and locks that. Two holders, both certain they are alone. Leaving one
long-lived file at a stable path makes that impossible.

WHAT THIS DOES NOT COVER
------------------------
One filesystem. The lock is held on a local file, so it serialises every process on ONE
machine - which is the supported deployment (one server, one API process, API_WORKERS=1, many
UI users). Behind a load balancer, two application servers would each acquire their own local
lock and both compile. That needs a shared lock or a single compilation service, and neither
belongs here: this module should be REPLACED at that point, not extended.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
from contextlib import contextmanager

from app.observability import get_logger

log = get_logger()

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
# Beside the catalog it protects, at a stable path. Both facts matter: a lock somewhere else is
# a lock people forget exists, and a path that varies is not a lock at all.
LOCK_PATH = os.path.join(_CACHE_DIR, "compile.lock")

# The single byte whose lock IS the mutex.
#
# IT SITS PAST THE DIAGNOSTIC JSON, NOT ON TOP OF IT, and that offset is load-bearing on
# Windows. A Windows byte-range lock is MANDATORY, not advisory: a process that does not hold
# it cannot even READ the locked bytes. With the mutex on byte 0 the refusal message could
# never name its holder - the read failed, the metadata came back empty, and every refusal read
# "another process is compiling" while the pid and hostname sat unreadable in the file. Locking
# a byte beyond any plausible payload leaves the JSON freely readable by the very process that
# needs to quote it. (POSIX flock is advisory and unaffected either way.)
#
# Locking past end-of-file is permitted on both platforms, so the file never has to be padded.
_LOCK_OFFSET = 4096
_LOCK_BYTES = 1
# Everything before the mutex byte is the diagnostic record.
_HOLDER_BYTES = _LOCK_OFFSET


class CompileLockError(RuntimeError):
    """Another compile holds the lock. Deliberately NOT a compile failure.

    A caller has to tell "your compile ran and failed" apart from "your compile never started,
    someone else is already compiling". The first needs the rules or the schema looked at; the
    second needs nothing but patience. Raising the same exception type for both makes the API
    and the CLI report a healthy system as broken.
    """

    def __init__(self, message: str, holder: dict | None = None) -> None:
        super().__init__(message)
        self.holder = holder or {}


# ── The two platform primitives ────────────────────────────────────────────────
#
# Windows and POSIX are deliberately NOT made to look identical. msvcrt.locking() locks a byte
# range starting at the CURRENT FILE POSITION and needs that position restored before
# unlocking; fcntl.flock() locks the whole descriptor and ignores position entirely. Writing
# one to imitate the other is how a lock ends up covering a different region than it unlocks.
#
# What is guaranteed is the ABSTRACTION, not the mechanism: _try_lock() either takes an
# exclusive lock immediately or returns False, and the kernel releases whatever it took when
# this process dies.

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
            return True
        except OSError:
            # EACCES/EDEADLOCK both mean "held by someone else" here. There is no third case:
            # the file is open, so this cannot be a path or permission problem.
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:  # already gone - closing the fd releases it regardless
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _open_lockfile() -> int:
    """The lockfile's descriptor, creating the file only if it is not already there.

    O_CREAT|O_EXCL is used FIRST rather than a `if not exists: create` check, because two
    processes can both pass that check and both believe they created it. Exclusive creation is
    decided by the filesystem in one operation, on the local filesystems this deployment runs
    on. Losing that race is not an error - it only means the file already exists, which is the
    normal case after the first ever compile - so it falls through to opening it.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    try:
        return os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return os.open(LOCK_PATH, os.O_RDWR)


def _read_holder(fd: int) -> dict:
    """Who wrote this lockfile last. Diagnostics only - never used to decide anything."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _HOLDER_BYTES).decode("utf-8", "replace").strip("\x00").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a corrupt lockfile must not mask the real message
        return {}


def _write_holder(fd: int) -> None:
    """Record who holds the lock, so a refusal can say so. Never raises."""
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        # Truncated first so a shorter record cannot leave the tail of a longer previous one
        # behind it and produce unparseable JSON. Only the region BELOW the mutex byte is
        # touched; the mutex itself is a lock on a byte offset, not on any content.
        record = json.dumps(payload).encode("utf-8")[:_HOLDER_BYTES]
        os.truncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, record)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never cost the lock
        log.debug("lock: could not record holder details (%s)", exc)


def _describe(holder: dict) -> str:
    pid, host = holder.get("pid"), holder.get("hostname")
    started = holder.get("started_at")
    if not pid:
        return "another process is compiling"
    where = f"pid {pid}" + (f" on {host}" if host else "")
    return f"{where}" + (f", started {started}" if started else "")


@contextmanager
def compile_lock():
    """Hold the compile lock for the duration of the block, or refuse immediately.

    FAIL-FAST, NOT QUEUED, matching the API's own lock. A caller told "already running" can
    decide what to do; a caller silently queued behind a twenty-minute compile cannot tell that
    from a hang, and the progress stream it is watching would simply stall.

    Released by leaving the block - on success, on failure, and on an exception - and by the
    kernel if this process is killed while inside it.
    """
    fd = _open_lockfile()
    if not _try_lock(fd):
        holder = _read_holder(fd)
        os.close(fd)
        raise CompileLockError(
            "A compile is already running (" + _describe(holder) + "). Compiles rewrite the "
            "shared probe catalog, so only one can run at a time - wait for it to finish and "
            "try again.",
            holder,
        )

    _write_holder(fd)
    log.info("lock: compile lock acquired (pid %d)", os.getpid())
    try:
        yield
    finally:
        # Ordered: release the lock, then drop the descriptor. Closing alone would also release
        # it, but doing it explicitly keeps the intent readable and the two platforms
        # symmetrical. The FILE IS LEFT IN PLACE - see the module docstring.
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        log.info("lock: compile lock released (pid %d)", os.getpid())
