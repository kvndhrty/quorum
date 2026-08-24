from __future__ import annotations

from pathlib import Path

from quorum import fsio
from quorum.agent import AgentContext
from quorum.agents.steward import MAX_MOVE_ATTEMPTS, Steward, undo_moves
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


def test_flip_to_apply_acts_on_already_proposed_file(home: Path, clock, tmp_path: Path):
    """The advertised workflow: propose, review the board, then set apply=true.

    Nothing about the file changes in between, so a dedup keyed only on mtime
    would skip it forever and the backlog would never move.
    """
    watch = tmp_path / "downloads"
    papers = tmp_path / "papers"
    watch.mkdir()
    (watch / "paper.pdf").write_text("draft")
    rules = [{"match": "*.pdf", "dest": str(papers)}]

    make_steward(home, clock, {"watch": [str(watch)], "apply": False, "rules": rules}).tick()
    assert [m.type for m in topic(home)] == ["steward.proposal"]

    make_steward(home, clock, {"watch": [str(watch)], "apply": True, "rules": rules}).tick()

    assert not (watch / "paper.pdf").exists()
    assert (papers / "paper.pdf").read_text() == "draft"
    assert [m.type for m in topic(home)] == ["steward.proposal", "steward.moved"]
    undo_log = fsio.read_jsonl(home / "state" / "steward" / "undo.jsonl")
    assert [r["dest"] for r in undo_log] == [str(papers / "paper.pdf")]


def test_legacy_mtime_only_state_still_flips_to_apply(home: Path, clock, tmp_path: Path):
    """State written before actions were tracked stored a bare mtime; an upgrade
    must not leave those files wedged in proposed-forever limbo."""
    watch = tmp_path / "downloads"
    papers = tmp_path / "papers"
    watch.mkdir()
    stale = watch / "paper.pdf"
    stale.write_text("draft")
    fsio.atomic_write_json(
        home / "state" / "agents" / "steward" / "state.json",
        {"seen": {str(stale): stale.stat().st_mtime}},
    )

    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(papers)}],
    }
    make_steward(home, clock, settings).tick()
    assert (papers / "paper.pdf").read_text() == "draft"


def test_apply_mode_does_not_rescan_settled_file(home: Path, clock, tmp_path: Path):
    """The dedup must still do its real job: an unmatched file is reported once
    even in apply mode, not once per tick."""
    watch = tmp_path / "dl"
    watch.mkdir()
    (watch / "mystery.xyz").write_text("?")
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(tmp_path / "p")}],
    }
    s = make_steward(home, clock, settings)
    s.tick()
    clock.advance(minutes=1)
    s.tick()
    assert len([m for m in topic(home) if m.type == "steward.unmatched"]) == 1


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


def test_failed_move_is_retried_then_abandoned(home: Path, clock, tmp_path: Path, monkeypatch):
    """A move that keeps failing must not put steward.error on the board every
    tick: report once, retry a bounded number of times, then say so and stop."""
    watch = tmp_path / "downloads"
    watch.mkdir()
    (watch / "a.pdf").write_text("one")
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(tmp_path / "papers")}],
    }
    s = make_steward(home, clock, settings)

    def boom(*args, **kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr("quorum.agents.steward.shutil.move", boom)

    for _ in range(6):  # well past the budget
        clock.advance(hours=1)  # an hourly agent really does tick an hour apart
        s.tick()

    errors = [m for m in topic(home) if m.type == "steward.error"]
    assert len(errors) == 2, [m.payload["text"] for m in errors]
    assert errors[0].payload["attempts"] == 1
    assert errors[1].payload["gave_up"] is True
    assert errors[1].payload["attempts"] == MAX_MOVE_ATTEMPTS
    assert (watch / "a.pdf").exists()  # never moved, never deleted


def test_touching_an_abandoned_file_restarts_its_retry_budget(
    home: Path, clock, tmp_path: Path, monkeypatch
):
    watch = tmp_path / "downloads"
    papers = tmp_path / "papers"
    watch.mkdir()
    (watch / "a.pdf").write_text("one")
    settings = {
        "watch": [str(watch)],
        "apply": True,
        "rules": [{"match": "*.pdf", "dest": str(papers)}],
    }
    s = make_steward(home, clock, settings)

    def boom(*args, **kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr("quorum.agents.steward.shutil.move", boom)
    for _ in range(MAX_MOVE_ATTEMPTS + 2):
        clock.advance(hours=1)
        s.tick()
    assert len([m for m in topic(home) if m.type == "steward.error"]) == 2

    # the destination becomes writable again and the file is touched
    monkeypatch.undo()
    clock.advance(hours=1)
    (watch / "a.pdf").write_text("one, changed")
    s.tick()
    assert (papers / "a.pdf").exists()
    assert [m.type for m in topic(home)][-1] == "steward.moved"
