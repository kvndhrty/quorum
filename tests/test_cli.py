"""CLI-level tests. These cover behaviour that only exists in the command
layer — everything else is exercised through the agents and views directly."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from quorum.cli import app

runner = CliRunner()


def heartbeat(home: Path, name: str) -> dict:
    return json.loads((home / "state" / "agents" / name / "heartbeat.json").read_text())


def write_plugin(home: Path, name: str, body: str, schedule: str = "every 1h") -> None:
    (home / "plugins" / f"{name}.py").write_text(body)
    with open(home / "config.toml", "a", encoding="utf-8") as f:
        f.write(f'\n[agents.{name}]\ntype = "{name}:Plug"\nschedule = "{schedule}"\n')


OK_PLUGIN = """
from quorum.agent import Agent


class Plug(Agent):
    def tick(self):
        self.ctx.bus.post(self.name, "testing", "ran", text="tick")
"""

BOOM_PLUGIN = """
from quorum.agent import Agent


class Plug(Agent):
    def tick(self):
        raise RuntimeError("intentional explosion")
"""


def test_run_once_writes_a_heartbeat(home: Path):
    """Without this an agent exercised by hand keeps reading as never-ran in
    `quorum status` and both dashboards, which is how it looked in practice."""
    write_plugin(home, "okplug", OK_PLUGIN)
    result = runner.invoke(app, ["agent", "run-once", "okplug", "--home", str(home)])
    assert result.exit_code == 0, result.output

    hb = heartbeat(home, "okplug")
    assert hb["status"] == "idle"
    assert hb["last_start"] and hb["last_end"]
    assert hb["duration_ms"] >= 0


def test_run_once_records_a_failing_tick(home: Path):
    write_plugin(home, "boomplug", BOOM_PLUGIN)
    result = runner.invoke(app, ["agent", "run-once", "boomplug", "--home", str(home)])
    assert result.exit_code != 0

    hb = heartbeat(home, "boomplug")
    assert hb["status"] == "error"
    assert "intentional explosion" in hb["error"]


def test_run_once_rejects_an_unknown_agent(home: Path):
    result = runner.invoke(app, ["agent", "run-once", "nope", "--home", str(home)])
    assert result.exit_code == 1
    assert "no agent" in result.output


# -- tasks -----------------------------------------------------------------


def setup_task_env(home: Path, tmp_path: Path) -> str:
    """A registered git project plus a fake-harness config; returns the slug."""
    import subprocess
    import sys

    from quorum.projects import ProjectRegistry

    repo = tmp_path / "cliproj"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
            check=True, capture_output=True,
        )

    git("init", "-q")
    (repo / "f.txt").write_text("x")
    git("add", ".")
    git("commit", "-qm", "init")
    ProjectRegistry(home).add(repo, name="cliproj")
    fake = Path(__file__).parent / "bin" / "fake_harness.py"
    with open(home / "config.toml", "a", encoding="utf-8") as f:
        f.write(
            "\n[harness.fake]\n"
            f'start = ["{sys.executable}", "{fake}"]\n'
        )
    return "cliproj"


def test_task_add_requires_known_project_and_harness(home: Path):
    r = runner.invoke(app, ["task", "add", "ghost", "do it", "--home", str(home)])
    assert r.exit_code == 1 and "no project" in r.output


def test_task_lifecycle_through_the_cli(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)

    r = runner.invoke(app, ["task", "add", slug, "tidy the docs", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 0, r.output
    short = r.output.split("queued task ")[1].split(" ")[0]

    r = runner.invoke(app, ["task", "list", "--home", str(home)])
    assert short in r.output and "queued" in r.output

    r = runner.invoke(app, ["task", "nudge", short, "focus on the README", "--home", str(home)])
    assert r.exit_code == 0

    r = runner.invoke(app, ["task", "inbox", short, "--home", str(home)])
    assert "focus on the README" in r.output  # peek does not consume
    r = runner.invoke(app, ["task", "inbox", short, "--claim", "--home", str(home)])
    assert "focus on the README" in r.output
    r = runner.invoke(app, ["task", "inbox", short, "--home", str(home)])
    assert "no guidance waiting" in r.output  # claim consumed it

    r = runner.invoke(app, ["task", "run", short, "--home", str(home)])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["task", "report", short, "opened the PR", "--status", "pr",
                            "--pr-url", "https://example.com/pr/1", "--home", str(home)])
    assert r.exit_code == 0
    r = runner.invoke(app, ["task", "show", short, "--home", str(home)])
    assert '"status": "pr"' in r.output and "https://example.com/pr/1" in r.output
    r = runner.invoke(app, ["task", "tail", short, "--home", str(home)])
    assert "tidy the docs" in r.output  # the fake harness echoes its prompt

    r = runner.invoke(app, ["task", "cancel", short, "--home", str(home)])
    assert r.exit_code == 0
    r = runner.invoke(app, ["task", "list", "--home", str(home)])
    assert "cancelled" in r.output


def test_agent_control_commands_land_in_supervisor_inbox(home: Path):
    from quorum import fsio
    from quorum.messages import MessageBus

    r = runner.invoke(app, ["agent", "pause", "monitor", "--home", str(home)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["agent", "pause", "ghost", "--home", str(home)])
    assert r.exit_code == 1

    inbox = MessageBus(home).inbox_dir / "supervisor" / "new"
    entries = fsio.sorted_entries(inbox)
    assert len(entries) == 1
    msg = fsio.read_json(entries[0])
    assert msg["type"] == "agent.pause" and msg["payload"]["agent"] == "monitor"


def test_run_once_respects_the_tick_lock(home: Path):
    write_plugin(home, "lockplug", OK_PLUGIN)
    lock = home / "state" / "agents" / "lockplug" / "tick.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"pid": 1}\n')  # pid 1 is alive and never ours
    r = runner.invoke(app, ["agent", "run-once", "lockplug", "--home", str(home)])
    assert r.exit_code == 1 and "ticking elsewhere" in r.output
    lock.unlink()
    r = runner.invoke(app, ["agent", "run-once", "lockplug", "--home", str(home)])
    assert r.exit_code == 0, r.output
