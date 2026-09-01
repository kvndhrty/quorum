"""TUI behavior tests, driven through Textual's Pilot (the repo's first —
the audit found the board pane could be silently swapped away and the 2s
rebuild reset the reader's cursor, with nothing to catch either)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import DataTable, Input, Static

from quorum.messages import MessageBus
from quorum.tasks import TaskStore, inbox_name
from quorum.tui.app import QuorumTUI


def populate(home: Path) -> list[str]:
    """A couple of tasks and a board message; returns task ids."""
    store = TaskStore(home)
    ids = [
        store.add("proj-a", "first task", "fake").id,
        store.add("proj-a", "second task", "fake").id,
        store.add("proj-b", "third task", "fake").id,
    ]
    MessageBus(home).post("user", "notes", text="hello board")
    return ids


def drive(home: Path, script) -> None:
    async def main() -> None:
        app = QuorumTUI(home)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await script(app, pilot)

    asyncio.run(main())


def mode_text(app: QuorumTUI) -> str:
    return str(app.query_one("#logmode", Static).content)


def test_mounts_populated_with_the_board_showing(home: Path):
    populate(home)

    async def script(app, pilot):
        assert app.query_one("#tasks", DataTable).row_count == 3
        assert app.selected_task is None
        assert mode_text(app).startswith("board")
        assert any("hello board" in line for line in app._log_lines)

    drive(home, script)


def test_arrowing_through_tasks_never_swaps_the_board(home: Path):
    populate(home)

    async def script(app, pilot):
        await pilot.press("down", "down")
        await pilot.pause()
        assert app.selected_task is None
        assert mode_text(app).startswith("board")

    drive(home, script)


def test_enter_selects_a_task_and_escape_returns_to_the_board(home: Path):
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected_task == ids[1]
        assert mode_text(app).startswith(f"task {ids[1][-6:].lower()}")
        await pilot.press("escape")
        await pilot.pause()
        assert app.selected_task is None
        assert mode_text(app).startswith("board")

    drive(home, script)


def test_refresh_preserves_the_cursor_row(home: Path):
    populate(home)

    async def script(app, pilot):
        table = app.query_one("#tasks", DataTable)
        await pilot.press("down", "down")
        assert table.cursor_row == 2
        app.refresh_data()
        await pilot.pause()
        assert table.cursor_row == 2

    drive(home, script)


def test_nudge_lands_in_the_selected_tasks_inbox(home: Path):
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("enter")  # select the first task
        await pilot.press("n")
        box = app.query_one("#nudge", Input)
        assert box.display
        box.value = "check the edge cases"
        await pilot.press("enter")
        await pilot.pause()
        assert MessageBus(home).pending(inbox_name(ids[0]))

    drive(home, script)


def test_a_perpetual_task_is_badged_in_the_task_table(home: Path):
    """`∞` is how "40 runs and counting" reads as working rather than stuck."""
    store = TaskStore(home)
    store.add("proj-a", "watch CI", "fake", perpetual=True)
    store.add("proj-a", "one-off", "fake")

    async def script(app, pilot):
        table = app.query_one("#tasks", DataTable)
        statuses = [str(table.get_row_at(i)[2]) for i in range(table.row_count)]
        assert statuses[0].endswith("∞") and "∞" not in statuses[1]

    drive(home, script)


def test_selecting_an_agent_shows_its_notebook(home: Path):
    """The notebook is read-only here, like everything else in the TUI: a
    file reader, working with the supervisor stopped."""
    from quorum import notes

    notes.remember(home, "the user wants at most two tasks running")
    populate(home)

    async def script(app, pilot):
        app.query_one("#agents", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_agent == "manager"
        assert mode_text(app).startswith("agent manager")
        assert any("at most two tasks" in line for line in app._log_lines)
        await pilot.press("escape")
        await pilot.pause()
        assert app.selected_agent is None
        assert mode_text(app).startswith("board")

    drive(home, script)
