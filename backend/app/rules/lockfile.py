"""Cross-process guards on the two shared files that have more than one writer.

TWO LOCKS, AND THEY WAIT DIFFERENTLY ON PURPOSE
-----------------------------------------------
    compile_lock()   guards .cache/anomaly_catalog.json    FAIL-FAST
    decision_lock()  guards domain/anomalies/discovered.md WAITS BRIEFLY

The mechanism below is shared; only the waiting policy differs, and that difference is a
judgement about the work being protected rather than a detail.

A compile runs for 25-40 minutes. Queueing a second one behind it is indistinguishable from a
hang to the caller watching a progress stream, so a compile that cannot start says so at once.

A decision - Accept, Reject, Promote, Restore - is a few milliseconds of file editing behind a
button click. Refusing one because another operator clicked at the same moment would be a
worse answer than simply taking turns, and "try again" for a wait of microseconds is an
apology for nothing. So decision_lock() blocks, with a short ceiling that exists only so a
genuinely wedged holder cannot hang a web request for ever.

WHY THE COMPILE LOCK EXISTS
---------------------------
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

WHY THE DECISION LOCK EXISTS
----------------------------
`domain/anomalies/discovered.md` is read-modify-written twice by a single Accept: once to
allocate the next free DQ-S number, and again to append the rule block. Neither step is
protected by the API's own `threading.Lock`, because that lock is held only for the long jobs
- a decision is not one. FastAPI runs `def` endpoints in a threadpool, so two operators
clicking Accept at the same moment really do overlap, and there were two silent outcomes:

  * both reads see DQ-S04 as the highest, so BOTH decisions are written as DQ-S05. The loader
    keys rules by id, so one of the two simply vanishes;
  * A reads, B reads, A writes, B writes - and A's decision is gone altogether, from the store
    whose entire purpose is to be the durable record of what a human decided.

The individual write is atomic (tmp + os.replace), which protects a READER from ever seeing a
half-file. It does nothing whatever about two writers, and the two problems are often
confused. This lock is the part that was missing.

It covers the pending list as well, because `take_pending` is the same shape of
read-modify-write and a discovery run replaces that file wholesale while a decision may be
removing one entry from it.

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
import time
from contextlib import contextmanager

from app.observability import get_logger

log = get_logger()

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache"
)
# Beside the catalog it protects, at a stable path. Both facts matter: a lock somewhere else is
# a lock people forget exists, and a path that varies is not a lock at all.
LOCK_PATH = os.path.join(_CACHE_DIR, "compile.lock")

# A SEPARATE FILE, not a second range in the compile lock. A decision and a compile guard
# different things and must not block each other: accepting a proposal while a 35-minute
# compile is running is perfectly safe - the accepted rule is simply picked up by the NEXT
# compile - and making the two share a lock would take that away for no benefit.
#
# It lives in .cache rather than beside discovered.md because a lockfile is machine state, and
# domain/ is the operator's own directory. Losing it to a cache wipe costs nothing: the file is
# recreated on first use, and a wipe cannot happen while a decision is in flight.
DECISION_LOCK_PATH = os.path.join(_CACHE_DIR, "decision.lock")

# How long a decision waits for another decision before giving up, and how often it retries.
#
# The ceiling is not a timeout on the work - the work is a few milliseconds of file editing, so
# any real contention clears in well under one retry. It is a bound on how long a WEB REQUEST
# can be made to wait by a holder that has somehow wedged, because a click that never returns
# is worse than a click that reports a problem.
_DECISION_WAIT_SECONDS = 10.0
_DECISION_POLL_SECONDS = 0.05

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


class DecisionLockError(RuntimeError):
    """A decision could not take the ledger lock within the wait ceiling.

    A DISTINCT TYPE FROM CompileLockError, and not a subclass of it, because the two mean
    opposite things to a caller. "A compile is already running" is a normal, expected answer
    that the UI reports calmly and the operator resolves by waiting. This one is not normal: a
    decision holds the lock for milliseconds, so failing to get it within ten seconds means
    something is genuinely wrong - a wedged process, or a filesystem that is not behaving -
    and it should surface as an error, never be retried in a loop.
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


def _open_lockfile(path: str | None = None) -> int:
    """The lockfile's descriptor, creating the file only if it is not already there.

    `path=None` MEANS "resolve LOCK_PATH NOW", and it has to be written that way rather than as
    a `path: str = LOCK_PATH` default. A default argument is bound once, when the function is
    defined, so the default would capture whatever LOCK_PATH pointed at on import and ignore
    every later reassignment of it - which is exactly how the lock tests redirect this at a
    temporary file. Late binding keeps the module global authoritative.

    O_CREAT|O_EXCL is used FIRST rather than a `if not exists: create` check, because two
    processes can both pass that check and both believe they created it. Exclusive creation is
    decided by the filesystem in one operation, on the local filesystems this deployment runs
    on. Losing that race is not an error - it only means the file already exists, which is the
    normal case after the first ever compile - so it falls through to opening it.
    """
    path = path or LOCK_PATH
    os.makedirs(os.path.dirname(path) or _CACHE_DIR, exist_ok=True)
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return os.open(path, os.O_RDWR)


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
        # Shared by both locks, so it must not name either one's work. "another process is
        # compiling" was accurate when only the compile lock existed; said by a decision it
        # would send the reader looking for a compile that is not running.
        return "holder unknown"
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


@contextmanager
def decision_lock():
    """Hold the discovery-ledger lock for the duration of the block.

    WAITS, unlike compile_lock - see the module docstring for why the two differ. Contention
    here is measured in milliseconds, so in practice this never sleeps at all; the retry loop
    exists for the rare simultaneous click and the ceiling for a holder that has wedged.

    NOT REENTRANT, and that is a real constraint on callers rather than a caveat. The whole
    decision must be taken inside ONE of these blocks - allocate the id, remove the proposal
    from the pending list, and append the rule block - because splitting it into two locked
    steps would put the gap this exists to close straight back between them. The primitives in
    app/rules/discoveries.py are therefore deliberately lock-FREE: they compose, and taking the
    lock inside them would deadlock the moment one called another (take_pending calls
    save_pending) or a caller wrapped a pair of them.

    Released by leaving the block - on success, on failure, and on an exception - and by the
    kernel if this process is killed while inside it.
    """
    fd = _open_lockfile(DECISION_LOCK_PATH)
    deadline = time.monotonic() + _DECISION_WAIT_SECONDS
    got = _try_lock(fd)
    waited = False
    while not got and time.monotonic() < deadline:
        waited = True
        time.sleep(_DECISION_POLL_SECONDS)
        got = _try_lock(fd)

    if not got:
        holder = _read_holder(fd)
        os.close(fd)
        raise DecisionLockError(
            "Another decision has held the discovery ledger for more than "
            f"{_DECISION_WAIT_SECONDS:.0f}s ({_describe(holder)}). A decision normally takes "
            "milliseconds, so this means a process is stuck rather than that the system is "
            "busy - check for a hung worker before retrying.",
            holder,
        )

    _write_holder(fd)
    if waited:
        # Logged only when it actually happened, so the log is silent in the normal case and
        # says something real when two operators genuinely collided.
        log.info("lock: decision lock acquired after waiting (pid %d)", os.getpid())
    try:
        yield
    finally:
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
