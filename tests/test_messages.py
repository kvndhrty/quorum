from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path

import pytest

from quorum.messages import Message, MessageBus


def test_message_requires_exactly_one_destination():
    with pytest.raises(ValueError):
        Message.model_validate({"from": "a"})
    with pytest.raises(ValueError):
        Message.model_validate({"from": "a", "to": "b", "topic": "t"})


def test_board_roundtrip_and_ordering(home: Path, clock):
    bus = MessageBus(home, now=clock)
    bus.post("sentinel", "reminders", "deadline.approaching", text="7 days left")
    clock.advance(minutes=5)
    bus.post("tracker", "reminders", "note", text="second")
    msgs = bus.read_topic("reminders")
    assert [m.payload["text"] for m in msgs] == ["7 days left", "second"]
    assert msgs[0].sender == "sentinel"
    # on-disk format uses the "from" key
    first = sorted((home / "messages/board/reminders").glob("*.json"))[0]
    raw = json.loads(first.read_text())
    assert raw["from"] == "sentinel" and raw["v"] == 1


def test_cursor_resumption(home: Path, clock):
    bus = MessageBus(home, now=clock)
    bus.post("a", "t", text="1")
    clock.advance(minutes=1)
    bus.post("a", "t", text="2")
    msgs, cursor = bus.read_after_cursor("t", None)
    assert [m.payload["text"] for m in msgs] == ["1", "2"]
    msgs2, cursor2 = bus.read_after_cursor("t", cursor)
    assert msgs2 == [] and cursor2 == cursor
    clock.advance(minutes=1)
    bus.post("a", "t", text="3")
    msgs3, _ = bus.read_after_cursor("t", cursor)
    assert [m.payload["text"] for m in msgs3] == ["3"]


def test_inbox_claim_race_single_winner(home: Path):
    bus = MessageBus(home)
    bus.send("scout", "steward", text="one message")
    winners = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        winners.extend(list(bus.claim("steward")))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1
    winners[0].ack()
    assert list(bus.claim("steward")) == []
    # ack archived it
    assert list((home / "messages/archive").glob("*.jsonl.gz"))


def test_reject_returns_to_new(home: Path):
    bus = MessageBus(home)
    bus.send("a", "b", text="retry me")
    claimed = next(iter(bus.claim("b")))
    claimed.reject()
    again = list(bus.claim("b"))
    assert len(again) == 1 and again[0].message.payload["text"] == "retry me"


def test_recover_stale_claims(home: Path, clock):
    bus = MessageBus(home, now=clock)
    bus.send("a", "b", text="orphaned")
    next(iter(bus.claim("b")))  # claim but never ack (simulated crash)
    clock.advance(hours=2)
    assert bus.recover_stale_claims() == 1
    assert len(list(bus.claim("b"))) == 1


def test_archive_old_respects_retention_and_ttl(home: Path, clock):
    bus = MessageBus(home, now=clock)
    bus.post("a", "t", text="ancient")
    clock.advance(days=40)
    bus.post("a", "t", text="fresh")
    bus.post("a", "t", text="short-lived", ttl_days=0)
    clock.advance(days=1)
    archived = bus.archive_old(retention_days=30)
    assert archived == 2  # "ancient" (>30d) and "short-lived" (ttl 0)
    remaining = [m.payload["text"] for m in bus.read_topic("t")]
    assert remaining == ["fresh"]
    lines = [
        json.loads(l)
        for archive in (home / "messages/archive").glob("*.jsonl.gz")
        for l in gzip.open(archive, "rt")
    ]
    assert {l["payload"]["text"] for l in lines} == {"ancient", "short-lived"}
