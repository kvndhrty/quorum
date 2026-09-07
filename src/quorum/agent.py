"""Agent base class and the context handed to every agent.

An agent is a plain class with a synchronous, idempotent `tick()`. All of its
access to the outside world flows through the AgentContext, which keeps
custom agents trivial to write and makes every agent testable by constructing
a context directly — no scheduler required.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import fsio
from .messages import MessageBus
from .projects import ProjectRegistry

if TYPE_CHECKING:
    from .config import Config


class AgentContext:
    def __init__(
        self,
        home: Path,
        name: str,
        settings: dict[str, Any] | None = None,
        config: Config | None = None,
        bus: MessageBus | None = None,
        projects: ProjectRegistry | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.home = Path(home)
        self.name = name
        self.settings = settings or {}
        self.config = config
        self.now = now or fsio.utc_now
        self.bus = bus or MessageBus(self.home, now=self.now)
        self.projects = projects or ProjectRegistry(self.home)
        self._state_path = self.home / "state" / "agents" / name / "state.json"

    def prompt(self, template_name: str, **placeholders: str) -> str:
        """Render a prompt template from QUORUM_HOME/prompts/ (user-editable)."""
        from .prompts import render

        return render(self.home, template_name, **placeholders)

    # -- private state ----------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        return fsio.read_json_or(self._state_path, {})

    def save_state(self, state: dict[str, Any]) -> None:
        fsio.atomic_write_json(self._state_path, state)

    # -- action log (drives dashboards' "recent activity") -----------------

    def log_action(self, type: str, text: str, **data: Any) -> None:
        fsio.append_jsonl(
            self.home / "logs" / "actions.jsonl",
            {"at": fsio.iso(self.now()), "agent": self.name, "type": type, "text": text, **data},
        )


def tick_lock_path(home: Path, name: str) -> Path:
    """Per-agent tick lock, held for the duration of one tick.

    Taken by both the supervisor's tick wrapper and `quorum agent run-once`,
    so a hand-run tick and a scheduled one can never interleave and clobber
    each other's load_state()/save_state()."""
    return Path(home) / "state" / "agents" / name / "tick.lock"


def heartbeat_path(home: Path, name: str) -> Path:
    """Where an agent's heartbeat lives — the one spelling of the path."""
    return Path(home) / "state" / "agents" / name / "heartbeat.json"


def read_heartbeat(home: Path, name: str) -> dict[str, Any]:
    """An agent's heartbeat, or {} when there is none to read.

    Every reader of a heartbeat goes through here: the supervisor's tick
    wrapper, `write_heartbeat`'s merge, `views.agent_rows` and `quorum
    doctor`. A missing, unreadable or hand-edited file (including
    well-formed JSON that is not an object) reads as {} — a heartbeat is a
    bookkeeping side channel, never a rail, so it must not raise into an
    APScheduler job or a dashboard refresh.
    """
    return fsio.read_json_or(heartbeat_path(home, name), {})


def write_heartbeat(home: Path, name: str, **fields: Any) -> None:
    """Merge `fields` into an agent's heartbeat file.

    Heartbeats are the only record of an agent having run, so both the
    supervisor and `quorum agent run-once` write them: an agent exercised by
    hand would otherwise keep reading as never-ran in every dashboard.
    """
    path = heartbeat_path(home, name)
    current = read_heartbeat(home, name)
    current.update(fields)
    fsio.atomic_write_json(path, current)


def success_heartbeat_fields(started: datetime, ended: datetime) -> dict[str, Any]:
    """The heartbeat a *successful* tick writes, wherever the tick was run.

    Heartbeat writes are merges, so success has to clear the failure fields
    explicitly, or a long-fixed agent reads as broken in every dashboard —
    and a stale `escalated_at` would suppress the next escalation forever.
    The supervisor's wrapper and `quorum agent run-once` share this so a
    proven-working hand-run tick closes a streak exactly as a scheduled one
    does.
    """
    return {
        "status": "idle",
        "last_start": fsio.iso(started),
        "last_end": fsio.iso(ended),
        "duration_ms": int((ended - started).total_seconds() * 1000),
        "error": None,
        "consecutive_failures": 0,
        "escalated_at": None,
    }


class Agent:
    """Base class for all agents. Subclass, set `default_schedule` if you
    like, and implement tick(). Ticks must tolerate being re-run."""

    default_schedule = "every 1h"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    @property
    def name(self) -> str:
        return self.ctx.name

    def tick(self) -> None:
        raise NotImplementedError
