"""`quorum task`: queue, run, guide, read and retire tasks."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import typer

from .. import fsio, usage
from .. import home as home_mod
from .. import prune as prune_mod
from .. import transcript as transcript_mod
from ..actor import current_actor
from ..messages import MessageBus
from ._common import (
    _FOLLOW_OPT,
    _LINES_OPT,
    _RAW_OPT,
    _VERBOSE_OPT,
    _actor_guard,
    _confirm,
    _echo,
    _fail,
    _follow,
    _journal_task,
    _load_config,
    _notebook_write,
    _parse_window,
    _print_table,
    _resolve_task,
    _task_action,
    _task_prompt,
    _task_table,
    _verbatim_text,
    get_home,
    task_app,
)


@task_app.command("add")
def task_add(
    project: str = typer.Argument(help="Registered project slug (see `quorum project list`)."),
    prompt: str = typer.Argument("", help="What the harness should do — or `-` to read it from stdin."),
    issue: str | None = typer.Option(None, "--issue", help="Queue this forge issue (number or URL): its title and body become the prompt."),
    harness: str | None = typer.Option(None, "--harness", help="\\[harness.<name>] to use (default: \\[tasks].default_harness)."),
    no_worktree: bool = typer.Option(False, "--no-worktree", help="Run in the project dir itself instead of a git worktree."),
    after: list[str] = typer.Option(None, "--after", help="Do not start before this task finishes (repeatable; accepts short ids)."),
    perpetual: bool = typer.Option(False, "--perpetual", help="A task that is never expected to finish: the manager relaunches it forever and only you end it."),
) -> None:
    """Queue a task. The manager starts it while `quorum up` runs; or start it
    yourself with `quorum task run`.

    Example: quorum task add my-api "fix the flaky auth tests"

    A long prompt does not have to fight the shell: `-` reads it from stdin,
    verbatim — `quorum task add my-api - < plan.md` queues a file.

    --issue queues an issue directly — `quorum task add my-api --issue 62`
    (a number or a full URL) fetches its title and body through the forge
    CLI, makes them the prompt, and records the issue url on the task so
    every listing shows `#62` and the harness can reference it in its PR. A
    prompt given as well is *appended* as extra instructions. Unlike the
    manager's PR probe this fails loudly: no gh, no auth or an unknown issue
    is an error, since the alternative is a task queued without its work.

    Chain work with --after: `quorum task add my-api "review the PR" --after a1b2c3`
    queues a task the manager will not launch until a1b2c3 finishes.

    With --perpetual the task works in cycles instead of finishing: its
    preamble tells it to deliver every cycle and never report done, the
    manager relaunches it whenever its runner dies, and `quorum task cancel`
    is the only way it ends.
    """
    from ..projects import ProjectRegistry
    from ..tasks import TaskStore, resolve_dependencies, short_handle

    target = get_home()
    config = _load_config(target)
    known_project = ProjectRegistry(target).get(project)
    if known_project is None:
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
    # The prompt is read *last*, after everything that can be checked without
    # consuming it: a piped issue is gone the moment stdin is drained, so a
    # typo in the slug must not eat it. With --issue the prompt is optional —
    # the issue is the work, and anything given here is appended to it.
    text = _task_prompt(prompt, optional=issue is not None)
    # The forge call comes after every free check for the same reason, and
    # it spends a subprocess and a network round trip besides. It runs in
    # the project's own directory so a bare number resolves against its
    # remote, exactly as gh does for a human standing in that checkout.
    issue_url = None
    if issue is not None:
        from ..forge import ForgeError, issue_prompt, issue_view

        try:
            fetched = issue_view(target, issue, known_project.dir)
        except ForgeError as e:
            raise _fail(str(e)) from None
        issue_url = fetched["url"]
        # Any prompt given as well is extra instructions *about* the issue,
        # so it follows the issue rather than framing it.
        text = issue_prompt(fetched) + (f"\n\n{text}" if text.strip() else "")
    _actor_guard(target, "task.add", args=f"{project}: {text[:80]}")
    task = store.add(
        project=project,
        prompt=text,
        harness=name,
        use_worktree=config.tasks.worktree and not no_worktree,
        depends_on=depends_on,
        perpetual=perpetual,
        issue_url=issue_url,
    )
    kind = "perpetual task" if perpetual else "task"
    typer.secho(f"queued {kind} {task.short_id} on {project} (harness: {name})", fg="green")
    if issue_url:
        typer.echo(f"from issue: {issue_url}")
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
) -> None:
    """Adopt a live interactive coding session into quorum, mid-problem.

    Creates an *attached* task pointing at the session's own directory —
    quorum never spawns runs for it. The manager observes it like any task
    and guides it with `quorum task nudge`; a harness-side hook (see
    `quorum integration list`) delivers the guidance into the live session.

    Example: quorum task adopt "refactoring the auth flow"  (from the session's directory)
    """
    from ..projects import ProjectRegistry
    from ..tasks import TaskStore, write_attached_state

    target = get_home()
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
def task_detach(task_id: str) -> None:
    """Detach an adopted task from its interactive session — after this the
    manager may run it headless like any other task."""
    from ..tasks import TaskStore

    target = get_home()
    task = _resolve_task(target, task_id)
    if not task.attached:
        raise _fail(f"task {task.short_id} is not attached")
    _journal_task(target, task, "task.detach")
    TaskStore(target).update(task.id, attached=False)
    typer.secho(f"task {task.short_id} detached — runnable again", fg="green")


def _match_attached(home: Path, session_id: str, cwd: str):
    """The task a harness hook is speaking for: exact session match first,
    then the working directory. The cwd fallback only fires when the task
    has no *live* session of its own — adopted id-less, or its known session
    already ended (a resume under a fresh id) — so a second concurrent
    session in the same checkout can't steal an adopted task's guidance or
    overwrite its session id."""
    from ..tasks import TERMINAL_STATUSES, TaskStore, attached_state

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
) -> None:
    """Harness stop/idle-hook entry point (reads the hook's JSON on stdin).

    For an adopted session this refreshes its liveness record and, when
    guidance is waiting in the task inbox, emits it — by default as the
    Stop-hook block-protocol JSON that continues the session (Claude Code and
    Codex speak the same one). For everything else it exits 0 silently — the
    hook is installed globally, so this must stay cheap and mute.
    """
    from ..messages import MessageBus
    from ..runner import guidance_note
    from ..tasks import TaskStore, inbox_name, write_attached_state

    if format not in ("decision", "text"):
        raise _fail(f"unknown --format {format!r} (expected 'decision' or 'text')")
    payload = _read_hook_payload()
    target = get_home(must_exist=False)
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
def task_hook_session_start() -> None:
    """Harness SessionStart-hook entry point: refreshes an adopted task's
    liveness record and learns the (possibly new) session id — harnesses
    whose sessions can't shell out with their own id at adopt time (Codex)
    get it associated here instead."""
    from ..tasks import TaskStore, write_attached_state

    payload = _read_hook_payload()
    target = get_home(must_exist=False)
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
def task_hook_session_end() -> None:
    """Harness SessionEnd-hook entry point: records that an adopted session
    ended (the task stays attached — sessions get reopened)."""
    from ..tasks import write_attached_state

    payload = _read_hook_payload()
    target = get_home(must_exist=False)
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
) -> None:
    """List tasks, newest last (`quorum status --legend` explains the glyphs)."""
    from .. import views

    rows = views.task_rows(get_home())
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no tasks — `quorum task add <project> \"<prompt>\"`")
        return
    _print_table(_task_table(rows))


@task_app.command("show")
def task_show(
    task_id: str,
    json_out: bool = typer.Option(False, "--json", help="Dump the full task record as JSON."),
) -> None:
    """Show one task: what it is, where it stands, its recent reports and
    its notebook."""
    from .. import notes as notes_mod
    from .. import views
    from ..config import load_config_or_default
    from ..tasks import (
        TaskStore,
        dependency_state,
        read_handoff,
        read_reports,
        runner_alive,
        short_handle,
    )

    target = get_home()
    task = _resolve_task(target, task_id)
    if json_out:
        typer.echo(json.dumps(task.model_dump(), indent=2, ensure_ascii=False))
        return
    running = runner_alive(target, task.id)
    # The badges every listing shows, then the words this surface has room
    # for. `views.task_badges` reads a row, and the two fields it wants are
    # on the task itself.
    state = task.status + views.task_badges(
        {"perpetual": task.perpetual, "pr_state": task.pr_state}
    )
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
    if task.issue_url:
        # The full url here, `#62` everywhere a listing has one column: this
        # is the page a human opens.
        typer.echo(f"  issue:    {task.issue_url}")
    if task.pr_url:
        typer.echo(f"  pr:       {task.pr_url}")
    if task.pr_state:
        # Observed by the manager tick, so it can be older than "now" — say
        # when, rather than implying it was just checked.
        typer.echo(f"  pr state: {task.pr_state} (observed {task.pr_state_at})")
    all_tasks = TaskStore(target).list()
    if task.depends_on:
        deps = dependency_state(task, {t.id: t for t in all_tasks})
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
    # The other direction: who is waiting on this task. This is how a
    # running task learns it should leave a handoff — the preamble tells it
    # to look here.
    dependents = [t.short_id for t in all_tasks if task.id in t.depends_on]
    if dependents:
        typer.echo(
            f"  dependents: {', '.join(dependents)}  (leave them a handoff: "
            f"`task report {task.short_id} --status done --handoff <file|->`)"
        )
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
        if usage.last_run_overages(
            task.runs, config.tasks.max_cost_per_run, config.tasks.max_tokens_per_run
        ):
            typer.secho(
                "  gated:    the last run exceeded its budget — `task run` refuses the "
                "next one (--force overrides)",
                fg="yellow",
            )
    typer.echo(f"  updated:  {task.updated_at}")
    reports = read_reports(target, task.id, limit=10)
    if reports:
        typer.echo("recent reports:")
        for r in reports:
            typer.echo(f"  [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')}")
    # The notebook, exactly as the runner renders it into the task's prompt
    # (header line included, so what you read here is what the harness
    # reads). The digest never carries it: the manager reads reports.
    book = notes_mod.task_notebook(target, task.id)
    standing = book.active()
    kept = book.render_notes(standing, unscanned=book.unscanned_bytes())
    if kept:
        typer.echo("notebook:")
        for line in kept:
            typer.echo(f"  {line}")
    if not standing:
        # A notebook can render lines and still hold no live note — the file
        # has outgrown its read window — so the hint hangs off the notes, not
        # off the rendering.
        empty = (
            f'(empty — `quorum task remember {task.short_id} "…"` keeps state '
            "between its runs)"
        )
        typer.echo(f"  {empty}" if kept else f"  notebook: {empty}")
    handoff = read_handoff(target, task.id)
    if handoff is not None:
        # In full: dependents see it capped in their prompt, and this is
        # where the clip points them.
        typer.echo("handoff (what this task left for the tasks that depend on it):")
        for line in handoff.rstrip("\n").splitlines():
            typer.echo(f"  {line}")
    typer.echo(f"more: `quorum task log {task.short_id}` for the transcript, `--json` for the raw record")


@task_app.command("history")
def task_history(
    task_id: str,
    json_out: bool = typer.Option(False, "--json", help="Emit the rows as JSON."),
) -> None:
    """Everything that happened to a task, oldest first: queued, each run's
    start and end (exit, cost, stopped, stalled, fresh session), every
    report, guidance sent to it and by whom, the PR state the manager
    observed, what the manager (or any agent) did to it, and its archival.

    Read straight off the files — task.json, reports.jsonl, the inbox and
    the message archive, the agents' journals — so it works with the
    supervisor stopped, and it still answers for a task `task prune` has
    moved into tasks/.archive.
    """
    from .. import views
    from ..prune import archived_task_dir, resolve_archived
    from ..tasks import TaskStore

    target = get_home()
    root = None
    try:
        task = TaskStore(target).resolve(task_id)
    except KeyError:
        try:
            task = resolve_archived(target, task_id)
        except KeyError:
            raise _fail(f"no task matching {task_id!r} — `quorum task list`") from None
        except ValueError as e:
            raise _fail(str(e)) from None
        root = archived_task_dir(target, task.id)
    except ValueError as e:
        raise _fail(str(e)) from None
    rows = views.task_history(target, task, root=root)
    if json_out:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    typer.echo(f"task {task.short_id}  ({task.id})  {len(rows)} event(s), oldest first")
    for row in rows:
        typer.echo(views.history_line(row))


@task_app.command("run")
def task_run(
    task_id: str,
    detach: bool = typer.Option(False, "--detach", help="Start the run in the background and return."),
    force: bool = typer.Option(
        False, "--force",
        help="Run even while the task's dependencies are unfinished or its last run went over budget.",
    ),
    fresh_session: bool = typer.Option(
        False,
        "--fresh-session",
        help="Forget the captured session id and start a new session (same worktree).",
    ),
) -> None:
    """Execute one harness run of a task (the manager does this automatically
    under `quorum up`).

    `--fresh-session` is for a session that is itself broken — one that hangs
    or errors the moment it resumes. The worktree (the actual work) is
    untouched, but the new session remembers nothing, so send a nudge
    summarizing where the task had got to.
    """
    from ..runner import (
        RunnerError,
        budget_blockers,
        budget_refusal,
        launch_detached,
        run_task,
        unmet_dependencies,
    )

    target = get_home()
    task = _resolve_task(target, task_id)
    config = _load_config(target)
    # mirror the runner's substrate rails here so --detach fails in the
    # parent too, instead of journaling a success and refusing in the child
    if task.attached:
        raise _fail(
            f"task {task.short_id} is attached to a live interactive session — "
            "guide it with `quorum task nudge`, or `quorum task detach` it first"
        )
    if not force:
        from ..tasks import TaskStore

        blockers = unmet_dependencies(TaskStore(target), task)
        if blockers:
            raise _fail(
                f"task {task.short_id} is waiting on {', '.join(blockers)} — "
                "unfinished dependencies; `--force` to run anyway"
            )
        over = budget_blockers(config.tasks, task)
        if over:
            raise _fail(budget_refusal(task, over))
    _journal_task(target, task, "task.run", "--fresh-session" if fresh_session else None)
    if detach:
        pid = launch_detached(target, task.id, force=force, fresh_session=fresh_session)
        typer.secho(f"task {task.short_id} running detached (pid {pid}) — `quorum task log {task.short_id} -f`", fg="green")
        return
    try:
        code = run_task(target, config, task.id, force=force, fresh_session=fresh_session)
    except RunnerError as e:
        raise _fail(str(e)) from None
    color = "green" if code == 0 else "red"
    typer.secho(f"run finished (exit {code}) — status: {_resolve_task(target, task.id).status}", fg=color)
    if code != 0:
        raise typer.Exit(1)

@task_app.command("log")
def task_log(
    task_id: str,
    lines: int = _LINES_OPT,
    follow: bool = _FOLLOW_OPT,
    raw: bool = _RAW_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Render a task's harness transcript as a narrative.

    The whole transcript by default; `-n 40` prints only the last forty
    entries and `-f` keeps printing new ones as the run writes them.
    """
    from ..tasks import transcript_path

    target = get_home()
    task = _resolve_task(target, task_id)
    path = transcript_path(target, task.id)

    def render(entries: list) -> list[str]:
        return transcript_mod.render(entries, verbose=verbose, raw=raw)

    entries = fsio.read_jsonl(path)
    if not entries and not follow:
        typer.echo(f"task {task.short_id} has no transcript yet")
        return
    _echo(render(entries[-lines:] if lines > 0 else entries))
    if follow:
        _follow(path, render, len(entries))


@task_app.command("report", rich_help_panel="Harness protocol")
def task_report(
    task_id: str,
    text: str = typer.Argument("", help="Short human-readable progress note."),
    status: str = typer.Option(..., "--status", help="One-word status (planning, executing, pr, done, blocked, ...)."),
    pr_url: str | None = typer.Option(None, "--pr-url", help="Pull request URL, when one was opened."),
    handoff: Path | None = typer.Option(
        None,
        "--handoff",
        help=(
            "A file (or - for stdin) with the handoff for the tasks that depend on this "
            "one: what changed, what is not done, what to check first. Stored whole as "
            "tasks/<id>/handoff.md; a later --handoff replaces it."
        ),
    ),
) -> None:
    """Record task progress (harnesses call this; humans can too)."""
    from .. import tasks as tasks_mod

    target = get_home()
    task = _resolve_task(target, task_id)
    # Read (and refuse) the handoff before anything is journaled or written:
    # an empty or unreadable body must not half-apply a report.
    body = _verbatim_text(handoff, "handoff") if handoff is not None else None
    if body is not None and not body.strip():
        raise _fail("the handoff is empty — give it a body, or leave --handoff off")
    _journal_task(
        target, task, "task.report", status + (" +handoff" if body is not None else "")
    )
    tasks_mod.report(target, task.id, status=status, text=text, pr_url=pr_url, handoff=body)
    typer.echo(
        f"task {task.short_id}: {status}"
        + (f" ({pr_url})" if pr_url else "")
        + (f" — handoff stored ({len(body.encode('utf-8'))} bytes)" if body is not None else "")
    )


@task_app.command("inbox")
def task_inbox(
    task_id: str,
    claim: bool = typer.Option(False, "--claim", help="Consume the messages (what a harness should do)."),
    clear: bool = typer.Option(
        False, "--clear", help="Archive the pending guidance instead of delivering it."
    ),
) -> None:
    """Read guidance sent to a task. Without --claim, messages are only peeked."""
    from ..runner import guidance_note
    from ..tasks import inbox_name

    target = get_home()
    task = _resolve_task(target, task_id)
    bus = MessageBus(target)
    if clear:
        if claim:
            raise _fail("--claim delivers guidance and --clear discards it — pick one")
        pending = bus.clear_inbox(inbox_name(task.id), dry_run=True)
        if not pending:
            typer.echo("no guidance waiting")
            return
        _journal_task(target, task, "task.inbox.clear", f"{len(pending)} message(s)")
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
        raw = fsio.read_json_or(path, None)
        if raw is None:
            continue
        typer.echo(f"[from {raw.get('from', '?')} at {raw.get('created_at', '')}] "
                   f"{raw.get('payload', {}).get('text', '')}")


@task_app.command("nudge")
def task_nudge(task_id: str, text: str) -> None:
    """Send guidance to a task; the next run (or a cooperative harness
    mid-run) will see it.

    Example: quorum task nudge a3f2k9 "use the existing retry helper"
    """
    from ..tasks import nudge

    target, task = _task_action("task.nudge", task_id, text[:80])
    nudge(target, task, text, sender=current_actor())
    typer.secho(f"guidance queued for task {task.short_id}", fg="green")


@task_app.command("remember")
def task_remember(
    task_id: str,
    text: str,
    ttl: int = typer.Option(0, "--ttl", help="Days until the note expires (0: never)."),
) -> None:
    """Write a standing note into a task's notebook; every future run of the
    task reads it — resumed or fresh.

    An attached task is the exception: an adopted session does not go through
    the runner, so nothing renders its notebook into the session and
    `quorum task show` is the read path.

    The notebook (`tasks/<id>/notes.jsonl`) is the task's memory between
    runs: what is done, what is left, what was tried and failed. The task's
    own harness writes it (tagged `task-<id>` by the runner), and so may the
    manager or you; another task or a prompt agent is refused — guide a task
    with `quorum task nudge` instead. A nudge is read once; a note stays
    until it expires or is forgotten.
    """
    from .. import notes as notes_mod

    target = get_home()
    task = _resolve_task(target, task_id)
    entry = _notebook_write(
        target, notes_mod.task_notebook(target, task.id), "task.remember",
        journal_target=task.short_id, target_status=task.status, arg=text[:80], ttl=ttl,
    )
    for_days = f", for {ttl}d" if ttl else ""
    if task.attached:
        # An adopted session does not go through the runner, so nothing
        # composes a prompt for it and the note is never rendered into the
        # session. It is kept, and `task show` is the read path.
        typer.secho(
            f"remembered ({notes_mod.short_id(entry['id'])}) — task {task.short_id} is "
            f"attached, so `quorum task show {task.short_id}` is where it is read; an "
            "adopted session is not handed its notebook" + for_days,
            fg="green",
        )
        return
    typer.secho(
        f"remembered ({notes_mod.short_id(entry['id'])}) — every future run of task "
        f"{task.short_id} reads it" + for_days,
        fg="green",
    )


@task_app.command("forget")
def task_forget(task_id: str, note_id: str) -> None:
    """Retire a note in a task's notebook (append-only: the file keeps it,
    readers hide it). The note id is the handle `task remember` printed and
    `task show` lists."""
    from .. import notes as notes_mod

    target = get_home()
    task = _resolve_task(target, task_id)
    note = _notebook_write(
        target, notes_mod.task_notebook(target, task.id), "task.forget",
        journal_target=task.short_id, target_status=task.status, arg=note_id, retire=True,
    )
    typer.echo(f"forgot ({notes_mod.short_id(note['id'])}) {note.get('text', '')[:60]}")


@task_app.command("stop")
def task_stop(
    task_id: str,
) -> None:
    """End a task's live run without ending the task.

    For a hung session: SIGTERM (then SIGKILL) the runner's process group,
    close the run record, and leave the task's status, queue position and
    worktree exactly as they were — `quorum task run <id> --detach` resumes
    it, `--fresh-session` too if the session itself is damaged. Use
    `quorum task cancel` when you mean to end the *task*.
    """
    from ..runner import RunnerError, stop_run

    target, task = _task_action("task.stop", task_id)
    try:
        result = stop_run(target, task.id)
    except RunnerError as e:
        raise _fail(str(e)) from None
    how = (
        f"{result['signal']} to pid {result['pid']}"
        if result["signal"]
        else f"pid {result['pid']} was already gone"
    )
    typer.secho(
        f"task {task.short_id}: run stopped ({how}) — status is still {task.status!r}",
        fg="green",
    )
    if result["run_recorded"]:
        typer.echo("the interrupted run was recorded as stopped")
    typer.echo(f"resume it with `quorum task run {task.short_id} --detach`")


@task_app.command("cancel")
def task_cancel(
    task_id: str,
    kill: bool = typer.Option(
        False, "--kill",
        help="Also SIGTERM a live runner (`task stop` ends a run without ending the task).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Mark a task cancelled so the manager stops attending to it."""
    from ..tasks import TaskStore, runner_lock_path

    target = get_home()
    task = _resolve_task(target, task_id)
    if kill:
        _confirm(yes, f"cancel task {task.short_id} and SIGTERM its live runner?")
    _journal_task(target, task, "task.cancel")
    TaskStore(target).update(task.id, status="cancelled")
    typer.echo(f"task {task.short_id} cancelled")
    if kill:
        pid = fsio.read_pid(runner_lock_path(target, task.id))
        if pid is not None and fsio.pid_alive(pid):
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
) -> None:
    """Archive finished tasks into `tasks/.archive/<id>/` so they leave every view.

    A task directory is never deleted: it is *moved*, and moving it back
    restores it. Refuses a task with a live runner, an attached task, one
    another task still depends on, and (without --force) one whose worktree
    holds uncommitted or unpushed work. With --worktrees, a worktree git
    refuses to remove is kept and its task left unarchived — --force does
    not override that, though it does force the branch delete.
    """
    from ..tasks import TaskStore

    target = get_home()
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


@task_app.command("export")
def task_export(
    task_id: str,
    out: Path | None = typer.Option(
        None, "--out", help="Archive path (default: ./quorum-task-<short-id>.tar.gz; never inside the home)."
    ),
    with_worktree_diff: bool = typer.Option(
        False, "--with-worktree-diff",
        help="Add worktree.diff: the task's worktree against the branch it forked from.",
    ),
    redact: bool = typer.Option(
        False, "--redact",
        help="Replace every tool result in the transcript with a marker; keep the assistant's "
             "text and its tool calls.",
    ),
) -> None:
    """Pack one task into a tar.gz for sharing or a bug report.

    The archive holds `tasks/<id>/` whole (record, reports, transcript,
    runner log, any subdirectory), the task's inbox — waiting, claimed and
    already-delivered guidance — and, with --with-worktree-diff, a patch of
    the worktree against its base. Nothing from the project directory, and
    nothing is written but the archive. A task that ran in your own checkout
    (--no-worktree, adopted) is refused the diff.
    """
    from .. import export as export_mod

    target = get_home()
    task = _resolve_task(target, task_id)
    destination = out if out is not None else export_mod.default_output(task)
    refused = export_mod.output_refusal(destination, target)
    if refused:
        raise _fail(refused)
    try:
        entries, redaction = export_mod.plan(
            target, task, with_worktree_diff=with_worktree_diff, redact=redact
        )
        names = export_mod.write_archive(destination, task, entries)
    except export_mod.ExportError as e:
        raise _fail(f"cannot export task {task.short_id}: {e}") from None
    except OSError as e:
        raise _fail(f"cannot write {destination}: {e}") from None
    typer.secho(f"exported task {task.short_id} to {destination} ({len(names)} entries)", fg="green")
    for name in names:
        typer.echo(f"  {name}")
    if redaction is not None:
        note = f"redacted {redaction.results} tool result(s)"
        if redaction.lines_kept:
            note += (
                f"; {redaction.lines_kept} plain-text line(s) kept verbatim — "
                "no structure to redact"
            )
        typer.secho(note, fg="yellow")
