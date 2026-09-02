"""The `quorum` command-line interface."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import typer

from . import doctor as doctor_mod
from . import fsio, usage
from . import home as home_mod
from . import prompts as prompts_mod
from . import prune as prune_mod
from .actor import (
    ACTOR_CAP_ENV,
    ACTOR_RUN_ENV,
    DEFAULT_MAX_ACTIONS_PER_RUN,
    current_actor,
    journal_path,
)
from .messages import MessageBus

app = typer.Typer(
    help="Quorum: orchestrate long-running coding tasks with your own harness.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
board_app = typer.Typer(help="Read and post to the public message board.", no_args_is_help=True)
project_app = typer.Typer(help="Manage registered projects.", no_args_is_help=True)
agent_app = typer.Typer(help="Inspect, run, and control agents.", no_args_is_help=True)
task_app = typer.Typer(help="Create, run, and guide harness-driven tasks.", no_args_is_help=True)
manager_app = typer.Typer(help="Talk to (and audit) the manager agent.", no_args_is_help=True)
prompt_app = typer.Typer(
    help="Inspect prompt templates and their local overlays.", no_args_is_help=True
)
integration_app = typer.Typer(
    help="Install harness adapters (session-adoption hooks and plugins).", no_args_is_help=True
)
app.add_typer(board_app, name="board")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(manager_app, name="manager")
app.add_typer(prompt_app, name="prompt")
app.add_typer(integration_app, name="integration")


def _version_callback(value: bool) -> None:
    if not value:
        return
    from importlib.metadata import PackageNotFoundError, version

    try:
        typer.echo(f"quorum {version('quorum-orchestrator')}")
    except PackageNotFoundError:
        typer.echo("quorum (unknown version — not an installed package)")
    raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Print the quorum version and exit.",
    ),
    home: Path | None = typer.Option(
        None, "--home",
        help="QUORUM_HOME directory (default: $QUORUM_HOME or ~/.quorum); also accepted after any subcommand.",
    ),
) -> None:
    if home is not None:
        # export so every subcommand (and any child it spawns) sees the same home
        os.environ["QUORUM_HOME"] = str(home)

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


def _actor_guard(
    home: Path,
    action: str,
    target: str | None = None,
    target_status: str | None = None,
    args: str | None = None,
    always_journal: bool = False,
) -> None:
    """Auto-journal (and rate-cap) actions taken by a harness-driven agent.

    Agent runs carry the actor env tag (see actor.py): the actor identity,
    a per-run id, and the action cap the agent resolved from its settings.
    Every mutating CLI command routes through here, so the journal is ground
    truth — not the model's self-report — and it is what the next digest
    feeds back to prevent degenerate loops. The only rail is rate: a per-run
    action cap. Choice is never second-guessed.

    User actions journal only when `always_journal` is set; they land in the
    manager's journal so notes left for the manager surface in its digest.
    """
    actor = current_actor()
    if actor == "user" and not always_journal:
        return
    journal = journal_path(home, actor if actor != "user" else "manager")
    run = os.environ.get(ACTOR_RUN_ENV, "") if actor != "user" else ""
    if run:
        try:
            cap = int(os.environ.get(ACTOR_CAP_ENV, DEFAULT_MAX_ACTIONS_PER_RUN))
        except ValueError:
            cap = DEFAULT_MAX_ACTIONS_PER_RUN
        # this run's entries sit at the journal's end, well inside the tail window
        used = len([e for e in fsio.read_jsonl_tail(journal) if e.get("run") == run])
        if used >= cap:
            typer.secho(
                f"action refused: {actor} action cap ({cap}) reached for this run — "
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
    fsio.append_jsonl(journal, entry)


def _confirm(yes: bool, what: str) -> None:
    """Interactive-only guard for destructive commands: prompts on a TTY,
    passes through everywhere else (scripts and harness-driven agents keep
    working; the actor guard remains their only rail)."""
    if yes or not sys.stdin.isatty():
        return
    if not typer.confirm(what):
        raise typer.Exit(1)


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
    fresh, prompts = home_mod.scaffold(target)
    if fresh:
        typer.secho(f"initialized quorum home at {target}", fg="green")
        typer.echo("next:")
        typer.echo(f"  1. edit {target / home_mod.CONFIG_NAME} — uncomment a [harness.*] table "
                   "and set [tasks].default_harness")
        typer.echo("  2. `quorum doctor` — verify the setup")
        typer.echo("  3. `quorum project add <dir>` — register a repo to work on")
        typer.echo("  4. `quorum task add <project> \"<prompt>\"` — queue work")
        typer.echo("  5. `quorum up` — start the supervisor (`--detach` for the background)")
    else:
        typer.echo(f"quorum home at {target} already initialized (config left untouched)")
    for name, outcome in sorted(prompts.items()):
        if outcome == "upgraded":
            typer.secho(f"prompts/{name}: unedited, upgraded to the new packaged default", fg="green")
        elif outcome == "edited":
            stem = name[:-3] if name.endswith(".md") else name
            typer.secho(
                f"prompts/{name}: keeping your edits, but the packaged default has changed — "
                f"`quorum prompt diff {stem}` shows what you are missing",
                fg="yellow",
            )
            typer.echo(
                f"  to resume upgrades: move your own lines into prompts/{stem}.local.md "
                f"(merged in at the default's {{local}} slot, never touched by init), "
                f"then delete prompts/{name} and re-run `quorum init`"
            )
        elif outcome == "seeded" and not fresh:
            typer.echo(f"prompts/{name}: seeded from the packaged default")


# -- prompts ---------------------------------------------------------------


def _prompt_names(home: Path) -> list[str]:
    """Every template name that resolves here: packaged defaults plus
    anything the user wrote into prompts/ (overlays are not templates)."""
    from importlib import resources

    names = set()
    try:
        defaults = resources.files("quorum") / "default_prompts"
        names |= {e.name[:-3] for e in defaults.iterdir() if e.name.endswith(".md")}
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    for entry in fsio.sorted_entries(home / "prompts", suffix=".md"):
        if entry.name.endswith(prompts_mod.LOCAL_SUFFIX):
            continue
        names.add(entry.name[:-3])
    return sorted(names)


def _read_prompt_file(target: Path) -> str | None:
    """A prompt file's text, or None when it cannot be read or decoded.

    One unreadable file must not take the whole listing down with it — see
    `prompt_list`, which marks it `?` and carries on."""
    try:
        return target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


@prompt_app.command("list")
def prompt_list(home: Path | None = _HOME_OPT) -> None:
    """Show each prompt template: home copy vs packaged default, and overlay."""
    target = get_home(home)
    names = _prompt_names(target)
    for name in names:
        default = prompts_mod.packaged(name)
        home_copy = prompts_mod.path(target, name)
        text = default
        if not home_copy.is_file():
            state = "packaged default (no home copy)"
        else:
            text = _read_prompt_file(home_copy)
            if text is None:
                state = "? unreadable (not UTF-8, or no permission) — every render of it fails"
            elif default is None:
                state = "yours (quorum packages no default)"
            elif text == default:
                state = "seeded, matches the packaged default"
            else:
                state = f"edited — `quorum prompt diff {name}` vs the packaged default"
        overlay = prompts_mod.local_path(target, name)
        if overlay.is_file():
            if _read_prompt_file(overlay) is None:
                # render() ignores an overlay it cannot decode; say so here,
                # because silently dead policy is the failure that hurts.
                note = "? unreadable — ignored when rendering"
            elif text is None:
                note = "merged where the template says, once it is readable"
            else:
                note = "{local} slot" if prompts_mod.has_slot(text) else "prepended"
            state += f" + {overlay.name} ({note})"
        typer.echo(f"  {name:<16} {state}")
    # an overlay for a template that does not exist is silently dead policy
    for entry in fsio.sorted_entries(target / "prompts", suffix=prompts_mod.LOCAL_SUFFIX):
        stem = entry.name[: -len(prompts_mod.LOCAL_SUFFIX)]
        if stem not in names:
            typer.secho(
                f"  {entry.name}: no prompt named {stem!r} — this overlay is never rendered",
                fg="yellow",
            )


@prompt_app.command("diff")
def prompt_diff(
    name: str = typer.Argument(help="Template name, e.g. manager (no .md)."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Diff this home's copy of a prompt against the packaged default.

    What `quorum init` will not do for an edited prompt: show you what the
    upgrade would have brought. Move your own lines into prompts/<name>.local.md
    and delete prompts/<name>.md to start receiving them again.
    """
    import difflib

    target = get_home(home)
    name = name[:-3] if name.endswith(".md") else name
    default = prompts_mod.packaged(name)
    if default is None:
        raise _fail(f"quorum packages no default prompt named {name!r} — `quorum prompt list`")
    home_copy = prompts_mod.path(target, name)
    if not home_copy.is_file():
        typer.echo(f"no prompts/{name}.md — this home uses the packaged default unchanged")
        return
    text = _read_prompt_file(home_copy)
    if text is None:
        raise _fail(
            f"prompts/{name}.md cannot be read (not UTF-8, or no permission) — "
            f"nothing to diff; fix or delete it to fall back to the packaged default"
        )
    if text == default:
        typer.echo(f"prompts/{name}.md is identical to the packaged default")
        return
    diff = difflib.unified_diff(
        default.splitlines(keepends=True),
        text.splitlines(keepends=True),
        fromfile=f"packaged default ({name}.md)",
        tofile=f"prompts/{name}.md",
    )
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("+"):
            typer.secho(line, fg="green")
        elif line.startswith("-"):
            typer.secho(line, fg="red")
        elif line.startswith("@@"):
            typer.secho(line, fg="cyan")
        else:
            typer.echo(line)
    overlay = prompts_mod.local_path(target, name)
    typer.echo("")
    typer.echo(
        f"prompts/{name}.md is yours, so `quorum init` never upgrades it. To take the "
        f"packaged default again, keep your own lines in prompts/{name}.local.md "
        + ("(which already exists) " if overlay.is_file() else "")
        + f"and delete prompts/{name}.md."
    )


# -- supervisor ------------------------------------------------------------


@app.command()
def up(
    home: Path | None = _HOME_OPT,
    detach: bool = typer.Option(
        False, "--detach", help="Start the supervisor in the background and return (`quorum down` stops it)."
    ),
    self_sandbox: bool = typer.Option(
        False, "--self-sandbox", help="Apply a nono-py kernel sandbox to this process before starting."
    ),
) -> None:
    """Run the supervisor: `quorum up` in the foreground (Ctrl-C stops it),
    `quorum up --detach` in the background."""
    from . import views
    from .supervisor import Supervisor

    target = get_home(home)
    config = _load_config(target)
    if detach:
        sup = views.supervisor_status(target)
        if sup.get("alive"):
            raise _fail(f"supervisor already running (pid {sup.get('pid')}) — `quorum down` first")
        from .actor import strip_actor_env

        log_path = target / "logs" / "supervisor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "quorum", "up", "--home", str(target)]
        if self_sandbox:
            argv.append("--self-sandbox")
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                argv,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=strip_actor_env({**os.environ, "QUORUM_HOME": str(target)}),
            )
        for _ in range(12):  # give the child up to 3s to take the lock
            time.sleep(0.25)
            if proc.poll() is not None or views.supervisor_status(target).get("alive"):
                break
        if proc.poll() is None and views.supervisor_status(target).get("alive"):
            typer.secho(
                f"supervisor running detached (pid {proc.pid}) — `quorum status` to watch, "
                "`quorum down` to stop",
                fg="green",
            )
        else:
            raise _fail(f"supervisor did not come up — see {log_path}")
        return
    if self_sandbox:
        from .sandbox import self_sandbox as apply_sandbox

        apply_sandbox(target, config)
    typer.echo(f"quorum supervisor starting (home: {target}) — Ctrl-C to stop")
    Supervisor(target, config).run()


@app.command()
def down(home: Path | None = _HOME_OPT) -> None:
    """Stop a running supervisor (started with `quorum up` or `up --detach`)."""
    from . import views

    target = get_home(home)
    sup = views.supervisor_status(target)
    if not sup.get("alive"):
        raise _fail("supervisor is not running")
    pid = int(sup["pid"])
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        # the supervisor releases its lock on shutdown — poll that, not the
        # pid (a detached child can linger as a zombie for its parent)
        if not views.supervisor_status(target).get("alive"):
            typer.secho(f"supervisor stopped (pid {pid})", fg="green")
            return
        time.sleep(0.25)
    raise _fail(f"supervisor (pid {pid}) did not exit within 5s — check `quorum status`")


@app.command()
def doctor(
    harness: str = typer.Argument(
        "",
        metavar="[HARNESS]",
        help="Which harness --smoke runs (default: the configured default harness). "
        "Naming one implies --smoke.",
    ),
    home: Path | None = _HOME_OPT,
    json_out: bool = typer.Option(False, "--json", help="Emit every check as JSON, for scripts."),
    smoke: bool = typer.Option(
        False,
        "--smoke",
        help="Also run the harness once for real, through the runner's own code "
        "(spends tokens).",
    ),
    smoke_timeout: float = typer.Option(
        doctor_mod.DEFAULT_SMOKE_TIMEOUT, "--smoke-timeout", help="Seconds to give the smoke run."
    ),
) -> None:
    """Check everything that fails soft: config, harnesses, projects, state.

    One line per check — ✓ ok, ✗ problem, – not applicable — and exit 1 if
    anything is ✗. Doctor diagnoses and never repairs: every ✗ names the fix.
    It is a pure reader apart from the opt-in `quorum doctor --smoke [HARNESS]`
    probe, which actually runs the harness in a scratch directory.
    """
    target = home_mod.resolve_home(home)
    smoke_arg = harness if (smoke or harness) else None
    checks = doctor_mod.run_checks(target, smoke=smoke_arg, smoke_timeout=smoke_timeout)
    counts = doctor_mod.tally(checks)
    if json_out:
        typer.echo(json.dumps(doctor_mod.report(target, checks), indent=2, ensure_ascii=False))
    else:
        typer.echo(f"home: {target}")
        colors = {doctor_mod.OK: "green", doctor_mod.PROBLEM: "red", doctor_mod.NA: "bright_black"}
        for check in checks:
            typer.secho(f"  {check.glyph} {check.summary}", fg=colors[check.status])
            if check.fix and check.status != doctor_mod.OK:
                typer.secho(f"      → {check.fix}", fg="bright_black")
        if counts["problems"]:
            typer.secho(
                f"\n{counts['problems']} problem(s) — fix the ✗ lines above", fg="red", err=True
            )
        else:
            typer.secho(
                f"\nall checks passed ({counts['ok']} ok, {counts['na']} not applicable)",
                fg="green",
            )
    if counts["problems"]:
        raise typer.Exit(1)


STATUS_LEGEND = """glyphs:
  tasks:  ▶ running   ⚭ attached to a live session   ✓ done   ✗ blocked   · other
          ∞ perpetual: never finishes; only you end it (`task add --perpetual`)
          ⏳ waiting on unfinished dependencies (`task add --after`); the
             runner refuses to start it. DEP-FAILED / DEP-MISSING / DEP-CYCLE
             name dependencies that can never finish — nothing waits on those,
             they are yours (or the manager's) to decide about
          ⚠ uncommitted/unpushed work in the task's workdir
          $! a run went over [tasks].max_cost_per_run / max_tokens_per_run
          cost/tokens are shown when the harness reported them, summed over runs
  agents: ● idle   ◐ running   ✗ error   ‖ paused   ○ never ran
          an agent's own harness spend is shown when its harness reports it"""


@app.command()
def status(
    legend: bool = typer.Option(False, "--legend", help="Explain the status glyphs and exit."),
    json_out: bool = typer.Option(False, "--json", help="Emit the full overview as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Show supervisor liveness, agents, tasks, and project deadlines
    (`--legend` explains the glyphs)."""
    from . import views

    if legend:
        typer.echo(STATUS_LEGEND)
        return
    target = get_home(home)
    if json_out:
        typer.echo(json.dumps(views.overview(target), indent=2, ensure_ascii=False))
        return
    sup = views.supervisor_status(target)
    if sup["alive"]:
        typer.secho(f"supervisor: running (pid {sup['pid']}, since {sup['started_at']})", fg="green")
    else:
        typer.secho("supervisor: not running", fg="yellow")

    attention = views.attention_summary(target)
    if attention["count"]:
        typer.secho(
            f"⚠ {attention['count']} on #attention in the last {attention['days']}d "
            "— `quorum board read attention`",
            fg="yellow",
        )

    rows = views.agent_rows(target)
    if rows:
        typer.echo("\nagents:")
        for r in rows:
            marker = {"idle": "●", "running": "◐", "error": "✗", "paused": "‖"}.get(r["status"], "○")
            line = f"  {marker} {r['name']:<12} {r['status']:<10} schedule: {r['schedule']}"
            if r["last_end"]:
                line += f"  last: {r['last_end']}"
            # Only when the agent's harness reported a spend — an agent that
            # reports nothing (or isn't harness-driven) shows no figure.
            if r.get("usage_text"):
                line += f"  {r['usage_text']}"
            if r["error"]:
                line += f"  [{r['error']}]"
            typer.echo(line)
        # A failing agent is almost never a quorum bug; it is a harness, an
        # auth token or a config that went quietly wrong, which is exactly
        # what doctor enumerates.
        if any(r["status"] == "error" for r in rows):
            typer.secho("  → an agent is failing: `quorum doctor`", fg="yellow")

    task_rows = views.task_rows(target)
    if task_rows:
        typer.echo("\ntasks:")
        for t in task_rows:
            _echo_task_row(t)
    else:
        typer.echo("\nno tasks — `quorum task add <project> \"<prompt>\"`")

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
    else:
        typer.echo("no projects registered — `quorum project add <dir>`")


def _echo_task_row(t: dict) -> None:
    if t.get("attached"):
        marker = "⚭"
    else:
        marker = "▶" if t["running"] else ("✓" if t["status"] == "done" else ("✗" if t["status"] == "blocked" else "·"))
    status = t["status"] + (" ∞" if t.get("perpetual") else "")
    line = f"  {marker} {t['id_short']:<9} {t['project']:<18} {status:<12} {t['harness']}"
    if t["last_report"]:
        line += f"  {t['last_report'][:60]}"
    if t["pr_url"]:
        line += f"  {t['pr_url']}"
    git = t.get("git")
    if git and (git["dirty"] or git["unpushed"]):
        risks = []
        if git["dirty"]:
            risks.append(f"{git['dirty']} uncommitted")
        if git["unpushed"]:
            risks.append(f"{git['unpushed']} unpushed")
        line += "  ⚠ " + ", ".join(risks)
    if t.get("waiting_on"):
        line += f"  waiting-on {','.join(t['waiting_on'])}"
    if t.get("dep_failed"):
        line += f"  DEP-FAILED {','.join(t['dep_failed'])}"
    if t.get("dep_missing"):
        line += f"  DEP-MISSING {','.join(t['dep_missing'])}"
    if t.get("dep_cycle"):
        line += "  DEP-CYCLE"
    if t.get("usage_text"):
        line += f"  {t['usage_text']}"
    if t.get("budget_overages"):
        line += "  $!"
    typer.echo(line)


# -- tasks -----------------------------------------------------------------


@task_app.command("add")
def task_add(
    project: str = typer.Argument(help="Registered project slug (see `quorum project list`)."),
    prompt: str = typer.Argument(help="What the harness should do."),
    harness: str | None = typer.Option(None, "--harness", help="\\[harness.<name>] to use (default: \\[tasks].default_harness)."),
    no_worktree: bool = typer.Option(False, "--no-worktree", help="Run in the project dir itself instead of a git worktree."),
    after: list[str] = typer.Option(None, "--after", help="Do not start before this task finishes (repeatable; accepts short ids)."),
    perpetual: bool = typer.Option(False, "--perpetual", help="A task that is never expected to finish: the manager relaunches it forever and only you end it."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Queue a task. The manager starts it while `quorum up` runs; or start it
    yourself with `quorum task run`.

    Example: quorum task add my-api "fix the flaky auth tests"

    Chain work with --after: `quorum task add my-api "review the PR" --after a1b2c3`
    queues a task the manager will not launch until a1b2c3 finishes.

    With --perpetual the task works in cycles instead of finishing: its
    preamble tells it to deliver every cycle and never report done, the
    manager relaunches it whenever its runner dies, and `quorum task cancel`
    is the only way it ends.
    """
    from .projects import ProjectRegistry
    from .tasks import TaskStore, resolve_dependencies, short_handle

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
    store = TaskStore(target)
    try:
        depends_on = resolve_dependencies(store, after or [])
    except ValueError as e:
        raise _fail(str(e)) from None
    _actor_guard(target, "task.add", args=f"{project}: {prompt[:80]}")
    task = store.add(
        project=project,
        prompt=prompt,
        harness=name,
        use_worktree=config.tasks.worktree and not no_worktree,
        depends_on=depends_on,
        perpetual=perpetual,
    )
    kind = "perpetual task" if perpetual else "task"
    typer.secho(f"queued {kind} {task.short_id} on {project} (harness: {name})", fg="green")
    if depends_on:
        waiting = ", ".join(short_handle(d) for d in depends_on)
        typer.echo(f"waits on: {waiting} — `task run` refuses until they finish (--force overrides)")
    if perpetual:
        typer.echo(
            f"it runs in cycles and never reports done — end it with `quorum task cancel {task.short_id}`"
        )
    typer.echo(f"start now: `quorum task run {task.short_id}` — or let the manager pick it up under `quorum up`")


@task_app.command("adopt")
def task_adopt(
    description: str = typer.Argument("", help="What this session is working on (optional)."),
    session: str = typer.Option("", "--session", help="The harness's own session id (enables exact hook matching and later resume)."),
    directory: Path | None = typer.Option(None, "--dir", help="The session's working directory (default: current directory)."),
    harness: str | None = typer.Option(None, "--harness", help="Which \\[harness.<name>] this session runs (default: \\[tasks].default_harness)."),
    herdr_pane: str = typer.Option("", "--herdr-pane", help="The herdr pane hosting the session (enables pane observation and the nudge doorbell)."),
    json_out: bool = typer.Option(False, "--json", help="Print the created task ids as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Adopt a live interactive coding session into quorum, mid-problem.

    Creates an *attached* task pointing at the session's own directory —
    quorum never spawns runs for it. The manager observes it like any task
    and guides it with `quorum task nudge`; a harness-side hook (see
    `quorum integration list`) delivers the guidance into the live session.

    Example: quorum task adopt "refactoring the auth flow"  (from the session's directory)
    """
    from .projects import ProjectRegistry
    from .tasks import TaskStore, write_attached_state

    target = get_home(home)
    config = _load_config(target)
    workdir = (directory or Path.cwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise _fail(f"no such directory: {workdir}")

    registry = ProjectRegistry(target)
    slug = next(
        (
            p.slug
            for p in registry.list()
            if workdir == p.dir.resolve() or p.dir.resolve() in workdir.parents
        ),
        None,
    )
    registered = False
    if slug is None:
        try:
            slug = registry.add(workdir).slug
            registered = True
        except ValueError as e:
            raise _fail(f"cannot auto-register {workdir} as a project: {e}") from None

    _actor_guard(target, "task.adopt", args=f"{slug}: {str(workdir)}")
    task = TaskStore(target).add(
        project=slug,
        prompt=description or f"adopted interactive session in {workdir}",
        harness=harness or config.tasks.default_harness,
        use_worktree=False,
        workdir=str(workdir),
        session=session or None,
        attached=True,
        status="attached",
    )
    if herdr_pane:
        task = TaskStore(target).update(task.id, herdr_pane=herdr_pane)
    write_attached_state(target, task.id, "adopt", session or None)
    if json_out:
        typer.echo(json.dumps({"id": task.id, "short_id": task.short_id, "project": slug}))
        return
    if registered:
        typer.echo(f"registered {workdir} as project {slug!r}")
    typer.secho(f"adopted session as attached task {task.short_id} on {slug}", fg="green")
    typer.echo(
        "guide it with `quorum task nudge` (delivered at the session's next stop); "
        f"`quorum task detach {task.short_id}` hands it back to the headless runner"
    )


@task_app.command("detach")
def task_detach(task_id: str, home: Path | None = _HOME_OPT) -> None:
    """Detach an adopted task from its interactive session — after this the
    manager may run it headless like any other task."""
    from .tasks import TaskStore

    target = get_home(home)
    task = _resolve_task(target, task_id)
    if not task.attached:
        raise _fail(f"task {task.short_id} is not attached")
    _actor_guard(target, "task.detach", target=task.short_id, target_status=task.status)
    TaskStore(target).update(task.id, attached=False)
    typer.secho(f"task {task.short_id} detached — runnable again", fg="green")


def _match_attached(home: Path, session_id: str, cwd: str):
    """The task a harness hook is speaking for: exact session match first,
    then the working directory. The cwd fallback only fires when the task
    has no *live* session of its own — adopted id-less, or its known session
    already ended (a resume under a fresh id) — so a second concurrent
    session in the same checkout can't steal an adopted task's guidance or
    overwrite its session id."""
    from .tasks import TERMINAL_STATUSES, TaskStore, attached_state

    candidates = [
        t
        for t in TaskStore(home).list()
        if t.attached and t.status not in TERMINAL_STATUSES
    ]
    if session_id:
        for t in candidates:
            if t.session == session_id:
                return t
    if cwd:
        resolved = str(Path(cwd).expanduser().resolve())
        for t in candidates:
            if not (t.workdir and str(Path(t.workdir).expanduser().resolve()) == resolved):
                continue
            if t.session is None:
                return t
            state = attached_state(home, t.id)
            if state and state.get("event") == "session-end":
                return t
    return None


def _read_hook_payload() -> dict:
    import sys as _sys

    try:
        return json.load(_sys.stdin)
    except Exception:
        return {}


@task_app.command("hook-stop", rich_help_panel="Harness protocol")
def task_hook_stop(
    format: str = typer.Option(
        "decision",
        "--format",
        help="Output when guidance is waiting: 'decision' (the Claude Code/Codex "
        "Stop-hook block protocol) or 'text' (bare guidance lines, for shims that "
        "inject the continuation themselves, e.g. the opencode plugin).",
    ),
    home: Path | None = _HOME_OPT,
) -> None:
    """Harness stop/idle-hook entry point (reads the hook's JSON on stdin).

    For an adopted session this refreshes its liveness record and, when
    guidance is waiting in the task inbox, emits it — by default as the
    Stop-hook block-protocol JSON that continues the session (Claude Code and
    Codex speak the same one). For everything else it exits 0 silently — the
    hook is installed globally, so this must stay cheap and mute.
    """
    from .messages import MessageBus
    from .runner import guidance_note
    from .tasks import TaskStore, inbox_name, write_attached_state

    if format not in ("decision", "text"):
        raise _fail(f"unknown --format {format!r} (expected 'decision' or 'text')")
    payload = _read_hook_payload()
    target = home_mod.resolve_home(home)
    if not (target / home_mod.CONFIG_NAME).exists():
        raise typer.Exit(0)
    session_id = str(payload.get("session_id") or "")
    task = _match_attached(target, session_id, str(payload.get("cwd") or ""))
    if task is None:
        raise typer.Exit(0)
    write_attached_state(target, task.id, "stop", session_id or task.session)
    if session_id and session_id != task.session:
        TaskStore(target).update(task.id, session=session_id)
    claimed = list(MessageBus(target).claim(inbox_name(task.id)))
    if not claimed:
        raise typer.Exit(0)
    # Loop-safe by construction: guidance is consumed on delivery, so a
    # blocked stop only recurs while new guidance keeps arriving.
    reason = "Guidance from quorum:\n" + "\n".join(
        f"- {guidance_note(c.message)}" for c in claimed
    )
    try:
        if format == "text":
            typer.echo(reason)
        else:
            typer.echo(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        for c in claimed:
            c.reject()
        raise
    for c in claimed:
        c.ack()


@task_app.command("hook-session-start", rich_help_panel="Harness protocol")
def task_hook_session_start(home: Path | None = _HOME_OPT) -> None:
    """Harness SessionStart-hook entry point: refreshes an adopted task's
    liveness record and learns the (possibly new) session id — harnesses
    whose sessions can't shell out with their own id at adopt time (Codex)
    get it associated here instead."""
    from .tasks import TaskStore, write_attached_state

    payload = _read_hook_payload()
    target = home_mod.resolve_home(home)
    if not (target / home_mod.CONFIG_NAME).exists():
        raise typer.Exit(0)
    session_id = str(payload.get("session_id") or "")
    task = _match_attached(target, session_id, str(payload.get("cwd") or ""))
    if task is None:
        raise typer.Exit(0)
    write_attached_state(target, task.id, "session-start", session_id or task.session)
    if session_id and session_id != task.session:
        TaskStore(target).update(task.id, session=session_id)


@task_app.command("hook-session-end", rich_help_panel="Harness protocol")
def task_hook_session_end(home: Path | None = _HOME_OPT) -> None:
    """Harness SessionEnd-hook entry point: records that an adopted session
    ended (the task stays attached — sessions get reopened)."""
    from .tasks import write_attached_state

    payload = _read_hook_payload()
    target = home_mod.resolve_home(home)
    if not (target / home_mod.CONFIG_NAME).exists():
        raise typer.Exit(0)
    task = _match_attached(
        target, str(payload.get("session_id") or ""), str(payload.get("cwd") or "")
    )
    if task is None:
        raise typer.Exit(0)
    write_attached_state(target, task.id, "session-end", task.session)


@task_app.command("list")
def task_list(
    json_out: bool = typer.Option(False, "--json", help="Emit rows as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """List tasks, newest last (`quorum status --legend` explains the glyphs)."""
    from . import views

    rows = views.task_rows(get_home(home))
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no tasks — `quorum task add <project> \"<prompt>\"`")
        return
    for t in rows:
        _echo_task_row(t)


@task_app.command("show")
def task_show(
    task_id: str,
    json_out: bool = typer.Option(False, "--json", help="Dump the full task record as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Show one task: what it is, where it stands, and its recent reports."""
    from .config import load_config_or_default
    from .tasks import TaskStore, dependency_state, read_reports, runner_alive, short_handle

    target = get_home(home)
    task = _resolve_task(target, task_id)
    if json_out:
        typer.echo(json.dumps(task.model_dump(), indent=2, ensure_ascii=False))
        return
    running = runner_alive(target, task.id)
    state = task.status
    if task.attached:
        state += " (attached to a live session)"
    elif running:
        state += " (runner alive)"
    if task.perpetual:
        state += " [perpetual — only you end it]"
    typer.echo(f"task {task.short_id}  ({task.id})")
    typer.echo(f"  project:  {task.project}")
    typer.echo(f"  status:   {state}")
    typer.echo(f"  harness:  {task.harness}")
    typer.echo(f"  prompt:   {task.prompt}")
    typer.echo(f"  workdir:  {task.workdir or '(worktree created on first run)'}")
    if task.session:
        typer.echo(f"  session:  {task.session}")
    if task.pr_url:
        typer.echo(f"  pr:       {task.pr_url}")
    if task.depends_on:
        deps = dependency_state(task, {t.id: t for t in TaskStore(target).list()})
        line = ", ".join(short_handle(d) for d in task.depends_on)
        if deps["waiting_on"]:
            line += f"  (waiting on {', '.join(deps['waiting_on'])})"
        if deps["failed"]:
            line += f"  DEP-FAILED: {', '.join(deps['failed'])}"
        if deps["missing"]:
            line += f"  DEP-MISSING: {', '.join(deps['missing'])}"
        if deps["cycle"]:
            line += "  DEP-CYCLE"
        typer.echo(f"  after:    {line}")
    if task.runs:
        last = task.runs[-1]
        typer.echo(
            f"  runs:     {len(task.runs)} (last: {last.started_at} → "
            f"{last.ended_at or 'running'}, exit {last.exit_code if last.exit_code is not None else '—'})"
        )
        spent = usage.describe(usage.total(r.usage for r in task.runs))
        if spent:
            typer.echo(f"  usage:    {spent} (as reported by the harness)")
        config = load_config_or_default(target)
        for note in usage.run_overages(
            task.runs, config.tasks.max_cost_per_run, config.tasks.max_tokens_per_run
        ):
            typer.secho(f"  budget:   {note}", fg="yellow")
    typer.echo(f"  updated:  {task.updated_at}")
    reports = read_reports(target, task.id, limit=10)
    if reports:
        typer.echo("recent reports:")
        for r in reports:
            typer.echo(f"  [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')}")
    typer.echo(f"more: `quorum task tail {task.short_id}` for the transcript, `--json` for the raw record")


@task_app.command("run")
def task_run(
    task_id: str,
    detach: bool = typer.Option(False, "--detach", help="Start the run in the background and return."),
    force: bool = typer.Option(False, "--force", help="Run even while the task's dependencies are unfinished."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Execute one harness run of a task (the manager does this automatically
    under `quorum up`)."""
    from .runner import RunnerError, launch_detached, run_task, unmet_dependencies

    target = get_home(home)
    task = _resolve_task(target, task_id)
    # mirror the runner's substrate rail here so --detach fails in the
    # parent too, instead of journaling a success and refusing in the child
    if task.attached:
        raise _fail(
            f"task {task.short_id} is attached to a live interactive session — "
            "guide it with `quorum task nudge`, or `quorum task detach` it first"
        )
    if not force:
        from .tasks import TaskStore

        blockers = unmet_dependencies(TaskStore(target), task)
        if blockers:
            raise _fail(
                f"task {task.short_id} is waiting on {', '.join(blockers)} — "
                "unfinished dependencies; `--force` to run anyway"
            )
    _actor_guard(target, "task.run", target=task.short_id, target_status=task.status)
    if detach:
        pid = launch_detached(target, task.id, force=force)
        typer.secho(f"task {task.short_id} running detached (pid {pid}) — `quorum task tail {task.short_id}`", fg="green")
        return
    config = _load_config(target)
    try:
        code = run_task(target, config, task.id, force=force)
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


@task_app.command("report", rich_help_panel="Harness protocol")
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
    _actor_guard(target, "task.report", target=task.short_id, target_status=task.status,
                   args=status)
    tasks_mod.report(target, task.id, status=status, text=text, pr_url=pr_url)
    typer.echo(f"task {task.short_id}: {status}" + (f" ({pr_url})" if pr_url else ""))


@task_app.command("inbox")
def task_inbox(
    task_id: str,
    claim: bool = typer.Option(False, "--claim", help="Consume the messages (what a harness should do)."),
    clear: bool = typer.Option(
        False, "--clear", help="Archive the pending guidance instead of delivering it."
    ),
    home: Path | None = _HOME_OPT,
) -> None:
    """Read guidance sent to a task. Without --claim, messages are only peeked."""
    from .runner import guidance_note
    from .tasks import inbox_name

    target = get_home(home)
    task = _resolve_task(target, task_id)
    bus = MessageBus(target)
    if clear:
        if claim:
            raise _fail("--claim delivers guidance and --clear discards it — pick one")
        pending = bus.clear_inbox(inbox_name(task.id), dry_run=True)
        if not pending:
            typer.echo("no guidance waiting")
            return
        _actor_guard(
            target, "task.inbox.clear", target=task.short_id, target_status=task.status,
            args=f"{len(pending)} message(s)",
        )
        cleared = bus.clear_inbox(inbox_name(task.id))
        typer.secho(
            f"archived {len(cleared)} pending message(s) for task {task.short_id}", fg="green"
        )
        return
    if claim:
        found = False
        for claimed in bus.claim(inbox_name(task.id)):
            typer.echo(guidance_note(claimed.message))
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
    mid-run) will see it.

    Example: quorum task nudge a3f2k9 "use the existing retry helper"
    """
    from .tasks import nudge

    target = get_home(home)
    task = _resolve_task(target, task_id)
    _actor_guard(target, "task.nudge", target=task.short_id, target_status=task.status,
                   args=text[:80])
    nudge(target, task, text, sender=current_actor())
    typer.secho(f"guidance queued for task {task.short_id}", fg="green")


@task_app.command("cancel")
def task_cancel(
    task_id: str,
    kill: bool = typer.Option(False, "--kill", help="Also SIGTERM a live runner."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Mark a task cancelled so the manager stops attending to it."""
    from .tasks import TaskStore, runner_lock_path

    target = get_home(home)
    task = _resolve_task(target, task_id)
    if kill:
        _confirm(yes, f"cancel task {task.short_id} and SIGTERM its live runner?")
    _actor_guard(target, "task.cancel", target=task.short_id, target_status=task.status)
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


@task_app.command("prune")
def task_prune(
    status: str = typer.Option(
        ",".join(prune_mod.DEFAULT_STATUSES), "--status",
        help="Comma-separated statuses to archive (default: the terminal ones).",
    ),
    older_than: str | None = typer.Option(
        None, "--older-than", help="Only tasks untouched for longer than this (e.g. 24h, 7d)."
    ),
    worktrees: bool = typer.Option(
        False, "--worktrees", help="Also `git worktree remove` and delete the task branch when merged."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; change nothing."),
    force: bool = typer.Option(
        False, "--force",
        help="Archive despite stranded work, and `git branch -D` an unmerged branch "
             "(losing its commits). Never forces a dirty worktree's removal.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Archive finished tasks into `tasks/.archive/<id>/` so they leave every view.

    A task directory is never deleted: it is *moved*, and moving it back
    restores it. Refuses a task with a live runner, an attached task, one
    another task still depends on, and (without --force) one whose worktree
    holds uncommitted or unpushed work. With --worktrees, a worktree git
    refuses to remove is kept and its task left unarchived — --force does
    not override that, though it does force the branch delete.
    """
    from .tasks import TaskStore

    target = get_home(home)
    window = _parse_window(older_than) if older_than else None
    statuses = [s for s in status.split(",") if s.strip()]
    candidates = prune_mod.plan(target, statuses=statuses, older_than=window, force=force)
    prunable = [c for c in candidates if c.prunable]
    for c in candidates:
        if c.refusal:
            typer.secho(f"  skip {c.task.short_id}  {c.refusal}", fg="yellow")
    if not prunable:
        typer.echo("nothing to prune" + ("" if candidates else f" matching {status}"))
        return
    verb = "would archive" if dry_run else "archiving"
    typer.echo(f"{verb} {len(prunable)} task(s):")
    for c in prunable:
        typer.echo(f"  {c.task.short_id}  {c.task.status:<10} {c.task.prompt[:60]}")
        if dry_run and worktrees:
            for note in prune_mod.worktree_plan(target, c.task, force=force):
                typer.echo(f"    {note}")
    if dry_run:
        typer.echo("(dry run — nothing changed)")
        return
    _confirm(
        yes,
        f"archive {len(prunable)} task(s) into tasks/.archive"
        + (
            (
                " and remove their worktrees, force-deleting unmerged branches?"
                if force
                else " and remove their worktrees, deleting merged branches?"
            )
            if worktrees
            else "?"
        ),
    )
    # One journal entry for the command, not one per task: a prune is a
    # single decision, and per-task entries would burn an agent's action cap
    # mid-sweep and leave the tidy half-finished.
    _actor_guard(
        target, "task.prune",
        args=f"{len(prunable)} task(s): {', '.join(c.task.short_id for c in prunable)}"
             + (" +worktrees" if worktrees else ""),
    )
    archived = 0
    # The batch as it stands *now*: a task that turns out to be unprunable
    # mid-sweep leaves it, so an upstream that only passed the dependency
    # check because its dependent was going too is refused again rather than
    # archived into a dangling `depends_on`. `plan` orders dependents first,
    # which is what makes that in-order recheck enough.
    by_id = {t.id: t for t in TaskStore(target).list()}
    batch = {c.task.id for c in prunable}
    for c in prunable:
        # Re-read the refusals: an interactive confirm is a long time for a
        # runner to take the lock, and the batch may have shrunk above.
        again = prune_mod.refusal(target, c.task, by_id, selected=batch, force=force)
        if again:
            typer.secho(f"  skip {c.task.short_id}  {again}", fg="yellow")
            batch.discard(c.task.id)
            continue
        if worktrees:
            removed, notes = prune_mod.remove_task_worktree(target, c.task, force=force)
            for note in notes:
                typer.echo(f"  {c.task.short_id}  {note}")
            if not removed:
                typer.secho(
                    f"  skip {c.task.short_id}  worktree kept, task not archived", fg="yellow"
                )
                batch.discard(c.task.id)
                continue
        try:
            prune_mod.archive_task(target, c.task.id)
        except OSError as e:
            typer.secho(f"  skip {c.task.short_id}  {e}", fg="yellow")
            batch.discard(c.task.id)
            continue
        archived += 1
    typer.secho(f"archived {archived} task(s) into tasks/.archive", fg="green")



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
    _actor_guard(target, "board.post", args=f"{topic}: {text[:80]}")
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


@board_app.command("clear")
def board_clear(
    topic: str,
    before: str | None = typer.Option(
        None, "--before",
        help="Only messages older than this: a window (7d) or a timestamp (2026-09-01).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be archived."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Archive a board topic, emptying it.

    The messages land in the same `messages/archive/YYYY-MM.jsonl.gz` the
    hourly janitor writes — nothing is lost, it just stops being live.
    `quorum board clear attention` is the one that empties the banner.
    """
    target = get_home(home)
    floor = _parse_before(before) if before else None
    bus = MessageBus(target)
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
    _actor_guard(target, "board.clear", args=f"{topic}: {len(doomed)} message(s)")
    archived = bus.archive_topic(topic, before=floor)
    typer.secho(f"archived {len(archived)} message(s) from {topic}", fg="green")



# -- projects --------------------------------------------------------------


@project_app.command("add")
def project_add(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Display name (default: dir name)."),
    deadline: str | None = typer.Option(None, "--deadline", help="ISO date, e.g. 2026-09-15."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    notes: str = typer.Option("", "--notes", help="Free-form notes shown in views."),
    marker: bool = typer.Option(False, "--marker", help="Also write a .quorum.toml into the project dir."),
    force: bool = typer.Option(False, "--force", help="Register even if the directory is not a git repository."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Register a project directory (tasks run against registered projects).

    Example: quorum project add ~/work/my-api --deadline 2026-09-15
    """
    from .projects import ProjectRegistry

    target = get_home(home)
    resolved = path.expanduser().resolve()
    if resolved.is_dir() and not (resolved / ".git").exists() and not force:
        raise _fail(
            f"{resolved} is not a git repository — tasks need one for worktrees; "
            "`git init` it, or pass --force to register anyway (tasks there will need --no-worktree)"
        )
    _actor_guard(target, "project.add", args=str(path))
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
def project_list(
    json_out: bool = typer.Option(False, "--json", help="Emit rows as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """List registered projects (marker-file fields merged in)."""
    from . import views

    rows = views.project_rows(get_home(home))
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
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
    deadline: str | None = typer.Option(None, "--deadline", help="ISO date; an empty string clears it."),
    notes: str | None = typer.Option(None, "--notes"),
    name: str | None = typer.Option(None, "--name"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Update a project's metadata in the registry."""
    from .projects import ProjectRegistry

    target = get_home(home)
    _actor_guard(target, "project.set", target=slug)
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
def project_remove(
    slug: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Unregister a project (its directory is untouched)."""
    from .projects import ProjectRegistry

    target = get_home(home)
    _confirm(yes, f"unregister project {slug!r}? (its directory is untouched)")
    _actor_guard(target, "project.remove", target=slug)
    if ProjectRegistry(target).remove(slug):
        typer.echo(f"removed {slug}")
    else:
        raise _fail(f"no project {slug!r}") from None


# -- integrations ----------------------------------------------------------


def _integrations_root() -> Path:
    """The bundled harness adapters: inside the package in a wheel install,
    at the repo root in a checkout."""
    packaged = Path(__file__).resolve().parent / "integrations"
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "integrations"
    if checkout.is_dir():
        return checkout
    raise _fail("no bundled integrations found — reinstall quorum-orchestrator")


def _adapter_files(root: Path, name: str) -> list[tuple[Path, Path]]:
    """(source, destination) pairs for a copy-installed adapter."""
    if name == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return [
            (root / "codex" / "hooks.json", codex_home / "hooks.json"),
            (
                root / "codex" / "prompts" / "quorum-adopt.md",
                codex_home / "prompts" / "quorum-adopt.md",
            ),
        ]
    if name == "opencode":
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
        return [
            (root / "opencode" / "plugin" / "quorum.js", cfg / "plugins" / "quorum.js"),
            (
                root / "opencode" / "commands" / "quorum-adopt.md",
                cfg / "commands" / "quorum-adopt.md",
            ),
        ]
    return []


_ADAPTER_NOTES = {
    "claude-code": "adopt with /quorum:adopt inside a session",
    "codex": "Codex asks you to trust the new hooks once; adopt with /prompts:quorum-adopt",
    "opencode": "adopt with /quorum-adopt inside a session",
}


@integration_app.command("list")
def integration_list() -> None:
    """Show the bundled harness adapters and whether they are installed."""
    root = _integrations_root()
    for name in ("claude-code", "codex", "opencode"):
        if name == "claude-code":
            state = "plugin-managed"
            detail = f"`claude plugin install {root / name}`"
        else:
            files = _adapter_files(root, name)
            installed = sum(1 for _, dest in files if dest.exists())
            state = (
                "installed" if installed == len(files)
                else "partial" if installed
                else "not installed"
            )
            detail = ", ".join(str(dest) for _, dest in files)
        typer.echo(f"{name:<12} {state:<14} {detail}")
    typer.echo("\ninstall one: `quorum integration install <name>`")


@integration_app.command("install")
def integration_install(
    name: str = typer.Argument(help="Adapter: claude-code, codex, or opencode."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing destination files."),
) -> None:
    """Install a harness adapter so live sessions can be adopted (`quorum task adopt`).

    Copies the adapter's hook config or plugin to the harness's user-wide
    config location; per-project installs are described in the adapter's
    README (integrations/<name>/README.md in the repo).
    """
    root = _integrations_root()
    if name == "claude-code":
        typer.echo("Claude Code adapters install through its plugin manager — run:")
        typer.echo(f"  claude plugin install {root / 'claude-code'}")
        typer.echo("then adopt a session with /quorum:adopt (manual, plugin-less install: "
                   "see the README in that directory)")
        return
    files = _adapter_files(root, name)
    if not files:
        raise _fail(f"no adapter {name!r} (available: claude-code, codex, opencode)")
    for src, dest in files:
        if dest.exists() and not force:
            if dest.read_bytes() == src.read_bytes():
                typer.echo(f"{dest} already installed (identical)")
                continue
            raise _fail(
                f"{dest} already exists with different content — merge the entries from "
                f"{src} by hand (see {root / name / 'README.md'}), or re-run with --force to overwrite"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        typer.secho(f"installed {dest}", fg="green")
    note = _ADAPTER_NOTES.get(name)
    if note:
        typer.echo(note)


# -- dashboards ------------------------------------------------------------


@app.command()
def web(
    port: int = typer.Option(8787, "--port"),
    home: Path | None = _HOME_OPT,
) -> None:
    r"""Serve the local web dashboard on 127.0.0.1 (requires the \[web] extra)."""
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
def agent_list(
    json_out: bool = typer.Option(False, "--json", help="Emit rows as JSON."),
    home: Path | None = _HOME_OPT,
) -> None:
    """List configured agents and their last heartbeat."""
    from . import views

    rows = views.agent_rows(get_home(home))
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for r in rows:
        state = r["status"] + ("" if r["enabled"] else " (disabled)")
        typer.echo(f"{r['name']:<14} type={r['type']:<20} {r['schedule']:<18} {state}")


@agent_app.command("run-once")
def agent_run_once(
    name: str,
    verbose: bool = typer.Option(False, "--verbose", help="Show the full traceback when the tick fails."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Construct an agent and run a single tick (no supervisor needed)."""
    from .agent import AgentContext, success_heartbeat_fields, tick_lock_path, write_heartbeat
    from .registry import AgentResolutionError, resolve

    target = get_home(home)
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


def _agent_command(home: Path | None, name: str, command: str, note: str) -> None:
    target = get_home(home)
    config = _load_config(target)
    if name not in config.agents:
        raise _fail(f"no agent {name!r} in config.toml or agents/") from None
    _actor_guard(target, f"agent.{command}", target=name)
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


def _prompt_exists(home: Path, name: str) -> bool:
    """True when `name` resolves to a prompt template — one the user wrote in
    prompts/, or one quorum packages (the shipped `babysitter` example)."""
    from . import prompts

    try:
        prompts.load(home, name)
    except KeyError:
        return False
    return True


@agent_app.command("create")
def agent_create(
    name: str,
    schedule: str = typer.Option("every 1h", "--schedule", help="'every <N><s|m|h|d>' or 'cron <5 fields>'."),
    type_: str = typer.Option("prompt", "--type", help="Agent type: builtin short name or module:Class."),
    harness: str = typer.Option("", "--harness", help="Harness table for a prompt agent (default: \\[tasks].default_harness)."),
    prompt_file: Path | None = typer.Option(None, "--prompt-file", help="File whose contents seed prompts/<name>.md."),
    prompt_text: str = typer.Option("", "--prompt-text", help="Inline prompt body for prompts/<name>.md."),
    prompt_template: str = typer.Option("", "--prompt", help="Use an existing template instead of writing one (e.g. the shipped 'babysitter')."),
    timeout: int = typer.Option(0, "--timeout", help="run_timeout_seconds for the agent's harness runs."),
    max_actions: int = typer.Option(0, "--max-actions", help="Per-run action cap for the agent's harness runs."),
    home: Path | None = _HOME_OPT,
) -> None:
    """Create a file-defined agent (agents/<name>.toml + prompts/<name>.md).

    A running supervisor picks it up within seconds — no restart, and
    config.toml is never touched.
    """
    from .config import ConfigError, create_agent

    target = get_home(home)
    text: str | None = None
    if prompt_file is not None:
        try:
            text = prompt_file.read_text(encoding="utf-8")
        except OSError as e:
            raise _fail(f"cannot read {prompt_file}: {e}") from None
    elif prompt_text:
        text = prompt_text
    template = prompt_template or name
    if prompt_template and text is not None:
        raise _fail("--prompt names an existing template; drop --prompt-text/--prompt-file")
    if type_ == "prompt" and text is None and not _prompt_exists(target, template):
        raise _fail(
            f"a prompt agent needs a prompt: --prompt-text or --prompt-file (they become "
            f"prompts/{name}.md), or --prompt <name> naming a template that already exists "
            f"(no prompts/{template}.md, and no packaged default by that name)"
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
    home: Path | None = _HOME_OPT,
) -> None:
    """Remove a file-defined agent (keeps its prompt and state files)."""
    from .config import agent_file_path

    target = get_home(home)
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
def agent_reload(name: str, home: Path | None = _HOME_OPT) -> None:
    """Ask the running supervisor to re-read an agent's config (after editing
    agents/<name>.toml or its prompt's settings)."""
    _agent_command(home, name, "reload", f"reload queued for {name} — takes effect while `quorum up` is running")


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
    _actor_guard(target, "note", args=text, always_journal=True)
    typer.echo("noted")


_AGENT_OPT = typer.Option(
    "manager", "--agent",
    help="Whose notebook (default: the manager's). An agent may only write its own.",
)


@manager_app.command("remember")
def manager_remember(
    text: str,
    ttl: int = typer.Option(0, "--ttl", help="Days until the note expires (0: never)."),
    agent: str = _AGENT_OPT,
    home: Path | None = _HOME_OPT,
) -> None:
    """Write a standing note every future run of that agent will read.

    The notebook (`state/manager/notes.jsonl`) is not the journal: `note`
    records why *this* run did what it did, `remember` records a fact the
    next run needs. Tasks and other agents are refused — they reach the
    manager with `task report` and `board post`.
    """
    from . import notes as notes_mod

    target = get_home(home)
    actor = current_actor()
    if not notes_mod.may_write(actor, agent):
        # journal the refusal too: an agent reaching for someone else's
        # notebook is exactly the kind of thing the next digest should show
        _actor_guard(target, "remember.refused", target=agent, args=text, always_journal=True)
        raise _fail(
            f"action refused: {actor} may not write to {agent}'s notebook — "
            "report to it with `quorum task report`, or reach it on the board "
            "with `quorum board post attention`"
        )
    _actor_guard(target, "remember", args=text, always_journal=True)
    try:
        entry = notes_mod.remember(
            target, text, owner=agent, sender=actor,
            run_id=os.environ.get(ACTOR_RUN_ENV, ""), ttl_days=ttl or None,
        )
    except notes_mod.NotebookError as e:
        raise _fail(str(e)) from None
    typer.secho(
        f"remembered ({notes_mod.short_id(entry['id'])}) — every future {agent} run reads it"
        + (f", for {ttl}d" if ttl else ""),
        fg="green",
    )


@manager_app.command("forget")
def manager_forget(
    note_id: str,
    agent: str = _AGENT_OPT,
    home: Path | None = _HOME_OPT,
) -> None:
    """Retire a standing note that stopped being true (append-only: the file
    keeps it, readers hide it)."""
    from . import notes as notes_mod

    target = get_home(home)
    actor = current_actor()
    if not notes_mod.may_write(actor, agent):
        _actor_guard(target, "forget.refused", target=agent, args=note_id, always_journal=True)
        raise _fail(f"action refused: {actor} may not write to {agent}'s notebook")
    _actor_guard(target, "forget", target=note_id, always_journal=True)
    try:
        note = notes_mod.forget(
            target, note_id, owner=agent, sender=actor,
            run_id=os.environ.get(ACTOR_RUN_ENV, ""),
        )
    except notes_mod.NotebookError as e:
        raise _fail(str(e)) from None
    typer.echo(f"forgot ({notes_mod.short_id(note['id'])}) {note.get('text', '')[:60]}")


@manager_app.command("notes")
def manager_notes(
    agent: str = _AGENT_OPT,
    home: Path | None = _HOME_OPT,
) -> None:
    """Print the notebook exactly as the digest renders it for that agent."""
    from . import notes as notes_mod

    # reading is validated like writing: `--agent` is a path component under
    # state/agents/, so `../../whatever` must not read outside QUORUM_HOME
    try:
        notes_mod.check_owner(agent)
        section = notes_mod.digest_section(get_home(home), owner=agent)
    except notes_mod.NotebookError as e:
        raise _fail(str(e)) from None
    for line in section:
        typer.echo(line)


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


def _parse_before(text: str) -> datetime:
    """A cutoff instant, written either way round: a window back from now
    (`7d`) or an absolute timestamp (`2026-09-01`, `2026-09-01T12:00:00Z`)."""
    text = text.strip()
    try:
        return fsio.utc_now() - _parse_window(text)
    except typer.BadParameter:
        pass
    try:
        return fsio.parse_iso(text)
    except ValueError:
        raise typer.BadParameter(
            f"invalid cutoff {text!r} (use a window like 7d, or a date like 2026-09-01)"
        ) from None


if __name__ == "__main__":
    app()
