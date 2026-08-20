"""Terminal dashboard (Textual). A pure reader of QUORUM_HOME, refreshed on a
timer — works whether or not the supervisor is running, including over SSH."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .. import views

STATUS_STYLE = {
    "idle": "green",
    "running": "cyan",
    "error": "red",
    "paused": "red",
    "never-ran": "dim",
}


class QuorumTUI(App):
    TITLE = "quorum"
    BINDINGS = [("q", "quit", "quit"), ("r", "refresh", "refresh")]
    CSS = """
    #top { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #columns { height: 1fr; }
    .pane { border: round $panel-lighten-2; padding: 0 1; }
    #agents { width: 42%; }
    #projects { width: 58%; }
    #board { height: 40%; border: round $panel-lighten-2; padding: 0 1; }
    DataTable { height: 1fr; }
    """

    def __init__(self, home: Path):
        super().__init__()
        self.home = Path(home)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="top")
        with Vertical():
            with Horizontal(id="columns"):
                yield DataTable(id="agents", classes="pane")
                yield DataTable(id="projects", classes="pane")
            yield RichLog(id="board", markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        agents = self.query_one("#agents", DataTable)
        agents.add_columns("agent", "status", "schedule", "last run")
        agents.cursor_type = "row"
        projects = self.query_one("#projects", DataTable)
        projects.add_columns("project", "deadline", "activity")
        projects.cursor_type = "row"
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        sup = views.supervisor_status(self.home)
        top = self.query_one("#top", Static)
        if sup.get("alive"):
            top.update(f"● supervisor running (pid {sup.get('pid')}, since {sup.get('started_at')})")
        else:
            top.update("○ supervisor not running — start it with `quorum up`")

        agents = self.query_one("#agents", DataTable)
        agents.clear()
        for r in views.agent_rows(self.home):
            style = STATUS_STYLE.get(r["status"], "")
            agents.add_row(
                r["name"],
                Text(r["status"], style=style),
                r["schedule"],
                (r["last_end"] or "—").replace("T", " ").rstrip("Z"),
            )

        projects = self.query_one("#projects", DataTable)
        projects.clear()
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
            projects.add_row(p["name"], deadline, p["activity_summary"] or "—")

        board = self.query_one("#board", RichLog)
        board.clear()
        for m in views.board_tail(self.home, limit=30):
            at = m["at"].replace("T", " ").rstrip("Z")
            board.write(f"[{at}] #{m['topic']} <{m['from']}> {m['text']}")
