"""The actor-identity protocol: how a quorum CLI call knows who is acting.

A harness-driven agent (the manager, or any prompt agent) tags the harness
it spawns with these env vars; the CLI reads them to journal (and rate-cap)
that agent's actions in its journal and to attribute messages. Anything that
spawns a further process on an actor's behalf (task runs, detached children)
strips the tag so the child acts as itself — a leaked tag would journal the
child's quorum calls as the agent's actions and burn the agent's cap. The
runner then tags a task's harness `QUORUM_ACTOR=task-<id>`, so a task run
acts under its own identity rather than as nobody.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ACTOR_ENV = "QUORUM_ACTOR"
ACTOR_RUN_ENV = "QUORUM_ACTOR_RUN"
ACTOR_CAP_ENV = "QUORUM_ACTOR_CAP"

DEFAULT_MAX_ACTIONS_PER_RUN = 20
# Seconds one agent harness run may take before `run_agent_harness` kills
# it (agents/harness_run.py). Kept next to the action cap because the two
# are the per-run [agents.<name>.settings] dials `dials.py` lists.
DEFAULT_RUN_TIMEOUT_SECONDS = 300

# A task run is tagged too, as `task-<id>` — the same string as its inbox
# name (`tasks.inbox_name`), so one identity names a task everywhere the bus
# or the CLI needs to. The tag is what lets a task's own notebook refuse a
# note from another task (`notes.Notebook.may_write`); it carries no run id
# and no cap, so `_actor_guard` journals and rate-limits nothing for it —
# the runner is a task's rail, and reports.jsonl + the transcript are its
# record.
TASK_ACTOR_PREFIX = "task-"


def journal_path(home: Path, name: str = "manager") -> Path:
    """An agent's action journal: appended by the CLI guard, read by digests.

    The manager keeps its historical spot at `state/manager/`; every other
    agent journals under `state/agents/<name>/`.
    """
    if name == "manager":
        return Path(home) / "state" / "manager" / "journal.jsonl"
    return Path(home) / "state" / "agents" / name / "journal.jsonl"


def transcript_path(home: Path, name: str = "manager") -> Path:
    """Where an agent's harness-run transcript streams to (same split as
    `journal_path`)."""
    if name == "manager":
        return Path(home) / "state" / "manager" / "transcript.jsonl"
    return Path(home) / "state" / "agents" / name / "transcript.jsonl"


def notes_path(home: Path, name: str = "manager") -> Path:
    """An agent's notebook: standing notes a *future* run needs (same split as
    `journal_path`).

    Deliberately its own file, next to the journal rather than inside it: the
    journal is a bounded tail of what an agent *did* this run, so a note meant
    for next week is pushed out of it by the next busy tick. See `notes.py`.
    """
    if name == "manager":
        return Path(home) / "state" / "manager" / "notes.jsonl"
    return Path(home) / "state" / "agents" / name / "notes.jsonl"


def runs_dir(home: Path, name: str = "manager") -> Path:
    """Where an agent keeps a snapshot of what each run was given (same split
    as `journal_path`).

    The one thing a tick used to leave no trace of: the digest it reasoned
    over was rendered, sent to the harness and dropped, so "why did it launch
    that" was unanswerable an hour later. One file per run, bounded twice —
    head-truncated on write, and only the newest `SNAPSHOT_KEEP` kept — so
    this is an observability artifact of the journal's class, never state
    anything reads back to decide something.
    """
    if name == "manager":
        return Path(home) / "state" / "manager" / "runs"
    return Path(home) / "state" / "agents" / name / "runs"


def run_snapshot_path(home: Path, name: str, run_id: str) -> Path:
    return runs_dir(home, name) / f"{run_id}.md"


def usage_path(home: Path, name: str = "manager") -> Path:
    """An agent's per-run spend ledger (same split as `journal_path`).

    A task records usage on its own run entry in task.json; an agent has no
    such record — its runs are the ticks of a schedule — so they get one
    append-only line each here. See `usage.record_agent_run`.
    """
    if name == "manager":
        return Path(home) / "state" / "manager" / "usage.jsonl"
    return Path(home) / "state" / "agents" / name / "usage.jsonl"


def task_actor(task_id: str) -> str:
    """The actor identity of a task run: `task-<full id>`."""
    return f"{TASK_ACTOR_PREFIX}{task_id}"


def is_task_actor(name: str) -> bool:
    """Whether an actor name is a task's (`task-<id>`) rather than an agent's.

    The bare prefix counts: `config.validate_agent_name` calls this to reject
    agent names that would collide with a task identity, and an agent named
    `task-` collides with the whole space.
    """
    return name.startswith(TASK_ACTOR_PREFIX)


def current_actor() -> str:
    """The tagged agent name when running under an actor-tagged environment,
    else "user"."""
    return os.environ.get(ACTOR_ENV) or "user"


def current_run() -> str:
    """The run id this process is tagged with, or "" — a task run carries
    none, and neither does an untagged one."""
    return os.environ.get(ACTOR_RUN_ENV, "")


def current_cap() -> int:
    """The per-run action cap this process is tagged with.

    A tag that is missing or unreadable falls back to the default rather
    than raising, because both readers — the guard that enforces the cap and
    `show self`, which reports it — run in front of an agent mid-run. The
    one implementation of that fallback, so the number a run is told is the
    number it is held to.
    """
    try:
        return int(os.environ.get(ACTOR_CAP_ENV, DEFAULT_MAX_ACTIONS_PER_RUN))
    except ValueError:
        return DEFAULT_MAX_ACTIONS_PER_RUN


def self_task_id() -> str | None:
    """The task id this process is acting as, or None when the tag names an
    agent or is absent. The resolution behind `quorum task show self`."""
    actor = current_actor()
    return actor[len(TASK_ACTOR_PREFIX) :] if is_task_actor(actor) else None


def self_agent_name() -> str | None:
    """The agent name this process is acting as, or None when the tag names
    a task or is absent. The resolution behind `quorum agent show self`."""
    actor = current_actor()
    return None if actor == "user" or is_task_actor(actor) else actor


@dataclass(frozen=True)
class SelfRun:
    """Who a process is acting as, and what its own run has spent.

    The whole of the run-scoped half of `quorum task show self` /
    `quorum agent show self`: the actor tag, the run id it was tagged with,
    the action cap it is held to and how much of that cap it has used. Read
    from the environment and the journal, never from a model's self-report,
    and handed to `views` so the rendering stays a pure file reader.

    A task run is tagged for identity only — no run id and no cap (see
    `task_actor_env`) — so `run` is "" and `capped` is False for one.
    """

    actor: str
    run: str
    cap: int
    actions: int

    @property
    def capped(self) -> bool:
        """Whether an action cap applies at all: only a tagged agent run."""
        return bool(self.run)


def self_run(home: Path) -> SelfRun:
    """The calling process's own actor facts. `actor` is "user" outside a
    tagged run, which is what the CLI turns into the error naming the fix."""
    actor = current_actor()
    run = current_run()
    return SelfRun(actor=actor, run=run, cap=current_cap(), actions=actions_used(home, actor, run))


def actions_used(home: Path, name: str, run_id: str) -> int:
    """How many actions `name` has journalled under `run_id` so far.

    The number the cap is checked against, read back out of the journal
    rather than counted in memory — the CLI calls that spend the cap are
    separate processes, so the file is the only place the count exists. A
    `cap.hit` line records a refusal, not an action, and does not count; a
    torn line is skipped like everywhere else. Returns 0 for an untagged
    run, which has no cap to spend.
    """
    from . import fsio

    if not run_id:
        return 0
    entries = fsio.read_jsonl_tail(journal_path(home, name))
    return len(
        [
            e
            for e in entries
            if isinstance(e, dict) and e.get("run") == run_id and e.get("action") != "cap.hit"
        ]
    )


def actor_env(name: str, run_id: str, cap: int) -> dict[str, str]:
    """The env vars an agent sets on the harness run it spawns."""
    return {ACTOR_ENV: name, ACTOR_RUN_ENV: run_id, ACTOR_CAP_ENV: str(cap)}


def task_actor_env(task_id: str) -> dict[str, str]:
    """The env var the runner sets on a task's harness: identity only — no
    run id (nothing journals a task's actions) and no cap."""
    return {ACTOR_ENV: task_actor(task_id)}


def strip_actor_env(env: dict[str, str]) -> dict[str, str]:
    """Remove the actor tag so a spawned process acts as itself; returns env."""
    for var in (ACTOR_ENV, ACTOR_RUN_ENV, ACTOR_CAP_ENV):
        env.pop(var, None)
    return env
