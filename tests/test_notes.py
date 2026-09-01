"""The notebook: the manager's *separate* memory buffer.

Its whole reason to exist is isolation — from the journal (which a busy run
scrolls), from the board (which anything may post to), and from the digest's
task budget (which grows with the number of live tasks). These tests hold
those three fences up, plus the CLI verbs that write and retire notes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, notes, views
from quorum.actor import journal_path, notes_path
from quorum.cli import app

runner = CliRunner()

NOTE = "a3f2k9's PR is waiting on the human — do not relaunch it"


def invoke(home: Path, *args: str):
    return runner.invoke(app, [*args, "--home", str(home)])


def lines(path: Path) -> list[dict]:
    return fsio.read_jsonl(path)


def test_remember_notes_and_forget_round_trip(home: Path):
    r = invoke(home, "manager", "remember", NOTE)
    assert r.exit_code == 0, r.output
    handle = r.output.split("(")[1].split(")")[0]

    written = lines(notes_path(home))
    assert len(written) == 1
    assert written[0]["text"] == NOTE and written[0]["sender"] == "user"
    assert notes.short_id(written[0]["id"]) == handle

    r = invoke(home, "manager", "notes")
    assert notes.SECTION_HEADER in r.output and NOTE in r.output
    assert f"({handle})" in r.output

    r = invoke(home, "manager", "forget", handle)
    assert r.exit_code == 0, r.output
    # append-only: the note stays on disk, a tombstone hides it
    assert len(lines(notes_path(home))) == 2
    assert notes.active(home) == []
    assert NOTE not in invoke(home, "manager", "notes").output

    # and the human's two writes are auditable in the manager's journal
    actions = [e["action"] for e in lines(journal_path(home))]
    assert actions == ["remember", "forget"]


def test_forget_resolves_a_prefix_and_rejects_an_unknown_handle(home: Path):
    entry = notes.remember(home, NOTE)
    assert notes.resolve(home, entry["id"][:8])["id"] == entry["id"]
    assert notes.resolve(home, notes.short_id(entry["id"]).upper())["id"] == entry["id"]

    r = invoke(home, "manager", "forget", "nosuch")
    assert r.exit_code == 1
    assert "no note matching" in r.output


def test_an_expired_note_retires_itself(home: Path):
    now = fsio.utc_now()
    notes.remember(home, "the codex harness is rate-limited today", ttl_days=2, now=now)
    assert len(notes.active(home, now=now + timedelta(days=1))) == 1
    assert notes.active(home, now=now + timedelta(days=3)) == []


def test_a_task_or_another_agent_may_not_write_the_managers_notebook(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Tasks reach the manager with `task report` and the board. Letting them
    write into its memory would recreate exactly the crowding a separate
    buffer exists to prevent."""
    monkeypatch.setenv("QUORUM_ACTOR", "task-01ABCDEF")
    r = invoke(home, "manager", "remember", "let me into your head")
    assert r.exit_code == 1
    assert "refused" in r.output and "task report" in r.output
    assert not notes_path(home).exists()

    monkeypatch.setenv("QUORUM_ACTOR", "babysitter")
    assert invoke(home, "manager", "remember", "not mine to write").exit_code == 1
    assert not notes_path(home).exists()

    # ... but an agent may write its own notebook, under state/agents/<name>/
    r = invoke(home, "manager", "remember", "CI is red on main", "--agent", "babysitter")
    assert r.exit_code == 0, r.output
    assert lines(notes_path(home, "babysitter"))[0]["sender"] == "babysitter"
    assert not notes_path(home).exists()


def test_the_manager_actor_and_an_untagged_human_both_write(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01TESTRUN")
    assert invoke(home, "manager", "remember", NOTE).exit_code == 0

    monkeypatch.delenv("QUORUM_ACTOR")
    monkeypatch.delenv("QUORUM_ACTOR_RUN")
    assert invoke(home, "manager", "remember", "keep at most two tasks running").exit_code == 0

    written = notes.active(home)
    assert [e["sender"] for e in written] == ["manager", "user"]
    assert written[0]["run_id"] == "01TESTRUN"


def test_a_note_is_capped_per_run_like_any_other_agent_action(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01CAPRUN")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "1")
    assert invoke(home, "manager", "remember", "first").exit_code == 0
    r = invoke(home, "manager", "remember", "second")
    assert r.exit_code == 1 and "action cap (1) reached" in r.output
    assert [e["text"] for e in notes.active(home)] == ["first"]


def test_over_its_cap_the_notebook_keeps_the_newest_and_says_what_it_dropped(home: Path):
    for i in range(notes.NOTES_MAX_ENTRIES + 3):
        notes.remember(home, f"standing fact {i}")

    section = notes.digest_section(home)
    assert section[0] == notes.SECTION_HEADER
    assert "3 older note(s) dropped" in section[1]
    assert "consolidate" in section[1]  # the manager is told what to do about it
    assert len(section) == 2 + notes.NOTES_MAX_ENTRIES
    assert "standing fact 0" not in "\n".join(section)
    assert f"standing fact {notes.NOTES_MAX_ENTRIES + 2}" in section[-1]


def test_the_newest_note_survives_however_long_the_others_are(home: Path):
    for i in range(6):
        notes.remember(home, f"{i} " + "x" * notes.NOTE_MAX_CHARS)
    notes.remember(home, "the one that matters")

    section = notes.digest_section(home)
    body = "\n".join(section[1:])
    assert "the one that matters" in section[-1]
    assert len(body) <= notes.NOTES_MAX_BYTES
    # long notes are truncated, never dropped silently mid-line
    assert "…" in body


def test_an_empty_notebook_teaches_the_command(home: Path):
    assert notes.digest_section(home) == [notes.SECTION_HEADER, notes.EMPTY_LINE]
    assert "quorum manager remember" in notes.EMPTY_LINE


def test_agent_detail_carries_the_notebook_for_the_views(home: Path):
    notes.remember(home, NOTE)
    detail = views.agent_detail(home, "manager")
    assert [e["text"] for e in detail["notes"]] == [NOTE]
    assert NOTE in detail["notes_text"]
    assert detail["notes_text"].startswith(notes.SECTION_HEADER)


def test_a_torn_or_foreign_line_never_breaks_a_reader(home: Path):
    """A digest build must survive anything on that file: the alternative is
    one hand-edited line failing every manager tick, forever."""
    notes.remember(home, NOTE)
    with open(notes_path(home), "a", encoding="utf-8") as f:
        f.write(json.dumps({"no": "id"}) + "\n")
        f.write(json.dumps({"id": 7, "ts": "2026-09-01T00:00:00Z", "text": "int id"}) + "\n")
        f.write(json.dumps({"id": None, "text": "null id"}) + "\n")
        f.write(json.dumps(["not", "even", "an", "object"]) + "\n")
        f.write('{"id": "01BROKEN", "ts": "not-a-date", "text": "x", "ttl_days": 1}\n')
        f.write("{half a line\n")

    section = "\n".join(notes.digest_section(home))
    assert NOTE in section
    assert "int id" not in section and "null id" not in section
    # every reader over the same file, not just the digest ("01BROKEN" keeps
    # its note: an unparseable ttl loses the expiry, never the text)
    assert [e["text"] for e in notes.active(home)] == [NOTE, "x"]
    assert notes.resolve(home, notes.short_id(notes.active(home)[0]["id"]))["text"] == NOTE
    r = invoke(home, "manager", "notes")
    assert r.exit_code == 0 and NOTE in r.output
    assert views.agent_detail(home, "manager")["notes_text"].count(NOTE) == 1


def test_a_notebook_owner_is_a_valid_agent_name(home: Path):
    """`--agent` becomes a path component under state/agents/ — on the read
    side as much as the write side."""
    r = invoke(home, "manager", "remember", "escape", "--agent", "../../etc")
    assert r.exit_code == 1 and "invalid agent name" in r.output
    assert not (home.parent.parent / "etc").exists()

    r = invoke(home, "manager", "forget", "abc123", "--agent", "../../etc")
    assert r.exit_code == 1 and "invalid agent name" in r.output

    r = invoke(home, "manager", "notes", "--agent", "../../etc")
    assert r.exit_code == 1 and "invalid agent name" in r.output
    assert notes.SECTION_HEADER not in r.output


def test_an_empty_handle_is_refused_rather_than_matching_everything(home: Path):
    notes.remember(home, NOTE)
    notes.remember(home, "and another")
    with pytest.raises(notes.NotebookError, match="handle is required"):
        notes.resolve(home, "   ")
    r = invoke(home, "manager", "forget", "")
    assert r.exit_code == 1 and "handle is required" in r.output
    assert len(notes.active(home)) == 2


def test_forget_honours_the_clock_it_is_given(home: Path):
    """`forget` resolves through `active`, which filters on `now`; a note that
    has expired by the caller's clock is no longer forgettable."""
    now = fsio.utc_now()
    entry = notes.remember(home, "rate-limited today", ttl_days=1, now=now)
    handle = notes.short_id(entry["id"])
    with pytest.raises(notes.NotebookError, match="no note matching"):
        notes.forget(home, handle, now=now + timedelta(days=2))
    notes.forget(home, handle, now=now)
    assert notes.active(home, now=now) == []


def test_a_notebook_past_the_scan_window_says_what_it_could_not_read(home: Path):
    """The tail is bounded, so a big enough file hides its oldest notes —
    permanent ones included. The manager is told, and left to judge."""
    notes.remember(home, "the oldest standing fact")
    padding = {"id": "01PADPADPAD", "ts": "2026-01-01T00:00:00Z", "retired": True,
               "pad": "x" * 4000}
    while notes.unscanned_bytes(home) == 0:
        fsio.append_jsonl(notes_path(home), padding)
    notes.remember(home, "today's fact")

    assert notes.unscanned_bytes(home) > 0
    section = notes.digest_section(home)
    assert section[0] == notes.SECTION_HEADER
    assert "not scanned" in section[1] and "invisible" in section[1]
    assert "the oldest standing fact" not in "\n".join(section)  # what it warns about
    assert "today's fact" in section[-1]
    assert "not scanned" in invoke(home, "manager", "notes").output
    assert "not scanned" in views.agent_detail(home, "manager")["notes_text"]


def test_a_small_notebook_says_nothing_about_the_scan_window(home: Path):
    notes.remember(home, NOTE)
    assert notes.unscanned_bytes(home) == 0
    assert "not scanned" not in "\n".join(notes.digest_section(home))


def test_a_refused_write_is_journaled_too(home: Path, monkeypatch: pytest.MonkeyPatch):
    """An agent reaching for someone else's notebook is exactly the kind of
    thing the next digest should show; the refusal must not be invisible."""
    monkeypatch.setenv("QUORUM_ACTOR", "babysitter")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01REFUSED")
    assert invoke(home, "manager", "remember", "not mine").exit_code == 1
    assert invoke(home, "manager", "forget", "abc123").exit_code == 1

    journaled = lines(journal_path(home, "babysitter"))
    assert [e["action"] for e in journaled] == ["remember.refused", "forget.refused"]
    assert journaled[0]["target"] == "manager" and journaled[0]["run"] == "01REFUSED"
    assert not notes_path(home).exists()
