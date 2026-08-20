from __future__ import annotations

from pathlib import Path

from quorum.agent import AgentContext
from quorum.agents.steward import Steward, undo_moves
from quorum.config import Config, LLMConfig
from quorum.messages import MessageBus


def make_steward(home: Path, clock, settings: dict, llm_cfg=None) -> Steward:
    ctx = AgentContext(
        home=home, name="steward", settings=settings, config=Config(llm=llm_cfg), now=clock
    )
    return Steward(ctx)


def topic(home: Path):
    return MessageBus(home).read_topic("steward")


def test_propose_mode_posts_once(home: Path, clock, tmp_path: Path):
    watch = tmp_path / "downloads"
    watch.mkdir()
    (watch / "paper.pdf").write_text("x")
    settings = {
        "watch": [str(watch)],
        "apply": False,
        "rules": [{"match": "*.pdf", "dest": str(tmp_path / "papers")}],
    }
    s = make_steward(home, clock, settings)
    s.tick()
    s.tick()  # unchanged file: no duplicate proposal
    msgs = topic(home)
    assert len(msgs) == 1 and msgs[0].type == "steward.proposal"
    assert (watch / "paper.pdf").exists()  # nothing moved


def test_apply_mode_moves_with_undo_and_collision(home: Path, clock, tmp_path: Path):
    watch = tmp_path / "downloads"
    papers = tmp_path / "papers"
    watch.mkdir()
    papers.mkdir()
    (watch / "a.pdf").write_text("one")
    (papers / "a.pdf").write_text("existing")  # collision
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(papers)}],
    }
    make_steward(home, clock, settings).tick()

    assert not (watch / "a.pdf").exists()
    assert (papers / "a-1.pdf").read_text() == "one"  # collision suffix, no overwrite
    assert (papers / "a.pdf").read_text() == "existing"

    undone = undo_moves(home, last=1)
    assert undone and (watch / "a.pdf").read_text() == "one"
    assert not (papers / "a-1.pdf").exists()
    assert undo_moves(home, last=1) == []  # log consumed


def test_unmatched_reported_once_without_llm(home: Path, clock, tmp_path: Path):
    watch = tmp_path / "dl"
    watch.mkdir()
    (watch / "mystery.xyz").write_text("?")
    settings = {"watch": [str(watch)], "rules": [{"match": "*.pdf", "dest": str(tmp_path / "p")}]}
    s = make_steward(home, clock, settings)
    s.tick()
    s.tick()
    msgs = [m for m in topic(home) if m.type == "steward.unmatched"]
    assert len(msgs) == 1


def test_llm_classification_of_unmatched(home: Path, clock, tmp_path: Path, fake_llm):
    watch = tmp_path / "dl"
    watch.mkdir()
    (watch / "mystery.dat").write_text("?")
    papers = tmp_path / "papers"
    llm_cfg = LLMConfig(
        executable=fake_llm[0],
        args=fake_llm[1:],
        env={"FAKE_LLM_MODE": "ok", "FAKE_LLM_OUTPUT": str(papers)},
    )
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(papers)}],
    }
    make_steward(home, clock, settings, llm_cfg=llm_cfg).tick()
    assert (papers / "mystery.dat").exists()
    moved = [m for m in topic(home) if m.type == "steward.moved"]
    assert moved and moved[0].payload["via_llm"] is True


def test_llm_offlist_answer_is_skipped(home: Path, clock, tmp_path: Path, fake_llm):
    watch = tmp_path / "dl"
    watch.mkdir()
    (watch / "mystery.dat").write_text("?")
    llm_cfg = LLMConfig(
        executable=fake_llm[0],
        args=fake_llm[1:],
        env={"FAKE_LLM_MODE": "ok", "FAKE_LLM_OUTPUT": "/etc/passwd"},
    )
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(tmp_path / "papers")}],
    }
    make_steward(home, clock, settings, llm_cfg=llm_cfg).tick()
    assert (watch / "mystery.dat").exists()  # not moved anywhere
    assert not (tmp_path / "papers").exists()
