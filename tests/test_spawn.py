"""Tasks that spawn tasks (#43): the readers, the one refusal, and what every
surface shows about a family.

The rails under test are the rate-limit kind: who may create a task at all
(`--allow-spawn`), how many one task may create (`[tasks].max_spawn_per_task`)
and how deep a chain may go (`[tasks].max_spawn_depth`). Nothing here judges
the work, nothing launches a child, and nothing cascades — a test below holds
each of those.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import harness_config, harness_table, make_repo
from quorum import fsio, tasks, views
from quorum.actor import task_actor
from quorum.agents import manager
from quorum.cli import app
from quorum.config import load_config
from quorum.projects import ProjectRegistry
from quorum.runner import run_task
from quorum.tasks import TaskStore, spawn_children, spawn_depth, spawn_refusal, spawn_states

runner = CliRunner()


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    return "proj"


def as_task(monkeypatch, task) -> None:
    """Run the next CLI call the way the runner runs a task's harness:
    tagged with that task's identity and nothing else (actor.py)."""
    monkeypatch.setenv("QUORUM_ACTOR", task_actor(task.id))


def transcript_text(home: Path, task_id: str) -> str:
    lines = []
    for e in fsio.read_jsonl(tasks.transcript_path(home, task_id)):
        lines.append(e.get("line") or json.dumps(e.get("event")))
    return "\n".join(lines)


def manager_journal(home: Path) -> list[dict]:
    from quorum.actor import journal_path

    return fsio.read_jsonl(journal_path(home))


# -- the readers -------------------------------------------------------------


def test_parent_and_allow_spawn_survive_a_round_trip_and_default_off(home: Path):
    store = TaskStore(home)
    plain = store.add("proj", "queued by a person", "fake")
    assert plain.parent is None and plain.allow_spawn is False

    child = store.add("proj", "queued by a run", "fake", parent=plain.id, allow_spawn=True)
    reread = store.get(child.id)
    assert reread.parent == plain.id and reread.allow_spawn is True


def test_a_task_json_written_before_spawning_existed_still_loads(home: Path):
    """Old homes upgrade in place: both fields are absent, not null, on every
    record written before this version."""
    store = TaskStore(home)
    task = store.add("proj", "old record", "fake")
    path = tasks.task_json_path(home, task.id)
    data = json.loads(path.read_text())
    del data["parent"], data["allow_spawn"]
    path.write_text(json.dumps(data))

    reread = store.get(task.id)
    assert reread.parent is None and reread.allow_spawn is False


def test_children_and_depth_are_read_from_the_listing(home: Path):
    store = TaskStore(home)
    root = store.add("proj", "root", "fake", allow_spawn=True)
    kid = store.add("proj", "kid", "fake", parent=root.id, allow_spawn=True)
    grandkid = store.add("proj", "grandkid", "fake", parent=kid.id)
    stranger = store.add("proj", "unrelated", "fake")
    all_tasks = store.list()
    by_id = {t.id: t for t in all_tasks}

    assert [t.id for t in spawn_children(root.id, all_tasks)] == [kid.id]
    assert spawn_children(stranger.id, all_tasks) == []
    assert spawn_depth(root, by_id) == 0
    assert spawn_depth(kid, by_id) == 1
    assert spawn_depth(grandkid, by_id) == 2


def test_depth_is_total_over_a_missing_ancestor_and_a_hand_edited_loop(home: Path):
    """Every reader here is total: `parent` is a string on a JSON file and
    can name a task that is gone, or a chain that loops."""
    store = TaskStore(home)
    orphan = store.add("proj", "parent's record is gone", "fake", parent="01GONEGONEGONE")
    a = store.add("proj", "a", "fake")
    b = store.add("proj", "b", "fake", parent=a.id)
    store.update(a.id, parent=b.id)  # only reachable by hand-editing
    by_id = {t.id: t for t in store.list()}

    assert spawn_depth(orphan, {orphan.id: orphan}) == 1  # the link still counts
    assert spawn_depth(by_id[a.id], by_id) == 2  # stops instead of spinning


def test_spawn_states_covers_only_families(home: Path):
    store = TaskStore(home)
    root = store.add("proj", "root", "fake", allow_spawn=True)
    kid = store.add("proj", "kid", "fake", parent=root.id)
    lonely = store.add("proj", "no family", "fake")
    states = spawn_states(store.list(), max_per_task=5)

    assert set(states) == {root.id, kid.id}
    assert states[root.id] == {"parent": "", "children": [kid.short_id], "capped": False, "depth": 0}
    assert states[kid.id]["parent"] == root.short_id and states[kid.id]["depth"] == 1
    assert lonely.id not in states


def test_spawn_states_marks_a_parent_that_used_its_share(home: Path):
    store = TaskStore(home)
    root = store.add("proj", "root", "fake", allow_spawn=True)
    for i in range(2):
        store.add("proj", f"kid {i}", "fake", parent=root.id)
    assert spawn_states(store.list(), max_per_task=3)[root.id]["capped"] is False
    assert spawn_states(store.list(), max_per_task=2)[root.id]["capped"] is True


def test_spawn_refusal_passes_a_spawn_enabled_task_inside_its_share(home: Path):
    store = TaskStore(home)
    root = store.add("proj", "root", "fake", allow_spawn=True)
    store.add("proj", "kid", "fake", parent=root.id)
    assert spawn_refusal(root, store.list(), max_per_task=5, max_depth=1) is None


@pytest.mark.parametrize(
    "case,names",
    [
        pytest.param("not-enabled", ["allow-spawn"], id="not-enabled"),
        pytest.param("too-deep", ["max_spawn_depth"], id="too-deep"),
        pytest.param("over-cap", ["max_spawn_per_task"], id="over-cap"),
    ],
)
def test_spawn_refusal_names_the_setting_behind_it_and_the_way_out(
    home: Path, case: str, names: list[str]
):
    """Three refusals, one shape: each names the setting that produced it and
    sends the idea to the report channel instead of dropping it."""
    store = TaskStore(home)
    root = store.add("proj", "root", "fake", allow_spawn=case != "not-enabled")
    parent = root
    if case == "too-deep":
        parent = store.add("proj", "kid", "fake", parent=root.id, allow_spawn=True)
    if case == "over-cap":
        for i in range(2):
            store.add("proj", f"kid {i}", "fake", parent=root.id)

    refusal = spawn_refusal(parent, store.list(), max_per_task=2, max_depth=1)
    assert refusal is not None
    for name in names:
        assert name in refusal
    assert "quorum task report" in refusal  # the idea has somewhere to go


# -- `quorum task add` under a task's actor tag -------------------------------


def setup(home: Path, tmp_path: Path, tasks_extra: str = "") -> str:
    repo = make_repo(tmp_path, "cliproj")
    ProjectRegistry(home).add(repo, name="cliproj")
    (home / "config.toml").write_text(
        "[tasks]\n" 'default_harness = "fake"\n' f"{tasks_extra}" f"{harness_table(resume=False)}"
    )
    return "cliproj"


def test_a_person_may_queue_freely_and_gets_no_parent(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "a human's task"])
    assert r.exit_code == 0, r.output
    task = TaskStore(home).list()[0]
    assert task.parent is None and task.allow_spawn is False
    assert manager_journal(home) == []  # a person's actions still journal nothing


def test_allow_spawn_marks_the_task_and_the_home_default_does_too(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path)
    assert runner.invoke(app, ["task", "add", slug, "trusted", "--allow-spawn"]).exit_code == 0
    assert TaskStore(home).list()[0].allow_spawn is True

    (home / "config.toml").write_text(
        (home / "config.toml").read_text().replace("[tasks]\n", "[tasks]\nallow_spawn = true\n")
    )
    assert runner.invoke(app, ["task", "add", slug, "trusted by default"]).exit_code == 0
    assert TaskStore(home).list()[1].allow_spawn is True


def test_a_spawn_enabled_task_queues_a_child_that_records_its_parent(
    home: Path, tmp_path: Path, monkeypatch
):
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "the work that found more work", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)

    r = runner.invoke(app, ["task", "add", slug, "the follow-up"])
    assert r.exit_code == 0, r.output
    assert f"spawned by task {parent.short_id}" in r.output

    child = [t for t in store.list() if t.id != parent.id][0]
    assert child.parent == parent.id
    assert child.harness == parent.harness  # inherited, since --harness said nothing
    assert child.allow_spawn is False  # no recursion unless asked for
    assert child.status == "queued"  # queued, never launched

    # The one CLI call a task makes that the manager has to see.
    entry = manager_journal(home)[-1]
    assert entry["action"] == "task.add"
    assert entry["actor"] == task_actor(parent.id) and entry["target"] == parent.short_id


def test_a_task_that_was_not_given_the_power_is_refused_and_the_refusal_journals(
    home: Path, tmp_path: Path, monkeypatch
):
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "an ordinary task", "fake")
    as_task(monkeypatch, parent)

    r = runner.invoke(app, ["task", "add", slug, "work it found"])
    assert r.exit_code == 1
    assert "spawn refused" in r.output and "--allow-spawn" in r.output
    assert len(store.list()) == 1  # nothing was queued

    entry = manager_journal(home)[-1]
    assert entry["action"] == "task.add.refused" and entry["target"] == parent.short_id


def test_a_retried_refusal_is_recorded_once_not_once_per_attempt(
    home: Path, tmp_path: Path, monkeypatch
):
    """The journal is read back as a bounded tail: a harness that retries a
    refused spawn must not push the manager's own actions out of it."""
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "an ordinary task", "fake")
    as_task(monkeypatch, parent)

    for _ in range(3):
        assert runner.invoke(app, ["task", "add", slug, "work it found"]).exit_code == 1
    assert [e["action"] for e in manager_journal(home)] == ["task.add.refused"]


def test_a_refusal_after_a_successful_spawn_is_recorded_again(
    home: Path, tmp_path: Path, monkeypatch
):
    """Deduping is per streak, not forever: a task that queued something and
    was then refused has a new fact to report."""
    slug = setup(home, tmp_path, tasks_extra="max_spawn_per_task = 1\n")
    store = TaskStore(home)
    parent = store.add(slug, "prolific", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)

    assert runner.invoke(app, ["task", "add", slug, "the one it gets"]).exit_code == 0
    assert runner.invoke(app, ["task", "add", slug, "one too many"]).exit_code == 1
    assert [e["action"] for e in manager_journal(home)] == ["task.add", "task.add.refused"]


def test_the_cap_refuses_the_next_child_not_the_ones_already_queued(
    home: Path, tmp_path: Path, monkeypatch
):
    slug = setup(home, tmp_path, tasks_extra="max_spawn_per_task = 2\n")
    store = TaskStore(home)
    parent = store.add(slug, "prolific", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)

    for i in range(2):
        assert runner.invoke(app, ["task", "add", slug, f"follow-up {i}"]).exit_code == 0
    r = runner.invoke(app, ["task", "add", slug, "one too many"])
    assert r.exit_code == 1 and "max_spawn_per_task" in r.output
    assert len(spawn_children(parent.id, store.list())) == 2


def test_depth_stops_the_chain_one_level_down(home: Path, tmp_path: Path, monkeypatch):
    """The default `max_spawn_depth = 1`: a task a person queued may spawn, and
    what it spawns may not — even when it was given `--allow-spawn`."""
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)
    assert runner.invoke(app, ["task", "add", slug, "a child", "--allow-spawn"]).exit_code == 0

    child = [t for t in store.list() if t.id != parent.id][0]
    as_task(monkeypatch, child)
    r = runner.invoke(app, ["task", "add", slug, "a grandchild"])
    assert r.exit_code == 1 and "max_spawn_depth" in r.output
    assert len(store.list()) == 2


def test_a_deeper_chain_is_allowed_when_the_home_says_so(
    home: Path, tmp_path: Path, monkeypatch
):
    slug = setup(home, tmp_path, tasks_extra="max_spawn_depth = 2\n")
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)
    assert runner.invoke(app, ["task", "add", slug, "a child", "--allow-spawn"]).exit_code == 0

    child = [t for t in store.list() if t.id != parent.id][0]
    as_task(monkeypatch, child)
    assert runner.invoke(app, ["task", "add", slug, "a grandchild"]).exit_code == 0
    grandchild = [t for t in store.list() if t.parent == child.id][0]
    assert spawn_depth(grandchild, {t.id: t for t in store.list()}) == 2


def test_an_actor_tag_naming_no_task_is_refused(home: Path, tmp_path: Path, monkeypatch):
    """A tag quorum cannot attribute must not queue an unparented task — it
    would read as user-created."""
    slug = setup(home, tmp_path)
    monkeypatch.setenv("QUORUM_ACTOR", "task-01GONEGONEGONEGONEGONEGONE")

    r = runner.invoke(app, ["task", "add", slug, "from nowhere"])
    assert r.exit_code == 1 and "names no task record" in r.output
    assert TaskStore(home).list() == []


def test_after_self_chains_the_new_work_behind_the_caller(
    home: Path, tmp_path: Path, monkeypatch
):
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "the work", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)

    r = runner.invoke(app, ["task", "add", slug, "benchmark it afterwards", "--after", "self"])
    assert r.exit_code == 0, r.output
    child = [t for t in store.list() if t.id != parent.id][0]
    assert child.depends_on == [parent.id]
    assert tasks.dependency_state(child, {t.id: t for t in store.list()})["waiting_on"] == [
        parent.short_id
    ]


def test_after_self_outside_a_task_run_is_an_error(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path)
    r = runner.invoke(app, ["task", "add", slug, "after what?", "--after", "self"])
    assert r.exit_code == 1 and "only means something inside a task run" in r.output
    assert TaskStore(home).list() == []


def test_cancelling_a_parent_leaves_its_children_alone(home: Path, tmp_path: Path, monkeypatch):
    """No cascade: a child is an ordinary queued task the moment it exists,
    and what happens to its parent is the manager's judgement, not quorum's."""
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "the work", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)
    assert runner.invoke(app, ["task", "add", slug, "independent follow-up"]).exit_code == 0
    assert runner.invoke(app, ["task", "add", slug, "post-task", "--after", "self"]).exit_code == 0
    monkeypatch.delenv("QUORUM_ACTOR")

    assert runner.invoke(app, ["task", "cancel", parent.short_id, "--yes"]).exit_code == 0

    children = spawn_children(parent.id, store.list())
    assert len(children) == 2
    assert [c.status for c in children] == ["queued", "queued"]
    assert store.get(parent.id).status == "cancelled"
    # the post-task's dependency is now unsatisfiable, which is reported as
    # exactly that — nothing waits on it, and nothing cancelled it
    post = [c for c in children if c.depends_on][0]
    state = tasks.dependency_state(post, {t.id: t for t in store.list()})
    assert state["waiting_on"] == [] and state["failed"] == [parent.short_id]


# -- the run prompt -----------------------------------------------------------


def test_only_a_spawn_enabled_run_is_told_how_to_create_work(home: Path, project: str):
    harness_config(home, tasks_extra="max_spawn_per_task = 3\n")
    config = load_config(home)
    store = TaskStore(home)
    trusted = store.add(project, "grow the queue", "fake", allow_spawn=True)
    ordinary = store.add(project, "just do the work", "fake")

    assert run_task(home, config, trusted.id) == 0
    assert run_task(home, config, ordinary.id) == 0

    grows = transcript_text(home, trusted.id)
    assert "Creating follow-up work" in grows
    assert "--after self" in grows
    assert "at most 3 of them" in grows  # the cap, so a refusal is not the first news
    assert "PROMPT| {spawn}" not in grows

    once = transcript_text(home, ordinary.id)
    assert "Creating follow-up work" not in once and "PROMPT| {spawn}" not in once


def test_a_spawn_run_survives_an_edited_preamble_without_the_placeholder(
    home: Path, project: str
):
    """A home that customized task-preamble.md before `{spawn}` existed never
    substitutes it — and a task allowed to create work would never be told."""
    from quorum import prompts

    harness_config(home)
    edited = prompts.load(home, "task-preamble").replace("\n{spawn}\n", "\n")
    assert "\n{spawn}\n" not in edited and "{{spawn}}" in edited
    (home / "prompts" / "task-preamble.md").write_text(edited)

    trusted = TaskStore(home).add(project, "grow the queue", "fake", allow_spawn=True)
    assert run_task(home, load_config(home), trusted.id) == 0
    assert "Creating follow-up work" in transcript_text(home, trusted.id)


def test_a_run_really_can_queue_work_and_an_untrusted_one_really_cannot(
    home: Path, tmp_path: Path
):
    """End to end through the runner: the harness calls `quorum task add`
    under the actor tag the runner set, exactly as a real one would."""
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    # the harness table's own env pins the mode, the way every fake-harness
    # test does, plus the project the spawned tasks go to
    harness_config(
        home,
        extra='env = { FAKE_HARNESS_MODE = "task_spawn", '
        'FAKE_HARNESS_SPAWN_PROJECT = "proj" }\n',
    )
    config = load_config(home)
    store = TaskStore(home)

    trusted = store.add("proj", "find more work", "fake", allow_spawn=True)
    assert run_task(home, config, trusted.id) == 0
    children = spawn_children(trusted.id, store.list())
    assert len(children) == 2
    assert [c.depends_on for c in children] == [[], [trusted.id]]
    assert "ACT| task add  -> exit 0" in transcript_text(home, trusted.id)

    ordinary = store.add("proj", "stay in your lane", "fake")
    assert run_task(home, config, ordinary.id) == 0
    out = transcript_text(home, ordinary.id)
    assert spawn_children(ordinary.id, store.list()) == []
    assert "REFUSED| spawn refused" in out


# -- what the surfaces show ---------------------------------------------------


def test_views_render_the_link_the_badge_and_the_cap(home: Path):
    store = TaskStore(home)
    parent = store.add("proj", "root", "fake", allow_spawn=True)
    child = store.add("proj", "kid", "fake", parent=parent.id)
    config = load_config(home) if (home / "config.toml").exists() else None
    rows = {r["id"]: r for r in views.task_rows(home, config)}

    assert "⇗" in views.task_badges(rows[parent.id])
    assert "⇗" not in views.task_badges(rows[child.id])
    assert f"parent {parent.short_id}" in views.task_flags(rows[child.id])
    assert rows[parent.id]["spawned"] == [child.short_id]
    assert "SPAWN-CAP" not in views.task_flags(rows[parent.id])


def test_the_cap_flag_appears_once_the_share_is_used(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path, tasks_extra="max_spawn_per_task = 1\n")
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    store.add(slug, "kid", "fake", parent=parent.id)
    rows = {r["id"]: r for r in views.task_rows(home, load_config(home))}
    assert "SPAWN-CAP" in views.task_flags(rows[parent.id])


def test_task_list_and_show_carry_the_family(home: Path, tmp_path: Path, monkeypatch):
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    as_task(monkeypatch, parent)
    assert runner.invoke(app, ["task", "add", slug, "the follow-up"]).exit_code == 0
    monkeypatch.delenv("QUORUM_ACTOR")
    child = spawn_children(parent.id, store.list())[0]

    listed = runner.invoke(app, ["task", "list"])
    assert listed.exit_code == 0, listed.output
    assert f"parent {parent.short_id}" in listed.output and "⇗" in listed.output

    shown = runner.invoke(app, ["task", "show", parent.short_id])
    assert f"spawned:  {child.short_id}" in shown.output
    assert "spawn:    allowed — 1/5 used" in shown.output
    assert f"parent:   {parent.short_id}" in runner.invoke(
        app, ["task", "show", child.short_id]
    ).output

    legend = runner.invoke(app, ["status", "--legend"])
    assert "⇗" in legend.output and "SPAWN-CAP" in legend.output


# -- the digest ---------------------------------------------------------------


def digest(home: Path, now=None) -> str:
    return manager.build_digest(
        home,
        TaskStore(home).list(),
        now or fsio.utc_now(),
        [],
        tasks_config=load_config(home).tasks,
    )


def test_the_digest_marks_the_link_both_ways(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path)
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    child = store.add(slug, "kid", "fake", parent=parent.id)

    text = digest(home)
    parent_line = [ln for ln in text.splitlines() if parent.short_id in ln][0]
    child_line = [ln for ln in text.splitlines() if child.short_id in ln and "parent=" in ln][0]
    assert f"spawned={child.short_id}" in parent_line
    assert f"parent={parent.short_id}" in child_line
    assert "SPAWN-CAP" not in text


def test_the_digest_flags_a_parent_that_wanted_more_than_its_share(home: Path, tmp_path: Path):
    slug = setup(home, tmp_path, tasks_extra="max_spawn_per_task = 1\n")
    store = TaskStore(home)
    parent = store.add(slug, "root", "fake", allow_spawn=True)
    store.add(slug, "kid", "fake", parent=parent.id)

    text = digest(home)
    assert "SPAWN-CAP" in text
    assert "a rate limit, not a verdict" in text
    assert "read them and decide" in text  # what to do about it stays a judgement


def test_the_manager_prompt_teaches_the_marks_it_will_see():
    """The digest's vocabulary and the prompt's have to match: a mark the
    prompt never mentions is a mark the manager ignores."""
    from importlib import resources

    text = (resources.files("quorum") / "default_prompts" / "manager.md").read_text()
    rule = manager_rule(text, "parent=")
    for mark in ("spawned=", "SPAWN-CAP", "--allow-spawn"):
        assert mark in rule
    assert "Cancelling a parent never cancels its children" in rule


def manager_rule(text: str, needle: str) -> str:
    """The numbered rule containing `needle`, by list-item boundary (the idiom
    `test_manager.rule_mentioning` uses — never by rule number)."""
    import re

    items = re.split(r"\n(?=\d+\. )", text)
    found = [i for i in items if needle in i]
    assert len(found) == 1, f"expected exactly one rule mentioning {needle!r}"
    return found[0]
