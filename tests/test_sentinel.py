from __future__ import annotations

from pathlib import Path

from quorum.agent import AgentContext
from quorum.agents.sentinel import Sentinel
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry


def make_sentinel(home: Path, clock) -> Sentinel:
    ctx = AgentContext(home=home, name="sentinel", now=clock)
    return Sentinel(ctx)


def days_from_now(clock, days: int) -> str:
    return str(clock().date().fromordinal(clock().date().toordinal() + days))


def reminders(home: Path) -> list:
    return MessageBus(home).read_topic("reminders")


def test_tier_escalation_no_double_fire(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="proj", deadline=days_from_now(clock, 10))
    s = make_sentinel(home, clock)

    s.tick()  # 10 days out -> tier 14
    assert len(reminders(home)) == 1
    assert reminders(home)[0].payload["tier"] == 14

    s.tick()  # same day, re-run: no double post
    assert len(reminders(home)) == 1

    clock.advance(days=4)  # 6 days out -> tier 7
    s.tick()
    assert len(reminders(home)) == 2
    assert reminders(home)[1].payload["tier"] == 7

    clock.advance(days=6)  # due today -> tier 0
    s.tick()
    assert reminders(home)[-1].payload["tier"] == 0
    assert "TODAY" in reminders(home)[-1].payload["text"]


def test_overdue_posts_daily(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "late"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="late", deadline=days_from_now(clock, -2))
    s = make_sentinel(home, clock)
    s.tick()
    s.tick()  # same day: once only
    assert len(reminders(home)) == 1
    assert reminders(home)[0].type == "deadline.overdue"
    clock.advance(days=1)
    s.tick()
    assert len(reminders(home)) == 2


def test_deadline_change_resets_ladder(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    reg = ProjectRegistry(home)
    reg.add(pdir, name="p", deadline=days_from_now(clock, 3))
    s = make_sentinel(home, clock)
    s.tick()  # tier 3
    assert reminders(home)[-1].payload["tier"] == 3
    clock.advance(minutes=1)
    reg.update("p", deadline=days_from_now(clock, 12))
    s.tick()  # new deadline, 12 days -> tier 14 fires fresh
    assert reminders(home)[-1].payload["tier"] == 14


def test_no_deadline_no_noise(home: Path, clock, tmp_path: Path):
    pdir = tmp_path / "nodl"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="nodl")
    make_sentinel(home, clock).tick()
    assert reminders(home) == []
