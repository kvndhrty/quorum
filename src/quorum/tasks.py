"""The task substrate: durable records for harness-driven development tasks.

A task is a prompt pointed at a registered project, executed by a
user-configured coding harness (claude, codex, opencode, ...) as a sequence
of *runs*. Everything durable lives under `tasks/<id>/`:

    task.json          spec + reported status + session id + run history
    runner.lock        pid of the active run (mtime doubles as a heartbeat)
    transcript.jsonl   the harness's stdout, one JSON line per line seen
    reports.jsonl      what the task said via `quorum task report`
    runner.log         stderr/bootstrap output of detached runs

Status is a *reported* string, not an enforced state machine: the harness
calls `quorum task report --status <word>` and quorum records whatever word
it chose. Only the TERMINAL_STATUSES set carries meaning inside quorum — the
manager stops attending to a task once it reaches one of them.

Guidance flows the other way through the ordinary message bus: each task
owns the inbox `task-<id>`, and the runner injects claimed messages into the
next run's prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import fsio
from .messages import MessageBus

BOARD_TOPIC = "tasks"

# The statuses quorum itself reacts to. "done" and "cancelled" end a task;
# "blocked" parks it for a human. Every other status string is free-form and
# merely displayed.
TERMINAL_STATUSES = {"done", "blocked", "cancelled"}


class TaskRun(BaseModel):
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None


class Task(BaseModel):
    v: int = 1
    id: str
    project: str
    prompt: str
    harness: str
    status: str = "queued"
    session: str | None = None
    pr_url: str | None = None
    use_worktree: bool = True
    workdir: str | None = None  # resolved on first run
    runs: list[TaskRun] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @property
    def short_id(self) -> str:
        """The tail of the ULID: its random half. The head is a timestamp,
        shared by every task created in the same instant, so a usable short
        handle must come from the end."""
        return self.id[-6:].lower()


def tasks_dir(home: Path) -> Path:
    return Path(home) / "tasks"


def task_dir(home: Path, task_id: str) -> Path:
    return tasks_dir(home) / task_id


def task_json_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "task.json"


def transcript_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "transcript.jsonl"


def reports_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "reports.jsonl"


def runner_lock_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "runner.lock"


def runner_log_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "runner.log"


def worktree_path(home: Path, task_id: str) -> Path:
    return Path(home) / "worktrees" / task_id


def inbox_name(task_id: str) -> str:
    """The bus inbox a task's guidance goes to."""
    return f"task-{task_id}"


class TaskStore:
    """CRUD over tasks/<id>/task.json. All writes are atomic; there is no
    index — the directory listing (ULID names) is the chronological index."""

    def __init__(self, home: Path):
        self.home = Path(home)

    def add(
        self,
        project: str,
        prompt: str,
        harness: str,
        use_worktree: bool = True,
        now: Any = None,
    ) -> Task:
        created = fsio.iso(now or fsio.utc_now())
        task = Task(
            id=fsio.ulid(),
            project=project,
            prompt=prompt,
            harness=harness,
            use_worktree=use_worktree,
            created_at=created,
            updated_at=created,
        )
        fsio.atomic_write_json(task_json_path(self.home, task.id), task.model_dump())
        return task

    def get(self, task_id: str) -> Task | None:
        try:
            return Task.model_validate(fsio.read_json(task_json_path(self.home, task_id)))
        except (OSError, ValueError):
            return None

    def list(self) -> list[Task]:
        root = tasks_dir(self.home)
        if not root.is_dir():
            return []
        out = []
        for entry in sorted(p for p in root.iterdir() if p.is_dir() and not fsio.is_tmp(p.name)):
            task = self.get(entry.name)
            if task is not None:
                out.append(task)
        return out

    def resolve(self, handle: str) -> Task:
        """Find a task by full id, unique id prefix, or unique suffix — the
        suffix form is what `short_id` hands out (case-insensitive).

        Raises KeyError when nothing matches, ValueError when the handle is
        ambiguous — callers turn both into friendly CLI errors.
        """
        handle = handle.strip().upper()
        exact = self.get(handle)
        if exact is not None:
            return exact
        matches = [
            t for t in self.list() if t.id.startswith(handle) or t.id.endswith(handle)
        ]
        if not matches:
            raise KeyError(handle)
        if len(matches) > 1:
            raise ValueError(
                f"task handle {handle!r} is ambiguous: {', '.join(t.short_id for t in matches)}"
            )
        return matches[0]

    def update(self, task_id: str, now: Any = None, **fields: Any) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        data = task.model_dump()
        data.update(fields)
        data["updated_at"] = fsio.iso(now or fsio.utc_now())
        task = Task.model_validate(data)
        fsio.atomic_write_json(task_json_path(self.home, task.id), task.model_dump())
        return task


def report(
    home: Path,
    task_id: str,
    status: str,
    text: str = "",
    pr_url: str | None = None,
    sender: str | None = None,
    now: Any = None,
) -> Task:
    """Record a progress report from the task itself (or a human on its behalf).

    Appends to reports.jsonl, updates the task's status (and pr_url when
    given), and mirrors the report onto the board so dashboards and the
    manager see it without polling every task directory's log.
    """
    home = Path(home)
    store = TaskStore(home)
    task = store.resolve(task_id)
    at = fsio.iso(now or fsio.utc_now())
    fsio.append_jsonl(
        reports_path(home, task.id),
        {"at": at, "status": status, "text": text, **({"pr_url": pr_url} if pr_url else {})},
    )
    updates: dict[str, Any] = {"status": status}
    if pr_url:
        updates["pr_url"] = pr_url
    task = store.update(task.id, **updates)
    MessageBus(home).post(
        sender or inbox_name(task.id),
        BOARD_TOPIC,
        f"task.{status}",
        text=text or f"task {task.short_id} reported {status}",
        payload={"task": task.id, "status": status, **({"pr_url": pr_url} if pr_url else {})},
    )
    return task


def read_transcript_tail(home: Path, task_id: str, limit: int = 40) -> list[dict]:
    return fsio.read_jsonl_tail(transcript_path(home, task_id), limit=limit)


def read_reports(home: Path, task_id: str, limit: int | None = None) -> list[dict]:
    entries = fsio.read_jsonl(reports_path(home, task_id))
    return entries[-limit:] if limit else entries


def runner_alive(home: Path, task_id: str) -> bool:
    """Whether a runner process currently holds this task's lock."""
    try:
        meta = fsio.read_json(runner_lock_path(home, task_id))
        pid = int(meta.get("pid", -1))
    except (OSError, ValueError):
        return False
    return pid > 0 and fsio.pid_alive(pid)


def last_activity(home: Path, task_id: str) -> datetime | None:
    """The newest sign of life: transcript, reports, or the runner lock."""
    newest = None
    for path in (
        transcript_path(home, task_id),
        reports_path(home, task_id),
        runner_lock_path(home, task_id),
    ):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=UTC)
