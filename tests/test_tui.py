"""TUI behavior tests, driven through Textual's Pilot (the repo's first —
the audit found the board pane could be silently swapped away and the 2s
rebuild reset the reader's cursor, with nothing to catch either)."""

from __future__ import annotations

import asyncio
import gzip
import os
from pathlib import Path

import pytest
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Static

from quorum import fsio, tasks
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


def test_task_table_shows_waiting_on_dependencies(home: Path):
    """A dependent task reads as waiting in the TUI too — a pure file read,
    same as every other cell here (#31)."""
    store = TaskStore(home)
    upstream = store.add("proj-a", "build it", "fake")
    store.add("proj-a", "review it", "fake", depends_on=[upstream.id])

    async def script(app, pilot):
        table = app.query_one("#tasks", DataTable)
        cells = [str(table.get_row_at(r)[2]) for r in range(table.row_count)]
        assert any(f"⏳{upstream.short_id}" in c for c in cells)


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


def test_run_refuses_a_task_gated_by_its_budget(home: Path, monkeypatch):
    """The runner's budget gate, surfaced as a notice instead of a silent
    failure in runner.log; a cheaper last run lifts it."""
    ids = populate(home)
    (home / "config.toml").write_text("[tasks]\nmax_cost_per_run = 0.10\n")
    over = {"started_at": "t0", "ended_at": "t1", "exit_code": 0,
            "usage": {"cost_usd": 0.42, "total_tokens": 100, "events": 1}}
    TaskStore(home).update(ids[0], runs=[over])
    launched: list[str] = []
    monkeypatch.setattr(
        "quorum.runner.launch_detached", lambda h, task_id: launched.append(task_id) or 1
    )

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        assert launched == []
        TaskStore(home).update(ids[0], runs=[over, {**over, "usage": {"cost_usd": 0.01}}])
        await pilot.press("s")
        await pilot.pause()
        assert launched == [ids[0]]

    drive(home, script)


def test_a_gated_task_says_so_in_the_table(home: Path):
    """`s` refuses a gated task (above); the table has to say so first, or
    the reader learns of the gate only from the refusal."""
    ids = populate(home)
    (home / "config.toml").write_text("[tasks]\nmax_cost_per_run = 0.10\n")
    over = {"started_at": "t0", "ended_at": "t1", "exit_code": 0,
            "usage": {"cost_usd": 0.42, "total_tokens": 100, "events": 1}}
    store = TaskStore(home)
    store.update(ids[0], runs=[over])
    # The same overage one run back: over budget once, but the next run is
    # not gated — "$!" without the word.
    store.update(ids[1], runs=[over, {**over, "usage": {"cost_usd": 0.01}}])

    def spent(app, row: int) -> str:
        return str(app.query_one("#tasks", DataTable).get_cell_at(Coordinate(row, 4)))

    async def script(app, pilot):
        assert "GATED" in spent(app, 0)
        assert "$!" in spent(app, 1) and "GATED" not in spent(app, 1)
        assert spent(app, 2) == ""  # nothing reported, nothing to mark

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


def test_a_merged_pull_request_is_badged_in_the_task_table(home: Path):
    """`✔` distinguishes "done and delivered" from "done and waiting on a
    human" — read off task.json, since the TUI never probes a forge."""
    store = TaskStore(home)
    shipped = store.add("proj-a", "shipped it", "fake", status="done")
    store.update(shipped.id, pr_state="merged")
    dropped = store.add("proj-a", "abandoned", "fake", status="done")
    store.update(dropped.id, pr_state="closed")
    store.add("proj-a", "never observed", "fake", status="done")

    async def script(app, pilot):
        table = app.query_one("#tasks", DataTable)
        statuses = [str(table.get_row_at(i)[2]) for i in range(table.row_count)]
        assert statuses[0].endswith("✔")
        assert statuses[1].endswith("⊘")
        assert "✔" not in statuses[2] and "⊘" not in statuses[2]

    drive(home, script)


def test_the_issue_a_task_came_from_is_shown_in_the_task_table(home: Path):
    """Short form here (`#62`), the full url in `quorum task show` — one
    renderer (tasks.issue_ref) behind both."""
    store = TaskStore(home)
    store.add("proj-a", "issue work", "fake", issue_url="https://github.com/o/r/issues/62")
    store.add("proj-a", "prompt work", "fake")

    async def script(app, pilot):
        table = app.query_one("#tasks", DataTable)
        issues = [str(table.get_row_at(i)[6]) for i in range(table.row_count)]
        assert issues[0] == "#62" and issues[1] == "—"

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


def attention_rows(app) -> list[str]:
    """The escalation column of the open attention list."""
    table = app.screen.query_one("#attention-list", DataTable)
    return [
        str(table.get_cell_at(Coordinate(r, 2))) for r in range(table.row_count)
    ]


def test_a_acks_the_highlighted_attention_line(home: Path):
    """The banner is a time window, so `a` is how a handled escalation leaves
    it: the list gives `a` something highlighted to act on, and the ack is an
    archive — the message is gone from the topic, not from the history."""
    populate(home)
    bus = MessageBus(home)
    bus.post("manager", "attention", "escalation", text="first escalation")
    second = bus.post("manager", "attention", "escalation", text="second escalation")

    async def script(app, pilot):
        assert "2 on #attention" in str(app.query_one("#top", Static).content)
        await pilot.press("a")
        await pilot.pause()
        assert attention_rows(app) == ["first escalation", "second escalation"]
        await pilot.press("down")  # point at the second one
        await pilot.press("a")
        await pilot.pause()
        live = [m.payload["text"] for m in bus.read_topic("attention")]
        assert live == ["first escalation"]
        assert "1 on #attention" in str(app.query_one("#top", Static).content)
        archive = list((home / "messages" / "archive").glob("*.jsonl.gz"))
        assert archive and second.id in gzip.open(archive[0], "rt").read()

    drive(home, script)


def test_escape_closes_the_attention_list_without_acking(home: Path):
    populate(home)
    MessageBus(home).post("manager", "attention", "escalation", text="left alone")

    async def script(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert [m.payload["text"] for m in MessageBus(home).read_topic("attention")] == [
            "left alone"
        ]
        assert not app.screen.query("#attention-list")

    drive(home, script)


def test_a_with_an_empty_attention_topic_says_so(home: Path):
    populate(home)

    async def script(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        assert not app.screen.query("#attention-list")
        assert [str(n.message) for n in app._notifications] == ["nothing on #attention"]

    drive(home, script)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only directory")
def test_an_ack_that_cannot_write_notifies_instead_of_crashing(home: Path):
    """The `_write` rule covers `a` too: an unwritable home is a notification,
    and the escalation stays on the board where it can still be seen."""
    populate(home)
    MessageBus(home).post("manager", "attention", "escalation", text="undeletable")
    board = home / "messages" / "board" / "attention"

    async def script(app, pilot):
        board.chmod(0o500)
        try:
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
        finally:
            board.chmod(0o700)
        assert app.is_running
        assert [m.payload["text"] for m in MessageBus(home).read_topic("attention")] == [
            "undeletable"
        ]
        assert [n.severity for n in app._notifications] == ["error"]

    drive(home, script)


def test_acking_a_vanished_escalation_notifies_instead_of_crashing(home: Path):
    """The attention list is a snapshot: the janitor, a second `board ack` or
    the web panel can archive the line between the render and the keystroke.
    That failure arrives as the KeyError board resolution raises, not as an
    OSError — and `_write` has to cover it, or the dashboard dies at the very
    keystroke you pressed to tidy up."""
    populate(home)
    MessageBus(home).post("manager", "attention", "escalation", text="handled elsewhere")

    async def script(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        assert attention_rows(app) == ["handled elsewhere"]
        MessageBus(home).archive_topic("attention")  # out of band, as the janitor does
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert [n.severity for n in app._notifications] == ["error"]
        assert MessageBus(home).read_topic("attention") == []

    drive(home, script)


def test_h_holds_and_releases_the_highlighted_task(home: Path):
    """`h` is a thin `TaskStore.update` on the highlighted row, and it toggles
    — hold is not destructive, so unlike `c` it does not confirm (#61)."""
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("h")
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).held is True
        # a status the harness owns is untouched by the parking brake
        assert TaskStore(home).get(ids[0]).status == "queued"
        table = app.query_one("#tasks", DataTable)
        assert "⏸" in str(table.get_row_at(0)[2])
        await pilot.press("h")
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).held is False

    drive(home, script)


def test_run_refuses_a_held_task(home: Path, monkeypatch):
    ids = populate(home)
    TaskStore(home).update(ids[0], held=True)
    launched: list[str] = []
    monkeypatch.setattr(
        "quorum.runner.launch_detached", lambda h, task_id: launched.append(task_id) or 1
    )

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.press("s")
        await pilot.pause()
        assert launched == []
        # `h` is the release, and the run goes through afterwards
        await pilot.press("h")
        await pilot.press("s")
        await pilot.pause()
        assert launched == [ids[0]]

    drive(home, script)


def test_plus_and_minus_nudge_priority_without_reordering_the_table(home: Path):
    ids = populate(home)

    async def script(app, pilot):
        await pilot.press("plus", "plus")
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).priority == 2
        table = app.query_one("#tasks", DataTable)
        assert "↑2" in str(table.get_row_at(0)[2])
        # the row the reader is pointing at must not move under the cursor
        assert [table.get_row_at(r)[0] for r in range(table.row_count)] == [
            t[-6:].lower() for t in ids
        ]
        await pilot.press("minus", "minus", "minus")
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).priority == -1
        assert "↓1" in str(app.query_one("#tasks", DataTable).get_row_at(0)[2])

    drive(home, script)


def test_h_says_a_live_run_keeps_going(home: Path):
    """`h` speaks the same line `quorum task hold` does: the brake gates the
    next launch, and the run already in flight is not stopped by it (#61)."""
    from quorum import fsio

    ids = populate(home)
    fsio.atomic_write_json(runner_lock_path(home, ids[0]), {"pid": 1})

    async def script(app, pilot):
        await pilot.press("h")
        await pilot.pause()
        assert TaskStore(home).get(ids[0]).held is True
        message = str(list(app._notifications)[-1].message)
        assert "live runner keeps going" in message and "task stop" in message

    drive(home, script)


def test_the_transcript_pane_shows_the_narrative_not_raw_events(home: Path):
    """The TUI, `task tail` and the web dashboard read one renderer, so what a
    person sees is the same wherever they look."""
    ids = populate(home)
    fsio.append_jsonl(tasks.transcript_path(home, ids[0]), {
        "at": "2026-09-01T10:00:00Z",
        "event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "uv run pytest -q"}}]}},
    })

    async def script(app, pilot):
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_task == ids[0]
        assert any("🔧 Bash  uv run pytest -q" in line for line in app._log_lines)
        assert not any('"tool_use"' in line for line in app._log_lines)

    drive(home, script)
