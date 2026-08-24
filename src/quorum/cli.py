"""The `quorum` command-line interface."""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import timedelta
from pathlib import Path

import typer

from . import fsio
from . import home as home_mod
from .actor import (
    DEFAULT_MAX_ACTIONS_PER_RUN,
    MANAGER_CAP_ENV,
    MANAGER_RUN_ENV,
    current_actor,
    journal_path,
)
from .messages import MessageBus

app = typer.Typer(
    help="Quorum: orchestrate long-running coding tasks with your own harness.",
    no_args_is_help=True,
)
board_app = typer.Typer(help="Read and post to the public message board.", no_args_is_help=True)
project_app = typer.Typer(help="Manage registered projects.", no_args_is_help=True)
agent_app = typer.Typer(help="Inspect, run, and control agents.", no_args_is_help=True)
task_app = typer.Typer(help="Create, run, and guide harness-driven tasks.", no_args_is_help=True)
manager_app = typer.Typer(help="Talk to (and audit) the manager agent.", no_args_is_help=True)
app.add_typer(board_app, name="board")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(manager_app, name="manager")

_HOME_OPT = typer.Option(None, "--home", help="QUORUM_HOME directory (default: $QUORUM_HOME or ~/.quorum).")


def get_home(explicit: Path | None = None, must_exist: bool = True) -> Path:
    home = home_mod.resolve_home(explicit)
    if must_exist and not (home / home_mod.CONFIG_NAME).exists():
        typer.secho(f"no quorum home at {home} — run `quorum init` first", fg="red", err=True)
        raise typer.Exit(1) from None
    return home


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg="red", err=True)
    return typer.Exit(1)


def _load_config(home: Path):
    from .config import ConfigError, load_config

    try:
        return load_config(home)
    except ConfigError as e:
        raise _fail(str(e)) from None


def _manager_guard(
    home: Path,
    action: str,
    target: str | None = None,
    target_status: str | None = None,
    args: str | None = None,
    always_journal: bool = False,
) -> None:
    """Auto-journal (and rate-cap) actions taken by the manager's harness.

    Manager runs carry the actor env tag (see actor.py): the actor identity,
    a per-run id, and the action cap the manager resolved from its settings.
    Every mutating CLI command routes through here, so the journal is ground
    truth — not the model's self-report — and it is what the next digest
    feeds back to prevent degenerate loops. The only rail is rate: a per-run
    action cap. Choice is never second-guessed.
    """
    actor = current_actor()
    if actor != "manager" and not always_journal:
        return
    run = os.environ.get(MANAGER_RUN_ENV, "") if actor == "manager" else ""
    if run:
        try:
            cap = int(os.environ.get(MANAGER_CAP_ENV, DEFAULT_MAX_ACTIONS_PER_RUN))
        except ValueError:
            cap = DEFAULT_MAX_ACTIONS_PER_RUN
        # this run's entries sit at the journal's end, well inside the tail window
        used = len([e for e in fsio.read_jsonl_tail(journal_path(home)) if e.get("run") == run])
        if used >= cap:
            typer.secho(
                f"action refused: manager action cap ({cap}) reached for this run — "
                "remaining work waits for your next scheduled run",
                fg="red", err=True,
            )
            raise typer.Exit(1)
    entry = {
        "at": fsio.iso(fsio.utc_now()),
        "run": run,
        "actor": actor,
        "action": action,
    }
    if target:
        entry["target"] = target
    if target_status:
        entry["target_status"] = target_status
    if args:
        entry["args"] = args
    fsio.append_jsonl(journal_path(home), entry)


def _resolve_task(home: Path, prefix: str):
    from .tasks import TaskStore

    try:
        return TaskStore(home).resolve(prefix)
    except KeyError:
        raise _fail(f"no task matching {prefix!r} — `quorum task list`") from None
    except ValueError as e:
        raise _fail(str(e)) from None


@app.command()
def init(home: Path | None = _HOME_OPT) -> None:
    """Create the QUORUM_HOME directory tree and a starter config.toml."""
    target = home_mod.resolve_home(home)
    fresh = home_mod.scaffold(target)
    if fresh:
        typer.secho(f"initialized quorum home at {target}", fg="green")
        typer.echo(f"next: edit {target / home_mod.CONFIG_NAME}, then `quorum up`")
    else:
        typer.echo(f"quorum home at {target} already initialized (config left untouched)")


# -- supervisor ------------------------------------------------------------


@app.command()
def up(
    home: Path | None = _HOME_OPT,
    self_sandbox: bool = typer.Option(
        False, "--self-sandbox", help="Apply a nono-py kernel sandbox to this process before starting."
    ),
) -> None:
    """Run the supervisor in the foreground (background it with nohup/tmux)."""
    from .supervisor import Supervisor

    target = get_home(home)
    config = _load_config(target)
    if self_sandbox:
        from .sandbox import self_sandbox as apply_sandbox

        apply_sandbox(target, config)
    typer.echo(f"quorum supervisor starting (home: {target}) — Ctrl-C to stop")
    Supervisor(target, config).run()


@app.command()
def status(home: Path | None = _HOME_OPT) -> None:
    """Show supervisor liveness, agents, tasks, and project deadlines."""
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

    task_rows = views.task_rows(target)
    if task_rows:
        typer.echo("\ntasks:")
        for t in task_rows:
            _echo_task_row(t)

    projects = views.project_rows(target)
    if projects:
        typer.echo("\nprojects:")
        for p in projects:
            dl = ""
            if p["deadline"]:
                dl = f"  due {p['deadline']}"
                if p["days_left"] is not None:
                    dl += f" ({p['days_left']}d)" if p["days_left"] >= 0 else f" (OVERDUE {-p['days_left']}d)"
            typer.echo(f"  {p['slug']:<24}{dl}")


def _echo_task_row(t: dict) -> None:
    marker = "▶" if t["running"] else ("✓" if t["status"] == "done" else ("✗" if t["status"] == "blocked" else "·"))
    line = f"  {marker} {t['id_short']:<9} {t['project']:<18} {t['status']:<12} {t['harness']}"
    if t["last_report"]:
        line += f"  {t['last_report'][:60]}"
    if t["pr_url"]:
        line += f"  {t['pr_url']}"
    typer.echo(line)


# -- tasks -----------------------------------------------------------------


@task_app.command("add")
def task_add(
    project: str = typer.Argument(help="Registered project slug (see `quorum project list`)."),
    prompt: str = typer.Argument(help="What the harness should do."),
    harness: str | None = typer.Option(None, "--harness", help="[harness.<name>] to use (default: [tasks].default_harness)."),
    no_worktree: bool = typer.Option(False, "--no-worktree", help="Run in the project dir itself instead of a git worktree."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Queue a task. The manager starts it while `quorum up` runs; or start it
    yourself with `quorum task run`."""
    from .projects import ProjectRegistry
    from .tasks import TaskStore

    target = get_home(home)
    config = _load_config(target)
    if ProjectRegistry(target).get(project) is None:
        known = ", ".join(p.slug for p in ProjectRegistry(target).list()) or "none"
        raise _fail(f"no project {project!r} (registered: {known}) — `quorum project add <dir>` first")
    name = harness or config.tasks.default_harness
    if not name:
        raise _fail("no harness given and [tasks].default_harness is unset — pass --harness or edit config.toml")
    if name not in config.harness:
        known = ", ".join(sorted(config.harness)) or "none configured"
        raise _fail(f"no [harness.{name}] in config.toml (known: {known})")
    _manager_guard(target, "task.add", args=f"{project}: {prompt[:80]}")
    task = TaskStore(target).add(
        project=project,
        prompt=prompt,
        harness=name,
        use_worktree=config.tasks.worktree and not no_worktree,
    )
    typer.secho(f"queued task {task.short_id} on {project} (harness: {name})", fg="green")
    typer.echo(f"start now: `quorum task run {task.short_id}` — or let the manager pick it up under `quorum up`")


@task_app.command("list")
def task_list(home: Path | None = _HOME_OPT) -> None:
    """List tasks, newest last."""
    from . import views

    rows = views.task_rows(get_home(home))
    if not rows:
        typer.echo("no tasks — `quorum task add <project> \"<prompt>\"`")
        return
    for t in rows:
        _echo_task_row(t)


@task_app.command("show")
def task_show(task_id: str, home: Path | None = _HOME_OPT) -> None:
    """Show one task's full record and recent reports."""
    from .tasks import read_reports, runner_alive

    target = get_home(home)
    task = _resolve_task(target, task_id)
    typer.echo(json.dumps(task.model_dump(), indent=2, ensure_ascii=False))
    typer.echo(f"runner: {'alive' if runner_alive(target, task.id) else 'not running'}")
    reports = read_reports(target, task.id, limit=10)
    if reports:
        typer.echo("\nrecent reports:")
        for r in reports:
            typer.echo(f"  [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')}")


@task_app.command("run")
def task_run(
    task_id: str,
    detach: bool = typer.Option(False, "--detach", help="Start the run in the background and return."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Execute one harness run of a task (the manager does this automatically
    under `quorum up`)."""
    from .runner import RunnerError, launch_detached, run_task

    target = get_home(home)
    task = _resolve_task(target, task_id)
    _manager_guard(target, "task.run", target=task.short_id, target_status=task.status)
    if detach:
        pid = launch_detached(target, task.id)
        typer.secho(f"task {task.short_id} running detached (pid {pid}) — `quorum task tail {task.short_id}`", fg="green")
        return
    config = _load_config(target)
    try:
        code = run_task(target, config, task.id)
    except RunnerError as e:
        raise _fail(str(e)) from None
    color = "green" if code == 0 else "red"
    typer.secho(f"run finished (exit {code}) — status: {_resolve_task(target, task.id).status}", fg=color)
    if code != 0:
        raise typer.Exit(1)


@task_app.command("tail")
def task_tail(
    task_id: str,
    lines: int = typer.Option(25, "-n", "--lines", help="Transcript lines to show."),
    follow: bool = typer.Option(False, "-f", "--follow", help="Keep printing new lines (Ctrl-C stops)."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Print the tail of a task's harness transcript."""
    from .tasks import transcript_path

    target = get_home(home)
    task = _resolve_task(target, task_id)
    path = transcript_path(target, task.id)
    entries = fsio.read_jsonl(path)
    for entry in entries[-lines:]:
        typer.echo(_render_transcript_entry(entry))
    if not follow:
        return
    seen = len(entries)
    try:
        while True:
            time.sleep(1.0)
            entries = fsio.read_jsonl(path)
            for entry in entries[seen:]:
                typer.echo(_render_transcript_entry(entry))
            seen = len(entries)
    except KeyboardInterrupt:
        pass


def _render_transcript_entry(entry: dict) -> str:
    at = str(entry.get("at", "")).replace("T", " ").rstrip("Z")
    if "line" in entry:
        return f"[{at}] {entry['line']}"
    return f"[{at}] {json.dumps(entry.get('event'), ensure_ascii=False)}"


@task_app.command("report")
def task_report(
    task_id: str,
    text: str = typer.Argument("", help="Short human-readable progress note."),
    status: str = typer.Option(..., "--status", help="One-word status (planning, executing, pr, done, blocked, ...)."),
    pr_url: str | None = typer.Option(None, "--pr-url", help="Pull request URL, when one was opened."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Record task progress (harnesses call this; humans can too)."""
    from . import tasks as tasks_mod

    target = get_home(home)
    task = _resolve_task(target, task_id)
    _manager_guard(target, "task.report", target=task.short_id, target_status=task.status,
                   args=status)
    tasks_mod.report(target, task.id, status=status, text=text, pr_url=pr_url)
    typer.echo(f"task {task.short_id}: {status}" + (f" ({pr_url})" if pr_url else ""))


@task_app.command("inbox")
def task_inbox(
    task_id: str,
    claim: bool = typer.Option(False, "--claim", help="Consume the messages (what a harness should do)."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Read guidance sent to a task. Without --claim, messages are only peeked."""
    from .tasks import inbox_name

    target = get_home(home)
    task = _resolve_task(target, task_id)
    bus = MessageBus(target)
    if claim:
        found = False
        for claimed in bus.claim(inbox_name(task.id)):
            msg = claimed.message
            typer.echo(f"[from {msg.sender} at {msg.created_at}] {msg.payload.get('text', '')}")
            claimed.ack()
            found = True
        if not found:
            typer.echo("no guidance waiting")
        return
    new_dir = bus.inbox_dir / inbox_name(task.id) / "new"
    entries = fsio.sorted_entries(new_dir)
    if not entries:
        typer.echo("no guidance waiting")
        return
    for path in entries:
        try:
            raw = fsio.read_json(path)
        except (OSError, ValueError):
            continue
        typer.echo(f"[from {raw.get('from', '?')} at {raw.get('created_at', '')}] "
                   f"{raw.get('payload', {}).get('text', '')}")


@task_app.command("nudge")
def task_nudge(task_id: str, text: str, home: Path | None = _HOME_OPT) -> None:
    """Send guidance to a task; the next run (or a cooperative harness
    mid-run) will see it."""
    from .tasks import inbox_name

    target = get_home(home)
    task = _resolve_task(target, task_id)
    _manager_guard(target, "task.nudge", target=task.short_id, target_status=task.status,
                   args=text[:80])
    MessageBus(target).send(current_actor(), inbox_name(task.id), type="guidance", text=text)
    typer.secho(f"guidance queued for task {task.short_id}", fg="green")


@task_app.command("cancel")
def task_cancel(
    task_id: str,
    kill: bool = typer.Option(False, "--kill", help="Also SIGTERM a live runner."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Mark a task cancelled so the manager stops attending to it."""
    from .tasks import TaskStore, runner_lock_path

    target = get_home(home)
    task = _resolve_task(target, task_id)
    _manager_guard(target, "task.cancel", target=task.short_id, target_status=task.status)
    TaskStore(target).update(task.id, status="cancelled")
    typer.echo(f"task {task.short_id} cancelled")
    if kill:
        try:
            pid = int(fsio.read_json(runner_lock_path(target, task.id)).get("pid", -1))
        except (OSError, ValueError):
            pid = -1
        if pid > 0 and fsio.pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"sent SIGTERM to runner pid {pid}")


# -- board -----------------------------------------------------------------


@board_app.command("post")
def board_post(
    topic: str,
    text: str,
    type: str = typer.Option("note", "--type", help="Message type tag."),
    sender: str = typer.Option("user", "--from", help="Sender name."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Post a message to a board topic."""
    target = get_home(home)
    _manager_guard(target, "board.post", args=f"{topic}: {text[:80]}")
    if sender == "user":
        sender = current_actor()  # a manager-tagged call attributes itself
    msg = MessageBus(target).post(sender=sender, topic=topic, type=type, text=text)
    typer.echo(f"posted {msg.id} to {topic}")


@board_app.command("read")
def board_read(
    topic: str | None = typer.Argument(None, help="Topic to read (default: all topics)."),
    since: str = typer.Option("24h", "--since", help="Window like 90m, 24h or 7d."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON lines."),
    home: Path | None = _HOME_OPT,
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


# -- projects --------------------------------------------------------------


@project_app.command("add")
def project_add(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Display name (default: dir name)."),
    deadline: str | None = typer.Option(None, "--deadline", help="ISO date, e.g. 2026-09-15."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    notes: str = typer.Option("", "--notes"),
    marker: bool = typer.Option(False, "--marker", help="Also write a .quorum.toml into the project dir."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Register a project directory (tasks run against registered projects)."""
    from .projects import ProjectRegistry

    target = get_home(home)
    _manager_guard(target, "project.add", args=str(path))
    registry = ProjectRegistry(target)
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
        raise _fail(str(e)) from None
    typer.secho(f"registered project {project.slug} ({project.path})", fg="green")


@project_app.command("list")
def project_list(home: Path | None = _HOME_OPT) -> None:
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
    deadline: str | None = typer.Option(None, "--deadline"),
    notes: str | None = typer.Option(None, "--notes"),
    name: str | None = typer.Option(None, "--name"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Update a project's metadata in the registry."""
    from .projects import ProjectRegistry

    target = get_home(home)
    _manager_guard(target, "project.set", target=slug)
    registry = ProjectRegistry(target)
    try:
        project = registry.update(
            slug,
            deadline=deadline,
            notes=notes,
            name=name,
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None,
        )
    except KeyError:
        raise _fail(f"no project {slug!r}") from None
    typer.secho(f"updated {project.slug}" + (f" (due {project.deadline})" if project.deadline else ""), fg="green")


@project_app.command("remove")
def project_remove(slug: str, home: Path | None = _HOME_OPT) -> None:
    """Unregister a project (its directory is untouched)."""
    from .projects import ProjectRegistry

    target = get_home(home)
    _manager_guard(target, "project.remove", target=slug)
    if ProjectRegistry(target).remove(slug):
        typer.echo(f"removed {slug}")
    else:
        raise _fail(f"no project {slug!r}") from None


# -- dashboards ------------------------------------------------------------


@app.command()
def web(
    port: int = typer.Option(8787, "--port"),
    home: Path | None = _HOME_OPT,
) -> None:
    """Serve the local web dashboard on 127.0.0.1 (requires the [web] extra)."""
    target = get_home(home)
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        raise _fail(
            "the web dashboard needs the [web] extra: "
            "uv tool install 'quorum-orchestrator[web]' "
            "(or pip install 'quorum-orchestrator[web]')"
        ) from None
    typer.echo(f"dashboard: http://127.0.0.1:{port}")
    uvicorn.run(create_app(target), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def tui(home: Path | None = _HOME_OPT) -> None:
    """Open the terminal dashboard."""
    target = get_home(home)
    try:
        from .tui.app import QuorumTUI
    except ImportError:
        # textual is a core dependency; only a broken/partial install lands here
        raise _fail(
            "textual is not importable — the TUI ships with quorum by default; "
            "reinstall with: uv tool install quorum-orchestrator"
        ) from None
    QuorumTUI(target).run()


# -- agents ----------------------------------------------------------------


@agent_app.command("list")
def agent_list(home: Path | None = _HOME_OPT) -> None:
    """List configured agents and their last heartbeat."""
    from . import views

    for r in views.agent_rows(get_home(home)):
        state = r["status"] + ("" if r["enabled"] else " (disabled)")
        typer.echo(f"{r['name']:<14} type={r['type']:<20} {r['schedule']:<18} {state}")


@agent_app.command("run-once")
def agent_run_once(name: str, home: Path | None = _HOME_OPT) -> None:
    """Construct an agent and run a single tick (no supervisor needed)."""
    from .agent import AgentContext, tick_lock_path, write_heartbeat
    from .registry import AgentResolutionError, resolve

    target = get_home(home)
    config = _load_config(target)
    acfg = config.agents.get(name)
    if acfg is None:
        raise _fail(f"no agent {name!r} in config.toml") from None
    try:
        cls = resolve(acfg.type, target)
    except AgentResolutionError as e:
        raise _fail(str(e)) from None
    agent = cls(AgentContext(home=target, name=name, settings=acfg.settings, config=config))
    # The same per-agent tick lock the supervisor takes, so a hand-run tick
    # can never interleave with a scheduled one and drop state updates.
    lock = tick_lock_path(target, name)
    try:
        fsio.acquire_pid_lock(lock, meta={"role": "tick", "agent": name})
    except fsio.LockError as e:
        raise _fail(f"agent {name} is ticking elsewhere ({e})") from None
    # Write the same heartbeat the supervisor would, so a hand-run agent stops
    # reading as never-ran in `quorum status` and the dashboards.
    started = fsio.utc_now()
    write_heartbeat(target, name, status="running", last_start=fsio.iso(started))
    try:
        agent.tick()
    except Exception as e:
        write_heartbeat(
            target,
            name,
            status="error",
            last_start=fsio.iso(started),
            last_end=fsio.iso(fsio.utc_now()),
            error=f"{type(e).__name__}: {e}",
        )
        raise
    finally:
        fsio.release_pid_lock(lock)
    ended = fsio.utc_now()
    write_heartbeat(
        target,
        name,
        status="idle",
        last_start=fsio.iso(started),
        last_end=fsio.iso(ended),
        duration_ms=int((ended - started).total_seconds() * 1000),
    )
    typer.secho(f"{name}: tick complete", fg="green")


def _agent_command(home: Path | None, name: str, command: str, note: str) -> None:
    target = get_home(home)
    config = _load_config(target)
    if name not in config.agents:
        raise _fail(f"no agent {name!r} in config.toml") from None
    _manager_guard(target, f"agent.{command}", target=name)
    MessageBus(target).send("user", "supervisor", type=f"agent.{command}", payload={"agent": name})
    typer.echo(note)


@agent_app.command("pause")
def agent_pause(name: str, home: Path | None = _HOME_OPT) -> None:
    """Pause an agent's schedule (applied by a running supervisor within seconds)."""
    _agent_command(home, name, "pause", f"pause queued for {name} — takes effect while `quorum up` is running")


@agent_app.command("resume")
def agent_resume(name: str, home: Path | None = _HOME_OPT) -> None:
    """Resume a paused agent (also clears the auto-pause failure counter)."""
    _agent_command(home, name, "resume", f"resume queued for {name} — takes effect while `quorum up` is running")


@agent_app.command("run-now")
def agent_run_now(name: str, home: Path | None = _HOME_OPT) -> None:
    """Ask the running supervisor to tick an agent immediately."""
    _agent_command(home, name, "run-now", f"run-now queued for {name} — takes effect while `quorum up` is running")


# -- manager ---------------------------------------------------------------


@manager_app.command("tell")
def manager_tell(text: str, home: Path | None = _HOME_OPT) -> None:
    """Send the manager a directive; its next run starts with it in the digest."""
    target = get_home(home)
    MessageBus(target).send("user", "manager", type="directive", text=text)
    typer.secho("directive queued for the manager's next run", fg="green")


@manager_app.command("note")
def manager_note(text: str, home: Path | None = _HOME_OPT) -> None:
    """Journal a reasoning note (the manager's harness calls this; humans can too)."""
    target = get_home(home)
    _manager_guard(target, "note", args=text, always_journal=True)
    typer.echo("noted")


@manager_app.command("journal")
def manager_journal(
    lines: int = typer.Option(20, "-n", "--lines", help="Entries to show."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Print the manager's recent action journal (auto-recorded, per-run tagged)."""
    entries = fsio.read_jsonl_tail(journal_path(get_home(home)), limit=lines)
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


def _parse_window(text: str) -> timedelta:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    text = text.strip()
    if text and text[-1] in units and text[:-1].isdigit():
        return timedelta(**{units[text[-1]]: int(text[:-1])})
    raise typer.BadParameter(f"invalid window {text!r} (use e.g. 90m, 24h, 7d)")


if __name__ == "__main__":
    app()
