"""Adopting a live interactive session as an attached task.

The harness side is exercised the way a real hook invokes it: `quorum task
hook-stop` / `hook-session-end` through the CLI with the hook's JSON on
stdin. No quorum-spawned process exists for an attached task — that's the
point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio
from quorum.agents.manager import build_digest
from quorum.cli import app
from quorum.config import load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.runner import RunnerError, run_task
from quorum.tasks import TaskStore, attached_state, inbox_name
from test_tasks import make_repo

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, "adoptrepo")


def adopt(home: Path, repo: Path, *extra: str):
    r = runner.invoke(
        app,
        ["task", "adopt", "fix the flaky auth test", "--dir", str(repo),
         "--session", "sess-live-1", "--json", "--home", str(home), *extra],
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output.strip().splitlines()[-1])
    return TaskStore(home).get(out["id"])


def stop_payload(task, session: str | None = None, cwd: str | None = None) -> str:
    return json.dumps(
        {"session_id": session or task.session, "cwd": cwd or task.workdir,
         "stop_hook_active": False}
    )


def test_adopt_creates_attached_task_and_registers_project(home: Path, repo: Path):
    task = adopt(home, repo)
    assert task.attached and task.status == "attached"
    assert task.workdir == str(repo) and task.use_worktree is False
    assert task.session == "sess-live-1"
    assert attached_state(home, task.id)["event"] == "adopt"
    # the unregistered directory was auto-registered
    assert any(p.dir == repo for p in ProjectRegistry(home).list())


def test_runner_refuses_attached_tasks(home: Path, repo: Path):
    task = adopt(home, repo)
    with pytest.raises(RunnerError, match="attached to a live interactive session"):
        run_task(home, load_config(home), task.id)


def test_hook_stop_delivers_pending_guidance_and_consumes_it(home: Path, repo: Path):
    task = adopt(home, repo)
    bus = MessageBus(home)
    bus.send("manager", inbox_name(task.id), type="guidance", text="run the tests before pushing")

    r = runner.invoke(app, ["task", "hook-stop"], input=stop_payload(task))
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["decision"] == "block"
    assert "run the tests before pushing" in out["reason"]
    assert not bus.pending(inbox_name(task.id))  # consumed on delivery
    assert attached_state(home, task.id)["event"] == "stop"

    # no guidance queued: silent, and never blocks (no delivery loop)
    r = runner.invoke(app, ["task", "hook-stop"], input=stop_payload(task))
    assert r.exit_code == 0 and r.output.strip() == ""


def test_hook_stop_matches_by_cwd_and_learns_the_session(home: Path, repo: Path):
    task = adopt(home, repo)
    TaskStore(home).update(task.id, session=None)  # adopted without --session

    payload = json.dumps({"session_id": "sess-learned", "cwd": str(repo)})
    r = runner.invoke(app, ["task", "hook-stop"], input=payload)
    assert r.exit_code == 0, r.output
    assert TaskStore(home).get(task.id).session == "sess-learned"


def test_hook_stop_ignores_unadopted_sessions(home: Path, repo: Path, tmp_path: Path):
    adopt(home, repo)
    payload = json.dumps({"session_id": "sess-other", "cwd": str(tmp_path / "elsewhere")})
    r = runner.invoke(app, ["task", "hook-stop"], input=payload)
    assert r.exit_code == 0 and r.output.strip() == ""


def test_hook_session_end_records_the_event(home: Path, repo: Path):
    task = adopt(home, repo)
    r = runner.invoke(app, ["task", "hook-session-end"], input=stop_payload(task))
    assert r.exit_code == 0, r.output
    st = attached_state(home, task.id)
    assert st["event"] == "session-end"
    assert TaskStore(home).get(task.id).attached  # sessions reopen; still attached


def test_detach_makes_the_task_runnable_again(home: Path, repo: Path):
    task = adopt(home, repo)
    r = runner.invoke(app, ["task", "detach", task.short_id, "--home", str(home)])
    assert r.exit_code == 0, r.output
    fresh = TaskStore(home).get(task.id)
    assert fresh.attached is False
    # the rail is gone; the run now fails only on the (unconfigured) harness
    with pytest.raises(RunnerError, match="harness"):
        run_task(home, load_config(home), task.id)


def test_digest_renders_attached_sessions_apart_from_active_tasks(home: Path, repo: Path):
    task = adopt(home, repo)
    digest = build_digest(home, TaskStore(home).list(), fsio.utc_now(), [])
    assert "## Attached sessions" in digest
    assert f"- [attached] {task.short_id}" in digest
    # never rendered in the shape the prompt reads as "launch it"
    active_section = digest.split("## Attached sessions")[0]
    assert task.short_id not in active_section
    assert "runner=dead" not in digest