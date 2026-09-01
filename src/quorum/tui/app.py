"""Terminal dashboard (Textual). A pure reader of QUORUM_HOME, refreshed on a
timer — works whether or not the supervisor is running, including over SSH.
Its single write affordance is steering: `n` sends guidance into the selected
task's inbox, the same channel the manager's pokes use."""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .. import views
from ..tasks import read_reports, read_transcript_tail

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


class QuorumTUI(App):
    TITLE = "quorum"
    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh"),
        ("n", "nudge", "nudge task"),
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
        self.selected_task = None
        self.query_one("#nudge", Input).display = False
        self.refresh_data()

    def action_nudge(self) -> None:
        if self.selected_task is None:
            self.notify("select a task first", severity="warning")
            return
        box = self.query_one("#nudge", Input)
        box.display = True
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        box = self.query_one("#nudge", Input)
        box.value = ""
        box.display = False
        if not text or self.selected_task is None:
            return
        from ..tasks import TaskStore, nudge

        task = TaskStore(self.home).get(self.selected_task)
        if task is None:
            return
        nudge(self.home, task, text, sender="user")
        self.notify(f"guidance queued for {task.short_id}")
        self.refresh_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # selection is deliberate (enter/click) — merely arrowing through the
        # table must never swap the board pane away
        if event.data_table.id == "tasks" and event.row_key is not None:
            self.selected_task = event.row_key.value
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
                if t.get("waiting_on"):
                    status += " ⏳" + ",".join(t["waiting_on"])
                if t.get("dep_failed"):
                    status += " DEP-FAILED"
                if t.get("dep_missing"):
                    status += " DEP-MISSING"
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

        def fill_agents(table: DataTable) -> None:
            for r in views.agent_rows(self.home):
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
            mode.update(f"task {short} — transcript tail   (esc: board · n: nudge)")
            lines = self._task_log_lines(self.selected_task)
        else:
            mode.update("board — recent messages   (enter on a task: transcript · ⚭ attached · ▶ running)")
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
