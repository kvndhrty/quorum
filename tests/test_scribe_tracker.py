from __future__ import annotations

import subprocess
from pathlib import Path

from quorum.agent import AgentContext
from quorum.agents.scribe import Scribe
from quorum.agents.tracker import Tracker
from quorum.config import Config, LLMConfig
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry


def make_ctx(home: Path, name: str, clock, settings=None, llm_cfg=None) -> AgentContext:
    config = Config(llm=llm_cfg)
    return AgentContext(home=home, name=name, settings=settings or {}, config=config, now=clock)


# -- tracker ---------------------------------------------------------------


def test_tracker_mtime_scan_and_stale_transitions(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    (pdir / "notes.txt").write_text("hi")
    ProjectRegistry(home).add(pdir, name="proj")

    tracker = Tracker(make_ctx(home, "tracker", clock, settings={"stale_days": 10}))
    tracker.tick()
    status = (home / "state/projects/proj/status.json").read_text()
    assert '"source": "mtime"' in status

    # file was just written -> active; advance 11 days with no changes -> stale posts once
    clock.advance(days=11)
    tracker.tick()
    tracker.tick()
    msgs = MessageBus(home).read_topic("projects")
    assert [m.type for m in msgs] == ["project.stale"]

    # touch the project (mtime aligned to the fake clock) -> active again
    import os

    (pdir / "new.txt").write_text("fresh")
    os.utime(pdir / "new.txt", (clock().timestamp(), clock().timestamp()))
    tracker.tick()
    msgs = MessageBus(home).read_topic("projects")
    assert [m.type for m in msgs] == ["project.stale", "project.active"]


def test_tracker_git_scan(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "gitproj"
    pdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=pdir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "initial work"], cwd=pdir, check=True)
    ProjectRegistry(home).add(pdir, name="gitproj")
    Tracker(make_ctx(home, "tracker", clock)).tick()
    import json

    status = json.loads((home / "state/projects/gitproj/status.json").read_text())
    assert status["source"] == "git"
    assert status["last_activity_at"] is not None


# -- scribe ------------------------------------------------------------------


def test_scribe_deterministic_brief(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="Big Paper", deadline="2026-08-25")
    bus = MessageBus(home, now=clock)
    bus.post("sentinel", "reminders", "deadline.approaching", text="Big Paper due in 5 day(s)")
    bus.post("tracker", "projects", "project.stale", text="Old Thing has gone quiet")

    scribe = Scribe(make_ctx(home, "scribe", clock))
    scribe.tick()

    brief = (home / "briefs" / f"{clock().date()}.md").read_text()
    assert "## Deadlines" in brief and "Big Paper due in 5 day(s)" in brief
    assert "| Big Paper |" in brief
    assert "Old Thing has gone quiet" in brief
    assert "canned narrative" not in brief  # no LLM configured

    system = bus.read_topic("system")
    assert any(m.type == "brief.ready" for m in system)


def test_scribe_llm_narrative_prepended(home: Path, clock, tmp_path: Path, fake_llm):
    ProjectRegistry(home).add(tmp_path, name="P", deadline="2026-08-30")
    llm_cfg = LLMConfig(
        executable=fake_llm[0], args=fake_llm[1:],
        env={"FAKE_LLM_MODE": "ok", "FAKE_LLM_OUTPUT": "A calm narrative about your day."},
    )
    Scribe(make_ctx(home, "scribe", clock, llm_cfg=llm_cfg)).tick()
    brief = (home / "briefs" / f"{clock().date()}.md").read_text()
    assert brief.splitlines()[0].startswith("# Daily brief")
    assert "A calm narrative about your day." in brief
    assert "## Projects" in brief  # deterministic sections still present


def test_scribe_llm_failure_still_completes(home: Path, clock, tmp_path: Path, fake_llm):
    ProjectRegistry(home).add(tmp_path, name="P2", deadline="2026-08-30")
    llm_cfg = LLMConfig(executable=fake_llm[0], args=fake_llm[1:], env={"FAKE_LLM_MODE": "fail"})
    Scribe(make_ctx(home, "scribe", clock, llm_cfg=llm_cfg)).tick()
    brief = (home / "briefs" / f"{clock().date()}.md").read_text()
    assert "## Projects" in brief and "| P2 |" in brief


def test_user_prompt_template_overrides_default(home: Path, clock, tmp_path: Path, fake_llm):
    (home / "prompts" / "scribe-brief.md").write_text("CUSTOM TEMPLATE {date}\n{data}")
    ProjectRegistry(home).add(tmp_path, name="P3")
    llm_cfg = LLMConfig(
        executable=fake_llm[0], args=fake_llm[1:], env={"FAKE_LLM_MODE": "echo"}
    )
    Scribe(make_ctx(home, "scribe", clock, llm_cfg=llm_cfg)).tick()
    brief = (home / "briefs" / f"{clock().date()}.md").read_text()
    assert "CUSTOM TEMPLATE" in brief  # echo mode returns the rendered prompt
