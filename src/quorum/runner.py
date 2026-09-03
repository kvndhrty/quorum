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
7. records the run's exit (and reported usage) in task.json and releases
   the lock.

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

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import fsio, prompts, usage
from .actor import strip_actor_env
from .config import Config, HarnessConfig, TasksConfig
from .messages import Message, MessageBus
from .projects import ProjectRegistry
from .tasks import (
    TERMINAL_STATUSES,
    Task,
    TaskRun,
    TaskStore,
    dependency_state,
    inbox_name,
    runner_lock_path,
    runner_log_path,
    short_handle,
    transcript_path,
    worktree_path,
)

# How long `stop_run` (and the stall watchdog) waits for a SIGTERMed process
# to go away before escalating to SIGKILL. Seconds, because a harness that
# means to exit cleanly flushes and dies in well under one.
STOP_GRACE_SECONDS = 5.0


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

    The lock guards the counters, the closed flag, and — the one piece of
    filesystem work under it — the *claim* of each inbox message (the
    rename out of `new/`), which is counted as delivered in the same
    critical section. That pairing is what makes the close condition
    sound: a `result` arriving on the transcript thread either still sees
    the message pending in `new/` (so it does not close) or sees it already
    counted (so the turn is still owed). Claiming outside the lock and
    counting after left a gap in which a result closed stdin with a nudge in
    flight — a real CI flake, one result event instead of two. Stdin writes
    stay outside the lock (there is one delivering thread, so the prompt
    turn always precedes guidance).
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
        claims = self._bus.claim(self._inbox)
        while True:
            with self._lock:
                if self._closed:
                    return  # unclaimed messages stay in new/ for the next run
                # the rename happens inside next(); counting it here, under
                # the same lock a result's close check takes, is the point
                claimed = next(claims, None)
                if claimed is None:
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


def note_transcript(path: Path, text: str) -> None:
    """Write one `quorum:` line into a task's transcript.

    Everything quorum itself does to a run — an auto-commit, a stall, a
    stop — says so in the same stream the harness writes to, because that is
    where every reader (the digest's `out|` tail, `task tail`, the TUI) is
    already looking. Never raises: a note that cannot be written must not
    cost the run its record.
    """
    with contextlib.suppress(OSError):
        fsio.append_jsonl(path, {"at": fsio.iso(fsio.utc_now()), "line": f"quorum: {text}"})


class StallWatchdog:
    """The mechanical half of hung-session recovery: no output for N seconds
    ends the run.

    Off unless `[tasks].run_stall_timeout_seconds` is set. A harness that
    hangs — blocked forever on stdin (#24), waiting on an API call that will
    never answer, plain stuck — keeps its process alive and its lock live, so
    passive observation reads it as a healthy run and the manager must judge
    it. This turns that into the case supervision already handles well: a
    **dead runner with a non-terminal status**, which is simply relaunched.

    It counts *silence*, not progress: any line the harness prints resets the
    clock, so the threshold has to sit above the longest silent step a real
    run takes. That is why it is off by default and why the runner never
    picks a value for the user.

    On firing it notes the stall in the transcript, SIGTERMs the harness,
    and SIGKILLs it after `STOP_GRACE_SECONDS`. It does not set status (no
    part of the runner does) and it does not kill the runner itself: the run
    ends the ordinary way, so the run record, auto-commit and lock release
    all still happen — with `stalled = true` on the record.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        timeout: float,
        transcript: Path,
        *,
        grace: float = STOP_GRACE_SECONDS,
        monotonic=time.monotonic,
    ):
        self._proc = proc
        self._timeout = timeout
        self._transcript = transcript
        self._grace = grace
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last = monotonic()
        self._stalled = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @property
    def stalled(self) -> bool:
        return self._stalled

    def start(self) -> None:
        self._thread.start()

    def saw_output(self) -> None:
        """One line arrived: the harness is alive and talking."""
        with self._lock:
            self._last = self._monotonic()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._grace + 5)

    def _loop(self) -> None:
        # Poll at a fraction of the timeout: the check is two cheap reads, and
        # a coarse poll would make the effective threshold up to 2x the
        # configured one.
        poll = max(0.05, min(1.0, self._timeout / 4))
        while not self._stop.wait(poll):
            if self._proc.poll() is not None:
                return  # the run ended on its own
            with self._lock:
                quiet = self._monotonic() - self._last
            if quiet >= self._timeout:
                self._fire(quiet)
                return

    def _fire(self, quiet: float) -> None:
        self._stalled = True
        note_transcript(
            self._transcript,
            f"run stalled — no harness output for {int(quiet)}s "
            f"(>= [tasks].run_stall_timeout_seconds = {self._timeout:g}); stopping the harness",
        )
        # Only the harness is signalled, never the group: the runner shares
        # that group (`launch_detached` makes the runner, not the harness,
        # the leader) and killpg would take the runner down with it. The
        # limitation that leaves: a *grandchild* that inherited the harness's
        # stdout and outlived it keeps the pipe open, so the run's
        # `stream_transcript` stays blocked and the watchdog's kill does not
        # by itself end the run. There is no cheap fix from here — closing
        # the read end under a thread already blocked in read() does not
        # reliably wake it — so the escape hatch is the group-wide one:
        # `quorum task stop` (see docs/architecture.md).
        with contextlib.suppress(OSError):
            self._proc.terminate()
        deadline = self._monotonic() + self._grace
        while self._monotonic() < deadline:
            if self._proc.poll() is not None:
                return
            time.sleep(0.05)
        with contextlib.suppress(OSError):
            self._proc.kill()


@contextmanager
def stall_watchdog(proc: subprocess.Popen, timeout: float, transcript: Path):
    """Attach a StallWatchdog to a live run when the config opts in."""
    if not timeout or timeout <= 0:
        yield None
        return
    watchdog = StallWatchdog(proc, timeout, transcript)
    watchdog.start()
    try:
        yield watchdog
    finally:
        watchdog.stop()


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


def dependency_note(home: Path, task: Task) -> str | None:
    """What this task's dependencies ended up as, or None when it has none.

    The cheapest sufficient answer to "how does a dependent task read its
    upstream's outcome": the fields are already in task.json, so composing
    the prompt costs one read per dependency and no new state. Anything
    deeper — the full record, reports, transcript — is a
    `quorum task show <id>` away, which the note says out loud.
    """
    if not task.depends_on:
        return None
    store = TaskStore(home)
    lines = []
    for dep_id in task.depends_on:
        dep = store.get(dep_id)
        if dep is None:
            lines.append(f"- {short_handle(dep_id)}: no record (the task was deleted)")
            continue
        line = f"- {dep.short_id}: status={dep.status}"
        if dep.pr_url:
            line += f" pr={dep.pr_url}"
        first = dep.prompt.strip().splitlines()[0] if dep.prompt.strip() else ""
        if first:
            line += f"\n  it was asked to: {first[:160]}"
        lines.append(line)
    return (
        "# Tasks this one depends on\n\n"
        + "\n".join(lines)
        + "\n\nRead the full record of any of them — reports, PR url, branch — with "
        "`quorum task show <id>`."
    )


_PERPETUAL_SLOT = re.compile(r"(?<!\{)\{perpetual\}(?!\})")


def compose_prompt(home: Path, task: Task, workdir: Path, guidance: list[str]) -> str:
    # A perpetual task gets an extra block in place of the preamble's
    # {perpetual} placeholder: it never reaches "done", so its delivery step
    # is commit + push every cycle (prompts/task-perpetual.md, user-editable
    # like every other template). An ordinary task substitutes nothing.
    perpetual = (
        prompts.render(home, "task-perpetual", task_id=task.short_id).strip()
        if task.perpetual
        else ""
    )
    preamble = prompts.render(
        home,
        "task-preamble",
        task_id=task.short_id,
        project_path=str(workdir),
        perpetual=perpetual,
    )
    # An edited preamble from before the placeholder existed never
    # substitutes it (format_map preserves unknown keys but cannot invent
    # one), and a perpetual task that silently gets the ordinary "report
    # done" instructions ends on its first cycle. Append the block instead.
    # (The header documents the key as an escaped `{{perpetual}}`, so look
    # for an *unescaped* placeholder, not the substring.)
    if perpetual and not _PERPETUAL_SLOT.search(prompts.load(home, "task-preamble")):
        preamble = f"{preamble.rstrip()}\n\n{perpetual}"
    parts = [
        re.sub(r"\n{3,}", "\n\n", preamble).strip(),
        f"# Task\n\n{task.prompt}",
    ]
    upstream = dependency_note(home, task)
    if upstream:
        parts.append(upstream)
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


def _merge_or_rebase_in_progress(workdir: Path) -> bool:
    git_dir = _git(workdir, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        return False  # the status call right after fails loudly instead
    gd = Path(git_dir.stdout.strip())
    return any(
        (gd / marker).exists()
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")
    )


def auto_commit_workdir(workdir: Path) -> str:
    """Commit everything a run left uncommitted in `workdir`.

    Returns a one-line note for the transcript, empty when the tree was
    already clean. Raises `RunnerError` when git refuses at any step (no
    identity configured, a stale index lock) — the caller turns that into a
    note too, because a failed safety net must never cost the run its record.

    Staging is `git add -A`, so untracked files count: a harness that crashed
    mid-edit usually leaves new files, and those are exactly the work worth
    saving (`--untracked-files=all` because a repo-level
    `status.showUntrackedFiles no` would otherwise hide them — an
    untracked-only crash is the net's core case). The commit runs with
    `--no-verify` and signing off: hooks and pinentry belong to attended
    commits, and here a failing hook or a signing prompt would defeat the
    net in exactly the crashed-harness case it exists for.

    Two states it refuses to touch, raising instead (the tree stays dirty,
    which `workdir_git_state` keeps reporting as stranded work): a detached
    HEAD, where a commit would be reachable from no branch and lost with the
    worktree; and an in-progress merge/rebase/cherry-pick, which `git add
    -A` + commit would *conclude*, conflict markers and all.

    Committing is the whole net — pushing is deliberately out of scope (it
    would assume a remote and credentials, and an unpushed branch is
    already surfaced as stranded work by `tasks.workdir_git_state`).
    """
    status = _git(workdir, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise RunnerError(f"git status failed: {status.stderr.strip()[:200]}")
    if not any(line.strip() for line in status.stdout.splitlines()):
        return ""
    branch = _git(workdir, "symbolic-ref", "-q", "--short", "HEAD")
    if branch.returncode != 0:
        raise RunnerError("HEAD is detached — a commit here would belong to no branch")
    if _merge_or_rebase_in_progress(workdir):
        raise RunnerError(
            "a merge/rebase/cherry-pick is in progress — committing would conclude it"
        )
    add = _git(workdir, "add", "-A")
    if add.returncode != 0:
        raise RunnerError(f"git add -A failed: {add.stderr.strip()[:200]}")
    staged = _git(workdir, "diff", "--cached", "--name-only")
    count = sum(1 for line in staged.stdout.splitlines() if line.strip())
    if staged.returncode != 0 or count == 0:
        return ""
    commit = _git(
        workdir, "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", AUTO_COMMIT_MESSAGE
    )
    if commit.returncode != 0:
        detail = (commit.stderr.strip() or commit.stdout.strip())[:200]
        raise RunnerError(f"git commit failed: {detail}")
    head = _git(workdir, "rev-parse", "--short", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else "?"
    return f"auto-committed {count} path(s) as {sha}"


def _maybe_auto_commit(
    home: Path, config: Config, store: TaskStore, task: Task, workdir: Path
) -> str | None:
    """Run the opt-in safety net when this run's tree is quorum's to commit.

    Guards, in order: the flag; the workdir must be the task's own worktree,
    compared `resolve()`d on both sides — the default `~/.quorum` home is
    not resolved while `--home` is, and a symlinked spelling must not
    silently disable the net (a `--no-worktree` task runs in the user's
    checkout, which quorum does not own); a task whose harness already
    reported a terminal status keeps its tree exactly as the harness left
    it — sweeping stray scratch files into a *finished* task's branch would
    re-flag it as stranded and push junk toward its PR; and a nono-sandboxed
    runner cannot run git at all (the capability set blocks it), so it says
    that once per run instead of failing cryptically.

    Mechanical, not a judgement: the runner still never sets status.
    Failures are recorded rather than raised — the tree simply stays dirty,
    which is the state `workdir_git_state` reports as STRANDED-WORK for the
    manager to chase. Returns the note, which the caller also records
    durably on the `TaskRun`; None when the net did not fire.
    """
    if not config.tasks.auto_commit:
        return None
    try:
        owned = workdir.resolve() == worktree_path(home, task.id).resolve()
    except OSError:
        owned = False
    if not owned:
        return None
    fresh = store.get(task.id)
    reported = ((fresh.status if fresh else task.status) or "").strip().lower()
    if reported in TERMINAL_STATUSES:
        return None
    if config.sandbox.use_nono:
        note = (
            "auto-commit skipped: the sandboxed runner cannot run git "
            "(docs/guide.md#sandboxing) — uncommitted work stays visible as stranded"
        )
    else:
        try:
            note = auto_commit_workdir(workdir)
        except (RunnerError, OSError, subprocess.SubprocessError) as e:
            note = f"auto-commit failed: {e}"
    if not note:
        return None
    note_transcript(transcript_path(home, task.id), note)
    return note


def stream_transcript(
    proc: subprocess.Popen,
    transcript: Path,
    *,
    extra: dict | None = None,
    on_event=None,
    on_line=None,
    now=fsio.utc_now,
) -> None:
    """Stream a harness process's stdout into a transcript.jsonl, line by line.

    Each non-empty line becomes one entry: `{"at": ..., **extra}` plus either
    `event` (parsed JSON, also passed to `on_event`) or `line` (raw text).
    Both the task runner and the manager write transcripts through here, so
    every reader (`read_transcript_tail`, the digest, `task tail`) sees one
    entry shape.

    `on_line` is called for every line, parsed or not — it is the "the
    harness said something" signal the stall watchdog counts, and silence is
    exactly what a hung harness produces.
    """
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if on_line is not None:
            on_line()
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


def run_task(
    home: Path,
    config: Config,
    task_prefix: str,
    force: bool = False,
    fresh_session: bool = False,
) -> int:
    """Execute one run of a task in the foreground. Returns the harness exit code.

    `force` waives the hold, dependency and budget refusals below; nothing
    else about a run changes.

    `fresh_session` drops the session id captured from earlier runs before
    composing the argv, so the harness starts a *new* session in the same
    worktree instead of resuming a damaged one (a thread that errors on
    every turn, a context the provider will not accept again). The work on
    disk is untouched — the worktree, not the session, is the durable state
    — but the new session remembers nothing, so a caller that has context
    worth carrying over should nudge it in. The run records
    `fresh_session = true`, which is how the digest counts restarts.
    """
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
    if task.held and not force:
        # A substrate rail of the same narrow class: the user parked this
        # task by hand (`quorum task hold`), so a launch is work they
        # explicitly said they did not want yet. Hold is not a status — the
        # task keeps whatever the harness last reported — and quorum never
        # releases it; only a human does.
        raise RunnerError(held_refusal(task))
    blockers = unmet_dependencies(store, task) if not force else []
    if blockers:
        # The second substrate rail, same class as the attached refusal: a
        # dependent task launched before its prerequisite is pure waste (it
        # reviews a PR that does not exist yet). The manager still decides
        # every launch — this only refuses the launch it already knows is
        # premature, and `--force` waives it.
        raise RunnerError(
            f"task {task.short_id} is waiting on {', '.join(blockers)} — "
            "unfinished dependencies; `--force` to run anyway"
        )
    over = budget_blockers(config.tasks, task) if not force else []
    if over:
        # The third substrate rail, and the second of the rate-limit class
        # the per-run action cap belongs to: a task whose *last* run went
        # past the configured budget is not relaunched until someone says
        # so. It gates the next run only — never a mid-run kill, never a
        # veto of any particular choice — and the manager (or the user)
        # decides what the task deserves instead: a sharper nudge, a
        # decomposition, an escalation, or `--force`.
        raise RunnerError(budget_refusal(task, over))
    harness = resolve_harness(config, task.harness)

    lock = runner_lock_path(home, task.id)
    try:
        # `fresh_session` rides the lock so that a `task stop` closing this
        # run's record for it can say what kind of run it killed — otherwise
        # a stopped fresh restart reads as an ordinary one and the digest's
        # `fresh_sessions=` count never moves.
        fsio.acquire_pid_lock(
            lock,
            meta={"role": "task-runner", "task": task.id, "fresh_session": fresh_session},
        )
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
        if fresh_session and task.session:
            # Forget the damaged session before it can be resumed. Durable,
            # not just local: the *next* run must not resume it either, and
            # the harness is about to hand us a new id anyway.
            store.update(task.id, session=None)
            task = task.model_copy(update={"session": None})
            note_transcript(
                transcript_path(home, task.id),
                "starting a fresh session (--fresh-session) — the worktree is unchanged, "
                "but this session remembers nothing of the previous ones",
            )
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
            # One stray non-UTF-8 byte must not abort transcript streaming —
            # an unhandled decode error here would skip the run record too.
            errors="replace",
            bufsize=1,
            env=env,
        )
        session = task.session
        spend = usage.UsageCollector()

        with (
            guidance_pump(home, inbox_name(task.id), harness, proc, prompt) as pump,
            stall_watchdog(
                proc, config.tasks.run_stall_timeout_seconds, transcript_path(home, task.id)
            ) as watchdog,
        ):

            def on_event(event: object) -> None:
                nonlocal session
                if session is None and isinstance(event, dict):
                    found = _find_session_id(event)
                    if found:
                        session = found
                        store.update(task.id, session=found)
                spend.add(event)
                if pump is not None:
                    pump.on_event(event)

            stream_transcript(
                proc,
                transcript_path(home, task.id),
                on_event=on_event,
                on_line=watchdog.saw_output if watchdog is not None else None,
            )
            exit_code = proc.wait()
            stalled = watchdog is not None and watchdog.stalled

        auto_commit_note = _maybe_auto_commit(home, config, store, task, workdir)

        run = TaskRun(
            started_at=fsio.iso(started),
            ended_at=fsio.iso(fsio.utc_now()),
            exit_code=exit_code,
            auto_commit=auto_commit_note,
            usage=spend.result(),
            stalled=stalled,
            fresh_session=fresh_session,
        )
        fresh = store.get(task.id)  # status may have moved via `task report` mid-run
        prior = [r.model_dump() for r in (fresh.runs if fresh else task.runs)]
        store.update(task.id, runs=[*prior, run.model_dump()])
        return exit_code
    finally:
        fsio.release_pid_lock(lock)


def stop_run(
    home: Path,
    task_prefix: str,
    *,
    grace_seconds: float = STOP_GRACE_SECONDS,
    now=fsio.utc_now,
) -> dict:
    """End a task's live run without ending the task. Returns what it did.

    The non-destructive half of `task cancel --kill`: that one marks the task
    `cancelled` and loses the work along with its queue position, which is
    the wrong tool for a hung session. This kills the run and leaves
    *everything else alone* — status untouched (the runner never sets one,
    and neither does this), worktree untouched, the task queued exactly where
    it was and ready to be relaunched, with `--fresh-session` when the
    session itself is the problem.

    The signal goes to the runner's **process group**: a detached run is a
    session leader (`launch_detached` uses start_new_session) and the harness
    and everything it spawned inherit that group, so the group is the only
    handle that reaches the whole tree. SIGTERM first, then SIGKILL after
    `grace_seconds` for a harness that ignores it. A foreground run sharing
    our own process group is signalled by pid instead — killing our group
    would kill the caller.

    The killed runner never gets to write its own record, so this writes it:
    a `run.stopped` transcript note and a `TaskRun` with `stopped = true`,
    the signal as the exit code and the killed run's `fresh_session` (read
    back off the lock the runner wrote), so views and the digest say a run
    ended here rather than showing one that never closed — and so a stopped
    fresh restart still counts as one. If the runner did manage to record
    the run itself (a harness that exits cleanly on SIGTERM), that record
    stands and nothing is duplicated. The stale lock is cleared too, since
    its pid is now provably gone.

    A lock whose runner is *already* dead — a crashed run, or a zombie its
    parent never reaped — gets the same tidying without a signal: only a
    task with no lock at all has "no live run to stop".

    Refuses an attached task: the same substrate rail as the runner's, and
    the sharpest one here — the "runner" of an attached task is the user's
    own interactive session, which quorum never kills.
    """
    home = Path(home)
    store = TaskStore(home)
    try:
        task = store.resolve(task_prefix)
    except KeyError:
        raise RunnerError(f"no task matching {task_prefix!r} — `quorum task list`") from None
    except ValueError as e:
        raise RunnerError(str(e)) from None
    if task.attached:
        raise RunnerError(
            f"task {task.short_id} is attached to a live interactive session — "
            "quorum never kills your session; end it yourself, or `quorum task detach` it"
        )
    lock = runner_lock_path(home, task.id)
    try:
        meta = fsio.read_json(lock)
        pid = int(meta.get("pid", -1))
    except (OSError, ValueError):
        meta, pid = {}, -1
    if pid <= 0:
        raise RunnerError(
            f"task {task.short_id} has no live run to stop "
            "(`quorum task run` to start one)"
        )
    runs_before = len(task.runs)
    if fsio.pid_alive(pid):
        sent, alive = _terminate_process_group(pid, grace_seconds)
        if alive:
            raise RunnerError(
                f"the run of task {task.short_id} (runner pid {pid}) survived SIGKILL — "
                "something in it is stuck in the kernel; check the processes by hand"
            )
        ended = f"ended with {sent.name} by `quorum task stop`"
    else:
        # The lock outlived its runner: the process died (or is a zombie its
        # parent has not reaped) without closing its own run. There is
        # nothing to kill, but the tidying below is exactly what this
        # command is for, so do it rather than send the caller away.
        sent, ended = None, "was already gone when `quorum task stop` looked"
    note_transcript(
        transcript_path(home, task.id),
        f"run.stopped — runner pid {pid} {ended}; "
        "the task keeps its status and its worktree",
    )
    fsio.clear_stale_pid_lock(lock)
    fresh = store.get(task.id) or task
    recorded = False
    if len(fresh.runs) == runs_before:
        # Nothing wrote the run record, because the process that would have
        # is the one we just killed. Close it honestly instead of leaving a
        # run that reads as still going.
        run = TaskRun(
            started_at=str(meta.get("started_at") or fresh.updated_at),
            ended_at=fsio.iso(now()),
            exit_code=-int(sent) if sent is not None else None,
            stopped=True,
            # Read back off the lock the killed runner wrote: this record is
            # the only trace of that run, and the digest counts fresh
            # restarts off exactly this field.
            fresh_session=bool(meta.get("fresh_session")),
        )
        store.update(task.id, runs=[*[r.model_dump() for r in fresh.runs], run.model_dump()])
        recorded = True
    return {
        "task": task.id,
        "pid": pid,
        "signal": sent.name if sent is not None else None,
        "run_recorded": recorded,
    }


def _terminate_process_group(
    pid: int, grace_seconds: float
) -> tuple[signal.Signals, bool]:
    """SIGTERM a runner's whole process group, SIGKILL what refuses to die.

    Returns the last signal sent and whether anything is still alive. A
    process that disappears between checks is a success, not an error.

    Liveness is asked of the *group*, not of the runner's pid: SIGTERM kills
    the runner (a plain python process) instantly, while the harness that
    ignores SIGTERM keeps running — checking only the pid would call that a
    clean stop and leave the hung harness behind. `_run_alive` answers "is
    anyone *live* left in this group", which is the question worth asking:
    the runner we just killed stays in its own group as a zombie until
    something reaps it, and a bare `killpg(group, 0)` cannot tell the two
    apart.
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = 0
    # Our own group: a foreground `quorum task run` shares it, and killing
    # the group would take the caller (and, under `quorum up`, the
    # supervisor) with it. Then the runner's pid is the only handle we have.
    group = pgid if pgid > 0 and pgid != os.getpgrp() else 0

    def send(sig: int) -> None:
        with contextlib.suppress(OSError):
            if group:
                os.killpg(group, sig)
            else:
                os.kill(pid, sig)

    def gone() -> bool:
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _run_alive(pid, group):
                return True
            time.sleep(0.05)
        return not _run_alive(pid, group)

    send(signal.SIGTERM)
    if gone():
        return signal.SIGTERM, False
    send(signal.SIGKILL)
    return signal.SIGKILL, not gone()


def _run_alive(pid: int, group: int) -> bool:
    """Whether any *live* process of a run survives — the group when we have
    one (so a harness outliving its runner still counts), else the runner's
    pid.

    Both answers are zombie-aware (`fsio.group_alive` / `fsio.pid_alive`).
    A runner we just killed becomes a zombie until its parent reaps it, and
    a parent that stays alive without waiting — the TUI's `s` binding, before
    `launch_detached` learned to reap — leaves it that way; reading that as
    "still alive" is what made a stop report a run that survived SIGKILL.
    """
    if not group:
        return fsio.pid_alive(pid)
    return fsio.group_alive(group)


def unmet_dependencies(store: TaskStore, task: Task) -> list[str]:
    """Short ids of the task's dependencies that have not finished — empty
    (and free, no listing) for the overwhelming majority of tasks, which
    declare none."""
    if not task.depends_on:
        return []
    by_id = {t.id: t for t in store.list()}
    return dependency_state(task, by_id)["waiting_on"]


def budget_blockers(budget: TasksConfig, task: Task) -> list[str]:
    """How the task's last run exceeded the `[tasks]` budget — the notes the
    budget gate refuses on; empty (and free) whenever no budget is set, the
    task has no runs, or its last run came in under budget or reported no
    usage at all."""
    return usage.last_run_overages(
        task.runs, budget.max_cost_per_run, budget.max_tokens_per_run
    )


def held_refusal(task: Task) -> str:
    """The one message the hold rail speaks with, wherever it is checked
    (`run_task`, mirrored in `quorum task run` so `--detach` fails in the
    parent too, and shown as a notice by the TUI's `s` binding)."""
    return (
        f"task {task.short_id} is held — `quorum task release {task.short_id}` "
        "to unpark it, or `--force` to run it anyway"
    )


def budget_refusal(task: Task, over: list[str]) -> str:
    """The one message the budget gate speaks with, wherever it is checked
    (here, and mirrored in `quorum task run` so `--detach` fails in the
    parent too)."""
    return (
        f"task {task.short_id}'s last run exceeded its budget ({'; '.join(over)}) — "
        "next run gated; `--force` to run anyway"
    )


def launch_detached(
    home: Path, task_id: str, force: bool = False, fresh_session: bool = False
) -> int:
    """Start `quorum task run <id>` as a detached process; returns its pid.

    stdout/stderr go to the task's runner.log (the transcript captures the
    harness's own output separately, inside the run). `start_new_session`
    makes the child a session (and process-group) leader, which is what lets
    `stop_run` later signal the harness and everything it spawned as one
    group.

    A daemon thread waits on the child so that a caller which keeps running
    (the TUI's `s` binding; anything long-lived) reaps it instead of leaving
    a zombie behind — an unreaped runner is still a process-table entry, and
    every liveness question quorum asks about a run would have to work
    around it. A caller that exits first loses the thread with the process
    and init does the reaping, as before.
    """
    home = Path(home)
    log_path = runner_log_path(home, task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The detached child re-invokes `quorum task run`; that inner invocation
    # is infrastructure, not a second manager action.
    env = strip_actor_env({**os.environ, "QUORUM_HOME": str(home)})
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "quorum", "task", "run", task_id,
                "--home", str(home), *(["--force"] if force else []),
                *(["--fresh-session"] if fresh_session else []),
            ],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    threading.Thread(target=proc.wait, daemon=True, name=f"reap-{proc.pid}").start()
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
