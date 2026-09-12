"""`show self`: what a run can read about itself (#94).

Three layers, tested where each one lives. `actor.py` resolves *who* is
asking out of the environment and the journal; `views.py` renders the facts
it is handed; the CLI joins the two and turns an unresolvable `self` into an
error naming the fix. The rule the whole feature is held to is that it is
read-only: reading a cap is not a way around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import harness_config, make_repo
from quorum import actor, fsio, notes, tasks, views
from quorum.cli import app
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore

runner = CliRunner()


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    harness_config(home)
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    return "proj"


@pytest.fixture
def agent_home(home: Path) -> Path:
    """A home with the manager and a prompt agent, so the agent paths are
    exercised on the historical name and on one under `state/agents/`."""
    harness_config(
        home,
        extra=(
            '\n[agents.manager]\ntype = "manager"\nschedule = "every 5m"\n'
            '\n[agents.scout]\ntype = "prompt"\nschedule = "every 5m"\n'
        ),
    )
    return home


def as_task(monkeypatch: pytest.MonkeyPatch, task_id: str) -> None:
    """Tag the process the way `runner.py` tags a task's harness: identity
    only, no run id and no cap."""
    monkeypatch.setenv(actor.ACTOR_ENV, actor.task_actor(task_id))
    monkeypatch.delenv(actor.ACTOR_RUN_ENV, raising=False)
    monkeypatch.delenv(actor.ACTOR_CAP_ENV, raising=False)


def as_agent(monkeypatch: pytest.MonkeyPatch, name: str, run: str = "01RUN", cap: int = 5) -> None:
    """Tag the process the way `run_agent_harness` tags an agent's harness."""
    for key, value in actor.actor_env(name, run, cap).items():
        monkeypatch.setenv(key, value)


def labels(rows: list[dict], section: str = "self") -> dict[str, dict]:
    return {r["label"]: r for r in rows if r["section"] == section and r["kind"] == "field"}


# -- resolving who is asking (actor.py) -------------------------------------


def test_the_tag_resolves_to_exactly_one_of_a_task_and_an_agent(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """`task-<id>` is a task and nothing else, a bare name is an agent and
    nothing else, and an untagged process is neither — which is what lets the
    CLI tell a run that typed the wrong `self` which one it wanted."""
    monkeypatch.delenv(actor.ACTOR_ENV, raising=False)
    assert (actor.self_task_id(), actor.self_agent_name()) == (None, None)

    as_task(monkeypatch, "01ABCDEF")
    assert (actor.self_task_id(), actor.self_agent_name()) == ("01ABCDEF", None)

    as_agent(monkeypatch, "manager")
    assert (actor.self_task_id(), actor.self_agent_name()) == (None, "manager")


def test_self_run_counts_this_runs_journalled_actions_and_not_the_cap_hit(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The number `show self` reports is the number the guard enforces: read
    back out of the journal, this run's lines only, and a `cap.hit` — the
    record of a refusal — is not an action."""
    as_agent(monkeypatch, "manager", run="01MINE", cap=4)
    journal = actor.journal_path(home, "manager")
    for entry in (
        {"run": "01MINE", "action": "task.run"},
        {"run": "01MINE", "action": "board.post"},
        {"run": "01MINE", "action": "cap.hit"},
        {"run": "01OTHER", "action": "task.run"},
        "not a dict at all",
    ):
        fsio.append_jsonl(journal, entry)

    state = actor.self_run(home)
    assert (state.actor, state.run, state.cap, state.actions) == ("manager", "01MINE", 4, 2)
    assert state.capped is True


def test_self_run_of_a_task_is_identity_without_a_cap(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A task's tag carries no run id and no cap, so nothing counts its
    actions — `capped` says so rather than reporting a cap it is not held
    to."""
    as_task(monkeypatch, "01ABCDEF")
    state = actor.self_run(home)
    assert (state.actor, state.run, state.actions) == ("task-01ABCDEF", "", 0)
    assert state.capped is False


def test_an_unreadable_cap_tag_falls_back_rather_than_raising(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both readers of the cap run in front of a live agent, so a tag that
    is not a number is the default, never a traceback."""
    monkeypatch.setenv(actor.ACTOR_CAP_ENV, "lots")
    assert actor.current_cap() == actor.DEFAULT_MAX_ACTIONS_PER_RUN


# -- rendering the facts (views.py) -----------------------------------------


def test_task_self_detail_adds_the_budget_as_a_fact_before_it_is_a_refusal(
    home: Path, project: str
):
    """The point of the section: the per-run limits are printed whether or
    not they have been exceeded, so a run can plan around the gate instead of
    meeting it as a refusal on its next launch."""
    config = home / "config.toml"
    config.write_text(
        config.read_text().replace("[tasks]", "[tasks]\nmax_cost_per_run = 0.50"),
        encoding="utf-8",
    )
    store = TaskStore(home)
    task = store.add(project, "do it", "fake")
    run = tasks.TaskRun(started_at=fsio.iso(fsio.utc_now()), usage={"cost_usd": 0.12})
    task = store.update(task.id, runs=[run])

    rows = views.task_self_detail(home, task, actor.SelfRun("task-" + task.id, "", 20, 0))
    fields = labels(rows)
    assert fields["limits"]["max_cost_per_run"] == 0.50
    assert "max_tokens_per_run off" in fields["limits"]["text"]
    assert fields["spent"]["last_run_usage"] == {"cost_usd": 0.12}
    # the run is still open, so its own spend is not knowable yet — say so
    # rather than letting the last figure read as this run's
    assert "recorded when it ends" in fields["spent"]["text"]
    # nothing about a task run is action-capped, and the row says that
    # instead of leaving a run to assume a number it cannot find
    assert fields["actions"]["capped"] is False


def test_task_self_detail_says_a_handoff_is_owed_and_then_that_it_is_not(
    home: Path, project: str
):
    """The check the preamble sends a task here for before it reports done.
    Both directions: owed to a dependent and not yet written (a row a surface
    colours), then written."""
    store = TaskStore(home)
    upstream = store.add(project, "build it", "fake")
    dependent = store.add(project, "review it", "fake", depends_on=[upstream.id])
    state = actor.SelfRun(actor.task_actor(upstream.id), "", 20, 0)

    owed = labels(views.task_self_detail(home, store.get(upstream.id), state))["handoff"]
    assert owed["dependents"] == [dependent.short_id] and owed["handoff_written"] is False
    assert owed["style"] == "warning" and "--handoff <file>" in owed["text"]

    tasks.write_handoff(home, upstream.id, "Changed: the thing.\n")
    left = labels(views.task_self_detail(home, store.get(upstream.id), state))["handoff"]
    assert left["handoff_written"] is True and left["style"] == ""

    # and the other task, which nothing depends on, owes nobody anything
    alone = labels(views.task_self_detail(home, store.get(dependent.id), state))["handoff"]
    assert alone["dependents"] == [] and "no handoff is owed" in alone["text"]


def test_task_self_detail_is_the_whole_record_with_one_section_added(
    home: Path, project: str
):
    """`self` is not a second rendering of a task: it is `task_detail` with
    the run-scoped section spliced in after the record's own fields, so every
    line a person would read is still there and in the same order."""
    task = TaskStore(home).add(project, "do it", "fake")
    state = actor.SelfRun(actor.task_actor(task.id), "", 20, 0)
    plain = views.task_detail(home, task)
    with_self = views.task_self_detail(home, task, state)

    assert [r for r in with_self if r["section"] != "self"] == plain
    # and the added section sits directly after the record's own fields,
    # where `_DETAIL_SECTIONS` says it goes
    sections = [r["section"] for r in with_self]
    first = sections.index("self")
    assert set(sections[:first]) == {"record"}
    assert "record" not in sections[first:]


def test_the_notebook_row_counts_what_the_rendering_will_carry(
    home: Path, project: str
):
    """Notebook size as numbers rather than as a rendering, because the
    rendering only says a note was dropped once it already has been."""
    task = TaskStore(home).add(project, "do it", "fake")
    book = notes.task_notebook(home, task.id)
    for i in range(3):
        book.remember(f"fact {i}")

    size = notes.task_notebook(home, task.id).size()
    assert (size["notes"], size["shown"], size["dropped"]) == (3, 3, 0)
    assert 0 < size["bytes"] < size["max_bytes"]

    row = labels(views.task_self_detail(home, task, actor.SelfRun("x", "", 20, 0)))["notebook"]
    assert row["notebook"] == size and "3 note(s)" in row["text"]


def test_a_notebook_over_its_byte_budget_reports_what_it_is_dropping(home: Path):
    """The failing half: past the budget, `size` counts the notes the
    rendering will silently leave out, and the row says how to consolidate."""
    book = notes.agent_notebook(home, "manager")
    for i in range(30):
        book.remember(f"note {i} " + "x" * 300)

    size = notes.agent_notebook(home, "manager").size()
    assert size["dropped"] > 0 and size["shown"] < size["notes"]
    assert size["bytes"] <= size["max_bytes"]


def test_agent_detail_rows_carry_the_action_cap_only_for_the_agents_own_run(
    agent_home: Path,
):
    """An agent's record is readable by anyone; how much of its action cap
    this run has left exists only inside the run, and is the one thing the
    `self` section adds."""
    rows = views.agent_detail_rows(agent_home, "scout")
    assert rows is not None and not [r for r in rows if r["section"] == "self"]

    mine = views.agent_detail_rows(
        agent_home, "scout", run=actor.SelfRun("scout", "01RUN", 5, 4)
    )
    actions = labels(mine)["actions"]
    assert (actions["actions"], actions["cap"], actions["remaining"]) == (4, 5, 1)
    assert actions["style"] == ""

    spent = views.agent_detail_rows(
        agent_home, "scout", run=actor.SelfRun("scout", "01RUN", 5, 5)
    )
    assert labels(spent)["actions"]["remaining"] == 0
    assert labels(spent)["actions"]["style"] == "warning"


def test_agent_detail_rows_are_none_for_an_agent_that_is_not_configured(home: Path):
    assert views.agent_detail_rows(home, "ghost") is None


# -- the CLI ----------------------------------------------------------------


def test_task_show_self_prints_the_rows_its_json_carries(
    home: Path, project: str, monkeypatch: pytest.MonkeyPatch
):
    """Same contract as `task show <id>` (#127): one assembly, printed and
    dumped, so the text and `--json` cannot say different things."""
    task = TaskStore(home).add(project, "do it", "fake")
    as_task(monkeypatch, task.id)

    text = runner.invoke(app, ["task", "show", "self"])
    assert text.exit_code == 0, text.output
    dumped = json.loads(runner.invoke(app, ["task", "show", "self", "--json"]).output)
    rows = dumped["detail"]
    assert text.output.splitlines() == [views.detail_line(r) for r in rows]
    assert dumped["id"] == task.id
    assert labels(rows)["actor"]["actor"] == actor.task_actor(task.id)


def test_agent_show_self_prints_the_rows_its_json_carries(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
):
    as_agent(monkeypatch, "scout", run="01RUN", cap=7)

    text = runner.invoke(app, ["agent", "show", "self"])
    assert text.exit_code == 0, text.output
    dumped = json.loads(runner.invoke(app, ["agent", "show", "self", "--json"]).output)
    assert dumped["name"] == "scout"
    assert text.output.splitlines() == [views.detail_line(r) for r in dumped["detail"]]
    assert labels(dumped["detail"])["actions"]["cap"] == 7


@pytest.mark.parametrize("name", ["manager", "scout"])
def test_agent_show_takes_a_name_from_a_person_too(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    """The record `self` prints is the one a person reads — same command,
    without the run-scoped section, which an outside reader has no run for.
    The manager is an agent like any other here, which is what makes
    `quorum agent show self` the manager's `show self` as well."""
    monkeypatch.delenv(actor.ACTOR_ENV, raising=False)
    r = runner.invoke(app, ["agent", "show", name])
    assert r.exit_code == 0, r.output
    assert r.output.startswith(f"agent {name}  (")
    assert "this run:" not in r.output


def test_agent_show_names_the_listing_when_there_is_no_such_agent(agent_home: Path):
    r = runner.invoke(app, ["agent", "show", "ghost"])
    assert r.exit_code == 1 and "no agent 'ghost'" in r.output


@pytest.mark.parametrize(
    ("command", "tag", "expected"),
    [
        pytest.param(
            ["task", "show", "self"], None,
            "run it from inside a task run or an agent run",
            id="task-untagged",
        ),
        pytest.param(
            ["agent", "show", "self"], None,
            "run it from inside a task run or an agent run",
            id="agent-untagged",
        ),
        pytest.param(
            ["task", "show", "self"], "manager",
            "is the agent 'manager', not a task — `quorum agent show self`",
            id="task-asked-by-an-agent",
        ),
        pytest.param(
            ["agent", "show", "self"], "task-01ABCDEFGHJKMNPQRSTVWXYZ0",
            "not an agent — `quorum task show self`",
            id="agent-asked-by-a-task",
        ),
        pytest.param(
            ["task", "show", "self"], "task-01ABCDEFGHJKMNPQRSTVWXYZ0",
            "no such task is in",
            id="task-tag-outlived-the-record",
        ),
    ],
)
def test_self_outside_a_matching_run_is_an_error_naming_the_fix(
    home: Path, monkeypatch: pytest.MonkeyPatch, command: list[str], tag: str | None, expected: str
):
    """`self` can only ever name the process that typed it, so every way of
    typing it in the wrong place is an error — and each one says what to do
    instead, because a run that gets this back has no other way to find out."""
    if tag is None:
        monkeypatch.delenv(actor.ACTOR_ENV, raising=False)
    else:
        monkeypatch.setenv(actor.ACTOR_ENV, tag)
    r = runner.invoke(app, command)
    assert r.exit_code == 1
    assert expected in r.output


def test_show_self_journals_nothing_and_spends_no_action_cap(
    agent_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Read-only, and the cap says so: `show self` is not a mutating command,
    so it writes no journal line and reading the cap does not spend it."""
    as_agent(monkeypatch, "scout", run="01RUN", cap=2)
    journal = actor.journal_path(agent_home, "scout")

    for _ in range(3):
        assert runner.invoke(app, ["agent", "show", "self"]).exit_code == 0
        assert runner.invoke(app, ["task", "show", "self"]).exit_code == 1

    assert not journal.exists()
    assert actor.self_run(agent_home).actions == 0


def test_the_preamble_points_a_task_at_its_own_record(home: Path, project: str):
    """The paragraph is part of the deliverable: a task only calls the
    command if its prompt says the command exists. Asserted by the marker it
    teaches, not by its wording (CLAUDE.md)."""
    from quorum.runner import compose_prompt

    task = TaskStore(home).add(project, "do it", "fake")
    text = compose_prompt(home, task, home, [])
    assert "quorum task show self" in text
    # the two moments it names, by the row labels a run greps the output for
    assert "`limits:`" in text and "`handoff:`" in text
