from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quorum import fsio


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    target = tmp_path / "x.json"
    fsio.atomic_write_json(target, {"a": 1})
    assert fsio.read_json(target) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_tmp_files_invisible_to_scans(tmp_path: Path):
    (tmp_path / ".partial.json.123.tmp").write_text("{")
    (tmp_path / "b.json").write_text("{}")
    assert [p.name for p in fsio.sorted_entries(tmp_path)] == ["b.json"]


def test_ulid_sorts_with_time():
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    assert fsio.ulid(t1) < fsio.ulid(t2)
    assert len(fsio.ulid()) == 26


def test_slugify():
    assert fsio.slugify("NeurIPS Rebuttal!") == "neurips-rebuttal"
    assert fsio.slugify("Ünicode  ok") == "unicode-ok"
    assert fsio.slugify("***") == "unnamed"


def test_jsonl_roundtrip_tolerates_torn_line(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    fsio.append_jsonl(log, {"n": 1})
    fsio.append_jsonl(log, {"n": 2})
    with open(log, "a") as f:
        f.write('{"n": 3')  # crash mid-append
    assert fsio.read_jsonl(log) == [{"n": 1}, {"n": 2}]


def test_pid_lock_conflict_and_stale_takeover(tmp_path: Path):
    lock = tmp_path / "supervisor.lock"
    fsio.acquire_pid_lock(lock)
    with pytest.raises(fsio.LockError):
        # our own pid counts as "alive but not us"? No: same pid re-acquire is takeover.
        # Simulate a *different live* pid: use pid 1 (init), always alive.
        fsio.release_pid_lock(lock)
        fsio.atomic_write_json(lock, {"pid": 1})
        fsio.acquire_pid_lock(lock)
    # Stale pid: nonexistent
    fsio.atomic_write_json(lock, {"pid": 2**22 + os.getpid()})
    fsio.acquire_pid_lock(lock)  # takes over
    fsio.release_pid_lock(lock)
    assert not lock.exists()


def zombie_child() -> subprocess.Popen:
    """A child that has exited and that nobody has waited on — a real zombie.

    `start_new_session` makes it a process-group leader too (pgid == pid), so
    the same process answers both the pid and the group question.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        # `ps` by hand, never `proc.poll()`: polling *reaps* the child, which
        # is the one thing this helper must not do.
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(proc.pid)], capture_output=True, text=True
        ).stdout.strip()
        if state.upper().startswith("Z"):
            return proc
        time.sleep(0.02)
    raise AssertionError("the child never became a zombie")


def test_a_zombie_is_not_alive(tmp_path: Path):
    """`os.kill(pid, 0)` says yes to a zombie, which is how an unreaped runner
    read as a live run: `task stop` then reported a run that survived SIGKILL
    and `task run` refused to start another."""
    proc = zombie_child()
    try:
        os.kill(proc.pid, 0)  # the trap: signalling a zombie succeeds
        assert not fsio.pid_alive(proc.pid)
        assert not fsio.group_alive(proc.pid)  # a group of nothing but zombies
    finally:
        proc.wait()  # reap it, so the test leaves no zombie behind
    assert not fsio.pid_alive(proc.pid)
    assert fsio.pid_alive(os.getpid()) and fsio.group_alive(os.getpgrp())


def test_liveness_keeps_the_process_tables_word_when_ps_cannot_answer(monkeypatch):
    """Fail-soft in the conservative direction: no `ps`, no new verdict."""
    monkeypatch.setattr(fsio, "_ps_rows", lambda *selector: None)
    proc = zombie_child()
    try:
        assert fsio.pid_alive(proc.pid)
        assert fsio.group_alive(proc.pid)
    finally:
        proc.wait()


def test_clear_stale_pid_lock_leaves_a_lock_that_was_taken_over(tmp_path: Path, monkeypatch):
    """The window is narrow, not closed — but a take-over this side of the
    liveness check must not lose the new runner's lock."""
    lock = tmp_path / "runner.lock"
    fsio.atomic_write_json(lock, {"pid": 2**22 + os.getpid()})

    def dead(pid: int) -> bool:  # a new run claims the lock while we look
        fsio.atomic_write_json(lock, {"pid": os.getpid()})
        return False

    monkeypatch.setattr(fsio, "pid_alive", dead)
    assert fsio.clear_stale_pid_lock(lock) is False
    assert fsio.read_json(lock)["pid"] == os.getpid()


def test_clear_stale_pid_lock_removes_a_dead_runners_lock(tmp_path: Path):
    lock = tmp_path / "runner.lock"
    fsio.atomic_write_json(lock, {"pid": 2**22 + os.getpid()})
    assert fsio.clear_stale_pid_lock(lock) is True
    assert not lock.exists()


def test_ulid_is_monotonic_within_one_millisecond():
    """Board filenames carry only second resolution, so ULIDs are what order
    two messages posted in the same tick. Redrawing the tail would make that a
    coin flip."""
    instant = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    ids = [fsio.ulid(instant) for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert {len(i) for i in ids} == {26}


def test_ulid_orders_across_instants_and_leaves_a_backwards_clock_alone():
    first = fsio.ulid(datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC))
    later = fsio.ulid(datetime(2026, 8, 24, 12, 0, 1, tzinfo=UTC))
    assert later > first
    # `now` is injectable and homes are independent, so an earlier timestamp
    # must not be silently dragged forward to preserve global monotonicity.
    assert fsio.ulid(datetime(2020, 1, 1, tzinfo=UTC)) < first


def test_ulid_is_unique_across_threads_at_one_instant():
    """The supervisor mints IDs from several scheduler threads at once."""
    instant = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    minted: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        got = [fsio.ulid(instant) for _ in range(200)]
        with lock:
            minted.extend(got)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(minted)) == 1600
