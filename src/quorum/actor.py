"""The actor-identity protocol: how a quorum CLI call knows who is acting.

A harness-driven agent (the manager, or any prompt agent) tags the harness
it spawns with these env vars; the CLI reads them to journal (and rate-cap)
that agent's actions in its journal and to attribute messages. Anything that
spawns a further process on an actor's behalf (task runs, detached children)
strips the tag so the child acts as itself — a leaked tag would journal the
child's quorum calls as the agent's actions and burn the agent's cap.
"""

from __future__ import annotations

import os
from pathlib import Path

ACTOR_ENV = "QUORUM_ACTOR"
ACTOR_RUN_ENV = "QUORUM_ACTOR_RUN"
ACTOR_CAP_ENV = "QUORUM_ACTOR_CAP"

DEFAULT_MAX_ACTIONS_PER_RUN = 20


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


def usage_path(home: Path, name: str = "manager") -> Path:
    """An agent's per-run spend ledger (same split as `journal_path`).

    A task records usage on its own run entry in task.json; an agent has no
    such record — its runs are the ticks of a schedule — so they get one
    append-only line each here. See `usage.record_agent_run`.
    """
    if name == "manager":
        return Path(home) / "state" / "manager" / "usage.jsonl"
    return Path(home) / "state" / "agents" / name / "usage.jsonl"


def current_actor() -> str:
    """The tagged agent name when running under an actor-tagged environment,
    else "user"."""
    return os.environ.get(ACTOR_ENV) or "user"


def actor_env(name: str, run_id: str, cap: int) -> dict[str, str]:
    """The env vars an agent sets on the harness run it spawns."""
    return {ACTOR_ENV: name, ACTOR_RUN_ENV: run_id, ACTOR_CAP_ENV: str(cap)}


def strip_actor_env(env: dict[str, str]) -> dict[str, str]:
    """Remove the actor tag so a spawned process acts as itself; returns env."""
    for var in (ACTOR_ENV, ACTOR_RUN_ENV, ACTOR_CAP_ENV):
        env.pop(var, None)
    return env
