"""The task notebook: `tasks/<id>/notes.jsonl`, the manager's notebook
generalized to a task.

Same schema, same rules — tombstones, TTL, malformed lines skipped, an
owner fence that is a convention rather than a boundary — with two
differences these tests pin: the owner is the task's actor identity
(`task-<id>`, which the runner now sets on the task's harness, and which
the manager and a human may also write), and the reader is the task's own
prompt on every run, resumed or fresh, never the digest.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, notes, tasks
from quorum.actor import journal_path, notes_path, task_actor
from quorum.agents.manager import build_digest
from quorum.cli import app
from quorum.config import load_config
from quorum.projects import ProjectRegistry
from quorum.runner import run_task
from quorum.tasks import TaskStore
from test_tasks import harness_config, make_repo, transcript_text

runner = CliRunner()


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    ProjectRegistry(home).add(make_repo(tmp_path), name="proj")
    return "proj"

NOTE = "the parser is written and committed; the tests for it are not"


def invoke(home: Path, *args: str):
    return runner.invoke(app, [*args, "--home", str(home)])


def notes_file(home: Path, task_id: str) -> Path:
    return tasks.task_dir(home, task_id) / "notes.jsonl"


def prompt_of_run(home: Path, task_id: str, index: int) -> str:
    """The prompt the fake harness echoed on its `index`-th run."""
    runs: list[list[str]] = []
    for e in fsio.read_jsonl(tasks.transcript_path(home, task_id)):
        if "argv" in e.get("event", {}):
            runs.append([])
        line = e.get("line") or ""
        if runs and line.startswith("PROMPT|"):
            runs[-1].append(line[len("PROMPT| "):])
    return "\n".join(runs[index])


# -- the CLI verbs ---------------------------------------------------------


def test_remember_show_and_forget_round_trip(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    r = invoke(home, "task", "remember", task.short_id, NOTE)
    assert r.exit_code == 0, r.output
    handle = r.output.split("(")[1].split(")")[0]

    written = fsio.read_jsonl(notes_file(home, task.id))
    assert len(written) == 1
    assert written[0]["text"] == NOTE and written[0]["sender"] == "user"
    assert notes.short_id(written[0]["id"]) == handle

    shown = invoke(home, "task", "show", task.short_id).output
    assert "notebook:" in shown and NOTE in shown and f"({handle})" in shown

    r = invoke(home, "task", "forget", task.short_id, handle)
    assert r.exit_code == 0, r.output
    # append-only: the note stays on disk, a tombstone hides it
    assert len(fsio.read_jsonl(notes_file(home, task.id))) == 2
    assert notes.task_notebook(home, task.id).active() == []
    shown = invoke(home, "task", "show", task.short_id).output
    assert NOTE not in shown and "notebook: (empty" in shown
    assert "quorum task remember" in shown  # an empty one teaches the command

    # a human's note to a task is not the manager's business: nothing is
    # journaled there, and the manager's own notebook is untouched
    assert not journal_path(home).exists()
    assert not notes_path(home).exists()


def test_forget_rejects_an_unknown_or_empty_handle_and_names_the_reader(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    notes.task_notebook(home, task.id).remember(NOTE)

    r = invoke(home, "task", "forget", task.short_id, "nosuch")
    assert r.exit_code == 1
    assert "no note matching" in r.output and f"quorum task show {task.short_id}" in r.output

    r = invoke(home, "task", "forget", task.short_id, "")
    assert r.exit_code == 1 and "handle is required" in r.output
    assert len(notes.task_notebook(home, task.id).active()) == 1

    r = invoke(home, "task", "remember", task.short_id, "   ")
    assert r.exit_code == 1 and "needs some text" in r.output


def test_a_note_with_a_ttl_expires_itself(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    now = fsio.utc_now()
    book.remember("the flaky test is quarantined until friday", ttl_days=2, now=now)
    assert len(book.active(now=now + timedelta(days=1))) == 1
    assert book.active(now=now + timedelta(days=3)) == []

    r = invoke(home, "task", "remember", task.short_id, "short-lived", "--ttl", "2")
    assert r.exit_code == 0 and "for 2d" in r.output
    assert "expires in" in invoke(home, "task", "show", task.short_id).output
    r = invoke(home, "task", "remember", task.short_id, "negative", "--ttl", "-1")
    assert r.exit_code == 1 and "--ttl" in r.output


# -- the owner fence -------------------------------------------------------


def test_only_the_task_the_manager_and_a_human_may_write(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Another task or a prompt agent reaches a task with `task nudge`. The
    fence reads QUORUM_ACTOR — a convention against crowding, not a
    boundary; the sandbox is the boundary."""
    store = TaskStore(home)
    mine = store.add("proj", "mine", "fake")
    other = store.add("proj", "theirs", "fake")

    monkeypatch.setenv("QUORUM_ACTOR", task_actor(other.id))
    r = invoke(home, "task", "remember", mine.short_id, "not my notebook")
    assert r.exit_code == 1
    assert "refused" in r.output and f"task nudge {mine.short_id}" in r.output
    assert invoke(home, "task", "forget", mine.short_id, "abc123").exit_code == 1
    assert not notes_file(home, mine.id).exists()

    monkeypatch.setenv("QUORUM_ACTOR", "babysitter")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01REFUSED")
    assert invoke(home, "task", "remember", mine.short_id, "not mine either").exit_code == 1
    assert invoke(home, "task", "forget", mine.short_id, "abc123").exit_code == 1
    assert not notes_file(home, mine.id).exists()
    # an agent reaching for a task's memory is journaled, like any refusal
    journaled = fsio.read_jsonl(journal_path(home, "babysitter"))
    assert [e["action"] for e in journaled] == ["task.remember.refused", "task.forget.refused"]
    assert journaled[0]["target"] == mine.short_id and journaled[0]["run"] == "01REFUSED"

    monkeypatch.setenv("QUORUM_ACTOR", task_actor(mine.id))
    monkeypatch.delenv("QUORUM_ACTOR_RUN")
    assert invoke(home, "task", "remember", mine.short_id, "my own note").exit_code == 0

    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01MGRRUN")
    assert invoke(home, "task", "remember", mine.short_id, "from the manager").exit_code == 0

    monkeypatch.delenv("QUORUM_ACTOR")
    monkeypatch.delenv("QUORUM_ACTOR_RUN")
    assert invoke(home, "task", "remember", mine.short_id, "from the human").exit_code == 0

    written = notes.task_notebook(home, mine.id).active()
    assert [e["sender"] for e in written] == [task_actor(mine.id), "manager", "user"]
    assert written[1]["run_id"] == "01MGRRUN"
    # the manager's write is an action of its run, journaled and attributed
    mgr = fsio.read_jsonl(journal_path(home))
    assert [(e["action"], e["run"]) for e in mgr] == [("task.remember", "01MGRRUN")]
    # and the manager's own notebook still refuses a task (unchanged rule)
    monkeypatch.setenv("QUORUM_ACTOR", task_actor(mine.id))
    assert invoke(home, "manager", "remember", "let me in").exit_code == 1
    assert not notes_path(home).exists()


def test_a_task_actor_is_neither_journaled_nor_capped(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The tag is identity only. A task's record is reports.jsonl and its
    transcript; the runner is its rail — nothing under state/agents/ and
    no action cap, even if a run id and cap somehow ride along."""
    task = TaskStore(home).add("proj", "p", "fake")
    monkeypatch.setenv("QUORUM_ACTOR", task_actor(task.id))
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01TASKRUN")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "1")
    assert invoke(home, "task", "remember", task.short_id, "one").exit_code == 0
    assert invoke(home, "task", "remember", task.short_id, "two").exit_code == 0
    assert invoke(home, "task", "report", task.short_id, "--status", "executing", "x").exit_code == 0
    assert invoke(home, "task", "nudge", task.short_id, "note to self").exit_code == 0

    agents_dir = home / "state" / "agents"
    assert not agents_dir.exists() or not any(p.name.startswith("task-") for p in agents_dir.iterdir())
    assert not journal_path(home).exists()
    assert [e["text"] for e in notes.task_notebook(home, task.id).active()] == ["one", "two"]
    # a task's other CLI calls now carry its identity, where they used to read as "user"
    pending = fsio.sorted_entries(home / "messages" / "inbox" / tasks.inbox_name(task.id) / "new")
    assert fsio.read_json(pending[0])["from"] == task_actor(task.id)


# -- the reader: the runner ------------------------------------------------


def test_the_runner_renders_the_notebook_on_every_run_resumed_or_fresh(
    home: Path, project: str
):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "build the parser", "fake")

    run_task(home, config, task.id)  # nothing kept yet: no section at all
    first = prompt_of_run(home, task.id, 0)
    assert notes.TASK_SECTION_HEADER not in first
    assert "quorum task remember" in first  # the preamble teaches it regardless

    notes.task_notebook(home, task.id).remember(NOTE, sender=task_actor(task.id))
    run_task(home, config, task.id)  # resumes the captured session
    resumed = prompt_of_run(home, task.id, 1)
    assert notes.TASK_SECTION_HEADER in resumed and NOTE in resumed
    assert f"{task_actor(task.id)}: {NOTE}" in resumed  # attributed, like the manager's

    run_task(home, config, task.id, fresh_session=True)  # the run that needs it most
    fresh = prompt_of_run(home, task.id, 2)
    assert notes.TASK_SECTION_HEADER in fresh and NOTE in fresh
    # after the task body and upstream, before guidance: the newest input last
    assert fresh.index("# Task") < fresh.index(notes.TASK_SECTION_HEADER)


def test_the_harness_writes_its_own_notebook_under_the_tag_the_runner_set(
    home: Path, project: str, monkeypatch: pytest.MonkeyPatch
):
    """End to end: the harness calls `quorum task remember` for its own task
    (allowed, attributed to `task-<id>`), then `quorum manager remember`
    (refused), and the next run finds the note in its prompt. A launcher's
    tag — the manager's, with a cap of one — never leaks into the run."""
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "build the parser", "fake")
    monkeypatch.setenv("FAKE_HARNESS_MODE", "task_remember")
    monkeypatch.setenv("FAKE_HARNESS_NOTE", NOTE)
    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "01LAUNCH")
    monkeypatch.setenv("QUORUM_ACTOR_CAP", "1")

    assert run_task(home, config, task.id) == 0
    text = transcript_text(home, task.id)
    assert "ACT| task remember -> exit 0" in text
    assert "ACT| manager remember -> exit 1" in text
    assert f"ACTOR| {task_actor(task.id)}" in text
    written = notes.task_notebook(home, task.id).active()
    assert [e["sender"] for e in written] == [task_actor(task.id)]
    assert not notes_path(home).exists()
    # The refused reach for the manager's notebook is journaled where the
    # manager will see it, attributed to the task — and to no run: the
    # launcher's run id and cap did not leak into the task's harness (its
    # cap of one would have refused the second CLI call outright).
    mgr = fsio.read_jsonl(journal_path(home))
    assert [(e["action"], e["actor"], e["run"]) for e in mgr] == [
        ("remember.refused", task_actor(task.id), "")
    ]

    assert run_task(home, config, task.id) == 0
    assert NOTE in prompt_of_run(home, task.id, 1)


# -- the budget ------------------------------------------------------------


def test_over_its_cap_the_prompt_keeps_the_newest_and_says_what_it_dropped(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    for i in range(notes.TASK_NOTES_MAX_ENTRIES + 3):
        book.remember(f"standing fact {i}")

    section = notes.task_notebook(home, task.id).render()
    assert section[0] == notes.TASK_SECTION_HEADER
    assert "3 older note(s) dropped" in section[1] and "prompt budget" in section[1]
    # told what to do about it, with *its* verbs
    assert f"quorum task remember {task.short_id}" in section[1]
    assert f"quorum task forget {task.short_id}" in section[1]
    assert len(section) == 2 + notes.TASK_NOTES_MAX_ENTRIES
    assert "standing fact 0" not in "\n".join(section)
    assert f"standing fact {notes.TASK_NOTES_MAX_ENTRIES + 2}" in section[-1]


def test_the_newest_note_survives_the_byte_budget(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    for i in range(12):
        book.remember(f"{i} " + "x" * notes.NOTE_MAX_CHARS)
    book.remember("the one that matters")

    section = notes.task_notebook(home, task.id).render()
    body = "\n".join(section[1:])
    assert "the one that matters" in section[-1]
    assert len(body) <= notes.TASK_NOTES_MAX_BYTES + len(section[1]) + 1  # the drop line
    assert sum(len(line) + 1 for line in section[2:]) <= notes.TASK_NOTES_MAX_BYTES
    assert "dropped" in section[1] and "…" in body


def test_a_notebook_past_the_scan_window_says_so_even_when_otherwise_empty(home: Path):
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    book.remember("the oldest standing fact")
    padding = {"id": "01PADPADPAD", "ts": "2026-01-01T00:00:00Z", "retired": True,
               "pad": "x" * 4000}
    while book.unscanned_bytes() == 0:
        fsio.append_jsonl(book.path, padding)

    section = notes.task_notebook(home, task.id).render()
    assert section[0] == notes.TASK_SECTION_HEADER
    assert "not scanned" in section[1] and len(section) == 2
    assert "not scanned" in invoke(home, "task", "show", task.short_id).output


# -- the rules shared with the manager's notebook --------------------------


def test_a_torn_or_foreign_line_never_breaks_a_run(home: Path):
    """Prompt composition is on every run; one hand-edited line must not
    fail the task forever."""
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    book.remember(NOTE)
    with open(book.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"no": "id"}) + "\n")
        f.write(json.dumps({"id": 7, "ts": "2026-09-01T00:00:00Z", "text": "int id"}) + "\n")
        f.write(json.dumps(["not", "even", "an", "object"]) + "\n")
        f.write("{half a line\n")

    section = "\n".join(notes.task_notebook(home, task.id).render())
    assert NOTE in section and "int id" not in section
    assert [e["text"] for e in book.active()] == [NOTE]
    r = invoke(home, "task", "show", task.short_id)
    assert r.exit_code == 0 and NOTE in r.output


def test_an_unreadable_notebook_reads_as_empty_rather_than_failing_the_run(
    home: Path, project: str
):
    """`compose_prompt` reads the notebook before the harness is spawned, so
    a notes.jsonl that cannot be read at all — a directory, here — would
    otherwise fail every run of that task with no run record and no
    transcript to say why."""
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "build the parser", "fake")
    notes_file(home, task.id).mkdir(parents=True)

    assert notes.task_notebook(home, task.id).active() == []
    assert notes.task_notebook(home, task.id).render() == []
    assert run_task(home, config, task.id) == 0
    assert notes.TASK_SECTION_HEADER not in prompt_of_run(home, task.id, 0)
    assert invoke(home, "task", "show", task.short_id).exit_code == 0


def test_a_manager_under_another_name_may_still_write_a_tasks_notebook(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The extra writer is "the manager", not the literal name: a manager
    configured as `[agents.boss]` is tagged QUORUM_ACTOR=boss."""
    harness_config(
        home,
        extra='\n[agents.boss]\ntype = "manager"\nschedule = "every 30m"\n',
    )
    task = TaskStore(home).add("proj", "p", "fake")
    assert notes.task_notebook(home, task.id).writers == {"manager", "boss"}

    monkeypatch.setenv("QUORUM_ACTOR", "boss")
    r = invoke(home, "task", "remember", task.short_id, NOTE)
    assert r.exit_code == 0, r.output
    assert [e["sender"] for e in notes.task_notebook(home, task.id).active()] == ["boss"]

    # a prompt agent is still refused, and an unreadable config still admits
    # the manager under its default name
    monkeypatch.setenv("QUORUM_ACTOR", "babysitter")
    assert invoke(home, "task", "remember", task.short_id, "not mine").exit_code == 1
    (home / "config.toml").write_text("[[[ not toml\n")
    assert notes.task_notebook(home, task.id).writers == {"manager"}


def test_the_byte_budget_counts_bytes_not_characters(home: Path):
    """A notebook in a non-Latin script costs up to three bytes a character;
    counting characters would hand the prompt three times the budget."""
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    for i in range(12):
        book.remember(f"{i} " + "実" * 400)

    section = notes.task_notebook(home, task.id).render()
    rendered = section[2:]  # past the header and the drop line
    assert len(rendered) < 12 and "dropped" in section[1]
    assert sum(len(line.encode("utf-8")) + 1 for line in rendered) <= (
        notes.TASK_NOTES_MAX_BYTES
    )
    assert "実" in section[-1]


def test_task_show_prints_the_notebook_a_run_reads_header_and_all(home: Path):
    """What `task show` prints is what the harness is handed — the section
    header included, which is how a reader knows the two agree."""
    task = TaskStore(home).add("proj", "p", "fake")
    notes.task_notebook(home, task.id).remember(NOTE)

    shown = invoke(home, "task", "show", task.short_id).output
    for line in notes.task_notebook(home, task.id).render():
        assert line in shown
    assert notes.TASK_SECTION_HEADER in shown
    assert "keeps state between its runs" not in shown  # it is not empty


def test_an_empty_notebook_past_its_scan_window_still_teaches_the_command(home: Path):
    """The two halves are independent: the file may render a warning and
    still hold no live note, and the hint belongs to the notes."""
    task = TaskStore(home).add("proj", "p", "fake")
    book = notes.task_notebook(home, task.id)
    padding = {"id": "01PADPADPAD", "ts": "2026-01-01T00:00:00Z", "retired": True,
               "pad": "x" * 4000}
    while book.unscanned_bytes() == 0:
        fsio.append_jsonl(book.path, padding)

    shown = invoke(home, "task", "show", task.short_id).output
    assert "not scanned" in shown
    assert f'quorum task remember {task.short_id}' in shown


def test_remember_on_an_attached_task_does_not_promise_a_run_will_read_it(
    home: Path, tmp_path: Path
):
    """An adopted session does not go through the runner, so nothing renders
    its notebook into the session. The note is kept; `task show` reads it."""
    store = TaskStore(home)
    plain = store.add("proj", "p", "fake")
    adopted = store.add("proj", "adopted", "fake")
    store.update(adopted.id, attached=True, workdir=str(tmp_path))

    ordinary = invoke(home, "task", "remember", plain.short_id, NOTE).output
    assert "every future run" in ordinary

    r = invoke(home, "task", "remember", adopted.short_id, NOTE)
    assert r.exit_code == 0, r.output
    assert "every future run" not in r.output
    assert f"quorum task show {adopted.short_id}" in r.output
    assert "attached" in r.output
    # the write itself is ordinary, and `task show` prints it
    assert [e["text"] for e in notes.task_notebook(home, adopted.id).active()] == [NOTE]
    assert NOTE in invoke(home, "task", "show", adopted.short_id).output


def test_the_digest_does_not_carry_a_tasks_notebook(home: Path, clock):
    """The manager reads reports; the notebook is the task's own."""
    store = TaskStore(home)
    task = store.add("proj", "p", "fake")
    notes.task_notebook(home, task.id).remember(NOTE)
    digest = build_digest(home, store.list(), clock(), [])
    assert task.short_id in digest
    assert NOTE not in digest and notes.TASK_SECTION_HEADER not in digest


def test_the_manager_notebook_is_the_same_object_with_a_different_face(home: Path):
    """One class, two constructors: the difference between a manager's
    notebook and a task's is carried by the `Notebook` value, not by code."""
    task = TaskStore(home).add("proj", "p", "fake")
    mgr = notes.agent_notebook(home)
    mine = notes.task_notebook(home, task.id)
    assert mgr.path == notes_path(home) and mine.path == notes_file(home, task.id)
    assert mgr.writers == frozenset() and mine.writers == {"manager"}
    assert mgr.may_write("manager") and not mgr.may_write(task_actor(task.id))
    assert mine.may_write(task_actor(task.id)) and mine.may_write("manager")
    assert not mine.may_write("babysitter") and not mine.may_write(task_actor("01OTHER"))
    assert (mgr.max_entries, mgr.max_bytes) == (notes.NOTES_MAX_ENTRIES, notes.NOTES_MAX_BYTES)
    assert (mine.max_entries, mine.max_bytes) == (
        notes.TASK_NOTES_MAX_ENTRIES, notes.TASK_NOTES_MAX_BYTES
    )
    assert mgr.render_notes([]) == [notes.SECTION_HEADER, notes.EMPTY_LINE]
    assert mine.render() == []
