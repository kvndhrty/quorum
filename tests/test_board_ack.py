"""Attention acknowledgement: `quorum board ack`.

The board carries no read-state by design, so "I have seen this one" is
*archival*: acking moves the message into `messages/archive/` and leaves the
topic. Every test here therefore checks both halves — it left the live view,
and the history still has it, with the `created_at` it was posted with.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, views
from quorum.cli import app
from quorum.messages import Message, MessageBus

runner = CliRunner()


def post_with_id(home: Path, message_id: str, text: str, topic: str = "attention") -> Message:
    """A board message with a chosen id — ULID heads are time-derived, so two
    posted a millisecond apart *usually* share a prefix and occasionally do
    not. Ambiguity is the behavior under test; it must not be the weather."""
    msg = Message.model_validate(
        {"from": "manager", "topic": topic, "type": "escalation", "id": message_id,
         "payload": {"text": text}}
    )
    fsio.atomic_write_json(home / "messages" / "board" / topic / msg.filename(), msg.dump())
    return msg


def archive_lines(home: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((home / "messages" / "archive").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            out.extend(json.loads(line) for line in f if line.strip())
    return out


# -- resolution ------------------------------------------------------------


def test_resolves_by_full_id_prefix_and_short_suffix(home: Path):
    bus = MessageBus(home)
    msg = bus.post("manager", "attention", "escalation", text="need a human")

    for handle in (msg.id, msg.id[:8], msg.short_id, msg.short_id.upper(), f" {msg.short_id} "):
        found, path = bus.resolve_board_message(handle)
        assert found.id == msg.id
        assert path.name == msg.filename()


def test_unknown_and_ambiguous_handles_both_raise(home: Path):
    bus = MessageBus(home)
    post_with_id(home, "01SHAREDHEAD0000000000000A", "one")
    post_with_id(home, "01SHAREDHEAD0000000000000B", "two")

    with pytest.raises(KeyError):
        bus.resolve_board_message("ZZZZZZ")
    with pytest.raises(KeyError):
        bus.resolve_board_message("")
    with pytest.raises(ValueError, match="ambiguous"):
        bus.resolve_board_message("01SHAREDHEAD")
    # ...but a full id is never ambiguous, even against its own prefix-mates
    assert bus.resolve_board_message("01SHAREDHEAD0000000000000A")[0].payload["text"] == "one"


def test_a_topic_scopes_the_search(home: Path):
    bus = MessageBus(home)
    msg = bus.post("manager", "attention", "escalation", text="escalated")
    with pytest.raises(KeyError):
        bus.resolve_board_message(msg.short_id, topic="notes")
    assert bus.resolve_board_message(msg.short_id, topic="attention")[0].id == msg.id


def test_ack_archives_the_message_and_keeps_its_created_at(home: Path):
    old = MessageBus(home, now=lambda: fsio.utc_now() - timedelta(days=3))
    msg = old.post("manager", "attention", "escalation", text="three days ago")

    acked = MessageBus(home).ack_board_message(msg.short_id)
    assert acked.id == msg.id
    assert MessageBus(home).read_topic("attention") == []
    [line] = archive_lines(home)
    assert line["created_at"] == msg.created_at
    assert line["payload"]["text"] == "three days ago"


# -- the CLI ---------------------------------------------------------------


def test_ack_drops_one_escalation_from_the_banner_and_the_archive_holds_it(home: Path):
    """The issue's acceptance path: post -> banner 1 -> ack -> banner 0 ->
    archive contains it."""
    bus = MessageBus(home)
    msg = bus.post("manager", "attention", "escalation", text="a human is needed")
    assert views.attention_summary(home)["count"] == 1

    result = runner.invoke(app, ["board", "ack", msg.short_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert msg.short_id in result.output
    assert views.attention_summary(home)["count"] == 0
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["a human is needed"]


def test_ack_leaves_the_other_escalations_alone(home: Path):
    bus = MessageBus(home)
    bus.post("manager", "attention", "escalation", text="first")
    second = bus.post("manager", "attention", "escalation", text="second")
    bus.post("manager", "attention", "escalation", text="third")

    result = runner.invoke(app, ["board", "ack", second.short_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    live = [m.payload["text"] for m in bus.read_topic("attention")]
    assert live == ["first", "third"]
    assert views.attention_summary(home)["count"] == 2


def test_ack_of_an_unknown_id_fails_loudly(home: Path):
    MessageBus(home).post("manager", "attention", "escalation", text="still here")
    result = runner.invoke(app, ["board", "ack", "ZZZZZZ", "--home", str(home)])
    assert result.exit_code != 0
    assert "no live board message" in result.output
    assert views.attention_summary(home)["count"] == 1
    assert archive_lines(home) == []


def test_ack_of_an_ambiguous_id_fails_loudly(home: Path):
    post_with_id(home, "01SHAREDHEAD0000000000000A", "one")
    post_with_id(home, "01SHAREDHEAD0000000000000B", "two")

    result = runner.invoke(app, ["board", "ack", "01SHAREDHEAD", "--home", str(home)])
    assert result.exit_code != 0
    assert "ambiguous" in result.output
    assert views.attention_summary(home)["count"] == 2


def test_ack_dry_run_changes_nothing(home: Path):
    msg = MessageBus(home).post("manager", "attention", "escalation", text="untouched")
    result = runner.invoke(
        app, ["board", "ack", msg.short_id, "--dry-run", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "would ack" in result.output
    assert views.attention_summary(home)["count"] == 1
    assert archive_lines(home) == []


def test_ack_refuses_before_without_all(home: Path):
    msg = MessageBus(home).post("manager", "attention", "escalation", text="here")
    result = runner.invoke(
        app, ["board", "ack", msg.short_id, "--before", "7d", "--home", str(home)]
    )
    assert result.exit_code != 0
    assert "--before applies to --all" in result.output
    assert views.attention_summary(home)["count"] == 1


def test_ack_all_is_board_clear(home: Path):
    """The alias is the same sweep, not a second implementation."""
    bus = MessageBus(home)
    bus.post("manager", "attention", "escalation", text="one")
    bus.post("manager", "attention", "escalation", text="two")

    result = runner.invoke(app, ["board", "ack", "--all", "attention", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "archived 2 message(s) from attention" in result.output
    assert views.attention_summary(home)["count"] == 0
    assert sorted(m["payload"]["text"] for m in archive_lines(home)) == ["one", "two"]


def test_ack_all_honours_before_and_dry_run(home: Path):
    old = MessageBus(home, now=lambda: fsio.utc_now() - timedelta(days=30))
    old.post("manager", "attention", "escalation", text="ancient")
    MessageBus(home).post("manager", "attention", "escalation", text="recent")

    dry = runner.invoke(
        app, ["board", "ack", "--all", "attention", "--dry-run", "--home", str(home)]
    )
    assert dry.exit_code == 0, dry.output
    assert "would archive 2" in dry.output
    assert archive_lines(home) == []

    result = runner.invoke(
        app,
        ["board", "ack", "--all", "attention", "--before", "7d", "--yes", "--home", str(home)],
    )
    assert result.exit_code == 0, result.output
    assert [m.payload["text"] for m in MessageBus(home).read_topic("attention")] == ["recent"]
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["ancient"]


def test_board_read_prints_the_handle_an_ack_needs(home: Path):
    msg = MessageBus(home).post("manager", "attention", "escalation", text="ack me")
    result = runner.invoke(app, ["board", "read", "attention", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert msg.short_id in result.output


def test_an_agents_ack_lands_in_its_journal(home: Path, monkeypatch):
    """`board ack` mutates, so a harness-driven actor journals it like every
    other mutating command — the manager acking its own escalation is exactly
    the case the journal exists to make visible."""
    msg = MessageBus(home).post("manager", "attention", "escalation", text="handled")
    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "run-1")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "10")

    result = runner.invoke(app, ["board", "ack", msg.short_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    entries = fsio.read_jsonl(home / "state" / "manager" / "journal.jsonl")
    assert [e["action"] for e in entries] == ["board.ack"]
    assert entries[0]["target"] == msg.short_id


def test_topic_scopes_the_cli_ack(home: Path):
    """Two topics can hand out the same short id; `--topic` is how you say
    which board you meant."""
    bus = MessageBus(home)
    msg = bus.post("manager", "attention", "escalation", text="escalated")
    result = runner.invoke(
        app, ["board", "ack", msg.short_id, "--topic", "notes", "--home", str(home)]
    )
    assert result.exit_code != 0
    assert "on notes" in result.output
    assert views.attention_summary(home)["count"] == 1

    result = runner.invoke(
        app, ["board", "ack", msg.short_id, "--topic", "attention", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert views.attention_summary(home)["count"] == 0


def test_ack_of_a_message_that_vanishes_mid_ack_stays_tidy(home: Path, monkeypatch):
    """Resolution hands back a path; between that and the archive the janitor
    (or a second `board ack`, or the web panel) can take the file. Acking the
    path we already have makes that a no-op instead of a traceback from a
    second resolution — and the message is archived once, not twice."""
    bus = MessageBus(home)
    msg = bus.post("manager", "attention", "escalation", text="handled elsewhere")
    real = MessageBus.resolve_board_message

    def racing(self, handle, topic=None):
        found = real(self, handle, topic)
        MessageBus(home).archive_topic("attention")  # someone else got there first
        return found

    monkeypatch.setattr(MessageBus, "resolve_board_message", racing)

    result = runner.invoke(app, ["board", "ack", msg.short_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert views.attention_summary(home)["count"] == 0
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["handled elsewhere"]


def test_ack_all_refuses_a_topic_option(home: Path):
    """With `--all` the argument *is* the topic, so a `--topic` next to it can
    only be a misunderstanding — and silently sweeping the wrong board is the
    one outcome an ack must never have."""
    bus = MessageBus(home)
    bus.post("manager", "attention", "escalation", text="still here")
    bus.post("manager", "notes", "note", text="also still here")

    result = runner.invoke(
        app,
        ["board", "ack", "--all", "attention", "--topic", "notes", "--yes", "--home", str(home)],
    )
    assert result.exit_code != 0
    assert "--topic does not apply to --all" in result.output
    assert views.attention_summary(home)["count"] == 1
    assert len(MessageBus(home).read_topic("notes")) == 1
    assert archive_lines(home) == []
