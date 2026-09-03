"""Filesystem message bus: a public board plus maildir-style direct inboxes.

Board:  messages/board/<topic>/<utc>-<ULID>.json — append-only fan-out.
        Filenames sort chronologically, so readers need no index; each
        consumer keeps its own cursor (last filename seen), so any number
        of readers coexist without coordination.
Inbox:  messages/inbox/<agent>/new/ -> cur/ — a consumer claims a message
        with os.rename(), which is atomic, so exactly one claimant wins
        even across processes. Processed messages are acked (deleted after
        being copied to the archive).

Everything is a plain JSON file written via tmp+rename, so the whole bus is
inspectable with ls/cat and works under a filesystem-only sandbox.
"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import fsio

PROTOCOL_VERSION = 1

# How long a claimed-but-never-acked message sits in cur/ before the
# supervisor's hourly janitor decides its consumer crashed and returns it to
# new/. `quorum doctor` reports claims older than this, so the two agree on
# what "stuck" means.
STALE_CLAIM_GRACE = timedelta(hours=1)


class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    v: int = PROTOCOL_VERSION
    id: str = Field(default_factory=fsio.ulid)
    sender: str = Field(alias="from")
    to: str | None = None
    topic: str | None = None
    type: str = "note"
    created_at: str = Field(default_factory=lambda: fsio.iso(fsio.utc_now()))
    ttl_days: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _direct_xor_board(self) -> Message:
        if (self.to is None) == (self.topic is None):
            raise ValueError("exactly one of 'to' or 'topic' must be set")
        self.payload.setdefault("text", "")
        return self

    @property
    def created(self) -> datetime:
        return fsio.parse_iso(self.created_at)

    @property
    def short_id(self) -> str:
        """The handle a human types: the ULID's random tail, like a task's
        `short_id` (the head is a shared timestamp, so it discriminates
        nothing)."""
        return self.id[-6:].lower()

    def filename(self) -> str:
        return f"{fsio.compact_ts(self.created)}-{self.id}.json"

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class ClaimedMessage:
    """A direct message moved to cur/; call ack() when fully processed."""

    def __init__(self, message: Message, path: Path, archive_dir: Path):
        self.message = message
        self.path = path
        self._archive_dir = archive_dir

    def ack(self) -> None:
        _archive_one(self._archive_dir, self.message.dump())
        self.path.unlink(missing_ok=True)

    def reject(self) -> None:
        """Return the message to new/ for a later attempt."""
        target = self.path.parent.parent / "new" / self.path.name
        try:
            os.rename(self.path, target)
        except OSError:
            pass


class MessageBus:
    def __init__(self, home: Path, now: Any = None):
        self.home = Path(home)
        self.board_dir = self.home / "messages" / "board"
        self.inbox_dir = self.home / "messages" / "inbox"
        self.archive_dir = self.home / "messages" / "archive"
        self._now = now or fsio.utc_now

    # -- writing ----------------------------------------------------------

    def post(
        self,
        sender: str,
        topic: str,
        type: str = "note",
        payload: dict[str, Any] | None = None,
        text: str = "",
        ttl_days: int | None = None,
    ) -> Message:
        """Broadcast to a board topic."""
        payload = dict(payload or {})
        if text:
            payload["text"] = text
        msg = Message.model_validate(
            {
                "from": sender,
                "topic": topic,
                "type": type,
                "payload": payload,
                "ttl_days": ttl_days,
                "created_at": fsio.iso(self._now()),
                "id": fsio.ulid(self._now()),
            }
        )
        fsio.atomic_write_json(self.board_dir / topic / msg.filename(), msg.dump())
        return msg

    def send(
        self,
        sender: str,
        to: str,
        type: str = "note",
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> Message:
        """Deliver directly into another agent's inbox."""
        payload = dict(payload or {})
        if text:
            payload["text"] = text
        msg = Message.model_validate(
            {
                "from": sender,
                "to": to,
                "type": type,
                "payload": payload,
                "created_at": fsio.iso(self._now()),
                "id": fsio.ulid(self._now()),
            }
        )
        inbox = self.inbox_dir / to
        (inbox / "cur").mkdir(parents=True, exist_ok=True)
        fsio.atomic_write_json(inbox / "new" / msg.filename(), msg.dump())
        return msg

    # -- board reading ----------------------------------------------------

    def topics(self) -> list[str]:
        if not self.board_dir.is_dir():
            return []
        return sorted(p.name for p in self.board_dir.iterdir() if p.is_dir())

    def read_topic(
        self,
        topic: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        """Chronological messages in a topic, optionally bounded."""
        entries = fsio.sorted_entries(self.board_dir / topic)
        if since is not None:
            floor = fsio.compact_ts(since)
            entries = [p for p in entries if p.name >= floor]
        if limit is not None:
            entries = entries[-limit:]
        return [m for m in (_load(p) for p in entries) if m]

    def read_after_cursor(self, topic: str, cursor: str | None) -> tuple[list[Message], str | None]:
        """Messages newer than `cursor` (a filename); returns the new cursor.

        This is how agents consume the board without marking it: each keeps
        its own cursor in its private state file.
        """
        entries = self.entries_after_cursor(topic, cursor)
        msgs = [m for _, m in entries if m]
        new_cursor = entries[-1][0] if entries else cursor
        return msgs, new_cursor

    def entries_after_cursor(
        self, topic: str, cursor: str | None, limit: int | None = None
    ) -> list[tuple[str, Message | None]]:
        """`(filename, message)` pairs newer than `cursor`, oldest first.

        The filename is the *on-disk* name, which is the only safe cursor: a
        message's own `filename()` is what `post()` writes, but a file copied
        in by hand under another name would leave a cursor that never passes
        it — a consumer that advances one message at a time (the notify hook)
        needs the real name. An unreadable file rides along as `None` so the
        cursor can step past it rather than re-reading it forever.

        `limit` keeps the *oldest* that many (a consumer works forwards and
        the rest wait for its next pass — the opposite end from
        `read_topic`), and bounds the parsing too, not just the result.
        """
        entries = fsio.sorted_entries(self.board_dir / topic)
        if cursor:
            entries = [p for p in entries if p.name > cursor]
        if limit is not None:
            entries = entries[:limit]
        return [(p.name, _load(p)) for p in entries]

    def topic_tail(self, topic: str) -> str | None:
        """The newest on-disk filename in `topic`, None when it is empty.

        A consumer arming its cursor at "everything before now is history"
        wants this and nothing else — reading the messages just to learn the
        last name parses a whole backlog to throw it away.
        """
        entries = fsio.sorted_entries(self.board_dir / topic)
        return entries[-1].name if entries else None

    # -- inbox claiming ---------------------------------------------------

    def pending(self, agent: str) -> bool:
        """True if unclaimed messages wait in `agent`'s inbox — a peek, no claim."""
        try:
            with os.scandir(self.inbox_dir / agent / "new") as entries:
                return any(
                    e.name.endswith(".json") and not fsio.is_tmp(e.name) for e in entries
                )
        except FileNotFoundError:
            return False

    def claim(self, agent: str) -> Iterator[ClaimedMessage]:
        """Yield direct messages for `agent`, each atomically claimed via rename."""
        inbox = self.inbox_dir / agent
        new, cur = inbox / "new", inbox / "cur"
        cur.mkdir(parents=True, exist_ok=True)
        for path in fsio.sorted_entries(new):
            target = cur / path.name
            try:
                os.rename(path, target)  # atomic: exactly one claimant wins
            except OSError:
                continue
            msg = _load(target)
            if msg is None:
                target.unlink(missing_ok=True)
                continue
            yield ClaimedMessage(msg, target, self.archive_dir)

    # -- reading an inbox without claiming --------------------------------

    def inbox_messages(self, agent: str, folder: str = "new") -> list[Message]:
        """The messages sitting in one folder of `agent`'s inbox — `new/`
        (unclaimed) or `cur/` (claimed by a consumer that has not acked yet)
        — oldest first, without touching them. A peek for readers (`task
        history`); an unreadable file is skipped, never raised."""
        if folder not in ("new", "cur"):
            raise ValueError(f"inbox folder must be 'new' or 'cur', not {folder!r}")
        entries = fsio.sorted_entries(self.inbox_dir / agent / folder)
        return [m for m in (_load(p) for p in entries) if m]

    def archived_direct(self, to: str, since: datetime | None = None) -> list[Message]:
        """Every archived message that was addressed to `to`, oldest first.

        The archive (`messages/archive/YYYY-MM.jsonl.gz`) is where a claimed
        inbox message goes when its consumer acks it — so for a task's inbox
        this is the record of guidance that was consumed. `since` bounds the
        read to the monthly files from that month on (the archive is filed by
        the month the message was archived, never earlier than it was sent).
        Fail-soft throughout: a file that will not decompress or a line that
        will not parse is skipped, because this is read by views, and a view
        that raises over one bad byte of history is worse than one missing
        line.
        """
        floor = f"{since:%Y-%m}" if since is not None else ""
        out: list[Message] = []
        for path in sorted(self.archive_dir.glob("*.jsonl.gz")):
            if path.name[:7] < floor:
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    for line in f:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict) or record.get("to") != to:
                            continue
                        try:
                            out.append(Message.model_validate(record))
                        except ValueError:
                            continue
            except (OSError, EOFError, gzip.BadGzipFile):
                continue
        out.sort(key=lambda m: m.created_at)
        return out

    # -- on-demand archival ----------------------------------------------
    #
    # The janitor's per-message path, exposed for `quorum board ack`,
    # `quorum board clear` and `quorum task inbox --clear`. Same destination
    # file, same "archive, never delete" rule: an acked or cleared message
    # keeps its created_at in messages/archive/, it just stops being live.

    def archive_board_message(self, path: Path) -> Message | None:
        """Archive one board message file and remove it from its topic.

        The reusable unit: `archive_topic` loops over it, and
        `ack_board_message` resolves one id to a path and calls it once. A file
        that will not parse is removed as the janitor removes it — it can be
        read by nobody, so keeping it live only makes every scan trip on it.
        """
        msg = _load(path)
        if msg is None:
            path.unlink(missing_ok=True)
            return None
        _archive_one(self.archive_dir, msg.dump(), when=msg.created)
        path.unlink(missing_ok=True)
        return msg

    def resolve_board_message(
        self, handle: str, topic: str | None = None
    ) -> tuple[Message, Path]:
        """Find one *live* board message by full id, unique prefix or unique
        suffix — the suffix form is what `short_id` hands out, and the whole
        handle grammar is the one `TaskStore.resolve` uses, because a reader
        who has learned to type six characters at a task should not have to
        learn a second rule at a message.

        Raises KeyError when nothing matches and ValueError when the handle is
        ambiguous, exactly as task resolution does — a wrong ack is silent (the
        banner just drops something else), so both fail loudly.
        """
        handle = handle.strip().upper()
        if not handle:
            raise KeyError(handle)
        matches: list[tuple[Message, Path]] = []
        for name in [topic] if topic else self.topics():
            for path in fsio.sorted_entries(self.board_dir / name):
                # the filename is <compact-ts>-<ULID>.json and the timestamp
                # carries no "-", so this is the id without reading the file
                candidate = path.stem.split("-", 1)[-1].upper()
                if not (candidate.startswith(handle) or candidate.endswith(handle)):
                    continue
                msg = _load(path)
                if msg is None:
                    continue
                if msg.id.upper() == handle:
                    return msg, path  # an exact id is never ambiguous
                matches.append((msg, path))
        if not matches:
            raise KeyError(handle)
        if len(matches) > 1:
            raise ValueError(
                f"message handle {handle!r} is ambiguous: "
                + ", ".join(m.short_id for m, _ in matches)
            )
        return matches[0]

    def ack_board_message(self, handle: str, topic: str | None = None) -> Message:
        """Archive the one board message `handle` names — the per-message half
        of `archive_topic`, and what `quorum board ack` and both dashboards
        call.

        Ack is *archival*, not a flag on the message: the board still carries
        no read-state, so acking only ever means "this one stops being live",
        and every reader keeps coexisting without coordination.
        """
        msg, path = self.resolve_board_message(handle, topic)
        return self.archive_board_message(path) or msg

    def archive_topic(
        self, topic: str, before: datetime | None = None, dry_run: bool = False
    ) -> list[Message]:
        """Archive a whole board topic (optionally only what predates
        `before`), returning the messages archived — or, under `dry_run`,
        the ones that would be."""
        archived: list[Message] = []
        for path in fsio.sorted_entries(self.board_dir / topic):
            msg = _load(path)
            if msg is None:
                if not dry_run:
                    path.unlink(missing_ok=True)
                continue
            if before is not None and msg.created >= before:
                continue
            if dry_run:
                archived.append(msg)
                continue
            moved = self.archive_board_message(path)
            if moved is not None:
                archived.append(moved)
        return archived

    def clear_inbox(self, agent: str, dry_run: bool = False) -> list[Message]:
        """Archive everything waiting unclaimed in `agent`'s inbox.

        Only `new/` — a message in `cur/` is being processed by someone, and
        pulling it out from under them is the one way this could lose work.
        """
        cleared: list[Message] = []
        for path in fsio.sorted_entries(self.inbox_dir / agent / "new"):
            msg = _load(path)
            if msg is None:
                if not dry_run:
                    path.unlink(missing_ok=True)
                continue
            if dry_run:
                cleared.append(msg)
                continue
            _archive_one(self.archive_dir, msg.dump(), when=msg.created)
            path.unlink(missing_ok=True)
            cleared.append(msg)
        return cleared

    # -- janitor ----------------------------------------------------------

    def recover_stale_claims(self, grace: timedelta = STALE_CLAIM_GRACE) -> int:
        """Move cur/ entries older than `grace` back to new/ (crashed consumer)."""
        recovered = 0
        cutoff = (self._now() - grace).timestamp()
        if not self.inbox_dir.is_dir():
            return 0
        for inbox in self.inbox_dir.iterdir():
            cur = inbox / "cur"
            for path in fsio.sorted_entries(cur):
                try:
                    if path.stat().st_mtime < cutoff:
                        os.rename(path, inbox / "new" / path.name)
                        recovered += 1
                except OSError:
                    continue
        return recovered

    def archive_old(self, retention_days: int) -> int:
        """Compact expired board messages into monthly gzip'd JSONL archives."""
        archived = 0
        now = self._now()
        for topic in self.topics():
            for path in fsio.sorted_entries(self.board_dir / topic):
                msg = _load(path)
                if msg is None:
                    path.unlink(missing_ok=True)
                    continue
                keep_days = msg.ttl_days if msg.ttl_days is not None else retention_days
                if msg.created < now - timedelta(days=keep_days):
                    _archive_one(self.archive_dir, msg.dump(), when=msg.created)
                    path.unlink(missing_ok=True)
                    archived += 1
        return archived


def _load(path: Path) -> Message | None:
    try:
        return Message.model_validate(fsio.read_json(path))
    except (OSError, ValueError):
        return None


def _archive_one(archive_dir: Path, record: dict[str, Any], when: datetime | None = None) -> None:
    when = when or datetime.now(UTC)
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{when:%Y-%m}.jsonl.gz"
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with gzip.open(path, "ab") as f:
        f.write(line)
