"""`quorum agent`: list, run, read and control agents, the manager included."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import fsio
from .. import transcript as transcript_mod
from ..actor import (
    current_actor,
)
from ..messages import MessageBus

if TYPE_CHECKING:
    pass

from ._common import (
    _FOLLOW_OPT,
    _LAST_OPT,
    _LINES_OPT,
    _RAW_OPT,
    _RUN_OPT,
    _VERBOSE_OPT,
    _actor_guard,
    _agent_table,
    _confirm,
    _echo,
    _fail,
    _follow,
    _load_config,
    _print_table,
    _verbatim_text,
    agent_app,
    get_home,
)

# -- reading a run ---------------------------------------------------------
# `agent log <name>` reads `state/<agent>/`: the digest snapshot a run was
# given, its transcript, the actions the CLI journaled for it, and the ledger
# line saying how it ended. With -n or -f it reads the transcript file
# directly instead, which is what a live tick needs. Rendering is
# `transcript.py`'s, the same one the TUI uses. The manager is an agent like
# any other here — `agent log manager`.


def _resolve_run(home: Path, name: str, ref: str) -> str:
    """A run id from a full id, a unique prefix, or a unique suffix.

    `fsio.resolve_handle`'s grammar, the same one task ids get, for the same
    reason: what a person has in front of them is the tail of a ULID off
    another line of output.
    """
    ids = transcript_mod.run_ids(home, name, limit=0)
    try:
        return fsio.resolve_handle(ref, ids, what=f"{name} run", render=lambda r: r)
    except KeyError:
        raise _fail(f"no {name} run matching {ref!r} (see `quorum agent log {name} --last 5`)") from None
    except ValueError as e:
        raise _fail(str(e)) from None


def _check_agent_name(name: str) -> None:
    """`state/agents/<name>/` is a path, so a name from the outside is a path
    component — held to the same rule `notes.check_owner` holds an owner to,
    with the manager's historical spot the one exemption."""
    from ..config import ConfigError, validate_agent_name

    if name == "manager":
        return
    try:
        validate_agent_name(name)
    except ConfigError as e:
        raise _fail(str(e)) from None


def _run_log(
    name: str, last: int, run: str | None, verbose: bool, raw: bool
) -> None:
    _check_agent_name(name)
    target = get_home()
    if run:
        ids = [_resolve_run(target, name, run)]
    else:
        ids = transcript_mod.run_ids(target, name, limit=max(last, 1))
    if not ids:
        typer.echo(f"no {name} runs recorded yet")
        return
    for i, run_id in enumerate(ids):
        if i:
            typer.echo("")
        _echo(transcript_mod.render_run(target, name, run_id, verbose=verbose, raw=raw))


def _run_tail(
    name: str, lines: int, follow: bool, verbose: bool, raw: bool
) -> None:
    from ..actor import transcript_path

    _check_agent_name(name)
    target = get_home()
    path = transcript_path(target, name)

    def render(entries: list) -> list[str]:
        return transcript_mod.render(entries, verbose=verbose, raw=raw)

    entries = fsio.read_jsonl(path)
    if not entries and not follow:
        typer.echo(f"{name} has written no transcript yet")
        return
    _echo(render(entries[-lines:] if lines > 0 else entries))
    if follow:
        _follow(path, render, len(entries))

# -- agents ----------------------------------------------------------------


@agent_app.command("list")
def agent_list(
    json_out: bool = typer.Option(False, "--json", help="Emit rows as JSON."),
) -> None:
    """List configured agents and their last heartbeat."""
    from .. import views

    rows = views.agent_rows(get_home())
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no agents configured")
        return
    _print_table(_agent_table(rows, with_type=True))


@agent_app.command("run-once")
def agent_run_once(
    name: str,
    verbose: bool = typer.Option(False, "--verbose", help="Show the full traceback when the tick fails."),
) -> None:
    """Construct an agent and run a single tick in this process.

    Use this when the supervisor is stopped, or when you want the tick's
    output and its failure in front of you. `quorum agent run-now` is the
    other one: it asks a *running* supervisor to tick the agent on its own
    schedule thread.
    """
    from ..agent import AgentContext, success_heartbeat_fields, tick_lock_path, write_heartbeat
    from ..registry import AgentResolutionError, resolve

    target = get_home()
    config = _load_config(target)
    acfg = config.agents.get(name)
    if acfg is None:
        raise _fail(f"no agent {name!r} in config.toml or agents/") from None
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
        if verbose:
            raise
        raise _fail(
            f"{name}: tick failed — {type(e).__name__}: {e} (re-run with --verbose for the traceback)"
        ) from None
    finally:
        fsio.release_pid_lock(lock)
    ended = fsio.utc_now()
    # The same success heartbeat a scheduled tick writes, failure fields and
    # escalation stamp cleared: a hand-run tick that demonstrably worked must
    # end the streak, not leave the agent reading as broken.
    write_heartbeat(target, name, **success_heartbeat_fields(started, ended))
    typer.secho(f"{name}: tick complete", fg="green")


@agent_app.command("log")
def agent_log(
    name: str,
    last: int = _LAST_OPT,
    run: str | None = _RUN_OPT,
    lines: int = _LINES_OPT,
    follow: bool = _FOLLOW_OPT,
    verbose: bool = _VERBOSE_OPT,
    raw: bool = _RAW_OPT,
) -> None:
    """Render an agent's run end to end: what it saw, said, did, and cost.

    The manager is an agent: `quorum agent log manager` reads one tick, the
    digest it was given included.

    A finished run reads best whole (`--last 5`, or `--run <id>` for one by
    id). A tick happening right now has no ledger line yet, so `-f` follows
    the transcript file as it is written and `-n 40` prints its last forty
    entries; neither can be combined with --last or --run.
    """
    if lines or follow:
        if run:
            raise _fail("--run reads a finished run; -n/-f follow the transcript itself")
        _run_tail(name, lines, follow, verbose, raw)
        return
    _run_log(name, last, run, verbose, raw)


def _agent_command(name: str, command: str, note: str) -> None:
    target = get_home()
    config = _load_config(target)
    if name not in config.agents:
        raise _fail(f"no agent {name!r} in config.toml or agents/") from None
    _actor_guard(target, f"agent.{command}", target=name)
    MessageBus(target).send("user", "supervisor", type=f"agent.{command}", payload={"agent": name})
    typer.echo(note)


@agent_app.command("pause")
def agent_pause(name: str) -> None:
    """Pause an agent's schedule (applied by a running supervisor within seconds)."""
    _agent_command(name, "pause", f"pause queued for {name} — takes effect while `quorum up` is running")


@agent_app.command("resume")
def agent_resume(name: str) -> None:
    """Resume a paused agent (also clears the auto-pause failure counter)."""
    _agent_command(name, "resume", f"resume queued for {name} — takes effect while `quorum up` is running")


@agent_app.command("run-now")
def agent_run_now(name: str) -> None:
    """Ask the running supervisor to tick an agent immediately.

    This is a message to `quorum up`, so it needs the supervisor running and
    returns before the tick does. With the supervisor stopped, or to watch
    the tick happen, use `quorum agent run-once`.
    """
    _agent_command(name, "run-now", f"run-now queued for {name} — takes effect while `quorum up` is running")


def _prompt_exists(home: Path, name: str) -> bool:
    """True when `name` resolves to a prompt template — one the user wrote in
    prompts/, or one quorum packages (the shipped `babysitter` example)."""
    from .. import prompts

    try:
        prompts.load(home, name)
    except KeyError:
        return False
    return True


@agent_app.command("create")
def agent_create(
    name: str,
    prompt: str = typer.Argument("", help="The agent's prompt body — or `-` to read it from stdin."),
    schedule: str = typer.Option("every 1h", "--schedule", help="'every <N><s|m|h|d>' or 'cron <5 fields>'."),
    type_: str = typer.Option("prompt", "--type", help="Agent type: builtin short name or module:Class."),
    harness: str = typer.Option("", "--harness", help="Harness table for a prompt agent (default: \\[tasks].default_harness)."),
    prompt_template: str = typer.Option("", "--prompt", help="Use an existing template instead of writing one (e.g. the shipped 'babysitter')."),
    timeout: int = typer.Option(0, "--timeout", help="run_timeout_seconds for the agent's harness runs."),
    max_actions: int = typer.Option(0, "--max-actions", help="Per-run action cap for the agent's harness runs."),
) -> None:
    """Create a file-defined agent (agents/<name>.toml + prompts/<name>.md).

    The prompt body is the second argument, or `-` to read it from stdin —
    `quorum agent create standup - < standup.md` — read verbatim, the same
    way `task add` reads a prompt. --prompt instead names a template that
    already exists, so nothing is written to prompts/.

    A running supervisor picks it up within seconds — no restart, and
    config.toml is never touched.
    """
    from ..config import ConfigError, create_agent

    target = get_home()
    text: str | None = None
    if prompt == "-":
        text = _verbatim_text(Path("-"), "prompt")
    elif prompt:
        text = prompt
    template = prompt_template or name
    if prompt_template and text is not None:
        raise _fail("--prompt names an existing template; drop the prompt argument")
    if type_ == "prompt" and text is None and not _prompt_exists(target, template):
        raise _fail(
            f"a prompt agent needs a prompt: pass its text as an argument or `-` to read "
            f"stdin (it becomes prompts/{name}.md), or --prompt <name> naming a template "
            f"that already exists (no prompts/{template}.md, and no packaged default by "
            f"that name)"
        )
    settings: dict = {}
    if prompt_template:
        settings["prompt"] = prompt_template
    if harness:
        settings["harness"] = harness
    if timeout:
        settings["run_timeout_seconds"] = timeout
    if max_actions:
        settings["max_actions_per_run"] = max_actions
    _actor_guard(target, "agent.create", target=name, args=f"{type_} @ {schedule}")
    try:
        create_agent(
            target, name, type_=type_, schedule=schedule, settings=settings, prompt_text=text
        )
    except ConfigError as e:
        raise _fail(str(e)) from None
    MessageBus(target).send(current_actor(), "supervisor", type="agent.reload", payload={"agent": name})
    typer.secho(
        f"agent {name} created (agents/{name}.toml"
        + (f", prompts/{name}.md" if text is not None else "")
        + ") — a running supervisor schedules it within seconds",
        fg="green",
    )


@agent_app.command("remove")
def agent_remove(
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove a file-defined agent (keeps its prompt and state files)."""
    from ..config import agent_file_path

    target = get_home()
    config = _load_config(target)
    path = agent_file_path(target, name)
    if not path.exists():
        if name in config.agents:
            raise _fail(
                f"{name!r} is defined in config.toml — remove it there and restart `quorum up`"
            ) from None
        raise _fail(f"no agent {name!r} — `quorum agent list`") from None
    _confirm(yes, f"remove agents/{name}.toml? (its prompt and state files are kept)")
    _actor_guard(target, "agent.remove", target=name)
    path.unlink()
    MessageBus(target).send(current_actor(), "supervisor", type="agent.reload", payload={"agent": name})
    typer.echo(f"removed agents/{name}.toml (kept prompts/{name}.md and state) — unschedule queued")


@agent_app.command("reload")
def agent_reload(name: str) -> None:
    """Ask the running supervisor to re-read an agent's config (after editing
    agents/<name>.toml or its prompt's settings)."""
    _agent_command(name, "reload", f"reload queued for {name} — takes effect while `quorum up` is running")
