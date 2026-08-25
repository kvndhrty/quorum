"""Manager agent tests: the harness-driven supervision loop.

The manager's harness is the fake in tests/bin/fake_harness.py running in a
manager mode; the tasks it acts on use the same fake in echo mode. Each
[harness.<name>] table pins its own FAKE_HARNESS_MODE via `env`, which is
exactly how the mechanism separates concerns in production too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from quorum import fsio, runner, tasks
from quorum.agent import AgentContext
from quorum.agents.manager import Manager, build_digest, journal_path, transcript_path
from quorum.config import load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore
from test_tasks import make_repo, repo_git

FAKE = str(Path(__file__).parent / "bin" / "fake_harness.py")


def write_config(
    home: Path,
    manager_mode: str,
    extra_settings: str = "",
    run_timeout_seconds: int = 60,
    mgr_inject: bool = False,
) -> None:
    (home / "config.toml").write_text(
        "[tasks]\n"
        'default_harness = "tasktool"\n'
        "[harness.tasktool]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        'env = { FAKE_HARNESS_MODE = "echo" }\n'
        "[harness.mgr]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        f'env = {{ FAKE_HARNESS_MODE = "{manager_mode}" }}\n'
        + ('inject = "stream-json"\n' if mgr_inject else "")
        + "[agents.manager]\n"
        'type = "manager"\n'
        'schedule = "every 5m"\n'
        "auto_pause = false\n"
        "[agents.manager.settings]\n"
        'harness = "mgr"\n'
        f"run_timeout_seconds = {run_timeout_seconds}\n"
        f"{extra_settings}"
    )


def make_manager(home: Path, clock) -> Manager:
    config = load_config(home)
    ctx = AgentContext(
        home=home, name="manager",
        settings=config.agents["manager"].settings, config=config, now=clock,
    )
    return Manager(ctx)


def manager_transcript_text(home: Path) -> str:
    return "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(transcript_path(home))
    )


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    repo = make_repo(tmp_path, "mgrproj")
    ProjectRegistry(home).add(repo, name="mgrproj")
    return "mgrproj"


def test_idle_home_skips_the_harness_run(home: Path, clock):
    write_config(home, "manager_act")
    make_manager(home, clock).tick()
    assert not transcript_path(home).exists()  # no tasks, no directives: no run


def test_full_loop_launch_nudge_journal_and_directives(home: Path, clock, project: str):
    write_config(home, "manager_act")
    task = TaskStore(home).add(project, "tidy up the docs", "tasktool")
    bus = MessageBus(home)
    bus.send("user", "manager", type="directive", text="prioritize the docs work today")

    make_manager(home, clock).tick()

    # the manager's harness actually ran the queued task (foreground in the fake)
    fresh = TaskStore(home).get(task.id)
    assert len(fresh.runs) == 1 and fresh.runs[0].exit_code == 0

    # every action was auto-journaled under one run id, actor=manager
    entries = fsio.read_jsonl(journal_path(home))
    actions = [e["action"] for e in entries]
    assert actions == ["task.run", "task.nudge", "note"]
    run_ids = {e["run"] for e in entries}
    assert len(run_ids) == 1 and run_ids != {""}
    assert {e["actor"] for e in entries} == {"manager"}
    assert entries[0]["target"] == task.short_id
    assert entries[0]["target_status"] == "queued"

    # the digest reached the harness: queued line + the user's directive
    text = manager_transcript_text(home)
    assert f"- [queued] {task.short_id}" in text
    assert "prioritize the docs work today" in text

    # directive consumed after a successful run; nudge waits in the task inbox
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "new") == []
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "cur") == []
    nudges = fsio.sorted_entries(bus.inbox_dir / tasks.inbox_name(task.id) / "new")
    assert len(nudges) == 1
    assert fsio.read_json(nudges[0])["from"] == "manager"


def test_mid_run_directive_reaches_a_live_manager_run(
    home: Path, clock, project: str, monkeypatch
):
    """`quorum manager tell` while a tick's harness is in flight: the pump
    forwards the directive as a user turn instead of holding it for the next
    tick (the fake posts the tell itself mid-run, for determinism)."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_INJECT_POST", "tell")
    write_config(home, "inject", mgr_inject=True)
    TaskStore(home).add(project, "x", "tasktool")  # something active, so the tick runs

    make_manager(home, clock).tick()

    raw = transcript_path(home).read_text()
    assert "pause new launches until tests pass" in raw  # delivered into the live run
    assert '"role": "user"' in raw  # ...as a stream-json user turn
    bus = MessageBus(home)
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "new") == []
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "cur") == []


def test_action_cap_refuses_and_bounds_the_journal(home: Path, clock, project: str):
    write_config(home, "manager_flood", extra_settings="max_actions_per_run = 2\n")
    TaskStore(home).add(project, "x", "tasktool")

    make_manager(home, clock).tick()

    entries = fsio.read_jsonl(journal_path(home))
    assert len(entries) == 2  # the cap, exactly
    assert "REFUSED|" in manager_transcript_text(home)


def test_failed_harness_raises_and_returns_directives(home: Path, clock, project: str):
    write_config(home, "fail")
    TaskStore(home).add(project, "x", "tasktool")
    bus = MessageBus(home)
    bus.send("user", "manager", type="directive", text="do the thing")

    with pytest.raises(RuntimeError, match="exited 3"):
        make_manager(home, clock).tick()

    # the directive went straight back to new/, ready for the next tick
    assert len(fsio.sorted_entries(bus.inbox_dir / "manager" / "new")) == 1


def test_hung_harness_is_killed_at_the_run_timeout(home: Path, clock, project: str):
    write_config(home, "hang", run_timeout_seconds=1)
    TaskStore(home).add(project, "x", "tasktool")
    with pytest.raises(RuntimeError, match="timed out"):
        make_manager(home, clock).tick()


def test_missing_harness_config_raises(home: Path, clock, project: str):
    (home / "config.toml").write_text('[agents.manager]\ntype = "manager"\n')
    TaskStore(home).add(project, "x", "tasktool")
    with pytest.raises(RuntimeError, match="no usable harness"):
        make_manager(home, clock).tick()


def test_digest_liveness_quiet_time_and_journal_outcomes(home: Path, clock, project: str):
    write_config(home, "manager_act")
    store = TaskStore(home)
    queued = store.add(project, "queued work", "tasktool")
    stuck = store.add(project, "stuck work", "tasktool")
    store.update(stuck.id, status="executing")
    fsio.append_jsonl(tasks.transcript_path(home, stuck.id), {"at": "t", "line": "output"})
    path = tasks.transcript_path(home, stuck.id)
    stamp = path.stat().st_mtime - 3600
    os.utime(path, (stamp, stamp))
    fsio.append_jsonl(
        journal_path(home),
        {"at": "2026-08-24T00:00:00Z", "run": "R1", "actor": "manager",
         "action": "task.nudge", "target": stuck.short_id, "target_status": "executing"},
    )

    digest = build_digest(home, store.list(), clock(), directives=["focus on stuck work"])

    assert f"- [queued] {queued.short_id}" in digest
    assert "runner=dead" in digest
    stuck_line = next(line for line in digest.splitlines() if f"[executing] {stuck.short_id}" in line)
    quiet = int(stuck_line.split("quiet=")[1].rstrip("m"))
    assert quiet >= 59  # backdated an hour: the model sees real silence
    assert "status_then=executing status_now=executing (UNCHANGED since)" in digest
    assert "- focus on stuck work" in digest


def test_digest_surfaces_stranded_work(home: Path, clock, tmp_path: Path):
    repo = make_repo(tmp_path, "strandproj")
    store = TaskStore(home)
    finished = store.add(project="p", prompt="reported done, left dirt", harness="t")
    store.update(finished.id, workdir=str(repo), status="done")
    active = store.add(project="p", prompt="still working, dirty tree", harness="t")
    store.update(active.id, workdir=str(repo), status="executing")
    (repo / "wip.txt").write_text("uncommitted")

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "STRANDED-WORK dirty=1 unpushed=no-remote" in digest
    assert "git: branch=" in digest

    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "delivered")
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "STRANDED-WORK" not in digest
    assert "git: branch=" not in digest
