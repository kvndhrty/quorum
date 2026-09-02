"""CLI-level tests. These cover behaviour that only exists in the command
layer — everything else is exercised through the agents and views directly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def test_run_once_clears_a_failure_streak(home: Path):
    """A hand-run tick that demonstrably works ends the streak. Without this
    the escalation stamp survives a proven-working run-once, and the next
    outage would never reach the attention banner."""
    from quorum.agent import write_heartbeat

    write_plugin(home, "okplug", OK_PLUGIN)
    write_heartbeat(
        home,
        "okplug",
        status="error",
        error="boom",
        consecutive_failures=7,
        escalated_at="2026-08-30T22:10:04Z",
    )

    result = runner.invoke(app, ["agent", "run-once", "okplug", "--home", str(home)])
    assert result.exit_code == 0, result.output

    hb = heartbeat(home, "okplug")
    assert hb["status"] == "idle"
    assert hb["error"] is None
    assert hb["consecutive_failures"] == 0
    assert hb["escalated_at"] is None


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
    assert "status:   pr" in r.output and "https://example.com/pr/1" in r.output
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


def test_a_torn_journal_line_does_not_break_the_cap_count(
    home: Path, tmp_path: Path, monkeypatch
):
    """The journal is read back to count this run's actions; a line that is
    valid JSON but not an object must be skipped, not crash every action."""
    from quorum import fsio
    from quorum.actor import journal_path

    slug = setup_task_env(home, tmp_path)
    journal = journal_path(home, "alpha")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('"not even a dict"\n')

    monkeypatch.setenv("QUORUM_ACTOR", "alpha")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01TORNRUN")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "2")

    r = runner.invoke(app, ["task", "add", slug, "after the torn line", "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 0, r.output
    entries = [e for e in fsio.read_jsonl(journal) if isinstance(e, dict)]
    assert [e["action"] for e in entries] == ["task.add"]


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
    assert outcomes["task-perpetual.md"] == "seeded"  # the perpetual block (#12)

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


def test_init_points_an_edited_prompt_at_the_overlay(tmp_path: Path):
    """An `edited` prompt is a home that stopped receiving upgrades; init has
    to say what to do about it, not just that it happened (#37)."""
    from quorum import home as home_mod

    target = tmp_path / "qhome"
    home_mod.scaffold(target)
    (target / "prompts" / "manager.md").write_text("my custom manager policy\n")

    result = runner.invoke(app, ["init", "--home", str(target)])
    assert result.exit_code == 0
    out = _plain(result.output)
    assert "quorum prompt diff manager" in out
    assert "prompts/manager.local.md" in out
    assert "{local}" in out


def test_prompt_diff_and_list_show_home_vs_packaged(home: Path):
    r = runner.invoke(app, ["prompt", "diff", "manager"])
    assert r.exit_code == 0
    assert "identical to the packaged default" in r.output

    (home / "prompts" / "manager.md").write_text("my custom manager policy\n")
    (home / "prompts" / "manager.local.md").write_text("one task at a time\n")
    r = runner.invoke(app, ["prompt", "diff", "manager.md"])  # .md tolerated
    assert r.exit_code == 0
    out = _plain(r.output)
    assert "-You are the manager of a quorum home" in out  # what you are missing
    assert "+my custom manager policy" in out
    assert "delete prompts/manager.md" in out

    r = runner.invoke(app, ["prompt", "list"])
    assert r.exit_code == 0
    out = _plain(r.output)
    assert "manager" in out and "edited" in out
    assert "manager.local.md (prepended)" in out  # the edit has no {local} slot
    assert "task-preamble" in out and "matches the packaged default" in out
    # an overlay is not a template of its own
    assert not any(line.split()[:1] == ["manager.local"] for line in out.splitlines())

    # a misspelled overlay is dead policy nobody would ever notice
    (home / "prompts" / "manger.local.md").write_text("oops\n")
    r = runner.invoke(app, ["prompt", "list"])
    assert r.exit_code == 0
    assert "manger.local.md: no prompt named 'manger'" in _plain(r.output)

    # a template quorum does not package has nothing to diff against
    r = runner.invoke(app, ["prompt", "diff", "nope"])
    assert r.exit_code == 1 and "packages no default prompt" in _plain(r.output)

    (home / "prompts" / "manager.md").unlink()
    r = runner.invoke(app, ["prompt", "diff", "manager"])
    assert r.exit_code == 0 and "packaged default unchanged" in r.output


def test_prompt_list_and_diff_degrade_over_an_unreadable_file(home: Path):
    """One prompt quorum cannot decode must not take the whole listing down
    with it — mark that file and keep going (review of #37)."""
    (home / "prompts" / "manager.md").write_bytes(b"\xff\xfe not utf-8\n")
    (home / "prompts" / "task-preamble.local.md").write_bytes(b"\xff\xfe policy\n")

    r = runner.invoke(app, ["prompt", "list"])
    assert r.exit_code == 0, r.output
    out = _plain(r.output)
    assert "manager" in out and "unreadable" in out
    assert "task-preamble.local.md (? unreadable — ignored when rendering)" in out
    assert "task-perpetual" in out and "matches the packaged default" in out

    r = runner.invoke(app, ["prompt", "diff", "manager"])
    assert r.exit_code == 1
    assert "cannot be read" in _plain(r.output)
    assert "Traceback" not in r.output


def test_agent_create_can_reuse_a_shipped_prompt(home: Path):
    """The babysitter example ships as a packaged prompt; creating an agent
    over it must not require pasting the prompt back in."""
    r = runner.invoke(app, [
        "agent", "create", "babysitter", "--schedule", "every 10m", "--home", str(home),
    ])
    assert r.exit_code == 0, r.output
    assert (home / "agents" / "babysitter.toml").exists()
    assert "CI babysitter" in (home / "prompts" / "babysitter.md").read_text()  # untouched

    # ...under any name, via --prompt
    r = runner.invoke(app, [
        "agent", "create", "ci-cop", "--prompt", "babysitter", "--home", str(home),
    ])
    assert r.exit_code == 0, r.output
    assert 'prompt = "babysitter"' in (home / "agents" / "ci-cop.toml").read_text()
    assert not (home / "prompts" / "ci-cop.md").exists()

    r = runner.invoke(app, ["agent", "create", "nope", "--prompt", "ghost", "--home", str(home)])
    assert r.exit_code == 1 and "prompts/ghost.md" in r.output
    r = runner.invoke(app, [
        "agent", "create", "nope", "--prompt", "babysitter", "--prompt-text", "x",
        "--home", str(home),
    ])
    assert r.exit_code == 1 and "drop --prompt-text" in r.output


def _quorum_invocations(text: str) -> list[str]:
    """Every `quorum ...` command a prompt tells an agent to run: inline code
    spans, list-item tool lines, and indented example blocks."""
    import re

    found = [span for span in re.findall(r"`([^`\n]+)`", text) if span.startswith("quorum ")]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if stripped.startswith("quorum "):
            found.append(stripped)
    return found


def test_shipped_prompts_only_name_real_cli_commands():
    """The packaged prompts ARE the product's policy layer; a command that
    was renamed out from under one fails silently at 3am, in a transcript
    nobody reads."""
    import re
    from importlib import resources

    def cmd_name(info) -> str:
        # An unnamed @app.command() takes its name from the callback.
        return info.name or info.callback.__name__.rstrip("_").replace("_", "-")

    known = {cmd_name(c) for c in app.registered_commands}
    for group in app.registered_groups:
        known |= {
            f"{group.name} {cmd_name(c)}" for c in group.typer_instance.registered_commands
        }

    checked = 0
    for entry in (resources.files("quorum") / "default_prompts").iterdir():
        if not entry.name.endswith(".md"):
            continue
        for invocation in _quorum_invocations(entry.read_text(encoding="utf-8")):
            words: list[str] = []
            for token in invocation.split()[1:]:
                if len(words) == 2 or not re.fullmatch(r"[a-z][a-z-]*", token):
                    break
                words.append(token)
            assert words, f"{entry.name}: bare `quorum` in {invocation!r}"
            assert " ".join(words) in known or words[0] in known, (
                f"{entry.name} names a command that does not exist: {invocation!r}"
            )
            checked += 1
    assert checked > 10  # the extractor still finds things


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


# -- Phase-A UX rails: help rendering, version, attention surfacing ----------


def _plain(text: str) -> str:
    """Help output minus ANSI codes — Rich force-colors on CI (GitHub Actions)."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_help_keeps_config_table_names():
    """Rich treats [bracketed] text as markup; unescaped, the help would tell
    users to edit "" instead of [harness.<name>] / [tasks] / [web]."""
    r = runner.invoke(app, ["task", "add", "--help"])
    assert "[harness.<name>]" in _plain(r.output)
    assert "[tasks].default_harness" in _plain(r.output)
    r = runner.invoke(app, ["web", "--help"])
    assert "[web]" in _plain(r.output)


def test_version_flag():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.output.startswith("quorum ")


def test_top_level_home_is_accepted(home: Path, monkeypatch):
    monkeypatch.delenv("QUORUM_HOME")
    r = runner.invoke(app, ["--home", str(home), "task", "list"])
    assert r.exit_code == 0, r.output
    assert "no tasks" in r.output


def test_status_surfaces_attention_and_empty_state(home: Path):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    assert "no tasks" in r.output
    assert "no projects registered" in r.output
    assert "#attention" not in r.output

    from quorum.messages import MessageBus

    MessageBus(home).post("manager", "attention", text="need a human decision")
    r = runner.invoke(app, ["status"])
    assert "1 on #attention" in r.output
    assert "quorum board read attention" in r.output


# -- Phase-B UX rails: doctor, lifecycle, humanized output, validation -------


def test_doctor_walks_a_setup_to_green(home: Path, tmp_path: Path):
    cfg = home / "config.toml"
    # The CI probe is on by default and the machine running these tests may
    # well have a gh that is installed but unauthenticated — which doctor is
    # right to call a problem, and which has nothing to do with this test.
    cfg.write_text(cfg.read_text().replace("[ci]\nenabled = true", "[ci]\nenabled = false"))

    # fresh scaffold: no harness uncommented yet. An unmade decision, not a
    # fault — one `–` line about it, and a green exit.
    r = runner.invoke(app, ["doctor", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "no harness configured yet" in r.output

    slug = setup_task_env(home, tmp_path)  # a [harness.fake] table, no default yet
    r = runner.invoke(app, ["doctor", "--home", str(home)])
    assert r.exit_code == 1
    assert "default_harness is unset" in r.output

    cfg.write_text(cfg.read_text().replace('default_harness = ""', 'default_harness = "fake"'))
    r = runner.invoke(app, ["doctor", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "all checks passed" in r.output
    assert f"project {slug}" in r.output

    # a harness whose binary is missing fails loudly
    with open(cfg, "a", encoding="utf-8") as f:
        f.write('\n[harness.ghost]\nstart = ["no-such-binary-xyz"]\n')
    r = runner.invoke(app, ["doctor", "--home", str(home)])
    assert r.exit_code == 1
    assert "no-such-binary-xyz" in r.output and "not found on PATH" in r.output


def test_task_show_is_human_first_json_on_request(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "tidy the docs", "--harness", "fake", "--home", str(home)])
    short = r.output.split("queued task ")[1].split(" ")[0]

    r = runner.invoke(app, ["task", "show", short, "--home", str(home)])
    assert r.exit_code == 0
    assert "project:  " + slug in r.output
    assert "tidy the docs" in r.output
    assert not r.output.lstrip().startswith("{")

    r = runner.invoke(app, ["task", "show", short, "--json", "--home", str(home)])
    record = json.loads(r.output)
    assert record["prompt"] == "tidy the docs"


def test_list_commands_emit_json(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    runner.invoke(app, ["task", "add", slug, "a task", "--harness", "fake", "--home", str(home)])
    tasks = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)
    assert tasks[0]["project"] == slug
    projects = json.loads(runner.invoke(app, ["project", "list", "--json", "--home", str(home)]).output)
    assert projects[0]["slug"] == slug
    agents = json.loads(runner.invoke(app, ["agent", "list", "--json", "--home", str(home)]).output)
    assert any(a["name"] == "manager" for a in agents)
    overview = json.loads(runner.invoke(app, ["status", "--json", "--home", str(home)]).output)
    assert overview["attention"]["count"] == 0


def test_status_and_task_show_surface_what_a_run_spent(
    home: Path, tmp_path: Path, monkeypatch
):
    """Surfacing end to end: a usage-reporting run shows up in `quorum status`,
    `task list --json` and `task show`, and a configured budget marks the row
    without stopping anything."""
    slug = setup_task_env(home, tmp_path)
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("worktree = true", "worktree = true\nmax_cost_per_run = 0.10"))
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    r = runner.invoke(app, ["task", "add", slug, "spendy work", "--harness", "fake", "--home", str(home)])
    short = r.output.split("queued task ")[1].split(" ")[0]
    assert runner.invoke(app, ["task", "run", short, "--home", str(home)]).exit_code == 0

    r = runner.invoke(app, ["status", "--home", str(home)])
    assert "$0.42 · 11.0k tok" in r.output
    assert "$!" in r.output  # over the configured budget — marked, not blocked

    row = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)[0]
    assert row["usage"]["cost_usd"] == 0.42 and row["usage"]["runs"] == 1
    assert row["budget_overages"] == ["run 1: cost $0.42 > max_cost_per_run $0.10"]

    r = runner.invoke(app, ["task", "show", short, "--home", str(home)])
    assert "usage:    $0.42 · 11.0k tok" in r.output
    assert "budget:   run 1: cost $0.42 > max_cost_per_run $0.10" in r.output


def test_status_stays_clean_when_no_harness_reports_usage(home: Path, tmp_path: Path):
    """The fail-soft half: no usage reported, nothing shown, no zeros."""
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "quiet work", "--harness", "fake", "--home", str(home)])
    short = r.output.split("queued task ")[1].split(" ")[0]
    runner.invoke(app, ["task", "run", short, "--home", str(home)])

    r = runner.invoke(app, ["status", "--home", str(home)])
    assert "tok" not in r.output and "$" not in r.output
    row = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)[0]
    assert row["usage"] is None and row["usage_text"] == "" and row["budget_overages"] == []
    r = runner.invoke(app, ["task", "show", short, "--home", str(home)])
    assert "usage:" not in r.output


def test_budget_config_is_validated(home: Path):
    from quorum.config import ConfigError, load_config

    cfg = home / "config.toml"
    original = cfg.read_text()
    cfg.write_text(original.replace("worktree = true", "worktree = true\nmax_cost_per_run = -1"))
    with pytest.raises(ConfigError, match="max_cost_per_run must be >= 0"):
        load_config(home)

    cfg.write_text(original.replace("worktree = true", "worktree = true\nmax_tokens_per_run = -5"))
    with pytest.raises(ConfigError, match="max_tokens_per_run must be >= 0"):
        load_config(home)

    cfg.write_text(original.replace("worktree = true", "worktree = true\nmax_cost_per_run = 2.5"))
    assert load_config(home).tasks.max_cost_per_run == 2.5
    assert load_config(home).tasks.max_tokens_per_run == 0  # off by default


def test_status_legend_names_the_glyphs(home: Path):
    r = runner.invoke(app, ["status", "--legend"])
    assert r.exit_code == 0
    assert "⚭" in r.output and "▶" in r.output and "‖" in r.output


def test_project_add_validates_the_directory(home: Path, tmp_path: Path):
    r = runner.invoke(app, ["project", "add", str(tmp_path / "nope"), "--home", str(home)])
    assert r.exit_code == 1
    assert "does not exist" in r.output

    plain = tmp_path / "plain-dir"
    plain.mkdir()
    r = runner.invoke(app, ["project", "add", str(plain), "--home", str(home)])
    assert r.exit_code == 1
    assert "not a git repository" in r.output and "--force" in r.output

    r = runner.invoke(app, ["project", "add", str(plain), "--force", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "registered project" in r.output


def test_destructive_commands_pass_through_without_a_tty(home: Path, tmp_path: Path):
    """CliRunner's stdin is not a tty, so scripts and harness-driven agents
    keep working with no prompt; --yes is for interactive shells."""
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["project", "remove", slug, "--home", str(home)])
    assert r.exit_code == 0
    assert "removed" in r.output


def test_up_detach_and_down(home: Path):
    r = runner.invoke(app, ["up", "--detach", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "running detached" in r.output
    try:
        r = runner.invoke(app, ["status", "--home", str(home)])
        assert "supervisor: running" in r.output
        r = runner.invoke(app, ["up", "--detach", "--home", str(home)])
        assert r.exit_code == 1 and "already running" in r.output
    finally:
        r = runner.invoke(app, ["down", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "supervisor stopped" in r.output
    r = runner.invoke(app, ["down", "--home", str(home)])
    assert r.exit_code == 1
    assert "not running" in r.output


def test_run_once_failure_is_one_line_not_a_traceback(home: Path):
    write_plugin(home, "boom2", BOOM_PLUGIN)
    r = runner.invoke(app, ["agent", "run-once", "boom2", "--home", str(home)])
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert "intentional explosion" in r.output and "--verbose" in r.output


def test_task_add_after_chains_two_tasks(home: Path, tmp_path: Path):
    """--after persists resolved full ids, refuses the premature run, and
    lets it through once the upstream finishes (#31)."""
    slug = setup_task_env(home, tmp_path)

    r = runner.invoke(app, ["task", "add", slug, "build it", "--harness", "fake", "--home", str(home)])
    first = r.output.split("queued task ")[1].split(" ")[0]

    r = runner.invoke(app, ["task", "add", slug, "review the PR", "--harness", "fake",
                            "--after", first, "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert f"waits on: {first}" in r.output
    second = r.output.split("queued task ")[1].split(" ")[0]

    # persisted as the resolved *full* id
    from quorum.tasks import TaskStore

    store = TaskStore(home)
    upstream, dependent = store.resolve(first), store.resolve(second)
    assert dependent.depends_on == [upstream.id]

    r = runner.invoke(app, ["task", "show", second, "--home", str(home)])
    assert f"after:    {first}  (waiting on {first})" in r.output
    r = runner.invoke(app, ["task", "list", "--home", str(home)])
    assert f"waiting-on {first}" in r.output

    r = runner.invoke(app, ["task", "run", second, "--home", str(home)])
    assert r.exit_code == 1 and f"waiting on {first}" in r.output
    assert store.resolve(second).runs == []

    # --force is the escape hatch, and the refusal lifts on its own once the
    # dependency reaches a terminal status
    r = runner.invoke(app, ["task", "report", first, "shipped", "--status", "done", "--home", str(home)])
    assert r.exit_code == 0
    r = runner.invoke(app, ["task", "run", second, "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert len(store.resolve(second).runs) == 1


def test_task_add_after_rejects_an_unknown_dependency(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "review something", "--harness", "fake",
                            "--after", "zzzzzz", "--home", str(home)])
    assert r.exit_code == 1 and "no task matching" in r.output
    from quorum.tasks import TaskStore

    assert TaskStore(home).list() == []  # nothing queued on a bad dependency


def test_task_run_force_overrides_the_dependency_refusal(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "build it", "--harness", "fake", "--home", str(home)])
    first = r.output.split("queued task ")[1].split(" ")[0]
    r = runner.invoke(app, ["task", "add", slug, "review it", "--harness", "fake",
                            "--after", first, "--home", str(home)])
    second = r.output.split("queued task ")[1].split(" ")[0]

    r = runner.invoke(app, ["task", "run", second, "--force", "--home", str(home)])
    assert r.exit_code == 0, r.output
    from quorum.tasks import TaskStore

    assert len(TaskStore(home).resolve(second).runs) == 1
# -- perpetual tasks (#12) ---------------------------------------------------


def test_perpetual_tasks_are_queued_and_badged_everywhere(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(
        app,
        ["task", "add", slug, "watch CI forever", "--perpetual", "--harness", "fake",
         "--home", str(home)],
    )
    assert r.exit_code == 0, r.output
    assert "queued perpetual task" in r.output and "task cancel" in r.output
    short = r.output.split("queued perpetual task ")[1].split(" ")[0]

    row = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)[0]
    assert row["perpetual"] is True

    assert "∞" in runner.invoke(app, ["task", "list", "--home", str(home)]).output
    assert "∞" in runner.invoke(app, ["status", "--home", str(home)]).output
    assert "∞" in runner.invoke(app, ["status", "--legend"]).output
    assert "perpetual" in runner.invoke(app, ["task", "show", short, "--home", str(home)]).output


def test_an_ordinary_task_carries_no_perpetual_badge(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    runner.invoke(app, ["task", "add", slug, "one-off", "--harness", "fake", "--home", str(home)])
    row = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)[0]
    assert row["perpetual"] is False
    assert "∞" not in runner.invoke(app, ["task", "list", "--home", str(home)]).output


# -- one load-config fallback (#34) ------------------------------------------


def test_load_config_or_default_is_the_one_fallback(home: Path, tmp_path: Path):
    """Missing and malformed config.toml both degrade to defaults for the
    read-only callers — and `try_load_config` tells a malformed one apart
    from a config that parsed (or was never written), which is what the
    fail-soft probes need."""
    from quorum.config import load_config_or_default, try_load_config

    empty = tmp_path / "no-home"
    empty.mkdir()
    # no file = the user said nothing: plain defaults, so the fail-soft
    # probes keep auto-detecting (only an *unreadable* file is None)
    assert try_load_config(empty).ci.enabled is True
    assert load_config_or_default(empty).tasks.default_harness == ""

    (home / "config.toml").write_text("[tasks\nthis is not toml")
    assert try_load_config(home) is None
    assert load_config_or_default(home).ci.enabled is True  # the model default

    (home / "config.toml").write_text('[tasks]\ndefault_harness = "claude"\n')
    assert try_load_config(home).tasks.default_harness == "claude"
    assert load_config_or_default(home).tasks.default_harness == "claude"


def test_views_still_render_over_a_broken_config(home: Path):
    """Views never demand config: a syntax error must not blank the dashboard."""
    from quorum import views

    (home / "config.toml").write_text("nonsense = [[[")
    overview = views.overview(home)
    assert overview["agents"] == [] and overview["tasks"] == []
