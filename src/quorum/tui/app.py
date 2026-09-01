"""Terminal dashboard (Textual). A file reader of QUORUM_HOME, refreshed on a
timer — works whether or not the supervisor is running, including over SSH.
Its write affordances stay thin bus/store calls, the same ones the CLI and the
web dashboard make: `n` sends guidance into a task's inbox, `m` sends a
directive to the manager's inbox (`quorum manager tell`), `s` launches a
detached run, and `c` cancels a task — the one destructive binding, so it
confirms first.

Two rules hold for all four. They act on the row the reader is *looking at* —
the highlighted row while the task table has focus, the open task while
reading its detail (`enter` opens a transcript; it does not arm the write
keys) — and they all go through `_write`, because a keystroke on a dashboard
must never take the dashboard down when QUORUM_HOME turns unwritable."""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .. import views
from ..messages import MessageBus
from ..tasks import (
    Task,
    TaskStore,
    nudge,
    read_reports,
    read_transcript_tail,
    runner_alive,
)

STATUS_STYLE = {
    "idle": "green",
    "running": "cyan",
    "error": "red",
    "paused": "red",
    "never-ran": "dim",
}

TASK_STATUS_STYLE = {
    "queued": "dim",
    "done": "green",
    "blocked": "red",
    "cancelled": "dim",
}

#: what `_write` returns when the write raised instead of happening
FAILED = object()


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no gate in front of the one destructive binding."""

    BINDINGS = [
        ("y", "confirm", "yes"),
        ("n", "refuse", "no"),
        ("escape", "refuse", "no"),
    ]
    CSS = """
    ConfirmScreen { align: center middle; }
    #question { width: 70; height: auto; border: round $warning; padding: 1 2; background: $surface; }
    """

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(f"{self.question}\n\ny: yes    n / esc: no", id="question")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_refuse(self) -> None:
        self.dismiss(False)


class QuorumTUI(App):
    TITLE = "quorum"
    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh"),
        ("n", "nudge", "nudge task"),
        ("m", "directive", "tell manager"),
        ("s", "run_task", "run task"),
        ("c", "cancel_task", "cancel task"),
        ("escape", "show_board", "board"),
    ]
    CSS = """
    #top { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #tasks { height: 35%; border: round $panel-lighten-2; padding: 0 1; }
    #columns { height: 1fr; }
    .pane { border: round $panel-lighten-2; padding: 0 1; }
    #agents { width: 42%; }
    #projects { width: 58%; }
    #logmode { height: 1; padding: 0 1; color: $text-muted; }
    #log { height: 40%; border: round $panel-lighten-2; padding: 0 1; }
    #nudge { display: none; dock: bottom; }
    DataTable { height: 1fr; }
    """

    def __init__(self, home: Path):
        super().__init__()
        self.home = Path(home)
        self.selected_task: str | None = None
        self.selected_agent: str | None = None
        # which inbox the shared input box writes to: "task" or "manager"
        self._input_target = "task"
        # the task a "task" nudge is aimed at, pinned when the box was opened
        self._input_task: str | None = None
        self._log_lines: list[str] | None = None  # last rendered log content

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="top")
        with Vertical():
            yield DataTable(id="tasks")
            with Horizontal(id="columns"):
                yield DataTable(id="agents", classes="pane")
                yield DataTable(id="projects", classes="pane")
            yield Static(id="logmode")
            yield RichLog(id="log", markup=False, wrap=True)
        yield Input(id="nudge", placeholder="guidance for the selected task — enter sends, esc cancels")
        yield Footer()

    def on_mount(self) -> None:
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_columns("task", "project", "status", "harness", "spent", "last report", "pr")
        tasks.cursor_type = "row"
        agents = self.query_one("#agents", DataTable)
        agents.add_columns("agent", "status", "schedule", "spent", "last run", "next run")
        agents.cursor_type = "row"
        projects = self.query_one("#projects", DataTable)
        projects.add_columns("project", "deadline", "path")
        projects.cursor_type = "row"
        tasks.focus()
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    # -- actions -----------------------------------------------------------

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_show_board(self) -> None:
        box = self.query_one("#nudge", Input)
        if box.display:
            # escape means "cancel what I am typing", exactly as the
            # placeholder promises — not "throw away my task selection"
            self._close_input()
            return
        self.selected_task = None
        self.selected_agent = None
        self.refresh_data()

    def action_nudge(self) -> None:
        task = self._target_task()
        if task is None:
            return
        # pin the target now: the box takes focus, so "the row I was looking
        # at when I pressed n" is the only answer that stays true at submit
        self._input_task = task.id
        self._open_input("task", f"guidance for {task.short_id} — enter sends, esc cancels")

    def action_directive(self) -> None:
        """`quorum manager tell`, from the dashboard: the manager's next run
        starts with the directive in its digest. No task selection needed."""
        self._open_input("manager", "directive for the manager — enter sends, esc cancels")

    def action_run_task(self) -> None:
        task = self._target_task()
        if task is None:
            return
        if task.attached:
            self.notify(
                f"task {task.short_id} is attached to a live session — nudge it instead",
                severity="warning",
            )
            return
        if runner_alive(self.home, task.id):
            self.notify(f"task {task.short_id} is already running", severity="warning")
            return
        from ..runner import launch_detached

        pid = self._write("start the run", lambda: launch_detached(self.home, task.id))
        if pid is FAILED:
            return
        self.notify(f"run started for {task.short_id} (pid {pid})")
        self.refresh_data()

    def action_cancel_task(self) -> None:
        task = self._target_task()
        if task is None:
            return
        question = f"cancel task {task.short_id} ({task.status})?"
        if runner_alive(self.home, task.id):
            question += "\nits live runner keeps going — `quorum task cancel --kill` SIGTERMs it"

        def cancel(confirmed: bool | None) -> None:
            if not confirmed:
                return
            done = self._write(
                f"cancel {task.short_id}",
                lambda: TaskStore(self.home).update(task.id, status="cancelled"),
            )
            if done is FAILED:
                return
            self.notify(f"task {task.short_id} cancelled")
            self.refresh_data()

        self.push_screen(ConfirmScreen(question), cancel)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        target = self._input_target
        task_id = self._input_task
        self._close_input()
        if not text:
            return
        if target == "manager":
            sent = self._write(
                "queue the directive",
                lambda: MessageBus(self.home).send("user", "manager", type="directive", text=text),
            )
            if sent is FAILED:
                return
            self.notify("directive queued for the manager's next run")
            self.refresh_data()
            return
        task = TaskStore(self.home).get(task_id) if task_id else None
        if task is None:
            self.notify("that task is gone", severity="warning")
            return
        sent = self._write(
            f"nudge {task.short_id}", lambda: nudge(self.home, task, text, sender="user")
        )
        if sent is FAILED:
            return
        self.notify(f"guidance queued for {task.short_id}")
        self.refresh_data()

    # -- write-affordance helpers -------------------------------------------

    def _write(self, what: str, do):
        """Run one write, or say why it did not happen. Every affordance goes
        through here: QUORUM_HOME can be read-only, full, or on a dead mount,
        and a dashboard that dies at the keystroke is the worst moment to lose
        the view of what is going on."""
        try:
            return do()
        except OSError as e:
            self.notify(f"could not {what}: {e}", severity="error")
            return FAILED

    def _open_input(self, target: str, placeholder: str) -> None:
        box = self.query_one("#nudge", Input)
        self._input_target = target
        box.placeholder = placeholder
        box.display = True
        box.focus()

    def _close_input(self) -> None:
        box = self.query_one("#nudge", Input)
        box.value = ""
        box.display = False
        self.query_one("#tasks", DataTable).focus()

    def _highlighted_task(self) -> str | None:
        """The task id under the table cursor, or None when the table is not
        the focused widget (the reader is in the input box or a modal)."""
        try:
            table = self.query_one("#tasks", DataTable)
        except NoMatches:
            return None
        if self.focused is not table or not table.row_count:
            return None
        row = Coordinate(table.cursor_row, 0)
        if not table.is_valid_coordinate(row):
            return None  # cursor briefly past the end of a shrinking table
        key = table.coordinate_to_cell_key(row).row_key
        return key.value

    def _target_task(self) -> Task | None:
        """Which task a write acts on: the highlighted row while the table has
        focus — what the reader is pointing at — falling back to the open task
        when they are down in its detail. `enter` opens a transcript; it must
        not quietly become the target of every later keystroke."""
        task_id = self._highlighted_task() or self.selected_task
        if task_id is None:
            self.notify("no task to act on", severity="warning")
            return None
        task = TaskStore(self.home).get(task_id)
        if task is None:
            self.notify("that task is gone", severity="warning")
        return task

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # selection is deliberate (enter/click) — merely arrowing through the
        # table must never swap the board pane away
        if event.row_key is None:
            return
        if event.data_table.id == "tasks":
            # tasks and agents share the one log pane, so selecting either
            # replaces whatever it was showing
            self.selected_task, self.selected_agent = event.row_key.value, None
            self.refresh_data()
        elif event.data_table.id == "agents":
            self.selected_agent, self.selected_task = event.row_key.value, None
            self.refresh_data()

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _refill(table: DataTable, add_rows) -> None:
        """Rebuild a table's rows, keeping the cursor and scroll where the
        reader left them — the 2s refresh must never fight navigation."""
        cursor = table.cursor_row
        scroll = table.scroll_y
        table.clear()
        add_rows(table)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        table.scroll_y = scroll

    def _agent_log_lines(self, name: str) -> list[str]:
        """An agent's notebook and action journal — the standing notes its
        next run will read, then what it has been doing."""
        detail = views.agent_detail(self.home, name)
        if detail is None:
            return [f"no agent {name}"]
        lines = detail["notes_text"].splitlines()
        journal = detail.get("journal") or []
        if journal:
            lines.append("— action journal —")
            for e in journal:
                at = str(e.get("at", "")).replace("T", " ").rstrip("Z")
                target = f" -> {e['target']}" if e.get("target") else ""
                args = f"  {e['args']}" if e.get("args") else ""
                lines.append(f"[{at}] {e.get('action', '')}{target}{args}")
        return lines

    def refresh_data(self) -> None:
        try:
            top = self.query_one("#top", Static)
        except NoMatches:
            return  # called before the layout mounted
        sup = views.supervisor_status(self.home)
        if sup.get("alive"):
            banner = Text(f"● supervisor running (pid {sup.get('pid')}, since {sup.get('started_at')})")
        else:
            banner = Text("○ supervisor not running — start it with `quorum up`")
        attention = views.attention_summary(self.home)
        if attention["count"]:
            banner.append(
                f"   ⚠ {attention['count']} on #attention — `quorum board read attention`",
                style="bold yellow",
            )
        top.update(banner)

        task_rows = views.task_rows(self.home)

        def fill_tasks(table: DataTable) -> None:
            for t in task_rows:
                status = t["status"] + (" ⚭" if t["attached"] else (" ▶" if t["running"] else ""))
                if t.get("perpetual"):
                    status += " ∞"  # never finishes by design; only the user ends it
                style = "cyan" if (t["running"] or t["attached"]) else TASK_STATUS_STYLE.get(t["status"], "")
                table.add_row(
                    t["id_short"],
                    t["project"],
                    Text(status, style=style),
                    t["harness"],
                    # "" whenever the harness reported no usage; a task over
                    # its configured budget is marked, never blocked.
                    Text(
                        t.get("usage_text", "") + (" $!" if t.get("budget_overages") else ""),
                        style="yellow" if t.get("budget_overages") else "",
                    ),
                    (t["last_report"] or t["prompt"])[:60],
                    t["pr_url"] or "—",
                    key=t["id"],
                )

        self._refill(self.query_one("#tasks", DataTable), fill_tasks)
        if self.selected_task and self.selected_task not in {t["id"] for t in task_rows}:
            self.selected_task = None

        agent_rows = views.agent_rows(self.home)
        if self.selected_agent and self.selected_agent not in {r["name"] for r in agent_rows}:
            self.selected_agent = None  # removed (or its config broke) since selection

        def fill_agents(table: DataTable) -> None:
            for r in agent_rows:
                style = STATUS_STYLE.get(r["status"], "")
                status = Text(r["status"], style=style)
                if r["error"]:
                    status = Text(f"{r['status']} !", style=style or "red")
                next_run = r["next_run"] or "—"
                if r["next_run"] and r["next_run_estimated"]:
                    next_run = f"~{r['next_run']}"
                table.add_row(
                    r["name"],
                    status,
                    r["schedule"],
                    # "" unless this agent's own harness reported a spend.
                    r.get("usage_text", ""),
                    (r["last_end"] or "—").replace("T", " ").rstrip("Z"),
                    next_run.replace("T", " ").rstrip("Z"),
                    key=r["name"],
                )

        self._refill(self.query_one("#agents", DataTable), fill_agents)

        def fill_projects(table: DataTable) -> None:
            for p in views.project_rows(self.home):
                if p["deadline"]:
                    days = p["days_left"]
                    if days is not None and days < 0:
                        deadline = Text(f"{p['deadline']} (overdue {-days}d)", style="bold red")
                    elif days is not None and days <= 3:
                        deadline = Text(f"{p['deadline']} ({days}d)", style="bold yellow")
                    else:
                        deadline = Text(f"{p['deadline']} ({days}d)")
                else:
                    deadline = Text("—", style="dim")
                table.add_row(p["name"], deadline, p["path"])

        self._refill(self.query_one("#projects", DataTable), fill_projects)

        mode = self.query_one("#logmode", Static)
        if self.selected_task:
            short = self.selected_task[-6:].lower()
            mode.update(
                f"task {short} — transcript tail   "
                "(esc: board · n: nudge · m: manager · s: run · c: cancel)"
            )
            lines = self._task_log_lines(self.selected_task)
        elif self.selected_agent:
            mode.update(f"agent {self.selected_agent} — notebook & journal   (esc: board)")
            lines = self._agent_log_lines(self.selected_agent)
        else:
            mode.update(
                "board — recent messages   "
                "(enter on a task or agent: its detail · n/s/c act on the highlighted row · "
                "m: tell manager · ⚭ attached · ▶ running)"
            )
            lines = [
                f"[{m['at'].replace('T', ' ').rstrip('Z')}] #{m['topic']} <{m['from']}> {m['text']}"
                for m in views.board_tail(self.home, limit=30)
            ]
        if lines != self._log_lines:
            # rewrite only on change, so scroll position survives quiet refreshes
            self._log_lines = lines
            log = self.query_one("#log", RichLog)
            log.clear()
            for line in lines:
                log.write(line)

    def _task_log_lines(self, task_id: str) -> list[str]:
        lines: list[str] = []
        for entry in read_transcript_tail(self.home, task_id, limit=25):
            at = str(entry.get("at", "")).replace("T", " ").rstrip("Z")
            if "line" in entry:
                lines.append(f"[{at}] {entry['line']}")
            else:
                lines.append(f"[{at}] {json.dumps(entry.get('event'), ensure_ascii=False)[:200]}")
        reports = read_reports(self.home, task_id, limit=8)
        if reports:
            lines.append("— reports —")
            for r in reports:
                lines.append(f"[{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')}")
        return lines
