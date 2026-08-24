"""The actor-identity protocol: how a quorum CLI call knows who is acting.

The manager tags the harness it spawns with these env vars; the CLI reads
them to journal (and rate-cap) manager actions in `state/manager/journal.jsonl`
and to attribute messages. Anything that spawns a further process on an
actor's behalf (task runs, detached children) strips the tag so the child
acts as itself — a leaked tag would journal the child's quorum calls as
manager actions and burn the manager's cap.
"""

from __future__ import annotations

import os
from pathlib import Path

ACTOR_ENV = "QUORUM_ACTOR"
MANAGER_RUN_ENV = "QUORUM_MANAGER_RUN"
MANAGER_CAP_ENV = "QUORUM_MANAGER_ACTION_CAP"

DEFAULT_MAX_ACTIONS_PER_RUN = 20


def journal_path(home: Path) -> Path:
    """The manager action journal: appended by the CLI guard, read by digests."""
    return Path(home) / "state" / "manager" / "journal.jsonl"


def current_actor() -> str:
    """"manager" when running under a manager-tagged environment, else "user"."""
    return "manager" if os.environ.get(ACTOR_ENV) == "manager" else "user"


def manager_env(run_id: str, cap: int) -> dict[str, str]:
    """The env vars the manager sets on the harness run it spawns."""
    return {ACTOR_ENV: "manager", MANAGER_RUN_ENV: run_id, MANAGER_CAP_ENV: str(cap)}


def strip_actor_env(env: dict[str, str]) -> dict[str, str]:
    """Remove the actor tag so a spawned process acts as itself; returns env."""
    for var in (ACTOR_ENV, MANAGER_RUN_ENV, MANAGER_CAP_ENV):
        env.pop(var, None)
    return env
