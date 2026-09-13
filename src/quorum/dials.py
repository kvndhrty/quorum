"""The trust dials: the settings that encode how far a home currently trusts
its models, listed once so `quorum doctor` and the guide say the same thing.

Quorum's constraints come in two kinds. Some encode distrust of the
*environment* — rate limits, money, a laptop with no root and no open ports,
a signal that must never be dropped silently — and those do not move when a
better model arrives; they are the "What does not move" list in
docs/guide.md. The rest are dials: how many tasks the manager may run at
once, how many actions a run gets, whether spend is budgeted, how often the
manager wakes, who launches, who decomposes, who merges. Each one is a
statement about how much the human trusts the model *today*, and each should
loosen as that trust is earned. Scattered across a prompt overlay, two config
tables and a convention, they were easy to leave at their cautious default
forever, or to confuse with the invariants next to them.

This module is the registry. `DIALS` names every dial with where it lives,
its default, and a reader for its current value; `doctor.check_dials` renders
those as informational `–` lines, and `tests/test_dials.py` holds the guide's
table to the same list — including every numeric `[tasks]` / `[agents]`
option with a default, so a new knob cannot be added without a row saying
when to move it. Nothing here decides or enforces anything: it reads config
and reports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from .actor import DEFAULT_MAX_ACTIONS_PER_RUN, DEFAULT_RUN_TIMEOUT_SECONDS
from .config import AgentConfig, Config, TasksConfig

# Numeric per-agent settings with a default, keyed as they appear under
# [agents.<name>.settings]. `AgentConfig` itself carries no numeric field;
# these live in its free-form `settings` dict, so the model cannot enumerate
# them and the registry has to. The defaults are owned by actor.py; this
# only names them.
NUMERIC_AGENT_SETTINGS: dict[str, int | float] = {
    "max_actions_per_run": DEFAULT_MAX_ACTIONS_PER_RUN,
    "run_timeout_seconds": DEFAULT_RUN_TIMEOUT_SECONDS,
}

# The manager's local overlay: the one place a launch cap can live today,
# since the packaged manager.md sets none and Python sorts nothing.
MANAGER_OVERLAY = "prompts/manager.local.md"

GUIDE_ANCHOR = "docs/guide.md#loosening-the-rails-as-trust-is-earned"


def _is_numeric_annotation(annotation) -> bool:
    """True for `int`, `float`, and unions of those with `None`.

    Checked on the annotation, not the value: `bool` is an `int` subclass and
    a switch is not a dial. Unions are unwrapped because `int | None` is an
    ordinary way to spell "off by default" for a numeric option, and a knob
    written that way must still be caught by the guide's table test.
    """
    if annotation in (int, float):
        return True
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return bool(args) and all(arg in (int, float) for arg in args)


def numeric_options(model: type) -> dict[str, int | float]:
    """Every int/float field of a pydantic config model that has a default.

    A required field (no default) is skipped — the table documents defaults,
    and a field with none has nothing to loosen.
    """
    found: dict[str, int | float] = {}
    for name, field in model.model_fields.items():
        if not _is_numeric_annotation(field.annotation):
            continue
        if field.is_required():
            continue
        found[name] = field.get_default()
    return found


def numeric_task_options() -> dict[str, int | float]:
    return numeric_options(TasksConfig)


def numeric_agent_options() -> dict[str, int | float]:
    """`[agents.<name>]` numeric fields (none today) plus the settings above."""
    return {**numeric_options(AgentConfig), **NUMERIC_AGENT_SETTINGS}


def _fmt(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _off(value: int | float) -> str:
    return f"{_fmt(value)} (off)" if not value else _fmt(value)


@dataclass(frozen=True)
class Dial:
    """One trust dial: `key` is stable (doctor names its line `dial.<key>`
    and the test looks it up), `label` is what a person calls it, `lives_in`
    is where to change it, `default` is what an untouched home has, and
    `read` renders the current value for a home + loaded config."""

    key: str
    label: str
    lives_in: str
    default: str
    read: Callable[[Path, Config], str]


def _read_launches(home: Path, config: Config) -> str:
    overlay = Path(home) / MANAGER_OVERLAY
    if overlay.is_file():
        return f"house rule in {MANAGER_OVERLAY} (not parsed)"
    return f"none (no {MANAGER_OVERLAY}; the packaged prompt sets no cap)"


def _per_agent(config: Config, setting: str, default: int | float) -> str:
    if not config.agents:
        return "no agents configured"
    parts = []
    for name in sorted(config.agents):
        raw = config.agents[name].settings.get(setting)
        if raw is None:
            parts.append(f"{name} {_fmt(default)} (default)")
        else:
            parts.append(f"{name} {raw}")
    return ", ".join(parts)


def _read_actions(home: Path, config: Config) -> str:
    return _per_agent(config, "max_actions_per_run", DEFAULT_MAX_ACTIONS_PER_RUN)


def _read_run_timeout(home: Path, config: Config) -> str:
    return _per_agent(config, "run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)


def _read_budget(home: Path, config: Config) -> str:
    t = config.tasks
    return (
        f"max_cost_per_run {_off(t.max_cost_per_run)}, "
        f"max_tokens_per_run {_off(t.max_tokens_per_run)}"
    )


def _read_stall(home: Path, config: Config) -> str:
    return _off(config.tasks.run_stall_timeout_seconds)


def _read_spawn(home: Path, config: Config) -> str:
    """Tasks that spawn tasks: whether this home queues them spawn-enabled by
    default, and the two rails on the ones that are."""
    t = config.tasks
    default = "on" if t.allow_spawn else "off (per-task --allow-spawn)"
    return (
        f"allow_spawn {default}, max_spawn_per_task {_fmt(t.max_spawn_per_task)}, "
        f"max_spawn_depth {_fmt(t.max_spawn_depth)}"
    )


def _read_cadence(home: Path, config: Config) -> str:
    manager = config.agents.get("manager")
    if manager is None:
        return "no manager agent configured"
    state = "" if manager.enabled else ", disabled"
    return f"{manager.schedule}{state}"


def _read_launcher(home: Path, config: Config) -> str:
    return "the manager, from its prompt (wake conditions, #83, are not built)"


def _read_decomposer(home: Path, config: Config) -> str:
    """Who may turn one piece of work into several: a person always, the
    manager from its prompt, and — where a task was queued with
    `--allow-spawn` — the task's own run, under the spawn rails."""
    return f"a person and the manager, with `task add`; tasks: {_read_spawn(home, config)}"


def _read_merge_gate(home: Path, config: Config) -> str:
    return "a person (quorum has no forge write path)"


DIALS: tuple[Dial, ...] = (
    Dial(
        key="launches",
        label="concurrent launches",
        lives_in=f"a house rule in {MANAGER_OVERLAY}",
        default="none",
        read=_read_launches,
    ),
    Dial(
        key="max_actions_per_run",
        label="actions per agent run",
        lives_in="[agents.<name>.settings] max_actions_per_run",
        default=_fmt(DEFAULT_MAX_ACTIONS_PER_RUN),
        read=_read_actions,
    ),
    Dial(
        key="run_timeout_seconds",
        label="seconds per agent run",
        lives_in="[agents.<name>.settings] run_timeout_seconds",
        default=_fmt(DEFAULT_RUN_TIMEOUT_SECONDS),
        read=_read_run_timeout,
    ),
    Dial(
        key="budget",
        label="per-run budget",
        lives_in="[tasks] max_cost_per_run / max_tokens_per_run",
        default="0 (off)",
        read=_read_budget,
    ),
    Dial(
        key="run_stall_timeout_seconds",
        label="stall watchdog",
        lives_in="[tasks] run_stall_timeout_seconds",
        default="0 (off)",
        read=_read_stall,
    ),
    Dial(
        key="spawn",
        label="tasks that spawn tasks",
        lives_in="[tasks] allow_spawn / max_spawn_per_task / max_spawn_depth",
        default="off, 5, 1",
        read=_read_spawn,
    ),
    Dial(
        key="cadence",
        label="manager cadence",
        lives_in="[agents.manager] schedule",
        default="every 5m (scaffold), every 1h (config model)",
        read=_read_cadence,
    ),
    Dial(
        key="launcher",
        label="who launches",
        lives_in="prompts/manager.md",
        default="the manager",
        read=_read_launcher,
    ),
    Dial(
        key="decomposer",
        label="who decomposes",
        lives_in="convention",
        default="a person",
        read=_read_decomposer,
    ),
    Dial(
        key="merge_gate",
        label="merge gate",
        lives_in="convention",
        default="a person",
        read=_read_merge_gate,
    ),
)


def current(home: Path, config: Config) -> list[tuple[Dial, str]]:
    """Every dial with its current value, in table order. Never raises on
    config content: each reader only looks at fields the model validated."""
    return [(dial, dial.read(Path(home), config)) for dial in DIALS]
