"""What every command group needs: the typer apps, the home, the guards.

`quorum/cli/` is one module per command group. This one holds what they
share — the app objects they hang commands on, `get_home`, the actor guard
that journals and rate-limits an agent's actions, the table builders, and
the option objects that appear on more than one command.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .. import fsio, usage
from .. import home as home_mod
from ..actor import (
    ACTOR_RUN_ENV,
    actions_used,
    current_actor,
    current_cap,
    is_task_actor,
    journal_path,
)

if TYPE_CHECKING:
    from rich.table import Table


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
notify_app = typer.Typer(
    help="The [notify] hook: how attention posts reach you.", no_args_is_help=True
)
app.add_typer(board_app, name="board")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(manager_app, name="manager")
app.add_typer(prompt_app, name="prompt")
app.add_typer(integration_app, name="integration")
app.add_typer(notify_app, name="notify")

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
        help="QUORUM_HOME directory (default: $QUORUM_HOME or ~/.quorum). "
             "It goes before the subcommand: `quorum --home /path task list`.",
    ),
) -> None:
    global _home_option
    _home_option = home
    if home is not None:
        # Exported as well as recorded: anything this process spawns without
        # an environment of its own — a [notify] hook that calls quorum back,
        # a harness the foreground supervisor runs — resolves the home the
        # command line named rather than the default one.
        os.environ["QUORUM_HOME"] = str(home)


#: the --home given on the command line, before the subcommand. One option on
#: the root app rather than a copy on every command; `get_home` is the only
#: reader, and `$QUORUM_HOME` still answers when nothing was given.
_home_option: Path | None = None


def get_home(must_exist: bool = True) -> Path:
    home = home_mod.resolve_home(_home_option)
    if must_exist and not (home / home_mod.CONFIG_NAME).exists():
        typer.secho(f"no quorum home at {home} — run `quorum init` first", fg="red", err=True)
        raise typer.Exit(1) from None
    return home


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg="red", err=True)
    return typer.Exit(1)


def _load_config(home: Path):
    from ..config import ConfigError, load_config

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
    A task run is tagged `task-<id>` (see actor.py) for *identity* — what
    lets its notebook refuse another task — and is treated like a human
    here: nothing journals or caps a task's actions (reports.jsonl and the
    transcript are its record; the runner is its rail).
    """
    actor = current_actor()
    agent = actor != "user" and not is_task_actor(actor)
    if not agent and not always_journal:
        return
    journal = journal_path(home, actor if agent else "manager")
    run = os.environ.get(ACTOR_RUN_ENV, "") if agent else ""
    if run:
        cap = current_cap()
        # this run's entries sit at the journal's end, well inside the tail window;
        # a torn or hand-edited line is skipped, never a crashed CLI call. The
        # count comes from `actor.actions_used`, which is also what `show self`
        # reports, so the number a run is told is the number it is held to.
        mine = [
            e
            for e in fsio.read_jsonl_tail(journal)
            if isinstance(e, dict) and e.get("run") == run
        ]
        used = actions_used(home, actor, run)
        if used >= cap:
            # The cap was silent from the agent's own point of view: it saw a
            # command refused mid-run and its next run saw nothing at all.
            # One journal line per run fixes that — the next digest's journal
            # section shows the run that ran out of budget, and the prompt
            # decides what to do about it. Still only a rate limit; nothing
            # here pauses or throttles anything.
            if not any(e.get("action") == "cap.hit" for e in mine):
                fsio.append_jsonl(
                    journal,
                    {
                        "at": fsio.iso(fsio.utc_now()),
                        "run": run,
                        "actor": actor,
                        "action": "cap.hit",
                        "args": f"refused {action} — action cap ({cap}) reached this run",
                    },
                )
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


def _agent_notebook(home: Path, name: str):
    """An agent's notebook, with the owner name validated first.

    `--agent` becomes a path component under `state/agents/`, so it is held
    to the agent-name rules on the read side as much as the write side; the
    error is the CLI's, not a traceback.
    """
    from ..notes import NotebookError, agent_notebook, check_owner

    try:
        check_owner(name)
    except NotebookError as e:
        raise _fail(str(e)) from None
    return agent_notebook(home, name)


def _notebook_write(
    home: Path,
    book,
    action: str,
    *,
    journal_target: str,
    arg: str,
    retire: bool = False,
    ttl: int = 0,
    target_status: str | None = None,
    always_journal: bool = False,
) -> dict:
    """The body shared by `manager remember|forget` and `task remember|forget`.

    One `may_write` decision, made by the notebook itself so
    `Notebook.writers` is the only fence; one journal line either way —
    `<action>.refused` when the actor may not write (an agent reaching for
    someone else's memory is exactly what the next digest should show),
    `<action>` when it may, so a refusal costs one slot of the run's action
    cap and no more; and the refusal wording comes from the notebook, which
    is what keeps the four commands from drifting apart.

    `retire` picks the write: append `arg` as a note, or retire the note
    `arg` names. Returns the note either way.
    """
    from ..notes import NotebookError

    actor = current_actor()
    if not book.may_write(actor):
        _actor_guard(
            home, f"{action}.refused", target=journal_target,
            target_status=target_status, args=arg, always_journal=always_journal,
        )
        raise _fail(f"action refused: {book.refusal(actor)}")
    _actor_guard(
        home, action, target=journal_target, target_status=target_status,
        args=arg, always_journal=always_journal,
    )
    run_id = os.environ.get(ACTOR_RUN_ENV, "")
    try:
        if retire:
            return book.forget(arg, sender=actor, run_id=run_id)
        return book.remember(arg, sender=actor, run_id=run_id, ttl_days=ttl or None)
    except NotebookError as e:
        raise _fail(str(e)) from None


def _confirm(yes: bool, what: str) -> None:
    """Interactive-only guard for destructive commands: prompts on a TTY,
    passes through everywhere else (scripts and harness-driven agents keep
    working; the actor guard remains their only rail)."""
    if yes or not sys.stdin.isatty():
        return
    if not typer.confirm(what):
        raise typer.Exit(1)


#: the handle a run passes instead of its own id — `quorum task show self`,
#: `quorum agent show self`. Resolved from the actor tag (actor.py), never
#: from an argument, so it can only ever name the process that typed it.
SELF = "self"

#: the fix named by every `self` that cannot be resolved. One string, because
#: a run that gets this back has no other way to find out what went wrong.
_UNTAGGED = (
    "`self` reads the actor tag this process was started with (QUORUM_ACTOR), "
    "and this one has none — run it from inside a task run or an agent run, "
    "or name what you want to see"
)


def _resolve_self_task(home: Path):
    """The task this process is a run of. Errors name the fix: an agent run
    asking for a task is pointed at `agent show self`, and an untagged one
    at the tag it is missing."""
    from ..actor import self_agent_name, self_task_id
    from ..tasks import TaskStore

    task_id = self_task_id()
    if task_id is None:
        if agent := self_agent_name():
            raise _fail(
                f"`self` here is the agent {agent!r}, not a task — `quorum agent show self`"
            ) from None
        raise _fail(f"{_UNTAGGED}: `quorum task show <id>` (`quorum task list`)") from None
    task = TaskStore(home).get(task_id)
    if task is None:
        # The tag outlived the record: a pruned task, or a home switched
        # under a live run.
        raise _fail(
            f"this run is tagged as task {task_id} but no such task is in {home} — "
            "it may have been pruned"
        ) from None
    return task


def _resolve_self_agent() -> str:
    """The agent this process is a run of, for `quorum agent show self`."""
    from ..actor import self_agent_name, self_task_id

    name = self_agent_name()
    if name is None:
        if task_id := self_task_id():
            raise _fail(
                f"`self` here is a task run ({task_id[-6:].lower()}), not an agent — "
                "`quorum task show self`"
            ) from None
        raise _fail(f"{_UNTAGGED}: `quorum agent show <name>` (`quorum agent list`)") from None
    return name


def _resolve_task(home: Path, prefix: str):
    from ..tasks import TaskStore

    try:
        return TaskStore(home).resolve(prefix)
    except KeyError:
        raise _fail(f"no task matching {prefix!r} — `quorum task list`") from None
    except ValueError as e:
        raise _fail(str(e)) from None


def _journal_task(home: Path, task, action: str, args: str | None = None) -> None:
    """Journal one action against a task.

    The three fields every task command records — the action, the short id
    and the status the task had at the time — spelled once, so the journal
    the next digest reads back cannot drift between commands. Every caller
    reaches this only *after* its own validation: a journal line says
    something happened, and it costs a slot of an agent's per-run action
    cap, so a refused command must not write one.
    """
    _actor_guard(home, action, target=task.short_id, target_status=task.status, args=args)


def _task_action(action: str, task_id: str, args: str | None = None):
    """The whole prelude of a task command that has nothing of its own to
    check: resolve the home, resolve the task, journal the action. Returns
    the home and the task.

    Commands with a check between the resolution and the journal line keep
    those three steps apart — `task report` reads its handoff first, `task
    detach` refuses a task that is not attached, `task cancel` asks before
    it kills a runner, `task run` applies the dependency and budget
    refusals. Forcing them through here would move the journal line in
    front of the refusal it belongs behind.
    """
    home = get_home()
    task = _resolve_task(home, task_id)
    _journal_task(home, task, action, args)
    return home, task

# -- tables ----------------------------------------------------------------
#
# Every listing (`status`, `task list`, `agent list`, `project list`) is a
# Rich table (typer already depends on rich): one column per field, so a
# row stays a row instead of a string that grows a clause per feature and
# wraps mid-cell past column 80. The same table renders two ways:
#
#   - on a terminal it is *fitted* to the window: the report/flags (or
#     error/tags) columns absorb the shortfall with an ellipsis — a cut cell
#     where a wrapped one used to be — so id, status, harness, pr and usage
#     stay whole down to the width where the give-way column has nothing
#     left to give (roughly 60 columns for a task listing). Below that floor
#     Rich clips the fixed columns evenly, and the id, the cell you copy into
#     `task run`, holds `ID_MIN_WIDTH` while the rest go first;
#   - anywhere else (a pipe, a file, a test) it is laid out at its natural
#     width, plain text, no ANSI, so `quorum task list | grep <id>` and a
#     redirected `status` keep every id, status and URL reference whole.
#
# Columns that are empty on every row are dropped, so a home with no PRs,
# flags or reported usage does not grow blank headers. Cells are built from
# the same `views.*_rows` dicts the TUI and `--json` read, so the
# CLI renders and never re-derives.

PLAIN_TABLE_WIDTH = 4096  # off-terminal render width: wide enough that no cell is cut
REPORT_MAX_CHARS = 120  # a report is a one-line note by protocol; `task show` has all of it
ID_MIN_WIDTH = 8  # marker + space + the 6-char short id: the handle you retype

_NO_WRAP: dict[str, Any] = {"no_wrap": True}
# The id is the cell you copy into `quorum task run`, so it is the last to be
# clipped when the window is too narrow even for the give-way columns to cover.
_ID: dict[str, Any] = {"no_wrap": True, "min_width": ID_MIN_WIDTH}


def _give_way(ratio: int) -> dict[str, Any]:
    """Column options for a cell that yields width on a narrow terminal —
    ellipsized rather than wrapped, sharing the shortfall by `ratio`."""
    return {"no_wrap": True, "overflow": "ellipsis", "ratio": ratio}


# (header, Rich column options) — headers match the `--json` keys where a key exists.
_TASK_COLUMNS: list[tuple[str, dict[str, Any]]] = [
    ("id", _ID),
    ("project", _NO_WRAP),
    ("status", _NO_WRAP),
    ("harness", _NO_WRAP),
    ("report", _give_way(2)),
    ("issue", _NO_WRAP),
    ("pr", _NO_WRAP),
    ("flags", _give_way(1)),
    ("usage", _NO_WRAP),
]
_AGENT_COLUMNS: list[tuple[str, dict[str, Any]]] = [
    ("name", _NO_WRAP),
    ("type", _NO_WRAP),
    ("status", _NO_WRAP),
    ("schedule", _NO_WRAP),
    ("last", _NO_WRAP),
    ("usage", _NO_WRAP),
    ("error", _give_way(1)),
]
_PROJECT_COLUMNS: list[tuple[str, dict[str, Any]]] = [
    ("slug", _NO_WRAP),
    ("name", _NO_WRAP),
    ("due", _NO_WRAP),
    ("tags", _give_way(1)),
]


def _build_table(
    columns: list[tuple[str, dict[str, Any]]],
    cells: list[dict[str, str]],
    *,
    keep: frozenset[str],
) -> Table:
    """A borderless table over `cells` (one dict per row, keyed by header),
    including a column when it is in `keep` or any row has text for it."""
    from rich.table import Table

    table = Table(box=None, show_header=True, pad_edge=False, padding=(0, 2, 0, 0))
    shown = [
        (header, opts)
        for header, opts in columns
        if header in keep or any(row.get(header) for row in cells)
    ]
    for header, opts in shown:
        table.add_column(header, **opts)
    for row in cells:
        table.add_row(*(row.get(header, "") for header, _ in shown))
    return table


def _print_table(table: Table, *, width: int | None = None) -> None:
    """Print a table fitted to `width` columns — a terminal's own when
    stdout is one — or, off a terminal with no width asked for, at its
    natural width so every cell is complete and the text is plain."""
    from rich.console import Console

    fit = width is not None or sys.stdout.isatty()
    # Fitted only when a give-way column survived `_build_table`'s drop of the
    # empty ones: with no ratio column to absorb it, Rich spreads a wide
    # window's whole surplus evenly over the no_wrap columns and leaves the
    # fields acres apart. Narrower than natural, Rich collapses either way.
    table.expand = fit and any(column.ratio for column in table.columns)
    console = Console(
        width=width if fit else PLAIN_TABLE_WIDTH,
        force_terminal=None if fit else False,
        markup=False,  # cell text is literal — a report may say "[harness.x]"
        highlight=False,
        emoji=False,
    )
    if fit:
        console.print(table)
        return
    with console.capture() as capture:
        console.print(table)
    # Rows are padded out to the widest cell of the last column; a
    # redirected listing should not carry that trailing whitespace.
    typer.echo("\n".join(line.rstrip() for line in capture.get().splitlines()))


def _one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def _pr_ref(url: str) -> str:
    """`#49` for a pull-request URL, `!49` for a GitLab merge request, else
    the URL as given. `task show` always prints the full URL."""
    m = re.search(r"/pulls?/(\d+)/?$", url)
    if m:
        return f"#{m.group(1)}"
    m = re.search(r"/merge_requests/(\d+)/?$", url)
    if m:
        return f"!{m.group(1)}"
    return url


def _task_cells(t: dict) -> dict[str, str]:
    from .. import views

    report = _one_line(t.get("last_report"))
    if len(report) > REPORT_MAX_CHARS:
        report = report[: REPORT_MAX_CHARS - 1] + "…"
    return {
        "id": f"{views.task_marker(t)} {t['id_short']}",
        "project": t["project"],
        "status": t["status"] + views.task_badges(t),
        "harness": t["harness"],
        "report": report,
        # Where the task came from and where it went: both short forms,
        # both dropped as whole columns on a home that uses neither.
        "issue": t.get("issue_ref", ""),
        "pr": _pr_ref(t["pr_url"]) if t.get("pr_url") else "",
        "flags": views.task_flags(t),
        "usage": views.usage_badge(t),
    }


def _task_table(rows: list[dict]) -> Table:
    return _build_table(
        _TASK_COLUMNS,
        [_task_cells(t) for t in rows],
        keep=frozenset({"id", "project", "status", "harness"}),
    )


def _agent_cells(r: dict, *, with_type: bool) -> dict[str, str]:
    marker = {"idle": "●", "running": "◐", "error": "✗", "paused": "‖"}.get(r["status"], "○")
    return {
        "name": f"{marker} {r['name']}",
        "type": r["type"] if with_type else "",
        "status": r["status"] + ("" if r["enabled"] else " (disabled)"),
        "schedule": r["schedule"],
        "last": r.get("last_end") or "",
        # Only when the agent's harness reported a spend — an agent that
        # reports nothing (or isn't harness-driven) shows no figure.
        "usage": r.get("usage_text") or "",
        "error": _one_line(r.get("error")),
    }


def _agent_table(rows: list[dict], *, with_type: bool = False) -> Table:
    return _build_table(
        _AGENT_COLUMNS,
        [_agent_cells(r, with_type=with_type) for r in rows],
        keep=frozenset({"name", "status", "schedule"}),
    )


def _project_cells(p: dict) -> dict[str, str]:
    due = ""
    if p["deadline"]:
        due = str(p["deadline"])
        if p["days_left"] is not None:
            due += (
                f" ({p['days_left']}d)"
                if p["days_left"] >= 0
                else f" (OVERDUE {-p['days_left']}d)"
            )
    return {
        "slug": p["slug"],
        "name": p["name"] if p["name"] != p["slug"] else "",
        "due": due,
        "tags": ", ".join(p["tags"] or []),
    }


def _project_table(rows: list[dict]) -> Table:
    return _build_table(
        _PROJECT_COLUMNS, [_project_cells(p) for p in rows], keep=frozenset({"slug"})
    )


_NUM: dict[str, Any] = {"no_wrap": True, "justify": "right"}
# `quorum usage`: one row per value of the `--by` dimension. Numeric columns
# are right-aligned; the delivery columns are dropped by `_build_table` when
# no task in the listing has the observation behind them.
_USAGE_COLUMNS: list[tuple[str, dict[str, Any]]] = [
    ("tasks", _NUM),
    ("reported", _NUM),
    ("runs", _NUM),
    ("reruns", _NUM),
    ("cost", _NUM),
    ("tokens", _NUM),
    ("done", _NUM),
    ("merged", _NUM),
    ("queue→run", _NUM),
    ("queue→done", _NUM),
    ("done→merged", _NUM),
]
_AGENT_USAGE_COLUMNS: list[tuple[str, dict[str, Any]]] = [
    ("agent", _NO_WRAP),
    ("runs", _NUM),
    ("reported", _NUM),
    ("raised", _NUM),
    ("timeout", _NUM),
    ("unknown", _NUM),
    ("cost", _NUM),
    ("tokens", _NUM),
    ("duration", _NUM),
]


def _count(n: int) -> str:
    return str(n) if n else ""


def _spend_cells(spent: dict | None) -> dict[str, str]:
    """`cost` and `tokens` off a `usage.total`: empty, never `$0.00`, when
    the harness reported no figure for that field."""
    cost = usage.number((spent or {}).get("cost_usd"))
    tokens = usage.number((spent or {}).get("total_tokens"))
    return {
        "cost": usage.format_cost(cost) if cost else "",
        "tokens": usage.format_tokens(tokens) if tokens else "",
    }


def _task_usage_cells(by: str, r: dict) -> dict[str, str]:
    from .. import stats

    merged = ""
    if r["observed"]:
        merged = f"{r['merged']}/{r['observed']} ({round(100 * r['share_merged'])}%)"
    spend = _spend_cells(r["usage"])
    # How many tasks the figures beside it cover — the cost's own coverage
    # wherever a cost is shown, since a row mixing a costing harness with a
    # tokens-only one has fewer tasks behind its `$` than behind its tokens,
    # and the cost is the number a reader takes for the whole row.
    reported = r["tasks_with_cost"] if spend["cost"] else r["tasks_with_usage"]
    return {
        by: r["key"],
        "tasks": str(r["tasks"]),
        # Only when it differs: a column of `5/5` says nothing.
        "reported": f"{reported}/{r['tasks']}" if reported != r["tasks"] else "",
        "runs": str(r["runs"]),
        "reruns": _count(r["reruns"]),
        **spend,
        "done": _count(r["done"]),
        "merged": merged,
        "queue→run": stats.describe_summary(r["queue_to_run"]),
        "queue→done": stats.describe_summary(r["queue_to_done"]),
        "done→merged": stats.describe_summary(r["done_to_merged"]),
    }


def _task_usage_table(by: str, rows: list[dict]) -> Table:
    return _build_table(
        [(by, _NO_WRAP), *_USAGE_COLUMNS],
        [_task_usage_cells(by, r) for r in rows],
        keep=frozenset({by, "tasks", "runs", "cost", "tokens"}),
    )


def _agent_usage_cells(r: dict) -> dict[str, str]:
    reported = r["runs_with_usage"]
    return {
        "agent": r["key"],
        "runs": str(r["runs"]),
        "reported": f"{reported}/{r['runs']}" if reported != r["runs"] else "",
        "raised": _count(r["outcomes"]["raised"]),
        "timeout": _count(r["outcomes"]["timeout"]),
        # A usage-log line written before outcomes existed (#59): `?` elsewhere.
        "unknown": _count(r["outcomes"]["unknown"]),
        **_spend_cells(r["usage"]),
        "duration": (
            usage.format_duration(r["duration"]["median_seconds"]) if r["duration"] else ""
        ),
    }


def _agent_usage_table(rows: list[dict]) -> Table:
    return _build_table(
        _AGENT_USAGE_COLUMNS,
        [_agent_usage_cells(r) for r in rows],
        keep=frozenset({"agent", "runs", "cost", "tokens"}),
    )

# -- tasks -----------------------------------------------------------------


def _stdin_prompt() -> str:
    """Everything on stdin, decoded as UTF-8 without newline translation."""
    stream = getattr(sys.stdin, "buffer", None)
    try:
        data = stream.read() if stream is not None else sys.stdin.read()
    except OSError as e:
        raise _fail(f"cannot read stdin: {e}") from None
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("stdin is not valid UTF-8") from None


def _stdin_is_tty() -> bool:
    """Whether stdin is a terminal — False for anything that cannot say."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def _task_prompt(prompt: str, optional: bool = False) -> str:
    """The task prompt from the positional argument, or from stdin when that
    argument is `-`. `""` when `optional` and nothing was given.

    `optional` is what `--issue` passes: there the issue is the prompt and
    anything given here is *appended*, so "nothing given" is the normal
    case. Empty input from a source that was named is still an error — an
    explicitly empty prompt is a mistake either way.

    Stdin is read as bytes and decoded here rather than through
    `sys.stdin.read`, so what lands in task.json is byte-for-byte what was
    piped — a prompt is quoted verbatim into the harness's context, and
    silently rewriting CRLF or the trailing newline would make a stored task
    differ from its source. A prompt kept in a file goes in the same way:
    `quorum task add <project> - < prompt.md`. Empty (or whitespace-only)
    input is refused: a task with nothing to do would queue, launch, and
    waste a whole run."""
    if prompt == "-":
        text = _verbatim_text(Path("-"), "prompt")
        source = "stdin"
    elif prompt:
        text = prompt
        source = "the prompt argument"
    elif optional:
        return ""
    else:
        raise _fail(
            "a task needs a prompt: pass it as an argument, or `-` to read stdin "
            "(`quorum task add <project> - < prompt.md`)"
        )
    if not text.strip():
        raise _fail(f"empty prompt ({source}) — a task needs something to do")
    return text

_RAW_OPT = typer.Option(
    False, "--raw", help="Print the transcript's own JSON lines instead of the narrative."
)
_VERBOSE_OPT = typer.Option(
    False, "-v", "--verbose", help="Unfold reasoning, full tool arguments and full results."
)


def _echo(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


def _tail_file(path: Path, render, seen: int) -> int:
    """Print whatever a jsonl file grew by since `seen` entries; returns the
    new count. The unit is entries, not bytes: a partially written line is
    dropped by the reader and picked up on the next pass."""
    entries = fsio.read_jsonl(path)
    _echo(render(entries[seen:]))
    return len(entries)


def _follow(path: Path, render, seen: int) -> None:
    """Keep printing new entries until Ctrl-C."""
    try:
        while True:
            time.sleep(1.0)
            seen = _tail_file(path, render, seen)
    except KeyboardInterrupt:
        pass


_LINES_OPT = typer.Option(
    0, "-n", "--lines", help="Show only the last N transcript entries (default: all of them)."
)
_FOLLOW_OPT = typer.Option(
    False, "-f", "--follow", help="Keep printing new entries as they arrive (Ctrl-C stops)."
)

def _verbatim_text(source: Path, what: str) -> str:
    """A `<file|->` option read the way `_task_prompt` reads a prompt: as
    bytes decoded here, not through `read_text`/`sys.stdin.read`.

    `--notes-file` and `--handoff` both feed prompt text — quoted verbatim
    into the preamble's {project} block, or into a dependent's prompt — so
    what lands on disk has to be byte-for-byte what was written, instead of
    whatever the locale encoding and universal newlines make of it. `what`
    names the text in the stdin hint."""
    if str(source) == "-":
        # Nothing is piped in: say so, or the blocking read looks like a hang.
        if _stdin_is_tty():
            typer.echo(
                f"reading the {what} from stdin — end with ctrl-D (ctrl-C to abort)", err=True
            )
        return _stdin_prompt()
    try:
        return source.read_bytes().decode("utf-8")
    except OSError as e:
        raise _fail(f"cannot read {source}: {e}") from None
    except UnicodeDecodeError:
        raise _fail(f"cannot read {source}: it is not valid UTF-8") from None

_LAST_OPT = typer.Option(1, "--last", help="How many recent runs to render (newest last).")
_RUN_OPT = typer.Option(None, "--run", help="One run, by id, unique prefix, or unique suffix.")

_AGENT_OPT = typer.Option(
    "manager", "--agent",
    help="Whose notebook (default: the manager's). An agent may only write its own.",
)

def _parse_window(text: str) -> timedelta:
    """A window option, rejected as a bad parameter rather than a traceback.

    The grammar is `fsio.parse_window`'s — one for every window quorum
    takes — and this is only the CLI's way of refusing a bad one.
    """
    try:
        return fsio.parse_window(text)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None


def _parse_before(text: str) -> datetime:
    """A cutoff instant, written either way round: a window back from now
    (`7d`) or an absolute timestamp (`2026-09-01`, `2026-09-01T12:00:00Z`).

    Anything shaped like a window is judged as one, so a count no date can
    express (`142857142w`) is refused in the words every other window option
    uses; only text that is not a window at all is tried as a date.
    """
    text = text.strip()
    if fsio.looks_like_window(text):
        return fsio.utc_now() - _parse_window(text)
    try:
        return fsio.parse_iso(text)
    except ValueError:
        raise typer.BadParameter(
            f"invalid cutoff {text!r} (use a window like 7d, or a date like 2026-09-01)"
        ) from None
