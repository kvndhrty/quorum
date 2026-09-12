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

from conftest import harness_config, make_repo, repo_git
from quorum import fsio, runner, tasks, usage
from quorum.config import HarnessConfig, load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.runner import RunnerError, run_task
from quorum.tasks import TaskStore, task_json_path

TESTS_BIN = Path(__file__).parent / "bin"
FAKE = str(TESTS_BIN / "fake_harness.py")


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
            sys.executable, "-m", "quorum", "task", "run", task_id,
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
    """A home that customized the preamble before {issue} existed would never
    tell the harness where the task came from, so the line is appended
    instead."""
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


def test_a_task_record_with_removed_queue_fields_still_loads(home: Path):
    """`priority` and `held` were removed with the queue controls (#102), and
    `perpetual` with the round-two review (#128). A task.json written before
    those still has the keys, so the model must ignore them rather than refuse
    the record and lose the task."""
    store = TaskStore(home)
    task = store.add("proj", "queued before the removal", "fake")
    record = fsio.read_json(task_json_path(home, task.id))
    record["priority"] = 3
    record["perpetual"] = True
    record["held"] = True
    fsio.atomic_write_json(task_json_path(home, task.id), record)

    loaded = store.get(task.id)
    assert loaded is not None
    assert loaded.status == "queued" and loaded.prompt == "queued before the removal"
    assert not hasattr(loaded, "priority") and not hasattr(loaded, "held")
    assert not hasattr(loaded, "perpetual")
    # and an ordinary update rewrites the record without the dead keys
    store.update(task.id, status="done")
    assert "priority" not in fsio.read_json(task_json_path(home, task.id))


# -- the per-project preamble block (#63) ------------------------------------


def test_a_task_run_picks_up_its_project_block(home: Path, project: str, tmp_path: Path):
    """A home with several projects cannot put per-repo conventions in the
    home-wide overlay. The preamble's {project} slot takes them from the
    registry notes and from the project's own .quorum file, in that order."""
    harness_config(home)
    ProjectRegistry(home).update(project, notes="Base every branch on main.")
    repo = tmp_path / "proj"
    (repo / ".quorum").mkdir()
    (repo / ".quorum" / "task-preamble.local.md").write_text(
        "In this repo, run `just check` before pushing.\n", encoding="utf-8"
    )

    store = TaskStore(home)
    task = store.add(project, "fix the docs", "fake")
    assert run_task(home, load_config(home), task.id) == 0

    text = transcript_text(home, task.id)
    assert "Base every branch on main." in text
    assert "In this repo, run `just check` before pushing." in text
    assert text.index("Base every branch on main.") < text.index("`just check`")
    assert "git push -u origin HEAD" in text  # the packaged preamble, unforked
    assert "PROMPT| {project}" not in text


def test_a_project_with_nothing_to_say_leaves_no_hole(home: Path, project: str):
    harness_config(home)
    store = TaskStore(home)
    task = store.add(project, "fix the docs", "fake")
    assert run_task(home, load_config(home), task.id) == 0

    text = transcript_text(home, task.id)
    # the slot's own line goes with the empty block (the header comment's
    # escaped {{project}} documentation is a different line, and stays)
    assert "PROMPT| {project}" not in text
    assert "Work autonomously" in text


def test_an_unreadable_project_block_costs_the_block_not_the_run(
    home: Path, project: str, tmp_path: Path
):
    """The project file is user-owned and read on every single run, so it
    fails soft exactly like the overlay: the block is dropped, the run goes
    ahead, and `quorum prompt list` is where the bad file is reported."""
    harness_config(home)
    ProjectRegistry(home).update(project, notes="Base every branch on main.")
    repo = tmp_path / "proj"
    (repo / ".quorum").mkdir()
    (repo / ".quorum" / "task-preamble.local.md").write_bytes(b"run \xff\xfe just-check\n")

    store = TaskStore(home)
    task = store.add(project, "fix the docs", "fake")
    assert run_task(home, load_config(home), task.id) == 0

    text = transcript_text(home, task.id)
    assert "Base every branch on main." in text  # the readable source survives
    assert "just-check" not in text
    assert store.get(task.id).runs[-1].exit_code == 0


def test_the_project_block_is_read_before_the_sandbox_shuts_the_project_dir(
    home: Path, project: str, tmp_path: Path, monkeypatch
):
    """`build_task_capabilities` grants the worktree and the project's `.git`
    — never the project directory, where both halves of the block live. Read
    after `apply_task_sandbox`, the fail-soft read returns "" and the whole
    feature silently does nothing under [sandbox].use_nono."""
    harness_config(home)
    repo = tmp_path / "proj"
    (repo / ".quorum.toml").write_text('notes = "Base every branch on main."\n', encoding="utf-8")
    (repo / ".quorum").mkdir()
    (repo / ".quorum" / "task-preamble.local.md").write_text(
        "In this repo, run `just check` before pushing.\n", encoding="utf-8"
    )

    # Stand in for the sandbox: from the moment it is applied the project
    # directory is out of reach. A real ruleset denies the open; here the
    # files are simply gone, which the same fail-soft read swallows.
    def fake_apply(home_, config_, task_, workdir_):
        (repo / ".quorum.toml").unlink()
        shutil.rmtree(repo / ".quorum")

    monkeypatch.setattr("quorum.sandbox.apply_task_sandbox", fake_apply)
    config = load_config(home)
    config.sandbox.use_nono = True

    store = TaskStore(home)
    task = store.add(project, "fix the docs", "fake")
    assert run_task(home, config, task.id) == 0

    text = transcript_text(home, task.id)
    assert "Base every branch on main." in text  # the .quorum.toml marker's notes
    assert "`just check`" in text  # .quorum/task-preamble.local.md


# -- handoffs (#92) ---------------------------------------------------------


def test_report_with_a_handoff_stores_it_whole_and_atomically(home: Path):
    """The body of `task report --handoff` lands at tasks/<id>/handoff.md,
    written whole (no tmp file survives), flagged on the report entry."""
    store = TaskStore(home)
    t = store.add("proj", "build it", "fake")
    body = "Changed the parser.\n\nNot done: the CLI flag.\nCheck first: tests/test_parse.py\n"
    tasks.report(home, t.id, "done", "shipped", handoff=body)
    assert tasks.read_handoff(home, t.id) == body
    assert tasks.has_handoff(home, t.id)
    leftovers = [p.name for p in tasks.task_dir(home, t.id).iterdir() if p.name.startswith(".")]
    assert leftovers == []  # the atomic write's tmp file was renamed away
    entries = tasks.read_reports(home, t.id)
    assert entries[-1]["handoff"] is True and entries[-1]["status"] == "done"
    assert store.get(t.id).status == "done"


def test_a_handoff_is_one_file_per_task_and_the_last_write_wins(home: Path):
    store = TaskStore(home)
    t = store.add("proj", "build it", "fake")
    tasks.report(home, t.id, "blocked", "stuck", handoff="first draft")
    tasks.report(home, t.id, "done", "unstuck", handoff="the finished state")
    assert tasks.read_handoff(home, t.id) == "the finished state"
    # a report without --handoff leaves the stored one alone
    tasks.report(home, t.id, "done", "re-reported")
    assert tasks.read_handoff(home, t.id) == "the finished state"
    assert "handoff" not in tasks.read_reports(home, t.id)[-1]


def test_read_handoff_fails_soft(home: Path):
    """Missing → None (most tasks never write one); undecodable → None
    (the readers sit on the prompt-composition and digest paths)."""
    store = TaskStore(home)
    t = store.add("proj", "x", "fake")
    assert tasks.read_handoff(home, t.id) is None
    assert not tasks.has_handoff(home, t.id)
    tasks.handoff_path(home, t.id).write_bytes(b"\xff\xfe not utf-8")
    assert tasks.read_handoff(home, t.id) is None


def test_a_dependent_gets_the_upstream_handoff_in_its_prompt(
    home: Path, project: str
):
    from quorum.runner import dependency_note

    harness_config(home)
    config = load_config(home)
    store = TaskStore(home)
    upstream = store.add(project, "build the thing", "fake")
    tasks.report(
        home, upstream.id, "done", "shipped", pr_url="https://x/pr/7",
        handoff="Changed: the thing.\nNot done: its docs.\nCheck first: tests/test_thing.py",
    )
    silent = store.add(project, "another upstream", "fake", status="done")
    dependent = store.add(
        project, "review the PR", "fake", depends_on=[upstream.id, silent.id]
    )

    note = dependency_note(home, store.get(dependent.id))
    assert f"- {upstream.short_id}: status=done pr=https://x/pr/7" in note
    assert "it left a handoff (below)" in note
    assert f"## Handoff from {upstream.short_id}\n\nChanged: the thing." in note
    assert "Check first: tests/test_thing.py" in note
    # the upstream that wrote none gets no section and no mention of one
    assert f"## Handoff from {silent.short_id}" not in note
    assert note.count("it left a handoff") == 1

    assert run_task(home, config, dependent.id) == 0
    text = transcript_text(home, dependent.id)
    assert f"## Handoff from {upstream.short_id}" in text
    assert "Not done: its docs." in text


def test_a_long_handoff_is_clipped_per_dependency_with_a_pointer_at_task_show(
    home: Path,
):
    from quorum.runner import HANDOFF_MAX_BYTES, clip_handoff, dependency_note

    store = TaskStore(home)
    # Multi-byte characters, so a byte cap that split one would be visible. The
    # leading "x" shifts the alignment by one byte, which puts the cut in the
    # middle of an "é" — the case the dropped-byte count has to account for.
    long_body = "x" + ("é" * 100 + "\n") * 200  # 1 + 200 * 201 bytes, over the cap
    chatty = store.add(project="proj", prompt="write a lot", harness="fake")
    tasks.report(home, chatty.id, "done", "done", handoff=long_body)
    terse = store.add(project="proj", prompt="write a little", harness="fake")
    tasks.report(home, terse.id, "done", "done", handoff="one line, kept whole")
    dependent = store.add("proj", "next", "fake", depends_on=[chatty.id, terse.id])

    note = dependency_note(home, dependent)
    section = note.split(f"## Handoff from {chatty.short_id}\n\n")[1].split("\n\n## ")[0]
    kept, _, tail = section.rpartition("\n")
    assert len(kept.encode("utf-8")) < HANDOFF_MAX_BYTES  # the split "é" went too
    assert kept.startswith("x" + "é" * 100)  # cut on a character boundary, nothing mangled
    # Counted against what was kept, not against the cap: the character the cut
    # landed inside is dropped as well, so this is one more than the cap arithmetic.
    dropped = len(long_body.encode("utf-8")) - len(kept.encode("utf-8"))
    assert dropped == len(long_body.encode("utf-8")) - HANDOFF_MAX_BYTES + 1
    assert tail == (
        f"[… {dropped} more bytes — `quorum task show {chatty.short_id}` prints the "
        "whole handoff]"
    )
    # the cap is per dependency: the terse upstream's handoff is untouched
    assert f"## Handoff from {terse.short_id}\n\none line, kept whole" in note
    # and a body under the cap is returned as is (trailing whitespace aside)
    assert clip_handoff("short\n", "abc123") == "short"
    # the stored file is never clipped — only the rendering is
    assert tasks.read_handoff(home, chatty.id) == long_body


def test_the_preamble_tells_a_task_how_to_leave_a_handoff(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "do it", "fake")
    assert run_task(home, config, task.id) == 0
    text = transcript_text(home, task.id)
    assert f"quorum task show {task.short_id}" in text
    assert "`dependents:` line" in text
    # A file, not stdin: an inject-mode harness shares its stdin with the
    # runner's guidance pump, so `--handoff -` from inside a run would block.
    assert f"quorum task report {task.short_id} --status done --handoff <file>" in text
    assert "--handoff <file|->" not in text
