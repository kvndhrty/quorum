"""Task substrate and runner tests: the store, one full harness run, guidance
injection, session capture/resume, and the cooperative report channel."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quorum import fsio, runner, tasks, usage
from quorum.config import HarnessConfig, load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.runner import RunnerError, run_task
from quorum.tasks import TaskStore, task_json_path

TESTS_BIN = Path(__file__).parent / "bin"
FAKE = str(TESTS_BIN / "fake_harness.py")


def make_repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
            check=True, capture_output=True,
        )
    git("init", "-q")
    # Committing inside a worktree (the auto-commit safety net does) has no
    # -c flags of its own, so the identity has to live in the repo config.
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (repo / "README.md").write_text("hello")
    git("add", ".")
    git("commit", "-qm", "init")
    return repo


def repo_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    ).stdout.strip()


def harness_config(home: Path, extra: str = "", tasks_extra: str = "") -> None:
    body = (
        "[tasks]\n"
        'default_harness = "fake"\n'
        f"{tasks_extra}"
        "[harness.fake]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        f'resume = ["{sys.executable}", "{FAKE}", "--resumed", "{{session}}"]\n'
        f"{extra}"
    )
    (home / "config.toml").write_text(body)


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    return "proj"


def transcript_text(home: Path, task_id: str) -> str:
    lines = []
    for e in fsio.read_jsonl(tasks.transcript_path(home, task_id)):
        lines.append(e.get("line") or json.dumps(e.get("event")))
    return "\n".join(lines)


# -- store ----------------------------------------------------------------


def test_store_add_resolve_and_prefix(home: Path):
    store = TaskStore(home)
    t1 = store.add("proj", "do a thing", "fake")
    t2 = store.add("proj", "another", "fake")
    assert store.resolve(t1.id).id == t1.id
    assert store.resolve(t1.short_id).id == t1.id  # case-insensitive suffix handle
    assert store.resolve(t2.short_id).id == t2.id  # same-instant tasks stay distinct
    with pytest.raises(KeyError):
        store.resolve("zzzzzz")
    shared = t1.id[:2]  # ULIDs minted the same second share their prefix
    assert t2.id.startswith(shared)
    with pytest.raises(ValueError):
        store.resolve(shared)


def test_report_updates_status_and_board(home: Path):
    store = TaskStore(home)
    t = store.add("proj", "x", "fake")
    tasks.report(home, t.short_id, status="executing", text="working on it")
    tasks.report(home, t.id, status="pr", text="opened", pr_url="https://example.com/pr/1")
    fresh = store.get(t.id)
    assert fresh.status == "pr" and fresh.pr_url == "https://example.com/pr/1"
    assert [r["status"] for r in tasks.read_reports(home, t.id)] == ["executing", "pr"]
    board = MessageBus(home).read_topic(tasks.BOARD_TOPIC)
    assert [m.type for m in board] == ["task.executing", "task.pr"]


# -- runner ---------------------------------------------------------------


def test_run_creates_worktree_and_streams_transcript(home: Path, project: str, tmp_path: Path):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "improve the README", "fake")

    assert run_task(home, config, task.id) == 0

    fresh = TaskStore(home).get(task.id)
    workdir = Path(fresh.workdir)
    assert workdir == tasks.worktree_path(home, task.id) and workdir.is_dir()
    branches = subprocess.run(
        ["git", "-C", str(tmp_path / "proj"), "branch", "--list", f"quorum/{task.short_id}"],
        capture_output=True, text=True,
    ).stdout
    assert f"quorum/{task.short_id}" in branches

    text = transcript_text(home, task.id)
    assert f"Task ID: {task.short_id}" in text  # preamble reached the harness
    assert "improve the README" in text  # so did the task prompt
    assert f"CWD| {workdir}" in text  # and it ran in the worktree
    assert fresh.session == "sess-fake-123"  # captured from the JSON stream
    assert len(fresh.runs) == 1 and fresh.runs[0].exit_code == 0


def test_guidance_is_claimed_and_injected(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    bus = MessageBus(home)
    bus.send("monitor", tasks.inbox_name(task.id), type="nudge", text="try the other approach")

    run_task(home, config, task.id)

    assert "try the other approach" in transcript_text(home, task.id)
    assert fsio.sorted_entries(bus.inbox_dir / tasks.inbox_name(task.id) / "new") == []


def test_resume_template_used_once_session_known(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    run_task(home, config, task.id)  # captures the session id
    run_task(home, config, task.id)

    entries = fsio.read_jsonl(tasks.transcript_path(home, task.id))
    argvs = [e["event"]["argv"] for e in entries if "event" in e and "argv" in e.get("event", {})]
    assert "--resumed" not in argvs[0]
    assert argvs[1][0] == "--resumed" and argvs[1][1] == "sess-fake-123"


def test_session_capture_accepts_codex_thread_ids():
    assert runner._find_session_id({"type": "thread.started", "thread_id": "th-1"}) == "th-1"
    assert runner._find_session_id({"threadId": "th-2"}) == "th-2"
    assert runner._find_session_id({"type": "system", "session_id": "s-1"}) == "s-1"
    assert runner._find_session_id({"type": "turn.started"}) is None


def test_inject_pump_delivers_mid_run_guidance(home: Path, project: str, monkeypatch):
    """A nudge that arrives while the harness is running reaches it as a
    stream-json user turn (the fake posts the nudge itself mid-run, before its
    first turn boundary, so delivery provably happens inside one run)."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    monkeypatch.setenv("FAKE_HARNESS_INJECT_POST", "nudge")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    text = transcript_text(home, task.id)
    assert "switch to the fallback plan" in text  # the nudge reached the live harness
    assert '"role": "user"' in text  # ...framed as a stream-json user turn
    inbox = MessageBus(home).inbox_dir / tasks.inbox_name(task.id)
    assert fsio.sorted_entries(inbox / "new") == []  # consumed, not re-delivered next run
    assert fsio.sorted_entries(inbox / "cur") == []


class _PipeEnd:
    """A stand-in for the harness's stdin pipe: records turns, knows if closed."""

    def __init__(self):
        self.turns: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.turns.append(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_pump_never_closes_stdin_with_a_claimed_message_in_flight(home: Path):
    """The turn-boundary race behind a CI flake (PR #74, `test (3.12)`): the
    harness posts a nudge and then emits its `result`; the pump claims the
    nudge (rename out of new/) and only *then* counts the delivery. A result
    landing in that gap saw "answered, nothing pending" and closed stdin
    with the nudge in flight — one result event instead of two, and the
    nudge bounced back to new/. This forces that interleaving: the result
    arrives while the claim is mid-way, on another thread, exactly as the
    transcript reader delivers it."""
    bus = MessageBus(home)
    inbox = tasks.inbox_name("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    bus.send("user", inbox, text="switch to the fallback plan")
    stdin = _PipeEnd()
    pump = runner.GuidancePump(home, inbox, stdin, "the prompt")

    real_claim = pump._bus.claim
    result_seen = threading.Event()

    def racing_claim(agent):
        for claimed in real_claim(agent):
            # the message is in cur/ now; before the pump can count it, the
            # harness's first result reaches on_event from the reader thread
            t = threading.Thread(target=lambda: (pump.on_event({"type": "result"}),
                                                 result_seen.set()))
            t.start()
            t.join(timeout=0.3)  # the fixed pump holds the lock here: it must wait
            yield claimed

    pump._bus.claim = racing_claim
    pump.start()
    try:
        assert result_seen.wait(5)
        deadline = time.monotonic() + 5
        while len(stdin.turns) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(stdin.turns) == 2, stdin.turns  # prompt turn, then the nudge
        assert "switch to the fallback plan" in stdin.turns[1]
        assert not stdin.closed  # the nudge's answer is still owed
        inbox_dir = bus.inbox_dir / inbox
        assert fsio.sorted_entries(inbox_dir / "new") == []  # delivered, not bounced
        while fsio.sorted_entries(inbox_dir / "cur") and time.monotonic() < deadline:
            time.sleep(0.01)  # the ack follows the write on the pump thread
        assert fsio.sorted_entries(inbox_dir / "cur") == []  # ...and acked

        pump.on_event({"type": "result"})  # the harness answers the nudge
        assert stdin.closed  # now the run is idle: every turn answered
    finally:
        pump.stop()


def test_build_harness_argv_strips_prompt_for_inject_harnesses():
    """A stream-json CLI reads user turns only from stdin and ignores an argv
    prompt (this is how real claude behaves), so inject templates lose their
    "{prompt}" element and never get the prompt appended."""
    inject = HarnessConfig(start=["h", "-p", "{prompt}", "--flag"], inject="stream-json")
    assert runner.build_harness_argv(inject, "the prompt") == ["h", "-p", "--flag"]
    bare = HarnessConfig(start=["h"], inject="stream-json")
    assert runner.build_harness_argv(bare, "the prompt") == ["h"]
    plain = HarnessConfig(start=["h"])
    assert runner.build_harness_argv(plain, "the prompt") == ["h", "the prompt"]


def test_inject_prompt_arrives_over_stdin_not_argv(home: Path, project: str, monkeypatch):
    """The composed prompt reaches an inject harness as the pump's opening
    stream-json user turn — the regression that hung every real claude run:
    the prompt sat on argv, which the stream-json protocol ignores, and the
    harness waited on stdin until the run timeout killed it."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "improve the README", "fake")

    assert run_task(home, config, task.id) == 0

    entries = fsio.read_jsonl(tasks.transcript_path(home, task.id))
    argvs = [e["event"]["argv"] for e in entries if "argv" in e.get("event", {})]
    assert argvs and all("improve the README" not in arg for arg in argvs[0])
    text = transcript_text(home, task.id)
    assert "improve the README" in text  # the prompt reached the harness via stdin…
    assert f"Task ID: {task.short_id}" in text  # …preamble included


def test_inject_pump_closes_an_idle_run(home: Path, project: str, monkeypatch):
    """With nothing in the inbox the pump closes stdin at the first turn
    boundary — an inject-mode run still ends on its own."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0
    fresh = TaskStore(home).get(task.id)
    assert fresh.runs[0].exit_code == 0


def test_run_records_the_usage_the_harness_reported(home: Path, project: str, monkeypatch):
    """Capture: a result event carrying cost and tokens lands on the run's
    entry in task.json, canonicalized, and adds up across runs."""
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    spent = TaskStore(home).get(task.id).runs[0].usage
    assert spent["cost_usd"] == 0.42
    assert spent["total_tokens"] == 11000  # input + output + cache read + cache creation
    assert spent["events"] == 1

    assert run_task(home, config, task.id) == 0
    fresh = TaskStore(home).get(task.id)
    total = usage.total(r.usage for r in fresh.runs)
    assert total["cost_usd"] == pytest.approx(0.84) and total["runs"] == 2


def test_a_harness_that_reports_no_usage_is_still_fully_supported(
    home: Path, project: str
):
    """Fail-soft: silence means unknown, recorded as None — never zero, and
    never an error."""
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    fresh = TaskStore(home).get(task.id)
    assert fresh.runs[0].usage is None
    assert usage.total(r.usage for r in fresh.runs) is None


def test_multi_turn_usage_is_reduced_by_max_not_summed(
    home: Path, project: str, monkeypatch
):
    """A pumped run emits one result event per turn, each reporting the
    session's cumulative totals; summing them would multiply the spend."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    monkeypatch.setenv("FAKE_HARNESS_INJECT_POST", "nudge")
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    spent = TaskStore(home).get(task.id).runs[0].usage
    assert spent["events"] >= 2  # more than one result event was seen…
    assert spent["cost_usd"] == 0.42  # …and the run still cost what it cost
    assert spent["total_tokens"] == 11000


def test_harness_reports_back_through_the_cli(home: Path, project: str, monkeypatch):
    """The cooperative return channel end to end: the harness subprocess calls
    `python -m quorum task report` against QUORUM_HOME and the task's status,
    reports file, and board all reflect it."""
    monkeypatch.setenv("FAKE_HARNESS_MODE", "report")
    monkeypatch.setenv("FAKE_HARNESS_PR_URL", "https://example.com/pr/9")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "done"
    assert fresh.pr_url == "https://example.com/pr/9"
    assert any(m.type == "task.done" for m in MessageBus(home).read_topic(tasks.BOARD_TOPIC))


def test_failing_harness_records_exit_code_and_no_status_change(home: Path, project: str, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "fail")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    assert run_task(home, config, task.id) == 3
    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "queued"  # the runner never sets status itself
    assert fresh.runs[0].exit_code == 3


def test_no_worktree_runs_in_project_dir(home: Path, project: str, tmp_path: Path):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake", use_worktree=False)
    run_task(home, config, task.id)
    assert f"CWD| {(tmp_path / 'proj').resolve()}" in transcript_text(home, task.id)


def test_auto_commit_captures_work_the_harness_left_behind(home: Path, project: str, monkeypatch):
    """The safety net's hard guarantee: with `[tasks].auto_commit` on, work a
    harness left uncommitted lands on the task branch, which outlives the
    worktree — no policy, no status change, no push."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    workdir = tasks.worktree_path(home, task.id)
    assert git_out(workdir, "status", "--porcelain") == ""  # nothing stranded
    assert git_out(workdir, "log", "-1", "--pretty=%s") == runner.AUTO_COMMIT_MESSAGE
    assert "scratch.txt" in git_out(workdir, "show", "--name-only", "--pretty=", "HEAD")
    assert git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD") == f"quorum/{task.short_id}"
    assert "auto-committed 1 path(s)" in transcript_text(home, task.id)
    fresh = TaskStore(home).get(task.id)
    assert fresh.runs[0].exit_code == 0 and fresh.status == "queued"
    assert fresh.runs[0].auto_commit.startswith("auto-committed 1 path(s)")  # durable record


def test_auto_commit_is_off_by_default(home: Path, project: str, monkeypatch):
    """Default off: the tree stays dirty and stranded-work detection sees it."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    run_task(home, config, task.id)

    workdir = tasks.worktree_path(home, task.id)
    assert "scratch.txt" in git_out(workdir, "status", "--porcelain")
    assert git_out(workdir, "log", "-1", "--pretty=%s") == "init"
    assert tasks.workdir_git_state(TaskStore(home).get(task.id))["dirty"] == 1


def test_auto_commit_never_touches_a_no_worktree_checkout(
    home: Path, project: str, tmp_path: Path, monkeypatch
):
    """A `--no-worktree` task runs in the user's own checkout on whatever
    branch they had out; committing there is not quorum's to do."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake", use_worktree=False)

    run_task(home, config, task.id)

    repo = tmp_path / "proj"
    assert "scratch.txt" in git_out(repo, "status", "--porcelain")
    assert git_out(repo, "log", "-1", "--pretty=%s") == "init"


def test_auto_commit_is_a_no_op_on_a_clean_tree(tmp_path: Path):
    """A harness that committed its own work gets no empty extra commit."""
    repo = make_repo(tmp_path)
    assert runner.auto_commit_workdir(repo) == ""
    assert git_out(repo, "log", "-1", "--pretty=%s") == "init"


def test_auto_commit_failure_is_recorded_not_raised(home: Path, project: str, tmp_path: Path):
    """A net that cannot fire leaves a note and the dirty tree behind — never
    an exception that would cost the run its record."""
    loose = tmp_path / "loose"
    loose.mkdir()
    with pytest.raises(RunnerError, match="git status failed"):
        runner.auto_commit_workdir(loose)

    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "x", "fake")
    assert run_task(home, config, task.id) == 0  # creates the worktree
    workdir = tasks.worktree_path(home, task.id)
    (workdir / "left.txt").write_text("x")
    repo_git(workdir, "checkout", "--detach")  # a state the net refuses to commit in

    note = runner._maybe_auto_commit(home, config, store, store.get(task.id), workdir)

    assert note.startswith("auto-commit failed")  # absorbed into a note...
    assert "auto-commit failed" in transcript_text(home, task.id)
    assert "left.txt" in git_out(workdir, "status", "--porcelain")  # ...tree left dirty


def test_auto_commit_sees_untracked_files_hidden_by_repo_config(
    home: Path, project: str, tmp_path: Path, monkeypatch
):
    """`status.showUntrackedFiles no` (a git-recommended perf setting on big
    repos, shared with linked worktrees) must not blind the net to an
    untracked-only crash — the net's core case."""
    repo_git(tmp_path / "proj", "config", "status.showUntrackedFiles", "no")
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    workdir = tasks.worktree_path(home, task.id)
    assert git_out(workdir, "log", "-1", "--pretty=%s") == runner.AUTO_COMMIT_MESSAGE
    # the stranded-work probe must see through the same setting
    (workdir / "more.txt").write_text("x")
    assert tasks.workdir_git_state(TaskStore(home).get(task.id))["dirty"] == 1


def test_auto_commit_bypasses_hooks_and_signing(
    home: Path, project: str, tmp_path: Path, monkeypatch
):
    """A failing pre-commit hook (or a signing prompt) would defeat the net in
    exactly the crashed-harness case it exists for — commits go through with
    --no-verify and signing off."""
    hook = tmp_path / "proj" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    workdir = tasks.worktree_path(home, task.id)
    assert git_out(workdir, "log", "-1", "--pretty=%s") == runner.AUTO_COMMIT_MESSAGE


def test_auto_commit_declines_off_branch_and_in_progress_states(tmp_path: Path):
    """Detached HEAD: the commit would belong to no branch and die with the
    worktree. Merge in progress: add -A + commit would *conclude* the merge,
    conflict markers and all. Both raise; the tree stays dirty and flagged."""
    detached = make_repo(tmp_path, "detached")
    repo_git(detached, "checkout", "--detach")
    (detached / "x.txt").write_text("x")
    with pytest.raises(RunnerError, match="detached"):
        runner.auto_commit_workdir(detached)

    merging = make_repo(tmp_path, "merging")
    (merging / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    (merging / "y.txt").write_text("y")
    with pytest.raises(RunnerError, match="in progress"):
        runner.auto_commit_workdir(merging)


def test_auto_commit_leaves_a_terminal_task_alone(home: Path, project: str, monkeypatch):
    """A harness that reported done owns its tree's final state: sweeping
    leftovers into a finished branch would re-flag the task as stranded and
    push junk toward its PR."""
    monkeypatch.setenv("FAKE_HARNESS_MODE", "report")  # reports status=done
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    workdir = tasks.worktree_path(home, task.id)
    assert git_out(workdir, "log", "-1", "--pretty=%s") == "init"
    assert "scratch.txt" in git_out(workdir, "status", "--porcelain")
    assert TaskStore(home).get(task.id).runs[0].auto_commit is None


def test_auto_commit_skips_sandboxed_runs_with_a_note(home: Path, project: str):
    """Under [sandbox].use_nono the runner can no longer run git at all, so
    the net says so instead of failing cryptically every run."""
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "x", "fake")
    assert run_task(home, config, task.id) == 0  # creates the worktree
    workdir = tasks.worktree_path(home, task.id)
    (workdir / "left.txt").write_text("x")
    with open(home / "config.toml", "a") as fh:
        fh.write("[sandbox]\nuse_nono = true\n")
    sandboxed = load_config(home)

    note = runner._maybe_auto_commit(home, sandboxed, store, store.get(task.id), workdir)

    assert "sandboxed" in note and note in transcript_text(home, task.id)
    assert "left.txt" in git_out(workdir, "status", "--porcelain")  # untouched


def test_auto_commit_counts_paths_not_status_lines(tmp_path: Path):
    """An untracked directory is one porcelain line however many files it
    holds; the note must count the files actually committed."""
    repo = make_repo(tmp_path, "many")
    gen = repo / "gen"
    gen.mkdir()
    for i in range(3):
        (gen / f"f{i}.txt").write_text("x")
    assert runner.auto_commit_workdir(repo).startswith("auto-committed 3 path(s)")


def test_auto_commit_ownership_check_survives_symlinked_home(
    home: Path, project: str, tmp_path: Path
):
    """The workdir/worktree comparison resolves both sides, so a symlinked
    spelling of the home never silently disables the net."""
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "x", "fake")
    assert run_task(home, config, task.id) == 0
    workdir = tasks.worktree_path(home, task.id)
    (workdir / "left.txt").write_text("x")
    alias = tmp_path / "home-alias"
    alias.symlink_to(home)

    note = runner._maybe_auto_commit(alias, config, store, store.get(task.id), workdir)

    assert note.startswith("auto-committed 1 path(s)")


def test_missing_harness_and_unknown_task_fail_loud(home: Path, project: str):
    (home / "config.toml").write_text("")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "ghost")
    with pytest.raises(RunnerError, match="no \\[harness.ghost\\]"):
        run_task(home, config, task.id)
    with pytest.raises(RunnerError, match="no task matching"):
        run_task(home, config, "zzzz")


def test_second_concurrent_run_is_refused(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    lock = tasks.runner_lock_path(home, task.id)
    # a live *foreign* pid: our own would read as a stale same-process lock
    lock.write_text('{"pid": 1}\n')
    try:
        with pytest.raises(RunnerError, match="already has a live run"):
            run_task(home, config, task.id)
    finally:
        lock.unlink()


# -- stopping a hung run --------------------------------------------------


def start_detached_run(
    home: Path, task_id: str, fresh_session: bool = False
) -> subprocess.Popen:
    """Launch a real detached run, in its own session, and never reap it.

    Deliberately unreaped: the test process stays alive, so a killed run
    lingers as a zombie in its group — exactly what a long-lived caller
    (the TUI) used to leave behind, and what every liveness answer here has
    to survive.
    """
    return subprocess.Popen(
        [
            sys.executable, "-m", "quorum", "task", "run", task_id, "--home", str(home),
            *(["--fresh-session"] if fresh_session else []),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "QUORUM_HOME": str(home)},
    )


def wait_for_zombie(pid: int, timeout: float = 20.0) -> None:
    """Block until `pid` has exited without being waited on. Asks `ps` by
    hand rather than `Popen.poll()`, which would reap the very zombie the
    caller wants."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if state.upper().startswith("Z"):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} never became a zombie")


def wait_for_live_run(home: Path, task_id: str, timeout: float = 20.0) -> None:
    """Block until the run holds its lock and the harness has said something."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tasks.runner_alive(home, task_id) and tasks.transcript_path(home, task_id).exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"no live run for {task_id} within {timeout}s")


def test_stop_ends_the_run_and_leaves_the_task_alone(home: Path, project: str):
    """`task stop` is the non-terminal kill: the run dies, the task does not."""
    harness_config(home, extra='env = { FAKE_HARNESS_MODE = "stall" }\n')
    task = TaskStore(home).add(project, "x", "fake")
    proc = start_detached_run(home, task.id)
    wait_for_live_run(home, task.id)

    result = runner.stop_run(home, task.short_id, grace_seconds=5)

    assert result["signal"] == "SIGTERM" and result["pid"] == proc.pid
    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "queued"  # stop never sets status
    assert Path(fresh.workdir).is_dir()  # nor touches the work
    assert len(fresh.runs) == 1
    run = fresh.runs[0]
    assert run.stopped and run.exit_code == -signal.SIGTERM and run.ended_at
    assert not tasks.runner_lock_path(home, task.id).exists()  # the dead runner's lock
    assert "run.stopped" in transcript_text(home, task.id)


def test_stop_sigkills_a_harness_that_ignores_sigterm(home: Path, project: str):
    harness_config(home, extra='env = { FAKE_HARNESS_MODE = "ignore_sigterm" }\n')
    task = TaskStore(home).add(project, "x", "fake")
    proc = start_detached_run(home, task.id)
    wait_for_live_run(home, task.id)
    group = os.getpgid(proc.pid)

    result = runner.stop_run(home, task.short_id, grace_seconds=1)

    # SIGTERM kills the runner but not the harness, so the group check is what
    # notices and escalates — nothing is left running in the group afterwards
    # (the unreaped runner is still *in* it, which is why the question has to
    # be `group_alive` and not a bare killpg).
    assert result["signal"] == "SIGKILL"
    assert not fsio.group_alive(group)
    assert TaskStore(home).get(task.id).runs[0].stopped


def test_stop_closes_a_run_whose_runner_is_a_zombie(home: Path, project: str):
    """A runner nobody reaped is a process-table entry, not a run.

    `launch_detached`'s caller may keep running (the TUI's `s` binding), and
    then the killed runner stays a zombie in its group. Reading that as
    "alive" made `task stop` raise "survived SIGKILL" — no run record, and a
    stale lock that also refused the next `task run`.
    """
    harness_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    dead = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    wait_for_zombie(dead.pid)
    lock = tasks.runner_lock_path(home, task.id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_json(
        lock,
        {"role": "task-runner", "task": task.id, "pid": dead.pid,
         "started_at": fsio.iso(fsio.utc_now()), "fresh_session": True},
    )

    try:
        result = runner.stop_run(home, task.short_id, grace_seconds=1)
    finally:
        dead.wait()  # reap it: the test leaves no zombie behind

    assert result["signal"] is None and result["run_recorded"]
    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "queued"  # stop is still not cancel
    run = fresh.runs[-1]
    assert run.stopped and run.fresh_session and run.ended_at
    assert not lock.exists()  # ...and the next run may start


def test_a_stopped_fresh_run_is_recorded_as_fresh(home: Path, project: str):
    """The digest counts fresh restarts off the run records, so the record
    `stop_run` writes for the run it killed has to know which kind it was —
    otherwise stop/--fresh-session/stop never reaches the escalation rung."""
    harness_config(home, extra='env = { FAKE_HARNESS_MODE = "stall" }\n')
    task = TaskStore(home).add(project, "x", "fake")
    start_detached_run(home, task.id, fresh_session=True)
    wait_for_live_run(home, task.id)

    runner.stop_run(home, task.short_id, grace_seconds=5)

    run = TaskStore(home).get(task.id).runs[0]
    assert run.stopped and run.fresh_session


def test_stop_refuses_an_attached_task_and_a_task_with_no_run(home: Path, project: str):
    harness_config(home)
    store = TaskStore(home)
    idle = store.add(project, "x", "fake")
    with pytest.raises(RunnerError, match="no live run"):
        runner.stop_run(home, idle.id)
    live = store.add(project, "x", "fake", attached=True)
    tasks.runner_lock_path(home, live.id).parent.mkdir(parents=True, exist_ok=True)
    tasks.runner_lock_path(home, live.id).write_text('{"pid": 1}\n')
    with pytest.raises(RunnerError, match="never kills your session"):
        runner.stop_run(home, live.id)  # the user's own session, not ours to kill


# -- the stall watchdog ---------------------------------------------------


def test_stall_watchdog_ends_a_silent_run(home: Path, project: str):
    """A harness that prints one line and hangs becomes a dead runner with a
    non-terminal status — the situation the manager already handles."""
    harness_config(
        home,
        extra='env = { FAKE_HARNESS_MODE = "stall" }\n',
        tasks_extra="run_stall_timeout_seconds = 1.0\n",
    )
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) != 0

    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "queued"  # the watchdog is mechanical: no status
    run = fresh.runs[0]
    assert run.stalled and run.exit_code != 0
    assert "run stalled" in transcript_text(home, task.id)


def test_the_watchdog_is_off_by_default_and_a_healthy_run_is_never_stalled(
    home: Path, project: str
):
    from quorum.config import TasksConfig

    assert TasksConfig().run_stall_timeout_seconds == 0.0
    harness_config(home)
    config = load_config(home)
    assert config.tasks.run_stall_timeout_seconds == 0.0
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    assert TaskStore(home).get(task.id).runs[0].stalled is False


def test_stall_watchdog_context_is_a_no_op_when_disabled(tmp_path: Path):
    with runner.stall_watchdog(None, 0.0, tmp_path / "t.jsonl") as watchdog:
        assert watchdog is None  # no thread, no timer, nothing to go wrong


# -- fresh sessions -------------------------------------------------------


def test_fresh_session_drops_the_resume_argv_and_is_recorded(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    run_task(home, config, task.id)  # captures sess-fake-123
    assert TaskStore(home).get(task.id).session == "sess-fake-123"
    run_task(home, config, task.id)  # resumes it

    run_task(home, config, task.id, fresh_session=True)

    entries = fsio.read_jsonl(tasks.transcript_path(home, task.id))
    argvs = [e["event"]["argv"] for e in entries if "argv" in e.get("event", {})]
    assert argvs[1][0] == "--resumed"  # the ordinary relaunch resumed
    assert "--resumed" not in argvs[2]  # the fresh one did not
    runs = TaskStore(home).get(task.id).runs
    assert [r.fresh_session for r in runs] == [False, False, True]
    assert "fresh session" in transcript_text(home, task.id)


def test_workdir_git_state_tracks_dirty_and_unpushed(home: Path, tmp_path: Path):
    repo = make_repo(tmp_path)
    store = TaskStore(home)
    task = store.add(project="proj", prompt="p", harness="fake")
    assert tasks.workdir_git_state(task) is None  # no workdir resolved yet

    task = store.update(task.id, workdir=str(repo))
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 0
    assert state["unpushed"] is None  # no remote: pushing does not apply

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo_git(repo, "remote", "add", "origin", str(bare))
    repo_git(repo, "push", "-q", "-u", "origin", "HEAD")
    assert tasks.workdir_git_state(task)["unpushed"] == 0

    (repo / "work.txt").write_text("wip")
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 1
    assert state["unpushed"] == 0

    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "wip")
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 0
    assert state["unpushed"] == 1
    assert state["branch"]

    repo_git(repo, "push", "-q", "origin", "HEAD")
    assert tasks.workdir_git_state(task)["unpushed"] == 0


def test_task_rows_surface_git_state_but_skip_settled_tasks(home: Path, tmp_path: Path):
    from datetime import timedelta

    from quorum import views

    repo = make_repo(tmp_path)
    store = TaskStore(home)
    task = store.add(project="proj", prompt="p", harness="fake")
    store.update(task.id, workdir=str(repo), status="executing")
    (repo / "work.txt").write_text("wip")

    row = views.task_rows(home)[0]
    assert row["git"]["dirty"] == 1

    # long-terminal tasks stop being probed (views refresh constantly)
    old = fsio.utc_now() - timedelta(hours=views.GIT_PROBE_TERMINAL_HOURS + 1)
    store.update(task.id, now=old, status="done")
    assert views.task_rows(home)[0]["git"] is None


# -- dependencies (#31) ---------------------------------------------------


def test_resolve_dependencies_accepts_short_ids_and_dedupes(home: Path):
    store = TaskStore(home)
    first = store.add("proj", "upstream", "fake")
    second = store.add("proj", "other upstream", "fake")
    resolved = tasks.resolve_dependencies(
        store, [first.short_id, second.id, first.short_id.upper()]
    )
    assert resolved == [first.id, second.id]


def test_resolve_dependencies_rejects_unknown_and_self(home: Path):
    store = TaskStore(home)
    existing = store.add("proj", "upstream", "fake")
    with pytest.raises(ValueError, match="no task matching"):
        tasks.resolve_dependencies(store, ["zzzzzz"])
    with pytest.raises(ValueError, match="cannot depend on itself"):
        tasks.resolve_dependencies(store, [existing.short_id], self_id=existing.id)


def test_cannot_depend_on_a_perpetual_task(home: Path):
    """A perpetual task never reaches a terminal status, so a dependent would
    wait on it forever — `task add --after` refuses the chain outright."""
    store = TaskStore(home)
    upstream = store.add("proj", "forever", "fake")
    store.update(upstream.id, perpetual=True)
    with pytest.raises(ValueError, match="perpetual"):
        tasks.resolve_dependencies(store, [upstream.short_id])


def test_dependency_state_reads_waiting_failed_and_missing(home: Path):
    store = TaskStore(home)
    running = store.add("proj", "still going", "fake")
    finished = store.add("proj", "shipped", "fake", status="done")
    dead = store.add("proj", "gave up", "fake", status="blocked")
    dependent = store.add(
        "proj", "the follow-up", "fake",
        depends_on=[running.id, finished.id, dead.id, "01GHOSTGHOSTGHOSTGHOSTGH0ST"],
    )
    state = tasks.dependency_state(dependent, {t.id: t for t in store.list()})
    # only a dependency that still might finish blocks
    assert state["waiting_on"] == [running.short_id]
    assert state["failed"] == [dead.short_id]  # never blocks: the manager judges it
    assert state["missing"] == ["tgh0st"]  # same class as failed, same treatment
    assert state["cycle"] is False

    # once the upstream reports done, nothing is waiting — a pruned dependency
    # is reported, not waited on, so it can never strand the dependent
    tasks.report(home, running.id, "done", "shipped it")
    state = tasks.dependency_state(
        store.get(dependent.id), {t.id: t for t in store.list()}
    )
    assert state["waiting_on"] == [] and state["missing"] == ["tgh0st"]


def test_dependency_state_flags_a_hand_edited_cycle_instead_of_crashing(home: Path):
    store = TaskStore(home)
    a = store.add("proj", "a", "fake")
    b = store.add("proj", "b", "fake", depends_on=[a.id])
    store.update(a.id, depends_on=[b.id])  # only reachable by hand-editing
    by_id = {t.id: t for t in store.list()}
    state = tasks.dependency_state(store.get(b.id), by_id)
    assert state["cycle"] is True and state["waiting_on"] == [a.short_id]
    # an upstream cycle the task is not itself part of is flagged too
    c = store.add("proj", "c", "fake", depends_on=[b.id])
    assert tasks.dependency_state(c, {t.id: t for t in store.list()})["cycle"] is True


def test_run_refuses_a_task_with_unfinished_dependencies(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    upstream = store.add(project, "do the work", "fake")
    dependent = store.add(project, "review the work", "fake", depends_on=[upstream.id])
    with pytest.raises(RunnerError, match=f"waiting on {upstream.short_id}"):
        run_task(home, config, dependent.id)
    assert store.get(dependent.id).runs == []  # nothing was spent


def test_force_overrides_the_dependency_refusal(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    upstream = store.add(project, "do the work", "fake")
    dependent = store.add(project, "review the work", "fake", depends_on=[upstream.id])
    assert run_task(home, config, dependent.id, force=True) == 0
    assert len(store.get(dependent.id).runs) == 1


def test_run_refuses_a_task_whose_last_run_blew_its_budget(
    home: Path, project: str, monkeypatch
):
    """The budget gate (#19): with a `[tasks]` budget set, a task whose last
    run reported more than it is refused its next run — the rate-limit-class
    rail beside the dependency refusal, checked before anything is spent."""
    harness_config(home, tasks_extra="max_cost_per_run = 0.10\n")
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "spendy work", "fake")

    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    assert run_task(home, config, task.id) == 0  # the first run is never gated
    with pytest.raises(RunnerError, match="exceeded its budget .*cost \\$0.42 > max_cost_per_run"):
        run_task(home, config, task.id)
    fresh = store.get(task.id)
    assert len(fresh.runs) == 1  # refused before spending anything
    assert fresh.status == "queued"  # a rail never sets status
    assert not runner.runner_lock_path(home, task.id).exists()


def test_force_overrides_the_budget_gate_and_a_cheaper_run_clears_it(
    home: Path, project: str, monkeypatch
):
    harness_config(home, tasks_extra="max_tokens_per_run = 1000\n")
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "spendy work", "fake")

    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")  # 11k tokens: over
    assert run_task(home, config, task.id) == 0
    with pytest.raises(RunnerError, match="next run gated"):
        run_task(home, config, task.id)

    # --force waives the gate for one run; the harness then reports nothing,
    # and silence is not evidence of spend — so the gate is clear again
    monkeypatch.delenv("FAKE_HARNESS_USAGE")
    assert run_task(home, config, task.id, force=True) == 0
    assert run_task(home, config, task.id) == 0
    assert len(store.get(task.id).runs) == 3

    # over again, then a forced run that comes in under budget clears it too
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "0.42")
    assert run_task(home, config, task.id) == 0
    with pytest.raises(RunnerError, match="tokens 11.0k > max_tokens_per_run 1.0k"):
        run_task(home, config, task.id)
    assert runner.budget_blockers(config.tasks, store.get(task.id)) == [
        "tokens 11.0k > max_tokens_per_run 1.0k"
    ]
    # a lighter run: same fake, so patch the recorded usage instead of the harness
    last = store.get(task.id).runs
    last[-1].usage = {"total_tokens": 10, "events": 1}
    store.update(task.id, runs=[r.model_dump() for r in last])
    assert runner.budget_blockers(config.tasks, store.get(task.id)) == []
    assert run_task(home, config, task.id) == 0


def test_budget_gate_is_off_at_zero(home: Path, project: str, monkeypatch):
    """No budget (the default) means no gate, whatever a run cost."""
    harness_config(home)
    config = load_config(home)
    assert config.tasks.max_cost_per_run == 0 and config.tasks.max_tokens_per_run == 0
    task = TaskStore(home).add(project, "expensive by design", "fake")
    monkeypatch.setenv("FAKE_HARNESS_USAGE", "250.00")
    assert run_task(home, config, task.id) == 0
    assert run_task(home, config, task.id) == 0
    assert len(TaskStore(home).get(task.id).runs) == 2


def test_a_satisfied_dependency_runs_and_reaches_the_prompt(
    home: Path, project: str, monkeypatch
):
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    upstream = store.add(project, "build the thing", "fake")
    tasks.report(home, upstream.id, "done", "shipped", pr_url="https://x/pr/7")
    dependent = store.add(project, "review the PR", "fake", depends_on=[upstream.id])

    assert run_task(home, config, dependent.id) == 0
    text = transcript_text(home, dependent.id)
    # the cheapest sufficient upstream handoff: status + pr url in the prompt,
    # and a pointer at `task show` for everything else
    assert f"- {upstream.short_id}: status=done pr=https://x/pr/7" in text
    assert "quorum task show" in text


def test_task_rows_surface_waiting_on(home: Path):
    from quorum import views

    store = TaskStore(home)
    upstream = store.add("proj", "first", "fake")
    dependent = store.add("proj", "second", "fake", depends_on=[upstream.id])
    rows = {r["id"]: r for r in views.task_rows(home)}
    assert rows[dependent.id]["waiting_on"] == [upstream.short_id]
    assert rows[dependent.id]["depends_on"] == [upstream.short_id]
    assert rows[upstream.id]["waiting_on"] == []

    tasks.report(home, upstream.id, "cancelled", "dropped")
    rows = {r["id"]: r for r in views.task_rows(home)}
    assert rows[dependent.id]["waiting_on"] == []
    assert rows[dependent.id]["dep_failed"] == [upstream.short_id]


def test_a_pruned_dependency_is_reported_not_waited_on(home: Path, project: str):
    """A dependency whose task directory is gone can never reach `done`, so it
    is treated exactly like a `blocked`/`cancelled` one: `DEP-MISSING` in the
    views, out of `waiting_on`, and no runner refusal. Waiting forever on it
    would strand the dependent with nothing on screen saying why."""
    from quorum import views

    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    upstream = store.add(project, "do the work", "fake")
    dependent = store.add(project, "review the work", "fake", depends_on=[upstream.id])
    shutil.rmtree(tasks.task_dir(home, upstream.id))

    row = {r["id"]: r for r in views.task_rows(home)}[dependent.id]
    assert row["waiting_on"] == [] and row["dep_missing"] == [upstream.short_id]
    assert run_task(home, config, dependent.id) == 0  # not refused


# -- perpetual tasks (#12) ---------------------------------------------------


def test_a_perpetual_run_gets_the_softened_delivery_conventions(
    home: Path, project: str
):
    """The preamble's "commit, push, report done" becomes "deliver every
    cycle, never report done" — and an ordinary task sees none of it."""
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    forever = store.add(project, "watch the build", "fake", perpetual=True)
    ordinary = store.add(project, "fix the docs", "fake")

    assert run_task(home, config, forever.id) == 0
    assert run_task(home, config, ordinary.id) == 0

    cycling = transcript_text(home, forever.id)
    assert "This is a PERPETUAL task" in cycling
    assert "Never report `done` or `cancelled`" in cycling
    assert "--status cycle-3" in cycling
    # the placeholder is always substituted (the preamble's comment header
    # still *documents* it, as it does {task_id} — hence the line anchor)
    assert "PROMPT| {perpetual}" not in cycling

    once = transcript_text(home, ordinary.id)
    assert "PERPETUAL" not in once and "PROMPT| {perpetual}" not in once
    # the ordinary delivery protocol survives in both
    assert "git push -u origin HEAD" in cycling and "git push -u origin HEAD" in once


def test_pr_state_survives_a_round_trip_and_defaults_to_unobserved(home: Path):
    """`pr_state` is what the *forge* said, kept beside — never merged into —
    the status the harness reported (#57)."""
    store = TaskStore(home)
    task = store.add("p", "ship it", "fake", status="done")
    assert task.pr_state is None and task.pr_state_at is None

    store.update(task.id, pr_state="merged", pr_state_at="2026-01-01T00:00:00Z")
    reread = store.get(task.id)
    assert reread.pr_state == "merged" and reread.pr_state_at == "2026-01-01T00:00:00Z"
    assert reread.status == "done"  # the observation never became the status


def test_a_task_json_written_before_pr_state_existed_still_loads(home: Path):
    """Old homes upgrade in place: the field is absent, not null, on every
    record written before this version."""
    import json as _json

    store = TaskStore(home)
    task = store.add("p", "old record", "fake")
    path = task_json_path(home, task.id)
    data = _json.loads(path.read_text())
    del data["pr_state"], data["pr_state_at"]
    path.write_text(_json.dumps(data))

    assert store.get(task.id).pr_state is None


def test_perpetual_survives_a_round_trip_and_defaults_off(home: Path):
    store = TaskStore(home)
    assert store.add("proj", "ordinary", "fake").perpetual is False
    forever = store.add("proj", "forever", "fake", perpetual=True)
    assert store.get(forever.id).perpetual is True
    # nothing about it changes what quorum treats as terminal
    assert store.update(forever.id, status="cycle-2").status not in tasks.TERMINAL_STATUSES


def test_a_perpetual_run_survives_an_edited_preamble_without_the_placeholder(
    home: Path, project: str
):
    """A home that customized task-preamble.md before {perpetual} existed
    never substitutes it — and a perpetual task that silently got the
    ordinary "report done" instructions would end on its first cycle."""
    from quorum import prompts

    harness_config(home)
    # drop the placeholder line only — the header's escaped `{{perpetual}}`
    # documentation stays, exactly as it would in a real edited copy
    edited = prompts.load(home, "task-preamble").replace("\n{perpetual}\n", "\n")
    assert "\n{perpetual}\n" not in edited and "{{perpetual}}" in edited
    (home / "prompts").mkdir(exist_ok=True)
    (home / "prompts" / "task-preamble.md").write_text(edited)

    store = TaskStore(home)
    forever = store.add(project, "watch the build", "fake", perpetual=True)
    assert run_task(home, load_config(home), forever.id) == 0
    cycling = transcript_text(home, forever.id)
    assert "This is a PERPETUAL task" in cycling
    assert "Never report `done` or `cancelled`" in cycling


# -- issue intake (#62) ------------------------------------------------------

ISSUE_URL = "https://github.com/kvndhrty/quorum/issues/62"


def test_a_run_from_an_issue_is_told_which_issue(home: Path, project: str):
    """The url is already inside the prompt; the preamble adds the
    convention — reference it, and never write to the forge."""
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    from_issue = store.add(project, f"Fix it\n\nbody\n\n({ISSUE_URL})", "fake", issue_url=ISSUE_URL)
    ordinary = store.add(project, "fix the docs", "fake")

    assert run_task(home, config, from_issue.id) == 0
    assert run_task(home, config, ordinary.id) == 0

    text = transcript_text(home, from_issue.id)
    assert f"This task came from {ISSUE_URL} (#62)" in text
    assert "Do not edit, comment on or close the issue itself" in text
    assert "PROMPT| {issue}" not in text  # always substituted

    once = transcript_text(home, ordinary.id)
    assert "This task came from" not in once and "PROMPT| {issue}" not in once


def test_an_issue_run_survives_an_edited_preamble_without_the_placeholder(
    home: Path, project: str
):
    """Same upgrade hazard as {perpetual}: a home that customized the
    preamble before {issue} existed would never tell the harness where the
    task came from, so the line is appended instead."""
    from quorum import prompts

    harness_config(home)
    edited = prompts.load(home, "task-preamble").replace("\n{issue}\n", "\n")
    assert "\n{issue}\n" not in edited and "{{issue}}" in edited
    (home / "prompts" / "task-preamble.md").write_text(edited)

    store = TaskStore(home)
    task = store.add(project, "fix it", "fake", issue_url=ISSUE_URL)
    assert run_task(home, load_config(home), task.id) == 0
    assert f"This task came from {ISSUE_URL} (#62)" in transcript_text(home, task.id)


def test_issue_url_survives_a_round_trip_and_defaults_to_none(home: Path):
    store = TaskStore(home)
    assert store.add("proj", "ordinary", "fake").issue_url is None
    from_issue = store.add("proj", "from an issue", "fake", issue_url=ISSUE_URL)
    assert store.get(from_issue.id).issue_url == ISSUE_URL
    # the record is where the work came from, never what happened to it:
    # nothing in quorum ever writes it again
    assert store.update(from_issue.id, status="done").issue_url == ISSUE_URL


def test_a_task_json_written_before_issue_url_existed_still_loads(home: Path):
    import json as _json

    store = TaskStore(home)
    task = store.add("p", "old record", "fake")
    path = task_json_path(home, task.id)
    data = _json.loads(path.read_text())
    del data["issue_url"]
    path.write_text(_json.dumps(data))

    assert store.get(task.id).issue_url is None


@pytest.mark.parametrize(
    "url,expected",
    [
        (ISSUE_URL, "#62"),
        (ISSUE_URL + "/", "#62"),
        ("https://gitlab.com/g/p/-/issues/7", "#7"),
        (None, ""),
        ("", ""),
        # not a shape we can abbreviate: shown whole rather than guessed at
        ("https://example.test/tickets/abc", "https://example.test/tickets/abc"),
    ],
)
def test_issue_ref_is_the_one_short_form_every_surface_uses(url, expected):
    assert tasks.issue_ref(url) == expected


def test_views_carry_both_the_url_and_the_short_form(home: Path):
    from quorum import views

    store = TaskStore(home)
    task = store.add("p", "from an issue", "fake", issue_url=ISSUE_URL)
    plain = store.add("p", "from a prompt", "fake")

    rows = {r["id"]: r for r in views.task_rows(home)}
    assert rows[task.id]["issue_url"] == ISSUE_URL
    assert rows[task.id]["issue_ref"] == "#62"
    assert rows[plain.id]["issue_url"] is None and rows[plain.id]["issue_ref"] == ""


# -- prompt overlay (#37) ----------------------------------------------------


def test_a_task_run_picks_up_the_preamble_overlay(home: Path, project: str):
    """House conventions belong in prompts/task-preamble.local.md — an
    overlay `quorum init` never seeds and never upgrades over — so the
    packaged preamble stays upgradable in a home that has policy."""
    harness_config(home)
    (home / "prompts" / "task-preamble.local.md").write_text(
        "Conventions in this home: always open DRAFT pull requests.\n"
    )

    store = TaskStore(home)
    task = store.add(project, "fix the docs", "fake")
    assert run_task(home, load_config(home), task.id) == 0

    text = transcript_text(home, task.id)
    assert "always open DRAFT pull requests" in text
    assert "git push -u origin HEAD" in text  # the packaged preamble, unforked
    assert "PROMPT| {local}" not in text


def test_the_perpetual_block_carries_its_own_overlay(home: Path, project: str):
    """task-perpetual.md has a {local} slot too, so cycle conventions land
    where they belong instead of being prepended by the fallback path."""
    harness_config(home)
    (home / "prompts" / "task-perpetual.local.md").write_text(
        "In this home, a cycle ends with `just check`.\n"
    )

    store = TaskStore(home)
    forever = store.add(project, "watch the build", "fake", perpetual=True)
    assert run_task(home, load_config(home), forever.id) == 0

    text = transcript_text(home, forever.id)
    assert "In this home, a cycle ends with `just check`." in text
    assert "This is a PERPETUAL task" in text  # the packaged block, unforked
    assert "PROMPT| {local}" not in text
    # the overlay lands inside the perpetual block, not ahead of the preamble
    assert text.index("You are an autonomous coding agent") < text.index("`just check`")


def test_run_refuses_a_held_task(home: Path, project: str):
    """The hold rail (#61): a task the user parked is refused before the lock
    is taken or anything is spent, and the refusal names how to lift it."""
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "do the work", "fake", held=True)
    with pytest.raises(RunnerError, match=f"task {task.short_id} is held"):
        run_task(home, config, task.id)
    assert store.get(task.id).runs == []
    # hold is not a status: the harness's word is untouched by the refusal
    assert store.get(task.id).status == "queued"


def test_force_overrides_the_hold_refusal(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "do the work", "fake", held=True)
    assert run_task(home, config, task.id, force=True) == 0
    assert len(store.get(task.id).runs) == 1
    # forcing a run does not release the hold — only a human does
    assert store.get(task.id).held is True


def test_releasing_a_held_task_makes_it_runnable_again(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    task = store.add(project, "do the work", "fake")
    store.update(task.id, held=True)
    with pytest.raises(RunnerError, match="is held"):
        run_task(home, config, task.id)
    store.update(task.id, held=False)
    assert run_task(home, config, task.id) == 0


def test_priority_defaults_to_zero_and_orders_nothing(home: Path):
    """Priority is data the manager reads: it round-trips through task.json
    and no reader in quorum sorts by it (#61)."""
    store = TaskStore(home)
    low = store.add("proj", "later", "fake", priority=-2)
    plain = store.add("proj", "whenever", "fake")
    high = store.add("proj", "first", "fake", priority=5)
    assert (low.priority, plain.priority, high.priority) == (-2, 0, 5)
    assert store.get(high.id).priority == 5
    # the listing stays chronological whatever the priorities say
    assert [t.id for t in store.list()] == [low.id, plain.id, high.id]
    assert store.update(high.id, priority=1).priority == 1
