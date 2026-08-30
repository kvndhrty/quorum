"""The task runner: one harness run, from lock to exit.

A run is the unit of control for a generic harness. The runner:

1. takes the task's `runner.lock` (one run at a time, pid = liveness),
2. prepares the working directory (a git worktree under
   `QUORUM_HOME/worktrees/<id>` by default, so parallel tasks on one repo
   never collide and the main checkout stays clean),
3. claims any guidance waiting in the task's inbox and injects it — this is
   how the manager's pokes and the user's steering reach the harness,
4. composes the prompt (preamble teaching the report/inbox protocol + the
   task prompt + guidance) and spawns the configured harness argv,
5. streams stdout into `transcript.jsonl` line by line, capturing a
   `session_id` (or codex `thread_id`) if the harness emits one (enables
   `resume` templates),
6. optionally (`[tasks].auto_commit`) commits anything the harness left
   uncommitted in its worktree — a mechanical safety net, since branches
   outlive worktrees,
7. records the run's exit in task.json and releases the lock.

A harness with `inject = "stream-json"` speaks over stdin instead of argv: a
stream-json CLI reads user turns only from stdin (claude ignores an argv
prompt entirely in this mode), so the runner delivers the composed prompt as
the first user turn, and a `GuidancePump` forwards inbox messages as further
turns *during* the run, closing stdin at the first idle turn boundary so the
run still ends on its own.

The runner never sets a task's status: status is whatever the harness last
reported via `quorum task report`. A run that exits without reporting is the
manager's cue to poke and resume.

Runs execute as their own processes (`quorum task run --detach` uses
start_new_session), not supervisor threads, so tasks survive supervisor
restarts and `quorum up` ticks stay short.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from . import fsio, prompts
from .actor import strip_actor_env
from .config import Config, HarnessConfig
from .messages import Message, MessageBus
from .projects import ProjectRegistry
from .tasks import (
    Task,
    TaskRun,
    TaskStore,
    inbox_name,
    runner_lock_path,
    runner_log_path,
    transcript_path,
    worktree_path,
)


class RunnerError(RuntimeError):
    """A run could not start; the message is fit to show a CLI user."""


# How often the guidance pump checks the inbox during a live run. Read at
# wait time so tests can shrink it; nudges are human-paced, so seconds are fine.
GUIDANCE_POLL_SECONDS = 2.0


class GuidancePump:
    """Speaks stream-json stdin to a live harness: the run's prompt first,
    then inbox messages as they arrive.

    For a harness with `inject = "stream-json"` the runner spawns it with a
    pipe on stdin and holds the pipe open; a background thread writes the
    run's prompt as the opening stream-json user turn (`{"type": "user",
    "message": {...}}`), then polls the given inbox and writes each claimed
    message as a further turn, which the harness queues and picks up at its
    next turn boundary. This is the Claude Code `--input-format stream-json`
    protocol; the harness's argv template must include the matching flags.
    Stdin is the *only* prompt channel — a stream-json CLI ignores an argv
    prompt, so `build_harness_argv` strips `{prompt}` for inject harnesses.

    A stream-json harness runs until stdin closes, so ending the run is the
    pump's job too. The protocol emits one `result` event per completed user
    turn (the prompt turn is the first). The pump counts results against
    deliveries and closes stdin once every delivered turn has its result and
    nothing is waiting in the inbox — so a run naturally extends while
    guidance keeps arriving and ends at the first idle turn boundary.
    Anything arriving after close stays in `new/` for the next run.

    The lock guards only the counters and the closed flag; inbox and stdin
    I/O runs outside it (there is one delivering thread, so the prompt turn
    always precedes guidance), and a `result` event on the transcript thread
    never waits on filesystem work. A claim is counted *before* its write so
    the close condition can't fire while a message is in flight.
    """

    def __init__(self, home: Path, inbox: str, stdin, prompt: str):
        self._bus = MessageBus(home)
        self._inbox = inbox
        self._stdin = stdin
        self._prompt = prompt
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._delivered = 0
        self._results = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def on_event(self, event: object) -> None:
        """Called for every parsed stdout event; watches for turn boundaries."""
        if not (isinstance(event, dict) and event.get("type") == "result"):
            return
        with self._lock:
            self._results += 1
            self._maybe_close_locked()

    def stop(self) -> None:
        """The run is over (or being torn down): stop polling, close stdin."""
        self._stop.set()
        self._thread.join(timeout=5)
        self._close()

    def _loop(self) -> None:
        if not self._write_turn(self._prompt):
            return  # harness died before its prompt; the run fails at exit
        while not self._stop.is_set():
            self._deliver_pending()
            self._stop.wait(GUIDANCE_POLL_SECONDS)

    def _write_turn(self, text: str) -> bool:
        turn = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
        try:
            self._stdin.write(json.dumps(turn) + "\n")
            self._stdin.flush()
        except (OSError, ValueError):
            self._close()
            return False
        return True

    def _deliver_pending(self) -> None:
        for claimed in self._bus.claim(self._inbox):
            with self._lock:
                if self._closed:
                    claimed.reject()
                    return
                self._delivered += 1
            if not self._write_turn(guidance_note(claimed.message)):
                with self._lock:
                    self._delivered -= 1
                claimed.reject()  # harness is gone; back to new/ for the next run
                return
            claimed.ack()

    def _maybe_close_locked(self) -> None:
        if self._closed:
            return
        answered = self._results >= 1 + self._delivered
        if answered and not self._bus.pending(self._inbox):
            self._close_locked()

    def _close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._stdin.close()
            except OSError:
                pass
        self._stop.set()


@contextmanager
def guidance_pump(
    home: Path, inbox: str, harness: HarnessConfig, proc: subprocess.Popen, prompt: str
):
    """Attach a GuidancePump to a live harness run when its config opts in.

    The one seam for inject-mode lifecycle: yields the started pump (or None
    for a harness without `inject`, whose prompt travels via argv instead)
    and stops it on exit. Callers pipe the process's stdin iff
    `harness.inject` is set; the pump then owns delivering `prompt` as the
    opening user turn.
    """
    if not harness.inject:
        yield None
        return
    pump = GuidancePump(home, inbox, proc.stdin, prompt)
    pump.start()
    try:
        yield pump
    finally:
        pump.stop()


def resolve_harness(config: Config, name: str) -> HarnessConfig:
    harness = config.harness.get(name)
    if harness is None:
        known = ", ".join(sorted(config.harness)) or "none configured"
        raise RunnerError(
            f"no [harness.{name}] in config.toml (known: {known}) — see docs/guide.md"
        )
    return harness


def prepare_workdir(home: Path, task: Task, store: TaskStore) -> Path:
    """Resolve (and on first run create) the directory the harness runs in."""
    if task.workdir:
        workdir = Path(task.workdir)
        if workdir.is_dir():
            return workdir
    project = ProjectRegistry(home).get(task.project)
    if project is None:
        raise RunnerError(f"task {task.short_id} references unknown project {task.project!r}")
    if not project.dir.is_dir():
        raise RunnerError(f"project directory {project.dir} does not exist")
    if not task.use_worktree:
        store.update(task.id, workdir=str(project.dir))
        return project.dir
    if not (project.dir / ".git").exists():
        raise RunnerError(
            f"{project.dir} is not a git repository — re-create the task with --no-worktree"
        )
    workdir = worktree_path(home, task.id)
    if not workdir.exists():
        branch = f"quorum/{task.short_id}"
        add = _git(project.dir, "worktree", "add", str(workdir), "-b", branch)
        if add.returncode != 0:
            # branch may survive a deleted worktree; reattach instead of -b
            add = _git(project.dir, "worktree", "add", str(workdir), branch)
        if add.returncode != 0:
            raise RunnerError(
                f"git worktree add failed for {project.dir}: {add.stderr.strip()[:300]}"
            )
    store.update(task.id, workdir=str(workdir))
    return workdir


def guidance_note(msg: Message) -> str:
    """One inbox message rendered as a line of guidance for the harness."""
    return f"[from {msg.sender} at {msg.created_at}] {msg.payload.get('text', '')}"


def claim_guidance(home: Path, task_id: str) -> list[str]:
    """Drain the task's inbox; each message becomes a line for the prompt."""
    notes = []
    for claimed in MessageBus(home).claim(inbox_name(task_id)):
        notes.append(guidance_note(claimed.message))
        claimed.ack()
    return notes


def compose_prompt(home: Path, task: Task, workdir: Path, guidance: list[str]) -> str:
    parts = [
        prompts.render(home, "task-preamble", task_id=task.short_id, project_path=str(workdir)),
        f"# Task\n\n{task.prompt}",
    ]
    if guidance:
        parts.append("# Guidance received since your last run\n\n" + "\n".join(f"- {g}" for g in guidance))
    return "\n\n".join(parts)


def build_harness_argv(harness: HarnessConfig, prompt: str, session: str | None = None) -> list[str]:
    """Substitute {prompt}/{session} into the start or resume template.

    A template with no "{prompt}" gets the prompt appended as the final
    argument — except for an inject harness: a stream-json CLI reads user
    turns only from stdin and ignores an argv prompt, so the prompt travels
    through the `GuidancePump` instead and "{prompt}" elements are dropped
    (`claude -p "{prompt}" ...` becomes `claude -p ...`).
    """
    template = harness.resume if (session and harness.resume) else harness.start
    if harness.inject:
        template = [e for e in template if e != "{prompt}"]
        return [e.replace("{prompt}", "").replace("{session}", session or "") for e in template]
    argv, saw_prompt = [], False
    for element in template:
        if "{prompt}" in element:
            saw_prompt = True
        argv.append(element.replace("{prompt}", prompt).replace("{session}", session or ""))
    if not saw_prompt:
        argv.append(prompt)
    return argv


AUTO_COMMIT_MESSAGE = "quorum: auto-commit uncommitted work after run"


def auto_commit_workdir(workdir: Path) -> str:
    """Commit everything a run left uncommitted in `workdir`.

    Returns a one-line note for the transcript, empty when the tree was
    already clean. Raises `RunnerError` when git refuses at any step (no
    identity configured, a stale index lock) — the caller turns that into a
    note too, because a failed safety net must never cost the run its record.

    Staging is `git add -A`, so untracked files count: a harness that crashed
    mid-edit usually leaves new files, and those are exactly the work worth
    saving. Committing is the whole net — pushing is deliberately out of
    scope (it would assume a remote and credentials, and an unpushed branch
    is already surfaced as stranded work by `tasks.workdir_git_state`).
    """
    status = _git(workdir, "status", "--porcelain")
    if status.returncode != 0:
        raise RunnerError(f"git status failed: {status.stderr.strip()[:200]}")
    paths = [line for line in status.stdout.splitlines() if line.strip()]
    if not paths:
        return ""
    add = _git(workdir, "add", "-A")
    if add.returncode != 0:
        raise RunnerError(f"git add -A failed: {add.stderr.strip()[:200]}")
    commit = _git(workdir, "commit", "-m", AUTO_COMMIT_MESSAGE)
    if commit.returncode != 0:
        detail = (commit.stderr.strip() or commit.stdout.strip())[:200]
        raise RunnerError(f"git commit failed: {detail}")
    head = _git(workdir, "rev-parse", "--short", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else "?"
    return f"auto-committed {len(paths)} path(s) as {sha}"


def _auto_commit_after_run(home: Path, task: Task, workdir: Path) -> None:
    """Run the safety net and record what happened in the transcript.

    Mechanical, not a judgement: the runner still never sets status, and a
    tree the harness already committed is left alone. Failures are recorded
    rather than raised — the tree simply stays dirty, which is the state
    `workdir_git_state` reports as STRANDED-WORK for the manager to chase.
    """
    try:
        note = auto_commit_workdir(workdir)
    except (RunnerError, OSError, subprocess.SubprocessError) as e:
        note = f"auto-commit failed: {e}"
    if not note:
        return
    fsio.append_jsonl(
        transcript_path(home, task.id),
        {"at": fsio.iso(fsio.utc_now()), "line": f"quorum: {note}"},
    )


def _should_auto_commit(config: Config, task: Task, home: Path, workdir: Path) -> bool:
    """Opt-in, and only ever inside a task's own worktree.

    A `--no-worktree` task runs in the user's checkout on whatever branch
    they had out; committing there would be quorum writing to a tree it does
    not own. Comparing against `worktree_path` (rather than trusting
    `use_worktree`) keeps that true even for a task whose recorded workdir
    predates the flag.
    """
    return (
        config.tasks.auto_commit
        and task.use_worktree
        and workdir == worktree_path(home, task.id)
    )


def stream_transcript(
    proc: subprocess.Popen,
    transcript: Path,
    *,
    extra: dict | None = None,
    on_event=None,
    now=fsio.utc_now,
) -> None:
    """Stream a harness process's stdout into a transcript.jsonl, line by line.

    Each non-empty line becomes one entry: `{"at": ..., **extra}` plus either
    `event` (parsed JSON, also passed to `on_event`) or `line` (raw text).
    Both the task runner and the manager write transcripts through here, so
    every reader (`read_transcript_tail`, the digest, `task tail`) sees one
    entry shape.
    """
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        entry: dict = {"at": fsio.iso(now()), **(extra or {})}
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            entry["line"] = line
        else:
            entry["event"] = event
            if on_event is not None:
                on_event(event)
        fsio.append_jsonl(transcript, entry)


def run_task(home: Path, config: Config, task_prefix: str) -> int:
    """Execute one run of a task in the foreground. Returns the harness exit code."""
    home = Path(home)
    store = TaskStore(home)
    try:
        task = store.resolve(task_prefix)
    except KeyError:
        raise RunnerError(f"no task matching {task_prefix!r} — `quorum task list`") from None
    except ValueError as e:
        raise RunnerError(str(e)) from None
    if task.attached:
        # A substrate rail, not supervision policy (same class as the runner
        # lock): the workdir is the user's live checkout with an interactive
        # session in it, and a headless run there would race the human.
        raise RunnerError(
            f"task {task.short_id} is attached to a live interactive session — "
            "guide it with `quorum task nudge`, or `quorum task detach` it first"
        )
    harness = resolve_harness(config, task.harness)

    lock = runner_lock_path(home, task.id)
    try:
        fsio.acquire_pid_lock(lock, meta={"role": "task-runner", "task": task.id})
    except fsio.LockError as e:
        raise RunnerError(f"task {task.short_id} already has a live run ({e})") from None
    try:
        workdir = prepare_workdir(home, task, store)
        if config.sandbox.use_nono:
            # Irreversibly sandbox this runner (and the harness it spawns).
            # Fails closed: no nono-py, no run.
            from .sandbox import apply_task_sandbox

            apply_task_sandbox(home, config, task, workdir)
        guidance = claim_guidance(home, task.id)
        prompt = compose_prompt(home, task, workdir, guidance)
        argv = build_harness_argv(harness, prompt, task.session)

        # The task harness acts as itself, not as whoever launched it.
        env = strip_actor_env({**os.environ, **harness.env, "QUORUM_HOME": str(home)})
        started = fsio.utc_now()
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            stdin=subprocess.PIPE if harness.inject else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        session = task.session

        with guidance_pump(home, inbox_name(task.id), harness, proc, prompt) as pump:

            def on_event(event: object) -> None:
                nonlocal session
                if session is None and isinstance(event, dict):
                    found = _find_session_id(event)
                    if found:
                        session = found
                        store.update(task.id, session=found)
                if pump is not None:
                    pump.on_event(event)

            stream_transcript(proc, transcript_path(home, task.id), on_event=on_event)
            exit_code = proc.wait()

        if _should_auto_commit(config, task, home, workdir):
            _auto_commit_after_run(home, task, workdir)

        run = TaskRun(
            started_at=fsio.iso(started), ended_at=fsio.iso(fsio.utc_now()), exit_code=exit_code
        )
        fresh = store.get(task.id)  # status may have moved via `task report` mid-run
        prior = [r.model_dump() for r in (fresh.runs if fresh else task.runs)]
        store.update(task.id, runs=[*prior, run.model_dump()])
        return exit_code
    finally:
        fsio.release_pid_lock(lock)


def launch_detached(home: Path, task_id: str) -> int:
    """Start `quorum task run <id>` as a detached process; returns its pid.

    stdout/stderr go to the task's runner.log (the transcript captures the
    harness's own output separately, inside the run).
    """
    home = Path(home)
    log_path = runner_log_path(home, task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The detached child re-invokes `quorum task run`; that inner invocation
    # is infrastructure, not a second manager action.
    env = strip_actor_env({**os.environ, "QUORUM_HOME": str(home)})
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "quorum", "task", "run", task_id, "--home", str(home)],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    return proc.pid


def _find_session_id(event: dict) -> str | None:
    # claude emits session_id; codex `exec --json` calls it thread_id
    # (first event: {"type": "thread.started", "thread_id": ...}).
    for key in ("session_id", "sessionId", "thread_id", "threadId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
