"""The narrative transcript renderer, and reading one agent run end to end.

Three sources of truth here, on purpose:

- `tests/fixtures/claude_transcript.jsonl` is a *real* claude run, captured
  verbatim from a dogfood task (its opening turns plus its `result` event).
  Renderer tests that only ever see hand-written events drift from the shapes
  the harness actually emits — an empty redacted `thinking` block, a
  `system/task_notification` carrying a `summary`, a `rate_limit_event` the
  run survived — and every one of those was a wrong line before it was a test.
- `tests/bin/fake_harness.py` supplies the other real shape: a harness that
  prints plain text, which must render as its own lines and never as an error.
- Codex-shaped events are written by hand (no codex binary in CI), which is
  the point of the normalizer: one place decides what each harness means.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, tasks, transcript
from quorum.actor import (
    journal_path,
    run_snapshot_path,
    runs_dir,
    transcript_path,
    usage_path,
)
from quorum.agents.harness_run import SNAPSHOT_KEEP, SNAPSHOT_MAX_BYTES, write_run_snapshot
from quorum.cli import app
from quorum.config import load_config
from quorum.projects import ProjectRegistry
from quorum.runner import run_task
from quorum.tasks import TaskStore
from test_tasks import harness_config, make_repo

runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "claude_transcript.jsonl"


@pytest.fixture
def claude_entries() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def rendered(entries: list, **kw) -> str:
    return "\n".join(transcript.render(entries, **kw))


# --- the narrative over a real claude run ------------------------------------


def test_real_claude_run_renders_as_a_story(claude_entries: list[dict]):
    out = rendered(claude_entries)
    lines = out.splitlines()

    assert lines[0].endswith("run started (session 7237a30f · "
                            "/Users/kdoh/.quorum/worktrees/01M1J9XS2QMT0ZJTNE418V3YK0)")
    # assistant text in full, tool calls as one line each with their argument
    assert "💬 I'll start by reading the issue and understanding the current structure." in out
    assert "🔧 Bash  gh issue view 62 --comments 2>&1 | head -200" in out
    # results collapsed to a size, never their content
    assert "  ↳ 33 lines · 489 B" in out
    assert "gh issue view" in out and "## Problem" not in out
    # the terminal result, off usage.py's own reading of the event
    assert lines[-1].endswith("■ result: success · 148 turns · 24m13s · $13.79 · 20.2M tok")
    # a whole run of JSON becomes something a person can read at a glance
    assert len(lines) < 20


def test_noise_is_folded_until_verbose(claude_entries: list[dict]):
    quiet = rendered(claude_entries)
    loud = rendered(claude_entries, verbose=True)

    # the init banner's tool list, the token-estimate pings, an allowed
    # rate-limit notice and a redacted (empty) thinking block say nothing
    for noise in ("thinking_tokens", "rate_limit_info", '"signature"', "WebSearch"):
        assert noise not in quiet
    assert "thinking_tokens" in loud and "rate_limit_info" in loud
    assert len(loud.splitlines()) > 4 * len(quiet.splitlines())


def test_raw_is_the_output_task_tail_always_printed(claude_entries: list[dict]):
    """--raw is a promise to anything grepping a transcript: byte for byte."""

    def before(entry: dict) -> str:  # `task tail`'s renderer prior to #82
        at = str(entry.get("at", "")).replace("T", " ").rstrip("Z")
        if "line" in entry:
            return f"[{at}] {entry['line']}"
        return f"[{at}] {json.dumps(entry.get('event'), ensure_ascii=False)}"

    assert transcript.render(claude_entries, raw=True) == [before(e) for e in claude_entries]


# --- fail-soft ---------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"at": "2026-09-01T00:00:00Z", "event": {"type": "invented_by_a_harness", "x": [1, 2]}},
        {"at": "2026-09-01T00:00:00Z", "event": "a bare string event"},
        {"at": "2026-09-01T00:00:00Z", "event": None},
        {"at": "not a timestamp", "line": "plain text"},
        {"event": {"type": "assistant", "message": {"content": "flat string content"}}},
        {},
        "not an entry at all",
        None,
        [1, 2, 3],
    ],
)
def test_unknown_and_malformed_entries_render_instead_of_raising(entry):
    lines = transcript.render([entry])
    assert isinstance(lines, list)
    assert all(isinstance(line, str) for line in lines)


def test_an_unrecognized_event_keeps_its_json():
    event = {"type": "invented_by_a_harness", "detail": "held onto"}
    out = rendered([{"at": "2026-09-01T00:00:00Z", "event": event}])
    assert "held onto" in out and "invented_by_a_harness" in out


def test_a_normalizer_that_explodes_still_yields_a_line(monkeypatch: pytest.MonkeyPatch):
    def boom(entry):
        raise RuntimeError("shapes changed under us")

    monkeypatch.setattr(transcript, "_normalize", boom)
    assert transcript.render([{"at": "x", "line": "hello"}]) == ['? {"at": "x", "line": "hello"}']


# --- the harnesses -----------------------------------------------------------


def test_plain_text_harness_lines_render_as_themselves():
    """The fake harness (and the shipped opencode template) print text, not
    JSON. That is a supported harness, not a degraded one."""
    entries = [
        {"at": "2026-09-01T10:00:00Z", "line": "PROMPT| Task ID: abc123"},
        {"at": "2026-09-01T10:00:01Z", "line": "quorum: worktree prepared at /tmp/wt"},
    ]
    out = rendered(entries).splitlines()
    assert out[0] == "[10:00:00] ? PROMPT| Task ID: abc123"
    assert out[1] == "[10:00:01] • worktree prepared at /tmp/wt"


def codex(event: dict, at: str = "2026-09-01T10:00:00Z") -> dict:
    return {"at": at, "event": event}


def test_codex_shapes_read_the_same_as_claude():
    entries = [
        codex({"type": "thread.started", "thread_id": "01998aa9-6c07-7a41-8c1e-3b2a1f5d9e04"}),
        codex({"type": "item.started",
               "item": {"item_type": "command_execution", "id": "c1", "command": "pytest -q"}}),
        codex({"type": "item.completed",
               "item": {"item_type": "command_execution", "id": "c1", "command": "pytest -q",
                        "exit_code": 1, "aggregated_output": "1 failed\n2 passed"}}),
        codex({"type": "item.completed",
               "item": {"item_type": "agent_message", "text": "the parser is the problem"}}),
        codex({"type": "turn.completed",
               "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}}),
    ]
    out = rendered(entries).splitlines()
    assert out[0].endswith("▶ run started (session 01998aa9)")
    assert out[1].endswith("🔧 command_execution  pytest -q")
    # one command is one call and one outcome, not two calls
    assert out[2].endswith("🔧 command_execution  pytest -q")
    assert out[3].endswith("↳ exit 1 · 2 lines · 17 B")
    assert out[4].endswith("💬 the parser is the problem")
    assert out[5].endswith("■ result: 1.5k tok")
    assert len(out) == 6


def test_tool_results_report_errors_and_the_argument_is_the_first_one():
    entries = [
        {"at": "2026-09-01T10:00:00Z", "event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit",
             "input": {"file_path": "src/quorum/notify.py",
                       "old_string": "one\ntwo", "new_string": "one\ntwo\nthree"}}]}}},
        {"at": "2026-09-01T10:00:01Z", "event": {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "String to replace not found in file.\nnothing changed"}]}}},
    ]
    out = rendered(entries).splitlines()
    assert out[0].endswith("🔧 Edit  src/quorum/notify.py  (+3 −2)")
    assert "error: String to replace not found in file." in out[1]


def test_a_long_argument_is_trimmed_but_kept_in_full_under_verbose():
    command = "echo " + "x" * 400
    entry = {"at": "2026-09-01T10:00:00Z", "event": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}}}
    quiet = rendered([entry])
    assert len(quiet) < 140 and quiet.endswith("…")
    assert command in rendered([entry], verbose=True)


def test_the_renderer_and_the_loop_signal_read_calls_the_same_way():
    """One vocabulary: `manager.loop_signal` asks `transcript.tool_call` what a
    node is, so a harness taught to one is understood by the other."""
    from quorum.agents.manager import loop_signal

    call = {"type": "tool_use", "id": "c%d", "name": "Bash", "input": {"command": "pytest -x"}}
    entries = [
        {"at": "2026-09-01T10:00:00Z",
         "event": {"type": "assistant", "message": {"content": [{**call, "id": f"c{i}"}]}}}
        for i in range(6)
    ]
    assert loop_signal(entries)["tool"] == "Bash"
    assert rendered(entries).count("🔧 Bash  pytest -x") == 6


def test_task_transcript_written_by_a_real_run_renders(home: Path, tmp_path: Path, project_run):
    """End to end over the fake harness: whatever it wrote, `task log` reads."""
    task_id, path = project_run
    entries = fsio.read_jsonl(path)
    out = rendered(entries)
    assert "Task ID:" in out  # the preamble the fake harness echoed back
    assert "▶ run started (session sess-fake-123)" in out


@pytest.fixture
def project_run(home: Path, tmp_path: Path) -> tuple[str, Path]:
    """One real task run of the fake harness; its id and its transcript."""
    ProjectRegistry(home).add(make_repo(tmp_path), name="proj")
    harness_config(home)
    task = TaskStore(home).add("proj", "improve the README", "fake")
    assert run_task(home, load_config(home), task.id) == 0
    return task.id, tasks.transcript_path(home, task.id)


# --- one agent run, end to end ----------------------------------------------


def agent_run(
    home: Path,
    name: str = "manager",
    run_id: str = "01AGENTRUN0000000000000001",
    *,
    snapshot: str | None = "# Situation digest\n- [queued] abc123 tidy the docs\n",
    ledger: bool = True,
) -> str:
    """The four files one tick leaves behind, written the way its own code
    writes them: a snapshot, transcript entries, journal lines, a ledger line."""
    if snapshot is not None:
        write_run_snapshot(home, name, run_id, snapshot)
    for entry in (
        {"at": "2026-09-01T10:00:00Z", "run": run_id,
         "event": {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "abc123 is queued and nothing is running; launching it"}]}}},
        {"at": "2026-09-01T10:00:02Z", "run": "00OLDERTICK00000000000000",
         "event": {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "an older tick, not this one"}]}}},
    ):
        fsio.append_jsonl(transcript_path(home, name), entry)
    fsio.append_jsonl(journal_path(home, name), {
        "at": "2026-09-01T10:00:03Z", "run": run_id, "actor": name,
        "action": "task.run", "target": "abc123", "target_status": "queued",
    })
    fsio.append_jsonl(journal_path(home, name), {
        "at": "2026-09-01T10:00:04Z", "run": "00OLDERTICK00000000000000", "actor": name,
        "action": "task.nudge", "target": "abc123", "args": "an older tick's action",
    })
    if ledger:
        fsio.append_jsonl(usage_path(home, name), {
            "at": "2026-09-01T10:00:05Z", "run": run_id, "outcome": "ok",
            "duration_seconds": 130.0, "usage": {"cost_usd": 0.42, "total_tokens": 120000},
        })
    return run_id


def test_render_run_is_the_four_files_read_together(home: Path):
    run_id = agent_run(home)
    out = "\n".join(transcript.render_run(home, "manager", run_id))

    assert out.startswith(f"=== manager run {run_id} — 2026-09-01T10:00:05Z")
    assert "- [queued] abc123 tidy the docs" in out                 # what it saw
    assert "💬 abc123 is queued and nothing is running" in out       # what it said
    assert "task.run -> abc123" in out                              # what it did
    assert "ok · 2m10s · $0.42 · 120.0k tok" in out                 # how it ended
    # strictly this run: another tick's transcript and journal lines stay out
    assert "an older tick" not in out


def test_render_run_reports_a_then_now_outcome_for_each_action(home: Path, tmp_path: Path):
    ProjectRegistry(home).add(make_repo(tmp_path), name="proj")
    task = TaskStore(home).add("proj", "tidy the docs", "fake")
    run_id = "01AGENTRUN0000000000000001"
    write_run_snapshot(home, "manager", run_id, "digest")
    fsio.append_jsonl(journal_path(home, "manager"), {
        "at": "2026-09-01T10:00:03Z", "run": run_id, "actor": "manager",
        "action": "task.run", "target": task.short_id, "target_status": "queued",
    })
    tasks.report(home, task.id, status="executing", text="on it")

    out = "\n".join(transcript.render_run(home, "manager", run_id))
    assert f"task.run -> {task.short_id}  [queued -> executing]" in out

    tasks.report(home, task.id, status="queued", text="back to the queue")
    out = "\n".join(transcript.render_run(home, "manager", run_id))
    assert f"task.run -> {task.short_id}  [unchanged]" in out


def test_a_run_missing_every_optional_piece_still_renders(home: Path):
    """A tick that died before its ledger line, whose snapshot has aged out."""
    run_id = agent_run(home, snapshot=None, ledger=False)
    out = "\n".join(transcript.render_run(home, "manager", run_id))
    assert "(no snapshot kept for this run)" in out
    assert "(still running, or the run ended before it could record)" in out
    assert "💬 abc123 is queued" in out  # everything that does exist still reads


def test_run_ids_include_a_tick_still_in_flight(home: Path):
    finished = agent_run(home, run_id="01AGENTRUN0000000000000001")
    fsio.append_jsonl(transcript_path(home, "manager"), {
        "at": "2026-09-01T11:00:00Z", "run": "01AGENTRUN0000000000000002",
        "event": {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "still going"}]}},
    })
    ids = transcript.run_ids(home, "manager")
    assert ids[-2:] == [finished, "01AGENTRUN0000000000000002"]  # oldest first
    assert transcript.run_ids(home, "manager", limit=1) == ["01AGENTRUN0000000000000002"]
    assert transcript.run_ids(home, "nobody") == []


def test_the_snapshot_is_bounded_head_and_count(home: Path):
    write_run_snapshot(home, "manager", "01RUNBIG", "x" * (SNAPSHOT_MAX_BYTES + 5000))
    kept = run_snapshot_path(home, "manager", "01RUNBIG").read_text()
    assert len(kept.encode()) < SNAPSHOT_MAX_BYTES + 200
    assert "truncated: 5000 more bytes" in kept

    for i in range(SNAPSHOT_KEEP + 5):
        write_run_snapshot(home, "prompter", f"01RUN{i:08d}", f"digest {i}")
    files = sorted(runs_dir(home, "prompter").glob("*.md"))
    assert len(files) == SNAPSHOT_KEEP
    assert files[0].name == f"01RUN{5:08d}.md"  # the oldest go, newest kept


def test_an_unwritable_home_costs_the_tick_nothing(home: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(fsio, "atomic_write_text", boom)
    write_run_snapshot(home, "manager", "01RUNX", "digest")  # must not raise
    assert transcript.read_snapshot(home, "manager", "01RUNX") == ""


# --- the commands ------------------------------------------------------------


def invoke(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_task_tail_and_log_render_the_narrative(home: Path, project_run):
    task_id, path = project_run
    short = task_id[-6:].lower()

    narrative = invoke("task", "log", short)
    assert "▶ run started (session sess-fake-123)" in narrative
    assert '{"type": "system"' not in narrative

    raw = invoke("task", "log", short, "--raw")
    assert '{"type": "system", "session_id": "sess-fake-123"}' in raw
    assert raw.splitlines() == transcript.render(fsio.read_jsonl(path), raw=True)

    assert invoke("task", "tail", short, "-n", "2").count("\n") <= 3


def test_task_log_on_a_task_that_never_ran(home: Path, tmp_path: Path):
    ProjectRegistry(home).add(make_repo(tmp_path), name="proj")
    task = TaskStore(home).add("proj", "not started", "fake")
    assert "no transcript yet" in invoke("task", "log", task.short_id)


def test_manager_log_renders_a_tick_and_resolves_a_run_by_suffix(home: Path):
    run_id = agent_run(home)
    out = invoke("manager", "log")
    assert "what it saw" in out and "what it said" in out
    assert "what it did" in out and "how it ended" in out

    assert run_id in invoke("manager", "log", "--run", run_id[-6:])
    assert run_id in invoke("manager", "log", "--run", run_id)


def test_manager_log_last_renders_several_ticks_oldest_first(home: Path):
    first = agent_run(home, run_id="01AGENTRUN0000000000000001")
    second = agent_run(home, run_id="01AGENTRUN0000000000000002")
    out = invoke("manager", "log", "--last", "2")
    assert out.index(first) < out.index(second)
    assert invoke("manager", "log").count("=== manager run") == 1  # one tick by default


def test_manager_log_says_so_when_there_is_nothing_to_read(home: Path):
    assert "no manager runs recorded yet" in invoke("manager", "log")
    assert "manager has written no transcript yet" in invoke("manager", "tail")


def test_an_unknown_run_reference_is_refused_not_guessed_at(home: Path):
    agent_run(home, run_id="01AGENTRUN0000000000000001")
    agent_run(home, run_id="01AGENTRUN0000000000000002")
    missing = runner.invoke(app, ["manager", "log", "--run", "ZZZZZZ"])
    assert missing.exit_code == 1 and "no manager run matching" in missing.output
    ambiguous = runner.invoke(app, ["manager", "log", "--run", "01AGENTRUN"])
    assert ambiguous.exit_code == 1 and "matches 2 manager runs" in ambiguous.output


def test_agent_log_and_tail_read_a_prompt_agent(home: Path):
    run_id = agent_run(home, name="standup", run_id="01AGENTRUN000000000000000S")
    out = invoke("agent", "log", "standup")
    assert run_id in out and "💬 abc123 is queued" in out
    assert "💬 abc123 is queued" in invoke("agent", "tail", "standup")
    assert "no nobody runs recorded yet" in invoke("agent", "log", "nobody")


@pytest.mark.parametrize("bad", ["../../etc", "Bad Name", "task-abc"])
def test_an_agent_name_from_the_outside_stays_inside_the_home(home: Path, bad: str):
    """`agent log <name>` builds a path under state/agents/, so the name is
    validated the way every other agent-name entry point validates it."""
    for command in (["agent", "log", bad], ["agent", "tail", bad]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1
        assert "invalid agent name" in result.output or "reserved" in result.output
