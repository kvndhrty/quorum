"""scripts/evidence.py — the read-only surface-use extractor behind #128.

The verdicts in a surface review rest on what this script counts, so the
counting is pinned here rather than eyeballed: a real task run and a real
manager tick against the conftest home, both driven by tests/bin/fake_harness.py,
then assertions that each verb landed in the right actor column and that a call
against a scratch home landed in none of them.

Loaded by file path, the way `test_example_steward.py` loads the shipped
example: `scripts/` is not part of the package and is never imported by quorum.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import make_repo
from quorum import fsio, runner, surfaces
from quorum.actor import transcript_path
from quorum.agent import AgentContext
from quorum.agents.manager import Manager
from quorum.config import load_config
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore, task_dir

_spec = importlib.util.spec_from_file_location(
    "quorum_evidence", Path(__file__).parent.parent / "scripts" / "evidence.py"
)
evidence = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = evidence  # @dataclass resolves annotations through it
_spec.loader.exec_module(evidence)

FAKE = str(Path(__file__).parent / "bin" / "fake_harness.py")


def tree() -> tuple[set[str], set[str]]:
    commands, _ = surfaces.cli_tree()
    return (
        {c["path"] for c in commands if c["kind"] == "command"},
        {c["path"] for c in commands if c["kind"] == "group"},
    )


def collect(home: Path) -> evidence.Evidence:
    paths, groups = tree()
    return evidence.collect(home, paths, groups)


def counts_for(ev: evidence.Evidence, path: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for inv in ev.invocations:
        if inv.path == path:
            totals[inv.column] = totals.get(inv.column, 0) + 1
    return totals


def write_config(home: Path) -> None:
    (home / "config.toml").write_text(
        "[tasks]\n"
        'default_harness = "tasktool"\n'
        "[harness.tasktool]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        'env = { FAKE_HARNESS_MODE = "report" }\n'
        "[harness.mgr]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        'env = { FAKE_HARNESS_MODE = "manager_act" }\n'
        "[agents.manager]\n"
        'type = "manager"\n'
        'schedule = "every 5m"\n'
        "auto_pause = false\n"
        "[agents.manager.settings]\n"
        'harness = "mgr"\n'
        "run_timeout_seconds = 60\n"
    )


@pytest.fixture
def worked_home(home: Path, tmp_path: Path, clock) -> Path:
    """A home that has actually been used: one task run and one manager tick.

    Both transcripts are what fake_harness.py streamed through the real
    runner, so the parser is reading the same entry shape production writes.
    """
    write_config(home)
    repo = make_repo(tmp_path, "evproj")
    ProjectRegistry(home).add(repo, name="evproj")
    store = TaskStore(home)
    task = store.add("evproj", "write the parser", "tasktool", issue_url="u/1")
    runner.run_task(home, load_config(home), task.id)
    # the manager only runs its harness on a home with something to decide
    store.add("evproj", "then write the tests", "tasktool")

    config = load_config(home)
    ctx = AgentContext(
        home=home, name="manager",
        settings=config.agents["manager"].settings, config=config, now=clock,
    )
    Manager(ctx).tick()
    return home


def test_task_verbs_count_against_the_task_actor(worked_home: Path):
    ev = collect(worked_home)
    # two task runs report: the one this fixture ran, and the one the manager
    # launched. Both are the task actor, neither is the manager's.
    assert counts_for(ev, "task report") == {"task": 2}


def test_manager_verbs_count_against_the_manager(worked_home: Path):
    ev = collect(worked_home)
    # manager_act launches, nudges and journals through the CLI
    assert counts_for(ev, "task run").get("manager") == 1
    assert counts_for(ev, "task nudge").get("manager") == 1
    assert counts_for(ev, "manager note").get("manager") == 1
    # and the journal agrees, which is the cross-check column
    assert ev.journal[("task.run", "manager")] == 1


def test_a_scratch_home_run_is_not_use(worked_home: Path):
    """A smoke or test invocation against a throwaway home counts in neither
    the actor columns nor the totals — in a home whose tasks are work *on*
    quorum, that is most of what a transcript holds."""
    task = TaskStore(worked_home).list()[0]
    fsio.append_jsonl(
        task_dir(worked_home, task.id) / "transcript.jsonl",
        {"at": "2026-09-12T00:00:00Z",
         "line": "export QUORUM_HOME=/tmp/smoke; uv run quorum init; uv run quorum task list"},
    )
    ev = collect(worked_home)
    assert counts_for(ev, "init") == {"scratch": 1}
    assert counts_for(ev, "task list") == {"scratch": 1}
    assert counts_for(ev, "task report") == {"task": 2}  # unchanged


def test_a_checkout_call_is_exercise_not_use(worked_home: Path):
    task = TaskStore(worked_home).list()[0]
    fsio.append_jsonl(
        task_dir(worked_home, task.id) / "transcript.jsonl",
        {"at": "2026-09-12T00:00:00Z", "line": "uv run quorum usage --by task"},
    )
    ev = collect(worked_home)
    assert counts_for(ev, "usage") == {"checkout": 1}


def test_a_tool_call_event_is_read_like_a_printed_line(worked_home: Path):
    """The claude-shaped `tool_use` payload is the production case; it must
    yield the same invocation a raw stdout line does."""
    task = TaskStore(worked_home).list()[0]
    fsio.append_jsonl(
        task_dir(worked_home, task.id) / "transcript.jsonl",
        {
            "at": "2026-09-12T00:00:00Z",
            "event": {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use", "id": "toolu_1", "name": "Bash",
                    "input": {"command": "quorum task inbox abc --claim 2>&1 | tail -3"},
                }]},
            },
        },
    )
    ev = collect(worked_home)
    assert counts_for(ev, "task inbox") == {"task": 1}
    assert any("--claim" in inv.options for inv in ev.invocations if inv.path == "task inbox")


def test_one_call_announced_twice_counts_once(home: Path):
    """codex pairs item.started with item.completed, both carrying the whole
    call; the call id is what makes it one observation."""
    write_config(home)
    store = TaskStore(home)
    task = store.add("evproj", "x", "tasktool")
    event = {
        "type": "tool_call", "call_id": "c1", "name": "shell",
        "command": "quorum task show abc",
    }
    for _ in range(2):
        fsio.append_jsonl(
            task_dir(home, task.id) / "transcript.jsonl",
            {"at": "2026-09-12T00:00:00Z", "event": event},
        )
    assert counts_for(collect(home), "task show") == {"task": 1}


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("quorum has no forge write path", [], id="prose-is-not-a-command"),
        pytest.param("grep -rn quorum src/", [], id="quorum-as-a-search-term"),
        pytest.param("quorum task", [("task (group only)", "task")], id="group-with-no-verb"),
        pytest.param("quorum web", [("web (removed in #102)", "task")], id="verb-removed-in-102"),
        pytest.param(
            "quorum --home /elsewhere task list", [("task list", "scratch")], id="another-home"
        ),
        pytest.param(
            "uv run --project . quorum doctor", [("doctor", "checkout")], id="wrapper-with-a-flag"
        ),
        pytest.param(
            "quorum task report t1 --status done 'all good' && git push",
            [("task report", "task")],
            id="one-of-several-segments",
        ),
    ],
)
def test_command_lines_resolve_to_the_right_surface(command, expected, tmp_path: Path):
    paths, groups = tree()
    found = evidence.invocations_in(command, "task", "", "t", tmp_path, paths, groups)
    assert [(i.path, i.column) for i in found] == expected


def test_an_unlexable_command_is_skipped_not_raised(tmp_path: Path):
    paths, groups = tree()
    assert evidence.invocations_in("quorum task list 'unbalanced", "task", "", "t",
                                   tmp_path, paths, groups) == []


def test_state_implies_the_option_that_produced_it(worked_home: Path):
    """Nothing journals `task add`, so a task record's own fields are the only
    evidence that `--issue` was typed."""
    ev = collect(worked_home)
    assert ("--issue", "person") in [
        (o, i.actor) for i in ev.invocations for o in i.options if i.path == "task add"
    ]


def test_config_keys_are_read_from_the_home(worked_home: Path):
    ev = collect(worked_home)
    assert ev.config_keys["tasks.default_harness"] == "'tasktool'"
    assert ev.config_keys["agents.manager.auto_pause"] == "False"
    assert "notify.command" not in ev.config_keys


def test_a_run_snapshot_is_never_read_as_use(worked_home: Path):
    """The digest quotes commands at the manager ("`quorum manager remember
    …`"); a quoted command is not an invocation."""
    snapshots = evidence.read_lines(
        sorted((worked_home / "state" / "manager" / "runs").glob("*.md"))[0]
    )
    assert any("quorum" in line for line in snapshots)  # the digest does quote commands
    ev = collect(worked_home)
    assert all(inv.source != "manager run snapshot" for inv in ev.invocations)


def test_no_trace_list_names_only_real_commands():
    paths, _ = tree()
    assert evidence.NO_TRACE <= paths


def test_removed_verbs_are_really_gone():
    paths, groups = tree()
    assert not (evidence.REMOVED_IN_102 & ({p.split()[0] for p in paths} | groups))


def test_the_report_renders_against_a_worked_home(worked_home: Path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["evidence.py", str(worked_home)])
    evidence.main()
    out = capsys.readouterr().out
    for heading in ("## 1. CLI commands", "## 2. CLI options", "## 3. Config keys",
                    "## 4. TUI key bindings", "## 5. Prompt placeholders"):
        assert heading in out
    assert "verdict" in out
    assert str(worked_home) in out


def test_reading_the_home_writes_nothing(worked_home: Path):
    before = {p: p.stat().st_mtime_ns for p in worked_home.rglob("*") if p.is_file()}
    collect(worked_home)
    after = {p: p.stat().st_mtime_ns for p in worked_home.rglob("*") if p.is_file()}
    assert before == after


def test_the_manager_transcript_is_the_manager_actor(worked_home: Path):
    entries = fsio.read_jsonl(transcript_path(worked_home))
    assert entries, "the manager tick must have streamed a transcript"
    assert any("RUN| quorum task run" in json.dumps(e) for e in entries)
