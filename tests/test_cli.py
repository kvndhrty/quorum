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
