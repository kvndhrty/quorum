"""`quorum board`: the message substrate's one CLI — the public board, and
the direct inboxes `board post --to` delivers into."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import fsio
from ..actor import current_actor
from ..messages import MessageBus
from ._common import (
    _actor_guard,
    _confirm,
    _fail,
    _load_config,
    _parse_before,
    _parse_window,
    board_app,
    get_home,
)

# -- board -----------------------------------------------------------------


@board_app.command("post")
def board_post(
    topic: str = typer.Argument(
        "", metavar="TOPIC|TEXT", help="Board topic to post to (with --to: the message)."
    ),
    text: str = typer.Argument("", help="The message."),
    to: str = typer.Option(
        "", "--to",
        help="Send into this agent's inbox instead of onto a topic (e.g. --to manager).",
    ),
    type: str = typer.Option("note", "--type", help="Message type tag."),
) -> None:
    """Post a message — onto a board topic, or into one agent's inbox.

    The board is public and anything may read it; `--to <agent>` is the other
    channel of the same schema, a direct delivery that only that agent claims.
    Guidance for the manager is `quorum board post --to manager "..."`, and
    its next tick starts with it in the digest.
    """
    target = get_home()
    if to:
        if text:
            raise _fail("--to takes one argument: the message (quote it)")
        if type != "note":
            raise _fail("--type tags a board post; --to always sends guidance")
        _tell(target, to, topic)
        return
    if not topic or not text:
        raise _fail('post to a topic (`board post <topic> "<text>"`) or an inbox (--to <agent>)')
    _actor_guard(target, "board.post", args=f"{topic}: {text[:80]}")
    msg = MessageBus(target).post(
        sender=current_actor(), topic=topic, type=type, text=text
    )
    typer.echo(f"posted {msg.id} to {topic}")


def _tell(home: Path, to: str, text: str) -> None:
    """The inbox half of `board post`: a direct delivery to one agent.

    The recipient is checked against the configured agents, because a topic
    is free-form but an inbox is not: a misspelled `--to` would write a
    maildir nobody ever claims, and the message would look sent.
    """
    if not text:
        raise _fail("nothing to send — pass the message as the argument")
    config = _load_config(home)
    known = set(config.agents) | {"supervisor"}
    if to not in known:
        raise _fail(
            f"no agent {to!r} to send to (known: {', '.join(sorted(known))}) — "
            "a board topic is free-form, an inbox is not"
        )
    _actor_guard(home, "board.post", target=to, args=text[:80])
    MessageBus(home).send(current_actor(), to, type="guidance", text=text)
    typer.secho(f"guidance queued for {to}'s next run", fg="green")


@board_app.command("read")
def board_read(
    topic: str | None = typer.Argument(None, help="Topic to read (default: all topics)."),
    since: str = typer.Option("24h", "--since", help="Window like 90m, 24h or 7d."),
) -> None:
    """Read recent board messages."""
    bus = MessageBus(get_home())
    window = _parse_window(since)
    topics = [topic] if topic else bus.topics()
    floor = fsio.window_start(fsio.utc_now(), window)
    empty = True
    for t in topics:
        for msg in bus.read_topic(t, since=floor):
            empty = False
            created = fsio.display_ts(msg.created_at)
            # the short id is here so `board clear --id` has something to name
            typer.echo(
                f"[{created}] {t} {msg.short_id} <{msg.sender}> "
                f"{msg.type}: {msg.payload.get('text', '')}"
            )
    if empty:
        typer.echo(f"no messages in the last {since}")


@board_app.command("clear")
def board_clear(
    topic: str | None = typer.Argument(
        None, help="Topic to empty (optional when --id names one message)."
    ),
    message_id: str | None = typer.Option(
        None, "--id", metavar="MESSAGE_ID",
        help="Archive just this message (an id, prefix or short suffix) instead of the topic.",
    ),
    before: str | None = typer.Option(
        None, "--before",
        help="Only messages older than this: a window (7d) or a timestamp (2026-09-01).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be archived."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Archive board messages: a whole topic, or one message with --id.

    They land in the same `messages/archive/YYYY-MM.jsonl.gz` the hourly
    janitor writes — nothing is lost, it just stops being live.

    The banner (`quorum status`, the TUI header) is a time window over
    #attention, not a read-state, so an escalation you have already handled
    sits there for a week. `quorum board clear attention` empties it;
    `quorum board clear --id 7c1af2` says "I have seen this one" and drops
    that message alone, keeping its original `created_at` in the history.
    """
    home_path = get_home()
    if message_id is not None:
        if before is not None:
            raise _fail("--before selects a window of a topic; --id names one message")
        _clear_message(home_path, message_id, topic=topic, dry_run=dry_run)
        return
    if not topic:
        raise _fail("name a topic to empty, or --id <message-id> for one message")
    _clear_topic(home_path, topic, before=before, dry_run=dry_run, yes=yes)


def _clear_message(home: Path, handle: str, topic: str | None, dry_run: bool) -> None:
    """The single-message path behind `board clear --id`."""
    bus = MessageBus(home)
    try:
        msg, path = bus.resolve_board_message(handle, topic=topic)
    except KeyError:
        where = f" on {topic}" if topic else ""
        raise _fail(
            f"no live board message matching {handle!r}{where} — `quorum board read`"
        ) from None
    except ValueError as e:
        raise _fail(str(e)) from None
    text = msg.payload.get("text", "")
    if dry_run:
        typer.echo(f"would archive #{msg.topic} {msg.short_id} <{msg.sender}> {text[:70]}")
        return
    _actor_guard(home, "board.clear", target=msg.short_id, args=f"#{msg.topic}: {text[:60]}")
    # archive the path resolution already handed us: resolving a second time
    # could miss (the janitor, another `board clear`, the TUI) and raise
    # where a tidy line belongs, and archiving a gone file is a no-op anyway
    bus.archive_board_message(path)
    typer.secho(f"archived {msg.short_id} from #{msg.topic} — archived, not deleted", fg="green")


def _clear_topic(home: Path, topic: str, before: str | None, dry_run: bool, yes: bool) -> None:
    """The sweep behind `board clear <topic>`: archive a whole topic, or the
    part of it older than `before`."""
    floor = _parse_before(before) if before else None
    bus = MessageBus(home)
    doomed = bus.archive_topic(topic, before=floor, dry_run=True)
    if not doomed:
        typer.echo(f"nothing to clear on {topic}")
        return
    if dry_run:
        typer.echo(f"would archive {len(doomed)} message(s) from {topic}:")
        for msg in doomed:
            typer.echo(f"  [{msg.created_at}] <{msg.sender}> {msg.payload.get('text', '')[:70]}")
        return
    _confirm(yes, f"archive {len(doomed)} message(s) from {topic}?")
    _actor_guard(home, "board.clear", args=f"{topic}: {len(doomed)} message(s)")
    archived = bus.archive_topic(topic, before=floor)
    typer.secho(f"archived {len(archived)} message(s) from {topic}", fg="green")
