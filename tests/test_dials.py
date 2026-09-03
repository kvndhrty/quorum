"""The trust dials (dials.py): the registry, its doctor lines, and the guide
section that must list every dial — including every numeric `[tasks]` /
`[agents]` option that has a default, so a new knob cannot land without a
row saying where it lives and when to move it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from quorum import dials, doctor
from quorum.actor import DEFAULT_MAX_ACTIONS_PER_RUN, DEFAULT_RUN_TIMEOUT_SECONDS
from quorum.agents import harness_run
from quorum.cli import app
from quorum.config import AgentConfig, Config, TasksConfig
from quorum.doctor import NA

runner = CliRunner()

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
CLAUDE_MD = ROOT / "CLAUDE.md"

SECTION = "## Loosening the rails as trust is earned"
ANCHOR = "#loosening-the-rails-as-trust-is-earned"
FACING = "What does not move"


def guide_section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index(SECTION)
    rest = text[start + len(SECTION) :]
    end = re.search(r"^## ", rest, flags=re.M)
    return rest[: end.start()] if end else rest


def table_rows(section: str) -> list[str]:
    return [line for line in section.splitlines() if line.startswith("|")]


# -- the registry --------------------------------------------------------------


def test_numeric_options_reads_int_and_float_defaults_and_skips_switches():
    found = dials.numeric_options(TasksConfig)
    assert found == {
        "max_cost_per_run": 0.0,
        "max_tokens_per_run": 0,
        "run_stall_timeout_seconds": 0.0,
    }
    assert "worktree" not in found  # bool is an int subclass; a switch is not a dial
    assert "auto_commit" not in found


def test_agent_numeric_options_name_the_settings_the_runs_read():
    found = dials.numeric_agent_options()
    assert found["max_actions_per_run"] == DEFAULT_MAX_ACTIONS_PER_RUN
    assert found["run_timeout_seconds"] == DEFAULT_RUN_TIMEOUT_SECONDS
    # the default the registry names is the one the run actually uses
    assert harness_run.DEFAULT_RUN_TIMEOUT_SECONDS == DEFAULT_RUN_TIMEOUT_SECONDS


def test_dial_keys_are_unique_and_stable():
    keys = [d.key for d in dials.DIALS]
    assert len(keys) == len(set(keys))
    assert keys[0] == "launches"
    assert "merge_gate" in keys


def test_current_reads_defaults_from_an_untouched_config(home: Path):
    values = dict((d.key, v) for d, v in dials.current(home, Config()))
    assert values["launches"].startswith("none (no prompts/manager.local.md")
    assert values["max_actions_per_run"] == "no agents configured"
    assert values["budget"] == "max_cost_per_run 0 (off), max_tokens_per_run 0 (off)"
    assert values["run_stall_timeout_seconds"] == "0 (off)"
    assert values["cadence"] == "no manager agent configured"
    assert "#83" in values["launcher"]
    assert "#43" in values["decomposer"]
    assert values["merge_gate"].startswith("a person")


def test_current_reads_a_loosened_config(home: Path):
    (home / "prompts" / "manager.local.md").write_text("- run at most TWO tasks\n")
    config = Config(
        tasks=TasksConfig(
            max_cost_per_run=5.0, max_tokens_per_run=250_000, run_stall_timeout_seconds=1800
        ),
        agents={
            "manager": AgentConfig(
                type="manager", schedule="every 1h", settings={"max_actions_per_run": 40}
            ),
            "standup": AgentConfig(
                type="prompt", schedule="every 1d", settings={"run_timeout_seconds": 900}
            ),
        },
    )
    values = dict((d.key, v) for d, v in dials.current(home, config))
    assert values["launches"] == "house rule in prompts/manager.local.md (not parsed)"
    assert values["max_actions_per_run"] == "manager 40, standup 20 (default)"
    assert values["run_timeout_seconds"] == "manager 300 (default), standup 900"
    assert values["budget"] == "max_cost_per_run 5, max_tokens_per_run 250000"
    assert values["run_stall_timeout_seconds"] == "1800"
    assert values["cadence"] == "every 1h"


def test_cadence_says_when_the_manager_is_disabled(home: Path):
    config = Config(agents={"manager": AgentConfig(type="manager", enabled=False)})
    values = dict((d.key, v) for d, v in dials.current(home, config))
    assert values["cadence"] == "every 1h, disabled"


# -- doctor ----------------------------------------------------------------------


def disable_ci(home: Path) -> None:
    """The machine running the tests (CI included) may have an unauthenticated
    gh, which is a legitimate ✗ from a check that is not the one under test."""
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("[ci]\nenabled = true", "[ci]\nenabled = false"))



def test_doctor_dial_lines_are_informational_only(home: Path):
    checks = doctor.check_dials(home, Config())
    assert [c.status for c in checks] == [NA] * len(dials.DIALS)
    assert [c.name for c in checks] == [f"dial.{d.key}" for d in dials.DIALS]
    assert all(c.summary.startswith("dial ") for c in checks)
    # one pointer at the guide, not one per line
    assert dials.GUIDE_ANCHOR in checks[0].fix
    assert all(not c.fix for c in checks[1:])


def test_doctor_dial_lines_show_the_configured_value(home: Path):
    config = Config(tasks=TasksConfig(max_cost_per_run=2.5))
    check = [c for c in doctor.check_dials(home, config) if c.name == "dial.budget"][0]
    assert "max_cost_per_run 2.5" in check.summary


def test_doctor_command_prints_the_dials_and_stays_green(home: Path):
    disable_ci(home)
    result = runner.invoke(app, ["doctor", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "– dial concurrent launches:" in result.output
    assert "– dial manager cadence: every 5m" in result.output  # the scaffold's manager
    assert "– dial merge gate: a person" in result.output
    assert dials.GUIDE_ANCHOR in result.output


def test_doctor_json_carries_every_dial(home: Path):
    disable_ci(home)
    result = runner.invoke(app, ["doctor", "--json", "--home", str(home)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    found = {c["name"]: c for c in payload["checks"] if c["name"].startswith("dial.")}
    assert set(found) == {f"dial.{d.key}" for d in dials.DIALS}
    assert {c["status"] for c in found.values()} == {NA}
    assert found["dial.max_actions_per_run"]["summary"].endswith("manager 20 (default)")


def test_doctor_skips_the_dials_when_the_config_is_unreadable(home: Path):
    """Dials read from defaults would be fiction; an unloadable config stops
    the run before them, like every other config-dependent check."""
    (home / "config.toml").write_text("[tasks\nbroken = ", encoding="utf-8")
    names = [c.name for c in doctor.run_checks(home)]
    assert not [n for n in names if n.startswith("dial.")]


# -- the guide ---------------------------------------------------------------------


def test_guide_section_exists_with_a_table_and_the_facing_list():
    section = guide_section()
    rows = table_rows(section)
    assert rows[0].lower().startswith("| dial |")
    assert len(rows) >= 2 + len(dials.DIALS)  # header, rule, one row per dial at least
    assert FACING in section


def test_guide_table_names_every_dial():
    section = guide_section()
    for dial in dials.DIALS:
        assert dial.label in section, f"guide table has no row for dial {dial.label!r}"


def test_guide_table_lists_every_numeric_tasks_and_agents_option():
    """The acceptance test from #85: add a numeric option with a default to
    `[tasks]` or `[agents]` and this fails until the table has a row that
    names it."""
    rows = "\n".join(table_rows(guide_section()))
    for name in dials.numeric_task_options():
        assert f"`{name}`" in rows, f"[tasks].{name} has no row in the trust-dials table"
    for name in dials.numeric_agent_options():
        assert f"`{name}`" in rows, f"[agents.*].{name} has no row in the trust-dials table"


def test_guide_facing_list_names_the_invariants():
    section = guide_section()
    tail = section[section.index(FACING) :]
    for phrase in (
        "No privileged infrastructure",
        "All state is plain files",
        "Fail loudly, recover automatically",
        "No decisions in Python",
        "Observations are never rails",
        "A dropped signal is a bug",
    ):
        assert phrase in tail, f"'What does not move' is missing {phrase!r}"


def test_guide_marks_the_unbuilt_dials_as_not_built():
    rows = table_rows(guide_section())
    for issue in ("#83", "#43"):
        row = [r for r in rows if issue in r]
        assert row, f"no table row mentions {issue}"
        assert "not built" in " ".join(row)


def test_guide_section_is_cross_linked_from_architecture_and_claude_md():
    assert ANCHOR in ARCHITECTURE.read_text(encoding="utf-8")
    assert ANCHOR in CLAUDE_MD.read_text(encoding="utf-8")
    assert dials.GUIDE_ANCHOR.endswith(ANCHOR)
