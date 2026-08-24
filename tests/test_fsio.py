from __future__ import annotations

import os
import threading
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
