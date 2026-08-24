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
    from .llm import LLMClient


class AgentContext:
    def __init__(
        self,
        home: Path,
        name: str,
        settings: dict[str, Any] | None = None,
        config: Config | None = None,
        bus: MessageBus | None = None,
        projects: ProjectRegistry | None = None,
        llm: LLMClient | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.home = Path(home)
        self.name = name
        self.settings = settings or {}
        self.config = config
        self.now = now or fsio.utc_now
        self.bus = bus or MessageBus(self.home, now=self.now)
        self.projects = projects or ProjectRegistry(self.home)
        self._llm = llm
        self._state_path = self.home / "state" / "agents" / name / "state.json"

    # -- LLM (optional) ---------------------------------------------------

    @property
    def llm(self) -> LLMClient:
        """An LLMClient; when nothing is configured it is a disabled client
        whose complete() always returns None. Agents must handle None."""
        if self._llm is None:
            from .llm import LLMClient

            self._llm = LLMClient.from_config(
                self.config.llm if self.config else None,
                agent_settings=self.settings,
                home=self.home,
                sandbox_config=self.config.sandbox if self.config else None,
                full_config=self.config,
            )
        return self._llm

    def prompt(self, template_name: str, **placeholders: str) -> str:
        """Render a prompt template from QUORUM_HOME/prompts/ (user-editable)."""
        from .prompts import render

        return render(self.home, template_name, **placeholders)

    # -- private state ----------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        try:
            return fsio.read_json(self._state_path)
        except (OSError, ValueError):
            return {}

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


def write_heartbeat(home: Path, name: str, **fields: Any) -> None:
    """Merge `fields` into an agent's heartbeat file.

    Heartbeats are the only record of an agent having run, so both the
    supervisor and `quorum agent run-once` write them: an agent exercised by
    hand would otherwise keep reading as never-ran in every dashboard.
    """
    path = Path(home) / "state" / "agents" / name / "heartbeat.json"
    current: dict[str, Any] = {}
    try:
        current = fsio.read_json(path)
    except (OSError, ValueError):
        pass
    current.update(fields)
    fsio.atomic_write_json(path, current)


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
