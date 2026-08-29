"""PromptAgent and agents/<name>.toml: the generic harness-driven agent.

The agent's harness is the fake in tests/bin/fake_harness.py; `agent_act`
mode acts through the quorum CLI so the per-agent journal and action cap are
exercised for real.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from quorum import fsio
from quorum.actor import journal_path, transcript_path
from quorum.agent import AgentContext
from quorum.agents.prompt_agent import PromptAgent
from quorum.config import ConfigError, create_agent, load_config, validate_agent_name
from quorum.messages import MessageBus

FAKE = str(Path(__file__).parent / "bin" / "fake_harness.py")


def write_config(home: Path, mode: str = "echo") -> None:
    (home / "config.toml").write_text(
        "[harness.agenttool]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        f'env = {{ FAKE_HARNESS_MODE = "{mode}" }}\n'
    )


def make_agent(home: Path, clock, name: str = "standup", **settings) -> PromptAgent:
    config = load_config(home)
    merged = {**config.agents[name].settings, **settings}
    ctx = AgentContext(home=home, name=name, settings=merged, config=config, now=clock)
    return PromptAgent(ctx)


def seed_agent(home: Path, name: str = "standup", prompt: str = "post a standup note\n") -> None:
    create_agent(
        home, name, schedule="every 30m", settings={"harness": "agenttool"}, prompt_text=prompt
    )


# -- agents/<name>.toml -----------------------------------------------------


def test_agent_files_merge_into_config(home: Path):
    write_config(home)
    seed_agent(home)
    config = load_config(home)
    assert "standup" in config.agents
    acfg = config.agents["standup"]
    assert acfg.type == "prompt"
    assert acfg.schedule == "every 30m"
    assert acfg.settings["harness"] == "agenttool"
    assert (home / "prompts" / "standup.md").read_text() == "post a standup note\n"


def test_agent_file_wins_over_config_toml(home: Path):
    write_config(home)
    (home / "config.toml").write_text(
        (home / "config.toml").read_text()
        + '[agents.standup]\ntype = "steward:Steward"\nschedule = "every 1d"\n'
    )
    fsio.atomic_write_text(
        home / "agents" / "standup.toml", 'type = "prompt"\nschedule = "every 5m"\n'
    )
    acfg = load_config(home).agents["standup"]
    assert acfg.type == "prompt" and acfg.schedule == "every 5m"


def test_malformed_agent_file_fails_loudly(home: Path):
    write_config(home)
    fsio.atomic_write_text(home / "agents" / "broken.toml", "schedule = not toml [")
    with pytest.raises(ConfigError, match="broken.toml"):
        load_config(home)


def test_create_agent_refuses_duplicates_and_reserved_names(home: Path):
    write_config(home)
    seed_agent(home)
    with pytest.raises(ConfigError, match="already exists"):
        seed_agent(home)
    for bad in ("manager", "supervisor", "task-abc", "Bad Name"):
        with pytest.raises(ConfigError):
            validate_agent_name(bad)


# -- ticking ----------------------------------------------------------------


def test_tick_renders_prompt_and_streams_to_agent_transcript(home: Path, clock):
    write_config(home)
    seed_agent(home, prompt="scan the board and post anything noteworthy\n")
    make_agent(home, clock).tick()

    entries = fsio.read_jsonl(transcript_path(home, "standup"))
    text = "\n".join(e.get("line", "") for e in entries)
    assert "scan the board and post anything noteworthy" in text
    assert not (home / "state" / "manager" / "transcript.jsonl").exists()


def test_directives_reach_the_prompt_and_are_acked(home: Path, clock):
    write_config(home)
    seed_agent(home, prompt="directives today:\n{directives}\n")
    bus = MessageBus(home)
    bus.send("user", "standup", type="directive", text="skip the retro section")

    make_agent(home, clock).tick()

    text = "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(transcript_path(home, "standup"))
    )
    assert "skip the retro section" in text
    assert not bus.pending("standup")


def test_directives_rejected_back_on_crash(home: Path, clock):
    write_config(home, mode="fail")
    seed_agent(home)
    bus = MessageBus(home)
    bus.send("user", "standup", type="directive", text="try again later")

    with pytest.raises(RuntimeError, match="exited 3"):
        make_agent(home, clock).tick()
    assert bus.pending("standup")  # back in new/ for the next tick


def test_actions_journal_per_agent_and_cap_applies(home: Path, clock):
    write_config(home, mode="agent_act")
    seed_agent(home)
    make_agent(home, clock, max_actions_per_run=1).tick()

    entries = fsio.read_jsonl(journal_path(home, "standup"))
    assert [e["action"] for e in entries] == ["board.post"]  # the note hit the cap
    assert {e["actor"] for e in entries} == {"standup"}
    assert fsio.read_jsonl(journal_path(home)) == []  # manager journal untouched
    text = "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(transcript_path(home, "standup"))
    )
    assert "standup action cap (1) reached" in text
