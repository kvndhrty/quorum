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
project_app = typer.Typer(help="Manage tracked projects.", no_args_is_help=True)
agent_app = typer.Typer(help="Inspect and run agents.", no_args_is_help=True)
app.add_typer(board_app, name="board")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")

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


@app.command()
def up(
    home: Optional[Path] = _HOME_OPT,
    self_sandbox: bool = typer.Option(
        False, "--self-sandbox", help="Apply a nono-py kernel sandbox to this process before starting."
    ),
) -> None:
    """Run the supervisor in the foreground (background it with nohup/tmux)."""
    from .config import load_config
    from .supervisor import Supervisor

    target = get_home(home)
    config = load_config(target)
    if self_sandbox:
        from .sandbox import self_sandbox as apply_sandbox

        apply_sandbox(target, config)
    typer.echo(f"quorum supervisor starting (home: {target}) — Ctrl-C to stop")
    Supervisor(target, config).run()


@app.command()
def status(home: Optional[Path] = _HOME_OPT) -> None:
    """Show supervisor liveness, agent heartbeats, and project deadlines."""
    from . import views

    target = get_home(home)
    sup = views.supervisor_status(target)
    if sup["alive"]:
        typer.secho(f"supervisor: running (pid {sup['pid']}, since {sup['started_at']})", fg="green")
    else:
        typer.secho("supervisor: not running", fg="yellow")

    rows = views.agent_rows(target)
    if rows:
        typer.echo("\nagents:")
        for r in rows:
            marker = {"idle": "●", "running": "◐", "error": "✗", "paused": "‖"}.get(r["status"], "○")
            line = f"  {marker} {r['name']:<12} {r['status']:<10} schedule: {r['schedule']}"
            if r["last_end"]:
                line += f"  last: {r['last_end']}"
            if r["error"]:
                line += f"  [{r['error']}]"
            typer.echo(line)

    projects = views.project_rows(target)
    if projects:
        typer.echo("\nprojects:")
        for p in projects:
            dl = ""
            if p["deadline"]:
                dl = f"  due {p['deadline']}"
                if p["days_left"] is not None:
                    dl += f" ({p['days_left']}d)" if p["days_left"] >= 0 else f" (OVERDUE {-p['days_left']}d)"
            act = f"  active {p['last_activity']}" if p["last_activity"] else ""
            typer.echo(f"  {p['slug']:<24}{dl}{act}")


@project_app.command("add")
def project_add(
    path: Path,
    name: Optional[str] = typer.Option(None, "--name", help="Display name (default: dir name)."),
    deadline: Optional[str] = typer.Option(None, "--deadline", help="ISO date, e.g. 2026-09-15."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    notes: str = typer.Option("", "--notes"),
    marker: bool = typer.Option(False, "--marker", help="Also write a .quorum.toml into the project dir."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Register a project directory."""
    from .projects import ProjectRegistry

    registry = ProjectRegistry(get_home(home))
    try:
        project = registry.add(
            path,
            name=name,
            deadline=deadline,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            notes=notes,
            write_marker=marker,
        )
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"registered project {project.slug} ({project.path})", fg="green")


@project_app.command("list")
def project_list(home: Optional[Path] = _HOME_OPT) -> None:
    """List registered projects (marker-file fields merged in)."""
    from . import views

    rows = views.project_rows(get_home(home))
    if not rows:
        typer.echo("no projects registered — `quorum project add <dir>`")
        return
    for p in rows:
        dl = f"  due {p['deadline']} ({p['days_left']}d)" if p["deadline"] else ""
        tags = f"  [{', '.join(p['tags'])}]" if p["tags"] else ""
        typer.echo(f"{p['slug']:<24} {p['name']}{dl}{tags}")


@project_app.command("set")
def project_set(
    slug: str,
    deadline: Optional[str] = typer.Option(None, "--deadline"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    name: Optional[str] = typer.Option(None, "--name"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Update a project's metadata in the registry."""
    from .projects import ProjectRegistry

    registry = ProjectRegistry(get_home(home))
    try:
        project = registry.update(
            slug,
            deadline=deadline,
            notes=notes,
            name=name,
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None,
        )
    except KeyError:
        typer.secho(f"no project {slug!r}", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"updated {project.slug}" + (f" (due {project.deadline})" if project.deadline else ""), fg="green")


@project_app.command("remove")
def project_remove(slug: str, home: Optional[Path] = _HOME_OPT) -> None:
    """Unregister a project (its directory is untouched)."""
    from .projects import ProjectRegistry

    if ProjectRegistry(get_home(home)).remove(slug):
        typer.echo(f"removed {slug}")
    else:
        typer.secho(f"no project {slug!r}", fg="red", err=True)
        raise typer.Exit(1)


@project_app.command("adopt")
def project_adopt(
    slug: str,
    deadline: Optional[str] = typer.Option(None, "--deadline"),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Register a project proposed by the scout agent."""
    from .agents.scout import load_candidates
    from .projects import ProjectRegistry

    target = get_home(home)
    candidates = load_candidates(target)
    path = candidates.get(slug)
    if path is None:
        typer.secho(
            f"no candidate {slug!r} — see proposals with `quorum board read projects`",
            fg="red", err=True,
        )
        raise typer.Exit(1)
    try:
        project = ProjectRegistry(target).add(path, name=None, deadline=deadline)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"adopted {project.slug} ({project.path})", fg="green")


@project_app.command("decline")
def project_decline(slug: str, home: Optional[Path] = _HOME_OPT) -> None:
    """Suppress a scout proposal permanently."""
    from .agents import scout

    scout.decline(get_home(home), slug)
    typer.echo(f"declined {slug} — the scout won't propose it again")


steward_app = typer.Typer(help="File steward operations.", no_args_is_help=True)
app.add_typer(steward_app, name="steward")


@steward_app.command("undo")
def steward_undo(
    last: int = typer.Option(1, "--last", help="How many recent moves to undo."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Undo the steward's most recent file move(s)."""
    from .agents.steward import undo_moves

    undone = undo_moves(get_home(home), last=last)
    if not undone:
        typer.echo("nothing to undo")
    for dest, src in undone:
        typer.echo(f"moved back: {dest} -> {src}")


@app.command()
def web(
    port: int = typer.Option(8787, "--port"),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Serve the local web dashboard on 127.0.0.1 (requires `quorum[web]`)."""
    target = get_home(home)
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        typer.secho(
            "the web dashboard needs the [web] extra: uv tool install 'quorum[web]' "
            "(or pip install 'quorum[web]')",
            fg="red", err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"dashboard: http://127.0.0.1:{port}")
    uvicorn.run(create_app(target), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def tui(home: Optional[Path] = _HOME_OPT) -> None:
    """Open the terminal dashboard (requires `quorum[tui]`)."""
    target = get_home(home)
    try:
        from .tui.app import QuorumTUI
    except ImportError:
        typer.secho(
            "the TUI needs the [tui] extra: uv tool install 'quorum[tui]' "
            "(or pip install 'quorum[tui]')",
            fg="red", err=True,
        )
        raise typer.Exit(1)
    QuorumTUI(target).run()


@agent_app.command("list")
def agent_list(home: Optional[Path] = _HOME_OPT) -> None:
    """List configured agents and their last heartbeat."""
    from . import views

    for r in views.agent_rows(get_home(home)):
        state = r["status"] + ("" if r["enabled"] else " (disabled)")
        typer.echo(f"{r['name']:<14} type={r['type']:<20} {r['schedule']:<18} {state}")


@agent_app.command("run-once")
def agent_run_once(name: str, home: Optional[Path] = _HOME_OPT) -> None:
    """Construct an agent and run a single tick (no supervisor needed)."""
    from .agent import AgentContext
    from .config import load_config
    from .registry import AgentResolutionError, resolve

    target = get_home(home)
    config = load_config(target)
    acfg = config.agents.get(name)
    if acfg is None:
        typer.secho(f"no agent {name!r} in config.toml", fg="red", err=True)
        raise typer.Exit(1)
    try:
        cls = resolve(acfg.type, target)
    except AgentResolutionError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    agent = cls(AgentContext(home=target, name=name, settings=acfg.settings, config=config))
    agent.tick()
    typer.secho(f"{name}: tick complete", fg="green")


@app.command()
def brief(
    date: Optional[str] = typer.Argument(None, help="Brief date (YYYY-MM-DD); default: latest."),
    home: Optional[Path] = _HOME_OPT,
) -> None:
    """Print a daily brief (latest by default)."""
    target = get_home(home)
    briefs = sorted((target / "briefs").glob("*.md"))
    if date:
        path = target / "briefs" / f"{date}.md"
        if not path.exists():
            typer.secho(f"no brief for {date}", fg="red", err=True)
            raise typer.Exit(1)
    elif briefs:
        path = briefs[-1]
    else:
        typer.echo("no briefs yet — run `quorum agent run-once scribe` or wait for the schedule")
        raise typer.Exit(0)
    typer.echo(path.read_text(encoding="utf-8"))


def _parse_window(text: str) -> timedelta:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    text = text.strip()
    if text and text[-1] in units and text[:-1].isdigit():
        return timedelta(**{units[text[-1]]: int(text[:-1])})
    raise typer.BadParameter(f"invalid window {text!r} (use e.g. 90m, 24h, 7d)")


if __name__ == "__main__":
    app()
