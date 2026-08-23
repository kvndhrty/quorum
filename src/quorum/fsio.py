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
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Crockford base32, as used by ULID.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now: datetime | None = None) -> str:
    """A 26-char ULID: 48-bit ms timestamp + 80 random bits, lexicographically sortable."""
    ts = int((now or datetime.now(UTC)).timestamp() * 1000)
    chars = []
    for _ in range(10):
        chars.append(_B32[ts & 0x1F])
        ts >>= 5
    head = "".join(reversed(chars))
    tail = "".join(secrets.choice(_B32) for _ in range(16))
    return head + tail


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


def sorted_entries(dirpath: Path, suffix: str = ".json") -> list[Path]:
    """Non-tmp files in a directory, lexicographic (= chronological for our names)."""
    if not dirpath.is_dir():
        return []
    return sorted(
        p for p in dirpath.iterdir() if p.name.endswith(suffix) and not is_tmp(p.name)
    )


class LockError(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    """Whether `pid` still exists. EPERM means it does, just owned by someone else."""
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


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
            try:
                existing = read_json(path)
                pid = int(existing.get("pid", -1))
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and pid_alive(pid) and pid != os.getpid():
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
    try:
        existing = read_json(path)
        if int(existing.get("pid", -1)) == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
