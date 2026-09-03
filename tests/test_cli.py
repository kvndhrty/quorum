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


# A prompt that would fight the shell: quotes, backticks, blank lines, and a
# trailing newline the way `gh issue view ... | ...` delivers one.
ISSUE_PROMPT = (
    "## Problem\n\n`quorum task add` takes \"the prompt\" as an argv string.\n\n"
    "## Proposal\n\n- read it from stdin\n"
)


def stored_prompt(home: Path) -> str:
    from quorum.tasks import TaskStore

    tasks = TaskStore(home).list()
    assert len(tasks) == 1
    return tasks[0].prompt


def test_task_add_reads_prompt_from_stdin(home: Path, tmp_path: Path):
    """`-` pipes the prompt in, byte-for-byte — no stripping, no newline
    translation: what the harness reads must be what was piped."""
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(
        app, ["task", "add", slug, "-", "--harness", "fake", "--home", str(home)],
        input=ISSUE_PROMPT,
    )
    assert r.exit_code == 0, r.output
    assert stored_prompt(home) == ISSUE_PROMPT


def test_task_add_reads_prompt_from_file(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    body = tmp_path / "issue.md"
    body.write_bytes(ISSUE_PROMPT.encode("utf-8"))
    r = runner.invoke(
        app,
        ["task", "add", slug, "--prompt-file", str(body), "--harness", "fake", "--home", str(home)],
    )
    assert r.exit_code == 0, r.output
    assert stored_prompt(home) == ISSUE_PROMPT


def test_task_add_prompt_file_keeps_crlf(home: Path, tmp_path: Path):
    """read_bytes, not read_text: universal newlines would rewrite the file."""
    slug = setup_task_env(home, tmp_path)
    body = tmp_path / "crlf.md"
    body.write_bytes(b"line one\r\nline two\r\n")
    r = runner.invoke(
        app,
        ["task", "add", slug, "--prompt-file", str(body), "--harness", "fake", "--home", str(home)],
    )
    assert r.exit_code == 0, r.output
    assert stored_prompt(home) == "line one\r\nline two\r\n"


@pytest.mark.parametrize(
    "args",
    [
        ["do it", "--prompt-file", "PROMPT"],
        ["-", "--prompt-file", "PROMPT"],
    ],
)
def test_task_add_refuses_two_prompt_sources(home: Path, tmp_path: Path, args: list[str]):
    slug = setup_task_env(home, tmp_path)
    body = tmp_path / "issue.md"
    body.write_text(ISSUE_PROMPT)
    args = [str(body) if a == "PROMPT" else a for a in args]
    r = runner.invoke(
        app, ["task", "add", slug, *args, "--harness", "fake", "--home", str(home)],
        input=ISSUE_PROMPT,
    )
    assert r.exit_code == 1
    assert "exactly one way" in r.output
    from quorum.tasks import TaskStore

    assert TaskStore(home).list() == []


def test_task_add_without_a_prompt_says_how(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "--harness", "fake", "--home", str(home)])
    assert r.exit_code == 1
    assert "--prompt-file" in r.output


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_task_add_refuses_empty_input(home: Path, tmp_path: Path, source: str):
    """A whitespace-only prompt would queue, launch, and waste a whole run."""
    slug = setup_task_env(home, tmp_path)
    args = ["-"]
    if source == "file":
        blank = tmp_path / "blank.md"
        blank.write_text("\n  \n")
        args = ["--prompt-file", str(blank)]
    r = runner.invoke(
        app, ["task", "add", slug, *args, "--harness", "fake", "--home", str(home)],
        input="\n  \n",
    )
    assert r.exit_code == 1
    assert "empty prompt" in r.output
    from quorum.tasks import TaskStore

    assert TaskStore(home).list() == []


def test_task_add_checks_everything_it_can_before_consuming_stdin(
    home: Path, tmp_path: Path, monkeypatch
):
    """A piped issue is gone the moment stdin is drained, so nothing that can
    be checked without it — the slug, the harness, `--after` — may be checked
    after it."""
    from quorum import cli

    slug = setup_task_env(home, tmp_path)
    read: list[str] = []
    monkeypatch.setattr(cli, "_stdin_prompt", lambda: read.append("drained") or ISSUE_PROMPT)

    for args, expected in (
        (["ghots", "-", "--harness", "fake"], "no project"),
        ([slug, "-", "--harness", "nope"], "no [harness.nope]"),
        ([slug, "-", "--harness", "fake", "--after", "ZZZZZZ"], "ZZZZZZ"),
    ):
        r = runner.invoke(app, ["task", "add", *args, "--home", str(home)], input=ISSUE_PROMPT)
        assert r.exit_code == 1 and expected in r.output
        assert read == []

    r = runner.invoke(
        app, ["task", "add", slug, "-", "--harness", "fake", "--home", str(home)],
        input=ISSUE_PROMPT,
    )
    assert r.exit_code == 0, r.output
    assert read == ["drained"] and stored_prompt(home) == ISSUE_PROMPT


def test_task_add_says_it_is_waiting_on_a_typed_prompt(home: Path, tmp_path: Path, monkeypatch):
    """`-` with nothing piped in blocks on a read that otherwise looks like a
    hang; a piped one says nothing extra."""
    from quorum import cli

    slug = setup_task_env(home, tmp_path)
    args = ["task", "add", slug, "-", "--harness", "fake", "--home", str(home)]
    assert "ctrl-D" not in runner.invoke(app, args, input=ISSUE_PROMPT).output

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    r = runner.invoke(app, args, input=ISSUE_PROMPT)
    assert r.exit_code == 0, r.output
    assert "reading the prompt from stdin" in r.output and "ctrl-D" in r.output


def test_task_add_reports_an_unreadable_prompt_file(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    missing = tmp_path / "nope.md"
    r = runner.invoke(
        app,
        ["task", "add", slug, "--prompt-file", str(missing), "--harness", "fake", "--home", str(home)],
    )
    assert r.exit_code == 1 and "cannot read" in r.output


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
def test_task_run_refuses_after_an_over_budget_run(home: Path, tmp_path: Path, monkeypatch):
    """The budget gate as the manager meets it: `task run` (and `--detach`,
    in the parent) refuse a task whose last run went over, the views say so,
    and `--force` is the override."""
    slug = setup_task_env(home, tmp_path)
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("worktree = true", "worktree = true\nmax_cost_per_run = 0.10"))
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    r = runner.invoke(app, ["task", "add", slug, "spendy work", "--harness", "fake", "--home", str(home)])
    short = r.output.split("queued task ")[1].split(" ")[0]
    assert runner.invoke(app, ["task", "run", short, "--home", str(home)]).exit_code == 0

    from quorum.tasks import TaskStore

    for extra in ([], ["--detach"]):
        r = runner.invoke(app, ["task", "run", short, *extra, "--home", str(home)])
        assert r.exit_code == 1, r.output
        assert "exceeded its budget" in r.output and "next run gated" in r.output
        assert "--force" in r.output
        assert len(TaskStore(home).resolve(short).runs) == 1

    r = runner.invoke(app, ["task", "list", "--home", str(home)])
    assert "$! GATED" in r.output
    row = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)[0]
    assert row["budget_gated"] is True
    r = runner.invoke(app, ["task", "show", short, "--home", str(home)])
    assert "gated:    the last run exceeded its budget" in r.output

    r = runner.invoke(app, ["task", "run", short, "--force", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert len(TaskStore(home).resolve(short).runs) == 2


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


# -- the merged observation (#57) --------------------------------------------


def test_a_merged_or_closed_pr_is_badged_everywhere(home: Path, tmp_path: Path):
    """`done ✔` is delivered; `done ⊘` is a PR a human closed unmerged. Both
    are read straight off task.json — the manager tick recorded them, this
    command never probes a forge."""
    from quorum.tasks import TaskStore

    slug = setup_task_env(home, tmp_path)
    store = TaskStore(home)
    shipped = store.add(slug, "shipped it", "fake", status="done")
    store.update(shipped.id, pr_state="merged", pr_state_at="2026-01-01T00:00:00Z")
    dropped = store.add(slug, "abandoned", "fake", status="done")
    store.update(dropped.id, pr_state="closed", pr_state_at="2026-01-01T00:00:00Z")
    store.add(slug, "never observed", "fake", status="done")

    rows = json.loads(runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output)
    assert [r["pr_state"] for r in rows] == ["merged", "closed", None]

    listing = runner.invoke(app, ["task", "list", "--home", str(home)]).output
    assert "done ✔" in listing and "done ⊘" in listing
    assert "✔" in runner.invoke(app, ["status", "--home", str(home)]).output
    assert "✔" in runner.invoke(app, ["status", "--legend"]).output
    shown = runner.invoke(app, ["task", "show", shipped.short_id, "--home", str(home)]).output
    assert "pr state: merged (observed 2026-01-01T00:00:00Z)" in shown


def test_a_task_with_no_observed_pr_state_is_not_badged(home: Path, tmp_path: Path):
    """Absence means "never observed" — no gh, no PR — never "not merged"."""
    slug = setup_task_env(home, tmp_path)
    runner.invoke(app, ["task", "add", slug, "one-off", "--harness", "fake", "--home", str(home)])
    listing = runner.invoke(app, ["task", "list", "--home", str(home)]).output
    assert "✔" not in listing and "⊘" not in listing
    short = json.loads(
        runner.invoke(app, ["task", "list", "--json", "--home", str(home)]).output
    )[0]["id_short"]
    assert "pr state" not in runner.invoke(app, ["task", "show", short, "--home", str(home)]).output


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


# -- listings as Rich tables (#52) -------------------------------------------


def _wide_task_rows() -> list[dict]:
    """Two `views.task_rows`-shaped rows with every optional field lit — the
    row shape that used to run past 80 columns and wrap mid-cell."""
    return [
        {
            "id_short": "38hskq", "project": "quorum", "status": "executing",
            "harness": "claude", "running": True, "attached": False, "perpetual": False,
            "last_report": "implementing the rich table for status rows\nand making sure "
                           "nothing wraps at eighty columns even with every field lit",
            "pr_url": "https://github.com/kvndhrty/quorum/pull/49",
            "git": {"dirty": 2, "unpushed": 1},
            "waiting_on": ["a3f2k9"], "dep_failed": [], "dep_missing": [], "dep_cycle": False,
            "usage_text": "$12.31 · 17.4M tok", "budget_overages": ["run 3: cost $12.31 > 1.0"],
        },
        {
            "id_short": "a3f2k9", "project": "quorum", "status": "done",
            "harness": "codex", "running": False, "attached": False, "perpetual": True,
            "last_report": "short", "pr_url": "", "git": None,
            "waiting_on": [], "dep_failed": [], "dep_missing": [], "dep_cycle": False,
            "usage_text": "1.2k tok", "budget_overages": [],
        },
    ]


def test_task_table_fits_eighty_columns_without_wrapping(capsys):
    """The wrapping the issue describes: fitted to 80 columns, every row is
    one line, ids/status/pr/usage are whole, and the report and flags cells
    are the ones that give way (ellipsis, never a wrap)."""
    from rich.cells import cell_len

    from quorum.cli import _print_table, _task_table

    rows = _wide_task_rows()
    _print_table(_task_table(rows), width=80)
    lines = _plain(capsys.readouterr().out).rstrip("\n").split("\n")
    assert len(lines) == 1 + len(rows), lines  # header + one line per row: no wrapped cell
    assert all(cell_len(line) <= 80 for line in lines), [cell_len(x) for x in lines]
    header, first, second = lines
    # the fixed columns keep their headers; report/flags may lose theirs to
    # the ellipsis at this width, exactly as their cells do
    assert header.split()[:4] == ["id", "project", "status", "harness"]
    assert "pr" in header.split() and "usage" in header.split()
    assert "▶ 38hskq" in first and "executing" in first and "claude" in first
    assert "#49" in first and "$12.31 · 17.4M tok $!" in first
    assert "…" in first  # the report gave way
    assert "https://" not in first  # the URL itself only in `task show`
    assert "✓ a3f2k9" in second and "done ∞" in second and "1.2k tok" in second


def test_task_table_is_whole_and_plain_off_a_terminal(capsys):
    """Piped (as here — pytest's capture is not a tty): natural width, no
    ANSI, the whole report on one line, so grep on an id or status works."""
    from quorum.cli import _print_table, _task_table

    rows = _wide_task_rows()
    _print_table(_task_table(rows))
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 1 + len(rows)
    assert all(line == line.rstrip() for line in lines)  # no padding past the last cell
    assert "…" not in lines[1]
    # the newline in the report was folded into the one row
    assert "status rows and making sure nothing wraps at eighty columns" in lines[1]
    assert "⚠ 2 uncommitted, 1 unpushed  waiting-on a3f2k9" in lines[1]
    assert "#49" in lines[1] and "$12.31 · 17.4M tok $!" in lines[1]


def test_task_table_drops_columns_nothing_fills(capsys):
    """A home with no PRs, flags or reported usage gets no blank headers."""
    from quorum.cli import _print_table, _task_table

    row = dict(_wide_task_rows()[1], perpetual=False, usage_text="")
    _print_table(_task_table([row]))
    header = capsys.readouterr().out.split("\n")[0].split()
    assert header == ["id", "project", "status", "harness", "report"]

    # ...and a fresh home whose queued tasks have not reported yet loses the
    # `report` header too: nothing is exempt from the drop but the identity
    # columns every row fills.
    _print_table(_task_table([dict(row, last_report="")]))
    header = capsys.readouterr().out.split("\n")[0].split()
    assert header == ["id", "project", "status", "harness"]


def test_table_stays_compact_on_a_wide_terminal(capsys):
    """A table left with no give-way column (the usual `agent list`) must not
    spread the window's slack over its fixed columns: at 200 columns the
    fields stay two spaces apart, exactly as they are when piped."""
    from rich.cells import cell_len

    from quorum.cli import _agent_table, _print_table

    rows = [
        {
            "name": "manager", "type": "manager", "status": "idle", "enabled": True,
            "schedule": "every 5 minutes", "last_end": "12:00", "usage_text": "",
            "error": "",
        }
    ]
    _print_table(_agent_table(rows, with_type=True), width=200)
    lines = _plain(capsys.readouterr().out).rstrip("\n").split("\n")
    assert len(lines) == 2, lines
    assert all(cell_len(line.rstrip()) < 60 for line in lines), lines
    assert "name       type     status  schedule" in lines[0]

    # a surviving give-way column still fills the window
    _print_table(_agent_table([dict(rows[0], error="boom")], with_type=True), width=200)
    assert "error" in _plain(capsys.readouterr().out).split("\n")[0]


def test_a_narrow_table_clips_every_column_before_the_id(capsys):
    """Below the width the report column can cover, Rich clips the fixed
    columns — but the id is the handle you retype into `task run`, so it
    holds `ID_MIN_WIDTH` while project/status/harness give up theirs."""
    from quorum.cli import _print_table, _task_table

    for width in (40, 30, 24):
        _print_table(_task_table(_wide_task_rows()), width=width)
        lines = _plain(capsys.readouterr().out).rstrip("\n").split("\n")
        assert len(lines) == 3, (width, lines)
        assert "▶ 38hskq" in lines[1] and "✓ a3f2k9" in lines[2], (width, lines)


def test_pr_ref_shortens_known_forges_and_leaves_the_rest():
    from quorum.cli import _pr_ref

    assert _pr_ref("https://github.com/kvndhrty/quorum/pull/49") == "#49"
    assert _pr_ref("https://github.com/kvndhrty/quorum/pull/49/") == "#49"
    assert _pr_ref("https://gitlab.example/g/p/-/merge_requests/7") == "!7"
    # not a PR-shaped URL: shown as given rather than guessed at
    assert _pr_ref("https://example.com/review/abc") == "https://example.com/review/abc"


def test_long_report_is_clipped_in_the_table_not_in_task_show(capsys):
    from quorum.cli import REPORT_MAX_CHARS, _print_table, _task_table

    row = dict(_wide_task_rows()[1], last_report="x" * 300)
    _print_table(_task_table([row]))
    line = capsys.readouterr().out.split("\n")[1]
    assert "x" * (REPORT_MAX_CHARS - 1) + "…" in line and "x" * REPORT_MAX_CHARS not in line


def test_status_and_task_list_stay_greppable_when_piped(home: Path, tmp_path: Path):
    """End to end through the CLI (CliRunner is not a tty): both listings
    carry every id and status, the PR as `#N`, and `task show` the full URL."""
    slug = setup_task_env(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "first", "--harness", "fake", "--home", str(home)])
    first = r.output.split("queued task ")[1].split(" ")[0]
    r = runner.invoke(app, ["task", "add", slug, "second", "--harness", "fake", "--home", str(home)])
    second = r.output.split("queued task ")[1].split(" ")[0]
    url = "https://github.com/kvndhrty/quorum/pull/49"
    r = runner.invoke(
        app, ["task", "report", second, "--status", "pr", "--pr-url", url, "opened", "--home", str(home)]
    )
    assert r.exit_code == 0, r.output

    for argv in (["task", "list"], ["status"]):
        r = runner.invoke(app, [*argv, "--home", str(home)])
        assert r.exit_code == 0, r.output
        assert "\x1b[" not in r.output
        rows = {line.split()[1]: line for line in r.output.split("\n") if f"  {slug}  " in line}
        assert set(rows) == {first, second}
        assert "queued" in rows[first]
        assert "pr" in rows[second].split() and "#49" in rows[second]
        assert url not in r.output
        header = next(line for line in r.output.split("\n") if line.startswith("id  "))
        assert header.split() == ["id", "project", "status", "harness", "report", "pr"]

    r = runner.invoke(app, ["task", "show", second, "--home", str(home)])
    assert f"pr:       {url}" in r.output


def test_agent_and_project_listings_are_tables(home: Path, tmp_path: Path):
    slug = setup_task_env(home, tmp_path)
    runner.invoke(app, ["project", "set", slug, "--deadline", "2099-01-01", "--home", str(home)])

    r = runner.invoke(app, ["agent", "list", "--home", str(home)])
    assert r.exit_code == 0, r.output
    lines = r.output.rstrip("\n").split("\n")
    assert lines[0].split()[:4] == ["name", "type", "status", "schedule"]
    manager = next(line for line in lines if "manager" in line)
    assert manager.startswith("○ manager") and "never-ran" in manager

    r = runner.invoke(app, ["status", "--home", str(home)])
    agents = r.output.split("agents:\n")[1].split("\n")[0].split()
    assert agents[:3] == ["name", "status", "schedule"] and "type" not in agents
    projects = r.output.split("projects:\n")[1].rstrip("\n").split("\n")
    assert projects[0].split() == ["slug", "due"]  # name == slug is not repeated
    assert projects[1].startswith(slug) and "2099-01-01 (" in projects[1]

    r = runner.invoke(app, ["project", "list", "--home", str(home)])
    assert r.exit_code == 0 and r.output.split("\n")[1].startswith(slug)
