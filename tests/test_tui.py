"""TUI behavior tests, driven through Textual's Pilot (the repo's first —
the audit found the board pane could be silently swapped away and the 2s
rebuild reset the reader's cursor, with nothing to catch either)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from quorum.messages import MessageBus
from quorum.tasks import TaskStore, inbox_name, runner_lock_path
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


def test_escape_while_typing_cancels_the_box_but_keeps_the_task(home: Path):
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("enter")  # select the first task
        await pilot.press("n")
        assert app.query_one("#nudge", Input).display
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#nudge", Input).display
        assert app.selected_task == ids[0]
        assert not MessageBus(home).pending(inbox_name(ids[0]))

    drive(home, script)


def test_directive_lands_in_the_manager_inbox_without_a_selection(home: Path):
    populate(home)

    async def script(app, pilot):
        await pilot.press("m")  # no task selected: directives need none
        box = app.query_one("#nudge", Input)
        assert box.display
        assert "manager" in box.placeholder
        box.value = "start the oldest queued task"
        await pilot.press("enter")
        await pilot.pause()
        claimed = [c for c in MessageBus(home).claim("manager")]
        assert [c.message.payload["text"] for c in claimed] == ["start the oldest queued task"]
        assert claimed[0].message.type == "directive"

    drive(home, script)


def test_run_launches_a_detached_run_for_the_selected_task(home: Path, monkeypatch):
    ids = populate(home)
    launched: list[str] = []
    monkeypatch.setattr(
        "quorum.runner.launch_detached", lambda h, task_id: launched.append(task_id) or 4242
    )

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        assert launched == [ids[0]]

    drive(home, script)


def test_run_refuses_while_the_runner_is_alive(home: Path, monkeypatch):
    ids = populate(home)
    # pid 1 is alive and never us — the repo's idiom for a "live" runner
    runner_lock_path(home, ids[0]).parent.mkdir(parents=True, exist_ok=True)
    runner_lock_path(home, ids[0]).write_text('{"pid": 1}\n')
    launched: list[str] = []
    monkeypatch.setattr(
        "quorum.runner.launch_detached", lambda h, task_id: launched.append(task_id) or 1
    )

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        assert launched == []
        # and it is the liveness that refused, not the keystroke going nowhere
        runner_lock_path(home, ids[0]).unlink()
        await pilot.press("s")
        await pilot.pause()
        assert launched == [ids[0]]

    drive(home, script)


def test_run_refuses_an_attached_task(home: Path, monkeypatch):
    ids = populate(home)
    TaskStore(home).update(ids[0], attached=True)
    launched: list[str] = []
    monkeypatch.setattr(
        "quorum.runner.launch_detached", lambda h, task_id: launched.append(task_id) or 1
    )

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        assert launched == []
        TaskStore(home).update(ids[0], attached=False)
        await pilot.press("s")
        await pilot.pause()
        assert launched == [ids[0]]

    drive(home, script)


def test_cancel_confirms_first_and_only_then_cancels(home: Path):
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("n")  # refuse
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).status != "cancelled"
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).status == "cancelled"

    drive(home, script)


def test_write_keys_act_on_the_highlighted_row_not_the_last_one_opened(home: Path):
    """`enter` opens a transcript for reading; it does not arm the write keys
    for the rest of the session. Arrow to another row and `c` cancels *that*
    row — the one the reader is pointing at."""
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("enter")  # open the first task's detail...
        await pilot.pause()
        assert app.selected_task == ids[0]
        await pilot.press("down")  # ...then merely point at the second
        await pilot.pause()
        assert app.selected_task == ids[0]  # still the open one
        await pilot.press("c")
        await pilot.pause()
        assert ids[1][-6:].lower() in str(app.screen.query_one("#question", Static).content)
        await pilot.press("y")
        await pilot.pause()
        assert TaskStore(home).get(ids[1]).status == "cancelled"
        assert TaskStore(home).get(ids[0]).status != "cancelled"

    drive(home, script)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only directory")
def test_a_write_that_cannot_write_notifies_instead_of_crashing(home: Path):
    """QUORUM_HOME turning unwritable is a notification, never a traceback —
    the dashboard is the thing you are watching when the machine misbehaves,
    so it is the last thing that may die of it."""
    ids = populate(home)
    unwritable = [home / "tasks" / ids[0], home / "messages" / "inbox"]

    async def script(app, pilot):
        for d in unwritable:
            d.chmod(0o500)
        try:
            await pilot.press("c")  # cancel writes task.json
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("m")  # a directive writes the manager inbox
            app.query_one("#nudge", Input).value = "look at task one"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("n")  # a nudge writes the task inbox
            app.query_one("#nudge", Input).value = "try the other branch"
            await pilot.press("enter")
            await pilot.pause()
        finally:
            for d in unwritable:
                d.chmod(0o700)
        assert app.is_running
        assert TaskStore(home).get(ids[0]).status != "cancelled"
        assert not MessageBus(home).pending(inbox_name(ids[0]))
        assert [n.severity for n in app._notifications] == ["error", "error", "error"]

    drive(home, script)


def test_typing_in_the_box_never_fires_the_bindings(home: Path):
    """`c` cancels a task — but only as a keystroke on the table, never while
    the reader is halfway through a word in the input box."""
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("n")
        await pilot.press("c", "a", "n", "c", "e", "l", "space", "m", "e")
        await pilot.pause()
        assert app.query_one("#nudge", Input).value == "cancel me"
        assert TaskStore(home).get(ids[0]).status != "cancelled"
        assert len(app.screen_stack) == 1  # no confirmation modal was pushed

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
