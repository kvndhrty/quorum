"""Monitor agent tests: launching queued tasks, stall detection, the bounded
poke-and-resume loop, and escalation to blocked. `launch_detached` is
monkeypatched — actually spawning runs is the runner tests' job."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quorum import fsio, tasks
from quorum.agent import AgentContext
from quorum.agents.monitor import Monitor
from quorum.messages import MessageBus
from quorum.tasks import TaskStore


@pytest.fixture
def launches(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("quorum.runner.launch_detached", lambda home, task_id: calls.append(task_id))
    return calls


def make_monitor(home: Path, clock, settings: dict | None = None) -> Monitor:
    ctx = AgentContext(home=home, name="monitor", settings=settings or {}, now=clock)
    return Monitor(ctx)


def board(home: Path):
    return MessageBus(home).read_topic(tasks.BOARD_TOPIC)


def inbox_texts(home: Path, task_id: str) -> list[str]:
    texts = []
    for path in fsio.sorted_entries(MessageBus(home).inbox_dir / tasks.inbox_name(task_id) / "new"):
        texts.append(fsio.read_json(path)["payload"]["text"])
    return texts


def hold_lock(home: Path, task_id: str) -> None:
    """A live runner, as far as the monitor can tell: our own pid in the lock."""
    fsio.atomic_write_json(tasks.runner_lock_path(home, task_id), {"pid": os.getpid()})


def backdate(path: Path, seconds: int) -> None:
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


def test_queued_task_is_launched_once_with_bootstrap_grace(home: Path, clock, launches):
    t = TaskStore(home).add("proj", "do it", "fake")
    m = make_monitor(home, clock)
    m.tick()
    m.tick()  # bootstrap may still be starting; do not stack launches
    assert launches == [t.id]
    assert [msg.type for msg in board(home)] == ["task.started"]

    clock.advance(minutes=5)  # grace expired, still no run: dead on arrival
    m.tick()
    assert launches == [t.id, t.id]
    assert TaskStore(home).get(t.id).resumes == 1  # retries spend the resume budget


def test_terminal_tasks_are_left_alone(home: Path, clock, launches):
    store = TaskStore(home)
    for status in ("done", "blocked", "cancelled"):
        t = store.add("proj", "x", "fake")
        store.update(t.id, status=status)
    make_monitor(home, clock).tick()
    assert launches == [] and board(home) == []


def test_live_quiet_task_gets_one_stall_warning_and_nudge(home: Path, clock, launches):
    t = TaskStore(home).add("proj", "x", "fake")
    TaskStore(home).update(t.id, status="executing")
    hold_lock(home, t.id)
    backdate(tasks.runner_lock_path(home, t.id), 60 * 60)

    m = make_monitor(home, clock, settings={"stall_minutes": 15})
    m.tick()
    m.tick()  # same silence: no duplicate warning

    stalls = [msg for msg in board(home) if msg.type == "task.stalled"]
    assert len(stalls) == 1
    assert len(inbox_texts(home, t.id)) == 1
    assert launches == []  # never relaunch over a live runner


def test_fresh_activity_resets_the_stall_warning(home: Path, clock, launches):
    t = TaskStore(home).add("proj", "x", "fake")
    TaskStore(home).update(t.id, status="executing")
    hold_lock(home, t.id)
    backdate(tasks.runner_lock_path(home, t.id), 60 * 60)

    m = make_monitor(home, clock, settings={"stall_minutes": 15})
    m.tick()
    fsio.append_jsonl(tasks.transcript_path(home, t.id), {"at": "now", "line": "progress"})
    m.tick()  # active again: warning state clears
    backdate(tasks.transcript_path(home, t.id), 60 * 60)
    backdate(tasks.runner_lock_path(home, t.id), 2 * 60 * 60)
    m.tick()  # quiet again: a second warning is legitimate

    assert len([msg for msg in board(home) if msg.type == "task.stalled"]) == 2


def test_dead_run_is_poked_and_resumed_until_blocked(home: Path, clock, launches):
    store = TaskStore(home)
    t = store.add("proj", "x", "fake")
    store.update(
        t.id,
        status="executing",
        runs=[{"started_at": "2026-08-23T00:00:00Z", "ended_at": "2026-08-23T00:01:00Z", "exit_code": 0}],
    )
    m = make_monitor(home, clock, settings={"max_resumes": 2})

    m.tick()
    m.tick()
    assert launches == [t.id, t.id]
    assert store.get(t.id).resumes == 2
    assert len(inbox_texts(home, t.id)) == 2  # each resume carries a poke

    m.tick()  # budget exhausted
    fresh = store.get(t.id)
    assert fresh.status == "blocked"
    assert launches == [t.id, t.id]  # no further launches
    assert any(msg.type == "task.blocked" for msg in board(home))

    m.tick()  # blocked is terminal: nothing more happens
    assert len([msg for msg in board(home) if msg.type == "task.blocked"]) == 1
