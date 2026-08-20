"""The `quorum` command-line interface."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Optional

import typer

from . import fsio, home as home_mod
from .messages import MessageBus

app = typer.Typer(help="Quorum: an agentic ecosystem of specialists.", no_args_is_help=True)
board_app = typer.Typer(help="Read and post to the public message board.", no_args_is_help=True)
app.add_typer(board_app, name="board")

_HOME_OPT = typer.Option(None, "--home", help="QUORUM_HOME directory (default: $QUORUM_HOME or ~/.quorum).")


def get_home(explicit: Optional[Path] = None, must_exist: bool = True) -> Path:
    home = home_mod.resolve_home(explicit)
    if must_exist and not (home / home_mod.CONFIG_NAME).exists():
        typer.secho(f"no quorum home at {home} — run `quorum init` first", fg="red", err=True)
        raise typer.Exit(1)
    return home


@app.command()
def init(home: Optional[Path] = _HOME_OPT) -> None:
    """Create the QUORUM_HOME directory tree and a starter config.toml."""
    target = home_mod.resolve_home(home)
    fresh = home_mod.scaffold(target)
    if fresh:
        typer.secho(f"initialized quorum home at {target}", fg="green")
        typer.echo(f"next: edit {target / home_mod.CONFIG_NAME}, then `quorum up`")
    else:
        typer.echo(f"quorum home at {target} already initialized (config left untouched)")


@board_app.command("post")
def board_post(
    topic: str,
    text: str,
    type: str = typer.Option("note", "--type", help="Message type tag."),
    sender: str = typer.Option("user", "--from", help="Sender name."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Post a message to a board topic."""
    bus = MessageBus(get_home(home))
    msg = bus.post(sender=sender, topic=topic, type=type, text=text)
    typer.echo(f"posted {msg.id} to {topic}")


@board_app.command("read")
def board_read(
    topic: Optional[str] = typer.Argument(None, help="Topic to read (default: all topics)."),
    since: str = typer.Option("24h", "--since", help="Window like 90m, 24h or 7d."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON lines."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Read recent board messages."""
    bus = MessageBus(get_home(home))
    window = _parse_window(since)
    topics = [topic] if topic else bus.topics()
    floor = fsio.utc_now() - window
    empty = True
    for t in topics:
        for msg in bus.read_topic(t, since=floor):
            empty = False
            if as_json:
                typer.echo(json.dumps(msg.dump(), ensure_ascii=False))
            else:
                created = msg.created_at.replace("T", " ").rstrip("Z")
                typer.echo(f"[{created}] {t} <{msg.sender}> {msg.type}: {msg.payload.get('text', '')}")
    if empty and not as_json:
        typer.echo(f"no messages in the last {since}")


def _parse_window(text: str) -> timedelta:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    text = text.strip()
    if text and text[-1] in units and text[:-1].isdigit():
        return timedelta(**{units[text[-1]]: int(text[:-1])})
    raise typer.BadParameter(f"invalid window {text!r} (use e.g. 90m, 24h, 7d)")


if __name__ == "__main__":
    app()
