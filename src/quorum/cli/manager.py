"""`quorum manager`: the journal and notebook writes an agent makes about
itself.

Reading an agent back is `quorum agent log` / `agent list`, and sending it
guidance is `quorum board post --to <agent>`; what is left here is what a run
writes down — why it did something (`note`) and what the next run needs
(`remember` / `forget` / `notes`).
"""

from __future__ import annotations

import typer

from ._common import (
    _AGENT_OPT,
    _actor_guard,
    _agent_notebook,
    _notebook_write,
    get_home,
    manager_app,
)

# -- manager ---------------------------------------------------------------


@manager_app.command("note")
def manager_note(text: str) -> None:
    """Journal the reason for an action (the manager's harness calls this; humans can too)."""
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
