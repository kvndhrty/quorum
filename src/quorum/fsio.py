"""Filesystem primitives shared by every quorum component.

All durable state in quorum is plain files. These helpers enforce the two
invariants the rest of the system relies on:

* writes are atomic (same-directory tmp file + fsync + rename), so readers
  never observe a partial file;
* tmp files are dot-prefixed, so directory scans can skip them uniformly.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

# Crockford base32, as used by ULID.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TAIL_BITS = 80
_TAIL_MAX = (1 << _TAIL_BITS) - 1

# Monotonic-ULID state, guarded because the supervisor mints IDs from several
# scheduler threads at once.
_ulid_lock = threading.Lock()
_ulid_last: tuple[int, int] | None = None  # (timestamp_ms, tail)


def _b32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_B32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid(now: datetime | None = None) -> str:
    """A 26-char ULID: 48-bit ms timestamp + 80 random bits, lexicographically sortable.

    Monotonic within a millisecond: rather than drawing a fresh tail, a ULID
    sharing its predecessor's timestamp increments it. Ordering is what the
    message bus actually relies on — board filenames carry only second
    resolution, so two messages posted in the same tick would otherwise sort by
    coin flip, and a consumer replaying by filename would see them in an order
    the sender never chose. Independent random tails still separate ULIDs
    minted by different processes in the same millisecond.

    A backwards clock is deliberately not clamped: `now` is injectable, homes
    are independent, and quietly rewriting a caller's timestamp would corrupt
    the correspondence between an ID and the `created_at` beside it. Ordering
    across a backwards step is ambiguous anyway, and the filename's own
    timestamp prefix — not the ULID — is what sorts a topic.
    """
    ts = int((now or datetime.now(UTC)).timestamp() * 1000)
    global _ulid_last
    with _ulid_lock:
        if _ulid_last is not None and _ulid_last[0] == ts:
            tail = _ulid_last[1] + 1
            if tail > _TAIL_MAX:
                # 2**80 IDs inside one millisecond. Unreachable in practice;
                # carry into the timestamp rather than wrap and go backwards.
                ts += 1
                tail = secrets.randbits(_TAIL_BITS)
        else:
            tail = secrets.randbits(_TAIL_BITS)
        _ulid_last = (ts, tail)
    return _b32(ts, 10) + _b32(tail, 16)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_ts(dt: datetime) -> str:
    """Timestamp for filenames; sorts chronologically."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_iso_or(value: Any, default: T = None) -> datetime | T:
    """The timestamp in `value`, or `default` when it is not one.

    The fail-soft companion to `parse_iso`, and the reason readers do not
    each write their own try/except: everything quorum writes goes through
    `iso`, so a value that fails here came off a torn line, a hand-edited
    file or a harness that wrote its own — which a listing should survive.
    """
    if not isinstance(value, str) or not value:
        return default
    try:
        return parse_iso(value)
    except ValueError:
        return default


def display_ts(value: Any) -> str:
    """A stored timestamp as a surface prints it: `2026-09-07 11:42:03`.

    One spelling of the same two edits (drop the `T`, drop the trailing
    `Z`) that every listing, pane and log line used to make for itself.
    Anything that is not a timestamp — an em dash a caller substituted for
    a missing value, say — passes through unchanged.
    """
    return str(value or "").replace("T", " ").rstrip("Z")


_WINDOW = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_WINDOW_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_window(text: str) -> timedelta:
    """`90m` / `36h` / `7d` / `2w` / `30s` → a timedelta.

    The one window grammar in quorum: `usage --since`, `board read --since`,
    `board clear --before` and `task prune --older-than` all take these five
    units, so a person who has learned one has learned them all.

    ValueError for anything else, including `0d`: an empty window is a typo,
    not a request. A count too large for a timedelta is a ValueError too,
    not the OverflowError the constructor would raise — the caller rejects
    one bad window with one message, and a number nobody can type by
    accident is still a typo.
    """
    m = _WINDOW.match(text or "")
    if not m or int(m.group(1)) <= 0:
        raise ValueError(
            f"invalid window {text!r} — a positive count and a unit, e.g. 90m, 24h, 7d, 2w"
        )
    try:
        return timedelta(**{_WINDOW_UNITS[m.group(2)]: int(m.group(1))})
    except OverflowError:
        raise ValueError(f"window {text!r} is longer than any date can express") from None


def resolve_handle(
    handle: str,
    candidates: Iterable[str],
    *,
    what: str = "handle",
    render: Callable[[str], str] | None = None,
) -> str:
    """The one candidate `handle` names: a full id, a unique prefix, or a
    unique suffix, matched case-insensitively.

    Every id quorum hands a person is a ULID, and what a person has in front
    of them is usually the tail of one (`short_id`), copied off another line
    of output — so tasks, board messages, notes, archived tasks and agent
    runs all accept the same three forms. This is the one implementation of
    that grammar; callers pass their own candidate ids.

    An exact id wins outright, so an id that also happens to be another id's
    prefix or suffix is never ambiguous. Raises KeyError(handle) when
    nothing matches (an empty handle included: it would match everything)
    and ValueError when more than one does, naming the matches with
    `render` — the short form, unless a caller says otherwise. Callers turn
    both into their own messages.
    """
    wanted = handle.strip().upper()
    if not wanted:
        raise KeyError(handle)
    ids = list(candidates)
    for candidate in ids:
        if candidate.upper() == wanted:
            return candidate
    matches = [
        c for c in ids if c.upper().startswith(wanted) or c.upper().endswith(wanted)
    ]
    if not matches:
        raise KeyError(handle)
    if len(matches) > 1:
        show = render or (lambda c: c[-6:].lower())
        raise ValueError(
            f"{what} {handle!r} is ambiguous: " + ", ".join(show(m) for m in matches)
        )
    return matches[0]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "unnamed"


def is_tmp(name: str) -> bool:
    return name.startswith(".")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write atomically: dot-prefixed tmp in the same directory, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_json_or(path: Path, default: T) -> dict[str, Any] | T:
    """The JSON object in `path`, or `default` when there is not one to read.

    The fail-soft companion to `read_json`, and the one place the rule is
    written down: a record that is missing, unreadable, not JSON, or JSON
    that is not an object reads as `default`. Callers used to spell this out
    themselves and disagreed about which exceptions to catch, which left
    valid-but-not-an-object JSON (a `runner.lock` holding `[]`) raising
    TypeError or AttributeError out of a digest build or a view.

    Swallowed: OSError (missing file, permissions, a directory, an I/O
    error), UnicodeDecodeError (a binary or mis-encoded file — a ValueError,
    but named here because it is the common one) and json.JSONDecodeError
    (truncated or malformed text, also a ValueError). Nothing else: a caller
    passing something that is not a path still gets its TypeError.

    The return is a dict whenever it is not `default`, so callers can use
    `.get()` without a further isinstance check.
    """
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    return record if isinstance(record, dict) else default


def read_pid(path: Path) -> int | None:
    """The live-process pid recorded in a lock file, or None.

    Every pid lock quorum writes (`runner.lock`, `supervisor.lock`, an
    agent's `tick.lock`) is a JSON object with a `pid`. This reads it and
    never raises: a missing, unreadable, malformed or non-object record, a
    `pid` that is not a number, and a non-positive pid all answer None, so
    the caller's only question is whether it got an int.
    """
    record = read_json_or(path, None)
    if record is None:
        return None
    try:
        pid = int(record.get("pid", -1))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def append_jsonl(path: Path, obj: Any) -> None:
    """Append one JSON line. A single write() of a full line is atomic enough for
    a same-host, append-only log (O_APPEND); readers tolerate a torn final line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[Any]:
    out: list[Any] = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn final line from a crash mid-append
    return out


def read_jsonl_tail(path: Path, limit: int | None = None, max_bytes: int = 256 * 1024) -> list[Any]:
    """The last `limit` entries of a jsonl file, reading at most `max_bytes`.

    The bounded companion to read_jsonl for append-only logs that grow without
    limit (transcripts, the manager journal): tailing one must not cost a full
    read + parse of its history. A line straddling the window boundary is
    dropped, like a torn line.
    """
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0:
        lines = lines[1:]  # the window almost surely opened mid-line
    out: list[Any] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:] if limit else out


def sorted_entries(dirpath: Path, suffix: str = ".json") -> list[Path]:
    """Non-tmp files in a directory, lexicographic (= chronological for our names)."""
    if not dirpath.is_dir():
        return []
    return sorted(
        p for p in dirpath.iterdir() if p.name.endswith(suffix) and not is_tmp(p.name)
    )


# `ps` answers a liveness question quorum asks rarely (a lock take-over, a
# stop, a dashboard refresh with a live run), so it may block briefly, but a
# hung `ps` must never hang a tick.
PS_TIMEOUT_SECONDS = 5.0


class LockError(RuntimeError):
    pass


def _ps_rows(*selector: str) -> list[tuple[int, str]] | None:
    """`(id, state letter)` rows from `ps`, or None when it cannot answer.

    The id is whatever the first `-o` column selects (a pid or a pgid); the
    second is the process state.

    The one place quorum shells out to `ps`. `os.kill(pid, 0)` cannot tell a
    running process from a *zombie* — an exited child its parent has not
    reaped is still a process-table entry — and the state letter is the
    portable way to ask. Verified on macOS 15 (`ps -o pid=,stat= -p <pid>`
    and `ps -A -o pgid=,stat=` both report `Z`) and used in the same form on
    Linux, where procps accepts both.

    Fail-soft in the *conservative* direction: any disappointment (no ps, a
    timeout, output we cannot parse) answers None, and every caller then
    keeps the old "the process table has it, so it is alive" reading rather
    than declaring a live run dead.
    """
    try:
        proc = subprocess.run(
            ["ps", *selector], capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            rows.append((int(fields[0]), fields[1]))
        except ValueError:
            continue
    # A non-zero exit with rows parsed is still an answer (`ps -p` exits 1
    # when *some* pid is gone); a non-zero exit with nothing means the
    # selected process is gone, which is an answer too.
    if proc.returncode != 0 and not rows and proc.stderr.strip():
        return None
    return rows


def _zombie(state: str) -> bool:
    return state.upper().startswith("Z")


def pid_alive(pid: int) -> bool:
    """Whether `pid` is a live process — a zombie does not count.

    EPERM means it exists, just owned by someone else (and is therefore not
    our unreaped child). A pid we *can* signal may still be a zombie: the
    process exited and its parent has not waited on it yet, which is exactly
    what a `launch_detached` run looks like to a caller that stays alive.
    Reading that as "the run is still going" would refuse the next run and
    make `task stop` report a runner that survived SIGKILL, so the state
    letter decides.
    """
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    rows = _ps_rows("-o", "pid=,stat=", "-p", str(pid))
    if rows is None:
        return True  # ps could not answer: keep the process table's word
    return any(not _zombie(state) for row_pid, state in rows if row_pid == pid)


def group_alive(pgid: int) -> bool:
    """Whether any *live* process is left in a process group.

    `killpg(pgid, 0)` alone is not enough at either end: on macOS a group
    holding only zombies answers EPERM (which for a single pid means "alive,
    someone else's"), and on Linux it succeeds outright. So the cheap signal
    probe only rules the group out — anything else asks `ps` for the group's
    members and their states.
    """
    try:
        os.killpg(pgid, 0)
    except PermissionError:
        pass  # exists, but nothing we may signal — possibly all zombies
    except OSError:
        return False  # ESRCH: nobody left
    rows = _ps_rows("-A", "-o", "pgid=,stat=")
    if not rows:
        return True  # ps could not answer (an empty listing cannot be true)
    return any(not _zombie(state) for row_pgid, state in rows if row_pgid == pgid)


def acquire_pid_lock(path: Path, meta: dict[str, Any] | None = None) -> None:
    """Single-instance lock via O_EXCL create; stale locks (dead pid) are taken over.

    Avoids flock() so it behaves identically on any filesystem a sandbox grants.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(meta or {})
    payload.update({"pid": os.getpid(), "started_at": iso(utc_now())})
    data = (json.dumps(payload) + "\n").encode()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return
        except FileExistsError:
            pid = read_pid(path)
            if pid is not None and pid_alive(pid) and pid != os.getpid():
                raise LockError(
                    f"another instance is running (pid {pid}, lock {path})"
                ) from None
            path.unlink(missing_ok=True)  # stale — take over
    raise LockError(f"could not acquire lock {path}")


def touch_lock(path: Path) -> None:
    try:
        os.utime(path, (time.time(), time.time()))
    except OSError:
        pass


def release_pid_lock(path: Path) -> None:
    if read_pid(path) == os.getpid():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def clear_stale_pid_lock(path: Path) -> bool:
    """Remove a lock whose recorded pid is gone; leave a live one alone.

    The counterpart to `release_pid_lock` for a lock this process does not
    own: `quorum task stop` kills someone else's runner, and only after the
    pid is confirmed dead may it clear the file the runner never got to.
    Returns whether it removed anything.

    Re-reads the pid immediately before unlinking, which *narrows* but does
    not close the window: `acquire_pid_lock` takes a stale lock over by
    unlink-and-create, so a new runner can still claim the file between that
    last read and this unlink, and its lock would be the one removed. There
    is no compare-and-unlink to be had without the flock the pid-lock
    deliberately avoids; the residue is a run holding a lock file that is
    gone, which the next acquisition simply recreates.
    """
    if read_json_or(path, None) is None:
        return False  # no lock record to reason about — leave the file alone
    pid = read_pid(path)
    if pid is not None and pid_alive(pid):
        return False
    if read_pid(path) != pid:
        return False  # somebody else's lock now — leave it alone
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True
