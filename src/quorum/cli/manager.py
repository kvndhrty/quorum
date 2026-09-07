"""`quorum manager`: talk to the manager agent and read what it wrote."""

from __future__ import annotations

import typer

from .. import fsio
from ..actor import journal_path
from ..messages import MessageBus
from ._common import (
    _AGENT_OPT,
    _actor_guard,
    _agent_notebook,
    _notebook_write,
    get_home,
    manager_app,
)

# -- manager ---------------------------------------------------------------


@manager_app.command("tell")
def manager_tell(text: str) -> None:
    """Send the manager a directive; its next run starts with it in the digest."""
    target = get_home()
    MessageBus(target).send("user", "manager", type="directive", text=text)
    typer.secho("directive queued for the manager's next run", fg="green")


@manager_app.command("note")
def manager_note(text: str) -> None:
    """Journal a reasoning note (the manager's harness calls this; humans can too)."""
    target = get_home()
    _actor_guard(target, "note", args=text, always_journal=True)
    typer.echo("noted")

@manager_app.command("remember")
def manager_remember(
    text: str,
    ttl: int = typer.Option(0, "--ttl", help="Days until the note expires (0: never)."),
    agent: str = _AGENT_OPT,
) -> None:
    """Write a standing note every future run of that agent will read.

    The notebook (`state/manager/notes.jsonl`) is not the journal: `note`
    records why *this* run did what it did, `remember` records a fact the
    next run needs. Tasks and other agents are refused — they reach the
    manager with `task report` and `board post`.
    """
    from .. import notes as notes_mod

    target = get_home()
    entry = _notebook_write(
        target, _agent_notebook(target, agent), "remember",
        journal_target=agent, arg=text, ttl=ttl, always_journal=True,
    )
    typer.secho(
        f"remembered ({notes_mod.short_id(entry['id'])}) — every future {agent} run reads it"
        + (f", for {ttl}d" if ttl else ""),
        fg="green",
    )


@manager_app.command("forget")
def manager_forget(
    note_id: str,
    agent: str = _AGENT_OPT,
) -> None:
    """Retire a standing note that stopped being true (append-only: the file
    keeps it, readers hide it)."""
    from .. import notes as notes_mod

    target = get_home()
    note = _notebook_write(
        target, _agent_notebook(target, agent), "forget",
        journal_target=agent, arg=note_id, retire=True, always_journal=True,
    )
    typer.echo(f"forgot ({notes_mod.short_id(note['id'])}) {note.get('text', '')[:60]}")


@manager_app.command("notes")
def manager_notes(
    agent: str = _AGENT_OPT,
) -> None:
    """Print the notebook exactly as the digest renders it for that agent."""
    for line in _agent_notebook(get_home(), agent).render():
        typer.echo(line)


@manager_app.command("journal")
def manager_journal(
    lines: int = typer.Option(20, "-n", "--lines", help="Entries to show."),
) -> None:
    """Print the manager's recent action journal (auto-recorded, per-run tagged)."""
    entries = fsio.read_jsonl_tail(journal_path(get_home()), limit=lines)
    if not entries:
        typer.echo("no manager actions recorded yet")
        return
    for e in entries:
        run = e.get("run", "")
        line = f"[{e.get('at', '')}] ({e.get('actor', '?')}{'/' + run[-6:].lower() if run else ''}) {e.get('action', '')}"
        if e.get("target"):
            line += f" -> {e['target']}"
        if e.get("args"):
            line += f"  {e['args']}"
        typer.echo(line)
