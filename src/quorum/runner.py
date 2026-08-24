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
   `session_id` if the harness emits one (enables `resume` templates),
6. records the run's exit in task.json and releases the lock.

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
from pathlib import Path

from . import fsio, prompts
from .config import Config, HarnessConfig
from .messages import MessageBus
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


def claim_guidance(home: Path, task_id: str) -> list[str]:
    """Drain the task's inbox; each message becomes a line for the prompt."""
    notes = []
    for claimed in MessageBus(home).claim(inbox_name(task_id)):
        msg = claimed.message
        notes.append(f"[from {msg.sender} at {msg.created_at}] {msg.payload.get('text', '')}")
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
    """Substitute {prompt}/{session} into the start or resume template."""
    template = harness.resume if (session and harness.resume) else harness.start
    argv, saw_prompt = [], False
    for element in template:
        if "{prompt}" in element:
            saw_prompt = True
        argv.append(element.replace("{prompt}", prompt).replace("{session}", session or ""))
    if not saw_prompt:
        argv.append(prompt)
    return argv


def build_argv(harness: HarnessConfig, task: Task, prompt: str) -> list[str]:
    return build_harness_argv(harness, prompt, task.session)


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
        argv = build_argv(harness, task, prompt)

        env = {**os.environ, **harness.env, "QUORUM_HOME": str(home)}
        # The task harness acts as itself, not as whoever launched it: a
        # manager-initiated run must not leak the manager's actor identity
        # (its quorum calls would be journaled and capped as manager actions).
        env.pop("QUORUM_ACTOR", None)
        env.pop("QUORUM_MANAGER_RUN", None)
        started = fsio.utc_now()
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        transcript = transcript_path(home, task.id)
        session = task.session
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            entry: dict = {"at": fsio.iso(fsio.utc_now())}
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                entry["line"] = line
            else:
                entry["event"] = event
                if session is None and isinstance(event, dict):
                    found = _find_session_id(event)
                    if found:
                        session = found
                        store.update(task.id, session=session)
            fsio.append_jsonl(transcript, entry)
        exit_code = proc.wait()

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
    env = {**os.environ, "QUORUM_HOME": str(home)}
    # The detached child re-invokes `quorum task run`; that inner invocation
    # is infrastructure, not a second manager action — strip the actor tag
    # so it is neither journaled again nor charged against the action cap.
    env.pop("QUORUM_ACTOR", None)
    env.pop("QUORUM_MANAGER_RUN", None)
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
    for key in ("session_id", "sessionId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
