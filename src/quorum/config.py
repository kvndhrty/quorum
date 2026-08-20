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


class ProjectsConfig(BaseModel):
    workspace_roots: list[str] = Field(default_factory=list)


class QuorumSection(BaseModel):
    timezone: str = "local"
    retention_days: int = 30


_SCHEDULE_RE = re.compile(r"^(every\s+\d+\s*(s|m|h|d)|cron\s+\S+(\s+\S+){4})$")


class AgentConfig(BaseModel):
    type: str
    schedule: str = "every 1h"
    enabled: bool = True
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
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)
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
