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

    r = runner.invoke(app, ["agent", "pause", "manager", "--home", str(home)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["agent", "pause", "ghost", "--home", str(home)])
    assert r.exit_code == 1

    inbox = MessageBus(home).inbox_dir / "supervisor" / "new"
    entries = fsio.sorted_entries(inbox)
    assert len(entries) == 1
    msg = fsio.read_json(entries[0])
    assert msg["type"] == "agent.pause" and msg["payload"]["agent"] == "manager"


def test_agent_create_remove_and_reload(home: Path):
    from quorum import fsio
    from quorum.messages import MessageBus

    r = runner.invoke(app, [
        "agent", "create", "standup", "--schedule", "every 30m",
        "--prompt-text", "post a standup note", "--harness", "fake", "--home", str(home),
    ])
    assert r.exit_code == 0, r.output
    assert (home / "agents" / "standup.toml").exists()
    assert (home / "prompts" / "standup.md").read_text() == "post a standup note"

    inbox = MessageBus(home).inbox_dir / "supervisor" / "new"
    entries = fsio.sorted_entries(inbox)
    assert len(entries) == 1
    assert fsio.read_json(entries[0])["type"] == "agent.reload"

    r = runner.invoke(app, ["agent", "list", "--home", str(home)])
    assert "standup" in r.output

    # duplicates and promptless prompt agents are refused
    r = runner.invoke(app, [
        "agent", "create", "standup", "--prompt-text", "again", "--home", str(home),
    ])
    assert r.exit_code == 1 and "already exists" in r.output
    r = runner.invoke(app, ["agent", "create", "mute", "--home", str(home)])
    assert r.exit_code == 1 and "--prompt-text or --prompt-file" in r.output

    # editing + reload is the update path
    r = runner.invoke(app, ["agent", "reload", "standup", "--home", str(home)])
    assert r.exit_code == 0, r.output

    # removal deletes the file, keeps the prompt, and pokes the supervisor
    r = runner.invoke(app, ["agent", "remove", "standup", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert not (home / "agents" / "standup.toml").exists()
    assert (home / "prompts" / "standup.md").exists()
    types = [fsio.read_json(p)["type"] for p in fsio.sorted_entries(inbox)]
    assert types.count("agent.reload") == 3

    # config.toml-defined agents are not removable from the CLI
    r = runner.invoke(app, ["agent", "remove", "manager", "--home", str(home)])
    assert r.exit_code == 1 and "config.toml" in r.output


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


# -- manager ---------------------------------------------------------------


def test_manager_tell_note_and_journal(home: Path):
    from quorum import fsio
    from quorum.agents.manager import journal_path
    from quorum.messages import MessageBus

    r = runner.invoke(app, ["manager", "tell", "focus on the api task", "--home", str(home)])
    assert r.exit_code == 0
    inbox = MessageBus(home).inbox_dir / "manager" / "new"
    assert len(fsio.sorted_entries(inbox)) == 1

    r = runner.invoke(app, ["manager", "note", "human-added context", "--home", str(home)])
    assert r.exit_code == 0
    entries = fsio.read_jsonl(journal_path(home))
    assert entries[-1]["action"] == "note" and entries[-1]["actor"] == "user"

    r = runner.invoke(app, ["manager", "journal", "--home", str(home)])
    assert "human-added context" in r.output


def test_mutating_commands_journal_only_for_the_manager_actor(
    home: Path, tmp_path: Path, monkeypatch
):
    from quorum import fsio
    from quorum.agents.manager import journal_path

    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "user-made task", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert fsio.read_jsonl(journal_path(home)) == []  # user actions: no journal

    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01TESTRUN")
    r = runner.invoke(app, ["task", "add", slug, "manager-made task", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 0, r.output
    entries = fsio.read_jsonl(journal_path(home))
    assert len(entries) == 1
    assert entries[0]["action"] == "task.add"
    assert entries[0]["actor"] == "manager" and entries[0]["run"] == "01TESTRUN"


def test_non_manager_actor_journals_to_its_own_path_and_hits_cap(
    home: Path, tmp_path: Path, monkeypatch
):
    from quorum import fsio
    from quorum.actor import journal_path

    slug = setup_task_env(home, tmp_path)
    monkeypatch.setenv("QUORUM_ACTOR", "alpha")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01ALPHARUN")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "2")

    for i in range(2):
        r = runner.invoke(app, ["task", "add", slug, f"alpha task {i}", "--harness", "fake", "--home", str(home)])
        assert r.exit_code == 0, r.output
    entries = fsio.read_jsonl(journal_path(home, "alpha"))
    assert len(entries) == 2
    assert all(e["actor"] == "alpha" and e["run"] == "01ALPHARUN" for e in entries)
    assert fsio.read_jsonl(journal_path(home)) == []  # the manager journal stays untouched

    r = runner.invoke(app, ["task", "add", slug, "one too many", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 1
    assert "alpha action cap (2) reached" in r.output


def test_detached_run_journals_once_not_twice(home: Path, tmp_path: Path, monkeypatch):
    """The detached child re-invokes `quorum task run`; without stripping the
    actor env it would journal a second entry (and burn the manager's cap)."""
    import time

    from quorum import fsio
    from quorum.agents.manager import journal_path
    from quorum.tasks import TaskStore, runner_lock_path

    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "detach journaling", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 0, r.output
    task = TaskStore(home).list()[0]

    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01DETACH")
    r = runner.invoke(app, ["task", "run", task.short_id, "--detach", "--home", str(home)])
    assert r.exit_code == 0, r.output

    deadline = time.time() + 15
    while time.time() < deadline:
        fresh = TaskStore(home).get(task.id)
        if fresh.runs and not runner_lock_path(home, task.id).exists():
            break
        time.sleep(0.3)
    else:
        raise AssertionError("detached run did not complete in time")

    entries = [e for e in fsio.read_jsonl(journal_path(home)) if e["run"] == "01DETACH"]
    assert len(entries) == 1  # the manager's own action — not the child's re-invocation


# -- init / prompt seeding -------------------------------------------------


def test_init_upgrades_pristine_prompts_and_keeps_edits(tmp_path: Path, monkeypatch):
    import hashlib

    from quorum import home as home_mod

    target = tmp_path / "qhome"
    fresh, outcomes = home_mod.scaffold(target)
    assert fresh
    assert outcomes["task-preamble.md"] == "seeded"

    # a pristine seed from an older quorum: content whose hash is registered
    old_default = "old packaged preamble\n"
    monkeypatch.setitem(
        home_mod.SUPERSEDED_PROMPT_HASHES,
        "task-preamble.md",
        {hashlib.sha256(old_default.encode()).hexdigest()},
    )
    preamble = target / "prompts" / "task-preamble.md"
    preamble.write_text(old_default)
    # a user-edited prompt: never touched, only reported
    manager = target / "prompts" / "manager.md"
    manager.write_text("my custom manager policy\n")

    fresh, outcomes = home_mod.scaffold(target)
    assert not fresh
    assert outcomes == {"task-preamble.md": "upgraded", "manager.md": "edited"}
    assert "git push" in preamble.read_text()  # the current packaged default
    assert manager.read_text() == "my custom manager policy\n"

    # up-to-date files produce no outcome at all
    _, outcomes = home_mod.scaffold(target)
    assert outcomes == {"manager.md": "edited"}

    result = runner.invoke(app, ["init", "--home", str(target)])
    assert result.exit_code == 0
    assert "keeping your edits" in result.output


def test_superseded_hashes_never_contain_the_current_defaults():
    """A current default hashed into SUPERSEDED_PROMPT_HASHES would make
    `quorum init` treat up-to-date files as stale forever; the set must only
    hold *replaced* versions."""
    import hashlib
    from importlib import resources

    from quorum import home as home_mod

    for entry in (resources.files("quorum") / "default_prompts").iterdir():
        if not entry.name.endswith(".md"):
            continue
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        assert digest not in home_mod.SUPERSEDED_PROMPT_HASHES.get(entry.name, set())
