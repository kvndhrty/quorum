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
    the runner keeps the harness's stdin open, delivers the composed prompt
    as the opening stream-json user turn, and forwards inbox messages as
    further turns (the Claude Code `--input-format stream-json` protocol,
    which reads user turns only from stdin and ignores an argv prompt — so
    "{prompt}" elements are dropped from an inject template's argv). The
    argv template must include the matching flags; harnesses without
    `inject` get the prompt via argv and guidance at the next run start,
    as before.
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
    # Opt-in safety net (runner.py): after a run, commit whatever the harness
    # left uncommitted in its worktree, so a crashed or forgetful harness can
    # never lose work — branches outlive worktrees. Off by default, and only
    # ever applied to a task's own worktree, never the user's checkout.
    auto_commit: bool = False


class HerdrConfig(BaseModel):
    """Optional [herdr] table for the fail-soft herdr adapter (herdr.py).

    Absent config is fine: the adapter auto-detects the default socket and
    silently does nothing when herdr isn't around."""

    socket: str = ""  # override for ~/.config/herdr/herdr.sock
    enabled: bool = True


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
    herdr: HerdrConfig | None = None
    harness: dict[str, HarnessConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)


class ConfigError(RuntimeError):
    pass


AGENTS_DIR = "agents"

# The names an agents/<name>.toml file may never claim: builtins configured in
# config.toml, the supervisor control inbox, and the task-inbox namespace.
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED_AGENT_NAMES = {"manager", "supervisor", "user"}


def validate_agent_name(name: str) -> None:
    if not AGENT_NAME_RE.match(name):
        raise ConfigError(
            f"invalid agent name {name!r}: use lowercase letters, digits, '-' or '_', "
            "starting with a letter (max 32 chars)"
        )
    if name in RESERVED_AGENT_NAMES or name.startswith("task-"):
        raise ConfigError(f"agent name {name!r} is reserved")


def agent_file_path(home: Path, name: str) -> Path:
    return Path(home) / AGENTS_DIR / f"{name}.toml"


def _load_agent_files(home: Path) -> dict[str, AgentConfig]:
    agents_dir = Path(home) / AGENTS_DIR
    found: dict[str, AgentConfig] = {}
    if not agents_dir.is_dir():
        return found
    for path in sorted(agents_dir.glob("*.toml")):
        if path.name.startswith("."):
            continue
        name = path.stem
        try:
            validate_agent_name(name)
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            found[name] = AgentConfig.model_validate(raw)
        except Exception as e:
            raise ConfigError(f"{path}: {e}") from e
    return found


def write_agent_file(home: Path, name: str, agent: AgentConfig) -> Path:
    """Write agents/<name>.toml — the one config location quorum may write
    (config.toml stays user-owned). Hand-serialized: the schema is small and
    fixed, and stdlib has no TOML writer; json.dumps produces valid TOML
    basic strings."""
    import json

    from . import fsio

    validate_agent_name(name)
    lines = [
        f"type = {json.dumps(agent.type)}",
        f"schedule = {json.dumps(agent.schedule)}",
        f"enabled = {'true' if agent.enabled else 'false'}",
        f"auto_pause = {'true' if agent.auto_pause else 'false'}",
    ]
    if agent.settings:
        lines += ["", "[settings]"]
        for key, value in agent.settings.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f"{key} = {json.dumps(str(value))}")
    path = agent_file_path(home, name)
    fsio.atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def create_agent(
    home: Path,
    name: str,
    *,
    type_: str = "prompt",
    schedule: str = "every 1h",
    auto_pause: bool = True,
    settings: dict | None = None,
    prompt_text: str | None = None,
) -> AgentConfig:
    """Create a file-defined agent: agents/<name>.toml plus, when given,
    prompts/<name>.md. Shared by `quorum agent create` and the web dashboard;
    callers send the `agent.reload` poke themselves."""
    from . import fsio

    validate_agent_name(name)
    if name in load_config(home).agents:
        raise ConfigError(
            f"agent {name!r} already exists — edit agents/{name}.toml (then "
            f"`quorum agent reload {name}`) or config.toml instead"
        )
    acfg = AgentConfig(
        type=type_, schedule=schedule, auto_pause=auto_pause, settings=settings or {}
    )
    write_agent_file(home, name, acfg)
    if prompt_text is not None:
        fsio.atomic_write_text(Path(home) / "prompts" / f"{name}.md", prompt_text)
    return acfg


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
        config = Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"{path}: {e}") from e
    # agents/<name>.toml files merge over config.toml [agents.*]: the file is
    # the machine-writable channel, so on a name collision the file wins.
    config.agents.update(_load_agent_files(home))
    return config
