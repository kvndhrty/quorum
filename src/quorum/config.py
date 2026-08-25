"""Configuration models. config.toml is user-owned: quorum reads it with
stdlib tomllib and never writes it back — machine state goes to JSON files."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .home import CONFIG_NAME


class LLMConfig(BaseModel):
    backend: Literal["cli", "proxy"] = "cli"
    executable: str = ""
    args: list[str] = Field(default_factory=list)
    input: Literal["stdin", "argv"] = "stdin"
    timeout_seconds: float = 120.0
    max_prompt_chars: int = 24000
    env: dict[str, str] = Field(default_factory=dict)


class SandboxConfig(BaseModel):
    use_nono: bool = False
    profile: str = ""
    # A user-authored nono-style JSON profile ({"fs_read": [...], "fs_write":
    # [...], "network": [...]}) merged into the capability sets quorum derives
    # for self-sandbox and task runs — the same file works with the nono
    # binary (mode 1) and with nono-py (modes 2/3).
    profile_file: str = ""
    # Extra grants for sandboxed task runs. Real harnesses keep their own
    # state outside the worktree (claude: ~/.claude, codex: ~/.codex) and
    # exec helper tools; grant those here.
    task_read: list[str] = Field(default_factory=list)
    task_write: list[str] = Field(default_factory=list)


class HarnessConfig(BaseModel):
    """One [harness.<name>] table: how to invoke a coding harness CLI.

    `start` and `resume` are argv templates; "{prompt}" and "{session}" are
    substituted element-wise (a template with no "{prompt}" gets the prompt
    appended as the final argument). `resume` is optional — without it, or
    without a captured session id, every run uses `start`; the worktree
    persists between runs, so a fresh session still sees prior progress.

    `inject = "stream-json"` opts a harness into mid-run guidance delivery:
    the runner keeps the harness's stdin open and forwards inbox messages as
    stream-json user turns (the Claude Code `--input-format stream-json`
    protocol). The argv template must include the matching flags; harnesses
    without it get guidance at the next run start, as before.
    """

    start: list[str]
    resume: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    inject: Literal["", "stream-json"] = ""

    @field_validator("start")
    @classmethod
    def _nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("harness 'start' argv must not be empty")
        return v


class TasksConfig(BaseModel):
    worktree: bool = True
    default_harness: str = ""


class QuorumSection(BaseModel):
    timezone: str = "local"
    retention_days: int = 30


_SCHEDULE_RE = re.compile(r"^(every\s+\d+\s*(s|m|h|d)|cron\s+\S+(\s+\S+){4})$")


class AgentConfig(BaseModel):
    type: str
    schedule: str = "every 1h"
    enabled: bool = True
    # False: repeated failures never pause the schedule — the agent keeps
    # retrying so it self-recovers when an external dependency (the LLM
    # service, for the manager) comes back. Failures still land in the
    # heartbeat and on the board.
    auto_pause: bool = True
    settings: dict = Field(default_factory=dict)

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, v: str) -> str:
        if not _SCHEDULE_RE.match(v.strip()):
            raise ValueError(
                f"invalid schedule {v!r}: use 'every <N><s|m|h|d>' (e.g. 'every 30m') "
                "or 'cron <minute> <hour> <dom> <month> <dow>'"
            )
        return v.strip()


def parse_schedule(schedule: str) -> dict:
    """Translate a schedule string into APScheduler trigger kwargs."""
    parts = schedule.split()
    if parts[0] == "every":
        m = re.match(r"(\d+)\s*(s|m|h|d)", "".join(parts[1:]))
        assert m, schedule
        n, unit = int(m.group(1)), m.group(2)
        key = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
        return {"trigger": "interval", key: n}
    minute, hour, day, month, dow = parts[1:6]
    return {
        "trigger": "cron",
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": dow,
    }


class Config(BaseModel):
    quorum: QuorumSection = Field(default_factory=QuorumSection)
    llm: LLMConfig | None = None
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    harness: dict[str, HarnessConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)


class ConfigError(RuntimeError):
    pass


def load_config(home: Path) -> Config:
    path = home / CONFIG_NAME
    if not path.exists():
        raise ConfigError(f"no {CONFIG_NAME} in {home} — run `quorum init` first")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"{path}: {e}") from e
