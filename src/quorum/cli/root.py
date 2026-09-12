"""The commands that are not in any group: init, up, down, doctor, status,
usage and tui."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from enum import StrEnum

import typer

from .. import doctor as doctor_mod
from .. import home as home_mod
from ._common import (
    _agent_table,
    _agent_usage_table,
    _fail,
    _load_config,
    _parse_window,
    _print_table,
    _project_table,
    _task_table,
    _task_usage_table,
    app,
    get_home,
)


@app.command()
def init() -> None:
    """Create the QUORUM_HOME directory tree and a starter config.toml."""
    target = get_home(must_exist=False)
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
        elif outcome == "unreadable":
            typer.secho(
                f"prompts/{name} cannot be read (not UTF-8, or no permission) — left "
                f"untouched, and every render of it fails; fix or delete it",
                fg="red",
            )
        elif outcome == "seeded" and not fresh:
            typer.echo(f"prompts/{name}: seeded from the packaged default")

# -- supervisor ------------------------------------------------------------


@app.command()
def up(
    detach: bool = typer.Option(
        False, "--detach", help="Start the supervisor in the background and return (`quorum down` stops it)."
    ),
    self_sandbox: bool = typer.Option(
        False, "--self-sandbox", help="Apply a nono-py kernel sandbox to this process before starting."
    ),
) -> None:
    """Run the supervisor: `quorum up` in the foreground (Ctrl-C stops it),
    `quorum up --detach` in the background."""
    from .. import views
    from ..supervisor import Supervisor

    target = get_home()
    config = _load_config(target)
    if detach:
        sup = views.supervisor_status(target)
        if sup.get("alive"):
            raise _fail(f"supervisor already running (pid {sup.get('pid')}) — `quorum down` first")
        from ..actor import strip_actor_env

        log_path = target / "logs" / "supervisor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "quorum", "--home", str(target), "up"]
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
        from ..sandbox import self_sandbox as apply_sandbox

        apply_sandbox(target, config)
    typer.echo(f"quorum supervisor starting (home: {target}) — Ctrl-C to stop")
    Supervisor(target, config).run()


@app.command()
def down() -> None:
    """Stop a running supervisor (started with `quorum up` or `up --detach`)."""
    from .. import views

    target = get_home()
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
    target = get_home(must_exist=False)
    smoke_arg = harness if (smoke or harness) else None
    checks = doctor_mod.run_checks(target, smoke=smoke_arg, smoke_timeout=smoke_timeout)
    counts = doctor_mod.tally(checks)
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


STATUS_LEGEND = """glyphs. The task marks are the same in `status`, `task list`
and the TUI; the agent markers below are this listing's own — the TUI's
agent table prints the status word itself.
  before a task's id:
          ▶ running   ⚭ attached to a live session   ✓ done   ✗ blocked   · other
  after its status:
          ✔ its pull request merged   ⊘ its pull request was closed unmerged.
             Observed by the manager tick, not by this command — no badge
             means nothing was ever observed (no PR yet, or no `gh` here)
  flags:  ⚠ uncommitted/unpushed work in the task's working directory
          waiting-on <ids> unfinished dependencies (`task add --after`); the
             runner refuses to start it. DEP-FAILED / DEP-MISSING / DEP-CYCLE
             name dependencies that can never finish — nothing waits on those,
             they are yours (or the manager's) to decide about
  spend:  $! a run went over [tasks].max_cost_per_run / max_tokens_per_run;
             $! GATED means the last one did, so the next run needs --force.
             cost/tokens are shown when the harness reported them, summed over runs
  agents: ● idle   ◐ running   ✗ error   ‖ paused   ○ never ran
          an agent's own harness spend is shown when its harness reports it"""


@app.command()
def status(
    legend: bool = typer.Option(False, "--legend", help="Explain the status glyphs and exit."),
) -> None:
    """Show supervisor liveness, agents, tasks, and project deadlines
    (`--legend` explains the glyphs)."""
    from .. import views

    if legend:
        typer.echo(STATUS_LEGEND)
        return
    target = get_home()
    sup = views.supervisor_status(target)
    if sup["alive"]:
        typer.secho(f"supervisor: running (pid {sup['pid']}, since {sup['started_at']})", fg="green")
    else:
        typer.secho("supervisor: not running", fg="yellow")

    attention = views.attention_summary(target)
    if attention["count"]:
        typer.secho(
            f"⚠ {attention['count']} on #attention in the last {attention['days']}d "
            "— `quorum board read attention`, then `quorum board clear --id <id>` "
            "for each one you have handled",
            fg="yellow",
        )

    rows = views.agent_rows(target)
    if rows:
        typer.echo("\nagents:")
        _print_table(_agent_table(rows))
        # A failing agent is almost never a quorum bug; it is a harness, an
        # auth token or a config that went quietly wrong, which is exactly
        # what doctor enumerates.
        if any(r["status"] == "error" for r in rows):
            typer.secho("  → an agent is failing: `quorum doctor`", fg="yellow")

    task_rows = views.task_rows(target)
    if task_rows:
        typer.echo("\ntasks:")
        _print_table(_task_table(task_rows))
    else:
        typer.echo("\nno tasks — `quorum task add <project> \"<prompt>\"`")

    projects = views.project_rows(target)
    if projects:
        typer.echo("\nprojects:")
        _print_table(_project_table(projects))
    else:
        typer.echo("no projects registered — `quorum project add <dir>`")


class UsageBy(StrEnum):
    project = "project"
    harness = "harness"
    week = "week"
    agent = "agent"


@app.command("usage")
def usage_cmd(
    by: UsageBy = typer.Option(
        UsageBy.project, "--by", case_sensitive=False, help="Group rows by this dimension."
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only tasks queued (or agent runs made) in the last 7d / 36h / 2w / 90m.",
    ),
) -> None:
    """Usage and delivery statistics by project, harness, week or agent:
    tasks, runs, reruns, cost and tokens as the harness reported them, and
    — where the manager observed a PR — queue-to-run, queue-to-done,
    done-to-merged medians and the share merged. A pure reader over the
    home; the supervisor need not be running."""
    from .. import stats

    target = get_home()
    window = _parse_window(since) if since is not None else None
    try:
        payload = stats.report(target, by=by.value, since=window)
    except ValueError as e:
        raise _fail(str(e)) from None
    what = "agent runs" if by is UsageBy.agent else "tasks queued"
    scope = f"{what} since {payload['cutoff']} ({since.strip()})" if window else "all time"
    typer.echo(f"usage by {by.value}, {scope}")
    if not payload["rows"]:
        typer.echo("nothing recorded" + (" in that window" if window else ""))
        return
    rows = list(payload["rows"])
    if len(rows) > 1 and payload["total"]:
        rows.append(payload["total"])
    if by is UsageBy.agent:
        _print_table(_agent_usage_table(rows))
        typer.echo(
            "cost/tokens: the harness's own figures, summed over the runs that reported them\n"
            "reported: runs that reported any usage, when not all did; duration: the median run"
        )
        return
    _print_table(_task_usage_table(by.value, rows))
    typer.echo(
        "cost/tokens: the harness's own figures, summed over the runs that reported them\n"
        "reported: tasks the cost covers (or, with no cost, that reported anything)\n"
        "queue→run / queue→done / done→merged: medians\n"
        "merged: over the PRs the manager observed — none observed, no figure"
    )

# -- dashboard -------------------------------------------------------------


@app.command()
def tui() -> None:
    """Open the terminal dashboard."""
    target = get_home()
    try:
        from ..tui.app import QuorumTUI
    except ImportError:
        # textual is a core dependency; only a broken/partial install lands here
        raise _fail(
            "textual is not importable — the TUI ships with quorum by default; "
            "reinstall with: uv tool install quorum-orchestrator"
        ) from None
    QuorumTUI(target).run()
