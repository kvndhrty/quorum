"""`views.agent_interventions` and `quorum agent interventions`: what an
agent's supervision did and what happened next (#97).

A pure reader over an agent's journal, its targets' reports.jsonl and the
board, so every test here writes those files the way the substrate does —
`_actor_guard`'s journal line, `tasks.report`, `MessageBus.post` — and reads
them back. Nothing is recorded for this view's sake, so there is nothing else
to set up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, views
from quorum.actor import journal_path
from quorum.cli import app
from quorum.messages import MessageBus
from quorum.tasks import TaskStore, report

runner = CliRunner()


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def journal(
    home: Path,
    action: str,
    when: datetime,
    *,
    agent: str = "manager",
    target: str | None = None,
    target_status: str | None = None,
    args: str | None = None,
    run: str = "R1",
) -> None:
    """One journal line, in the shape `cli/_common.py::_actor_guard` writes."""
    entry: dict = {"at": fsio.iso(when), "run": run, "actor": agent, "action": action}
    if target:
        entry["target"] = target
    if target_status:
        entry["target_status"] = target_status
    if args:
        entry["args"] = args
    fsio.append_jsonl(journal_path(home, agent), entry)


def a_task(home: Path, prompt: str = "do the thing", when: datetime | None = None):
    return TaskStore(home).add("proj", prompt, "fake", now=when or at(1))


# -- the three outcomes the issue names --------------------------------------


def test_a_nudge_followed_by_a_report(home: Path):
    task = a_task(home)
    journal(
        home, "task.nudge", at(2), target=task.short_id, target_status="executing",
        args="start with the tests",
    )
    report(home, task.id, "reviewing", "ran the tests", now=at(2, 30))
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    assert row["kind"] == "nudge"
    assert row["target"] == task.short_id
    assert row["status_then"] == "executing"
    assert row["status_now"] == "reviewing"
    assert row["next_report"] == {
        "at": fsio.iso(at(2, 30)),
        "status": "reviewing",
        "text": "ran the tests",
    }
    assert row["wait_seconds"] == 1800
    assert payload["summary"]["nudge"] == {"count": 1, "followed_by_report": 1}
    line = views.intervention_line(row)
    assert "nudge -> " + task.short_id in line
    assert "status then executing" in line
    assert "reported reviewing after 30m00s: ran the tests" in line


def test_a_launch_with_no_report(home: Path):
    task = a_task(home)
    report(home, task.id, "executing", "still going", now=at(1, 30))
    journal(home, "task.run", at(2), target=task.short_id, target_status="executing")
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    assert row["kind"] == "launch"
    assert row["next_report"] is None
    assert row["wait_seconds"] is None
    assert row["done_at"] is None
    assert payload["summary"]["launch"] == {"count": 1, "reported_done": 0}
    assert "no report since · status now executing" in views.intervention_line(row)


def test_a_launch_the_task_finished_after(home: Path):
    task = a_task(home)
    journal(home, "task.run", at(2), target=task.short_id, target_status="blocked")
    report(home, task.id, "executing", "retrying", now=at(3))
    report(home, task.id, "done", "opened the PR", now=at(4))
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    # the *next* report is the one right after the action; `done_at` is the
    # later fact the summary counts
    assert row["next_report"]["status"] == "executing"
    assert row["done_at"] == fsio.iso(at(4))
    assert payload["summary"]["launch"] == {"count": 1, "reported_done": 1}


def test_an_escalation_that_was_acked(home: Path):
    bus = MessageBus(home, now=lambda: at(3))
    msg = bus.post("manager", "attention", text="three tasks are blocked on a secret")
    journal(home, "board.post", at(3), args="attention: three tasks are blocked on a secret")
    bus.ack_board_message(msg.short_id, topic="attention")
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    assert row["kind"] == "escalation"
    assert row["topic"] == "attention"
    assert row["message"] == msg.short_id
    assert row["acked"] is True
    assert payload["summary"]["escalation"] == {"count": 1, "acked": 1}
    assert f"acked — archived ({msg.short_id})" in views.intervention_line(row)


def test_an_escalation_still_on_the_board(home: Path):
    bus = MessageBus(home, now=lambda: at(3))
    msg = bus.post("manager", "attention", text="CI has been red for two days")
    journal(home, "board.post", at(3), args="attention: CI has been red for two days")
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    assert row["acked"] is False
    assert payload["summary"]["escalation"] == {"count": 1, "acked": 0}
    assert f"still on the board ({msg.short_id})" in views.intervention_line(row)


def test_an_escalation_whose_post_cannot_be_found(home: Path):
    """The journal line carries no message id, so a post whose text no longer
    matches — or whose archive month has been swept away — reads as unknown
    rather than as unacked."""
    journal(home, "board.post", at(3), args="attention: something that was never posted")
    payload = views.agent_interventions(home, "manager")
    (row,) = payload["rows"]
    assert row["message"] is None
    assert row["acked"] is None
    assert payload["summary"]["escalation"] == {"count": 1, "acked": 0}
    assert "no matching post" in views.intervention_line(row)


# -- what the list does and does not read ------------------------------------


@pytest.mark.parametrize(
    ("action", "args"),
    [
        pytest.param("task.add", "proj: something", id="task.add"),
        pytest.param("task.report", "done", id="task.report"),
        pytest.param("note", "launching nothing this tick", id="note"),
        pytest.param("board.post", "tasks: a note for the feed", id="board.post-other-topic"),
        pytest.param("cap.hit", "refused task.run — action cap (20) reached", id="cap.hit"),
    ],
)
def test_other_journaled_actions_are_not_interventions(home: Path, action: str, args: str):
    task = a_task(home)
    journal(home, action, at(2), target=task.short_id, target_status="queued", args=args)
    assert views.agent_interventions(home, "manager")["rows"] == []


def test_all_four_kinds_with_their_summary_line(home: Path):
    first, second = a_task(home, "one"), a_task(home, "two")
    journal(home, "task.nudge", at(2), target=first.short_id, target_status="executing", args="try the helper")
    journal(home, "task.run", at(3), target=second.short_id, target_status="queued", args="--fresh-session")
    journal(home, "task.stop", at(4), target=second.short_id, target_status="executing")
    journal(home, "board.post", at(5), args="attention: a question only you can answer")
    report(home, first.id, "done", "shipped", now=at(6))
    payload = views.agent_interventions(home, "manager")
    assert [r["kind"] for r in payload["rows"]] == ["nudge", "launch", "stop", "escalation"]
    assert views.intervention_summary_line(payload["summary"]) == (
        "1 nudge, 1 followed by a report · 1 launch, 0 later reported done · "
        "1 stop · 1 escalation, 0 acked"
    )
    # `--fresh-session` is shown as it was journaled; only a nudge's text is quoted
    assert "--fresh-session" in views.intervention_line(payload["rows"][1])
    assert "“try the helper”" in views.intervention_line(payload["rows"][0])


def test_the_summary_line_pluralizes_every_kind(home: Path):
    task = a_task(home)
    for hour in (2, 3):
        journal(home, "task.nudge", at(hour), target=task.short_id, args=f"n{hour}")
        journal(home, "task.run", at(hour), target=task.short_id)
        journal(home, "task.stop", at(hour), target=task.short_id)
        journal(home, "board.post", at(hour), args=f"attention: ask {hour}")
    summary = views.agent_interventions(home, "manager")["summary"]
    assert views.intervention_summary_line(summary) == (
        "2 nudges, 0 followed by a report · 2 launches, 0 later reported done · "
        "2 stops · 2 escalations, 0 acked"
    )


def test_an_empty_journal_reads_as_nothing_yet(home: Path):
    payload = views.agent_interventions(home, "manager")
    assert payload == {
        "agent": "manager",
        "cutoff": None,
        "horizon": None,
        "scrolled": False,
        "rows": [],
        "summary": {
            "nudge": {"count": 0, "followed_by_report": 0},
            "launch": {"count": 0, "reported_done": 0},
            "stop": {"count": 0},
            "escalation": {"count": 0, "acked": 0},
        },
    }
    assert views.intervention_summary_line(payload["summary"]) == "no interventions recorded"


def test_a_target_that_is_no_longer_listed(home: Path):
    journal(home, "task.nudge", at(2), target="gone12", target_status="executing", args="hurry")
    (row,) = views.agent_interventions(home, "manager")["rows"]
    assert row["status_now"] is None
    assert row["next_report"] is None
    assert "no longer listed" in views.intervention_line(row)


def test_an_unparseable_stamp_is_dropped(home: Path):
    task = a_task(home)
    journal(home, "task.nudge", at(2), target=task.short_id, args="good")
    fsio.append_jsonl(
        journal_path(home, "manager"),
        {"at": "the other day", "actor": "manager", "action": "task.run", "target": task.short_id},
    )
    rows = views.agent_interventions(home, "manager")["rows"]
    assert [r["kind"] for r in rows] == ["nudge"]


# -- the window and the horizon ----------------------------------------------


def test_since_keeps_only_what_falls_in_the_window(home: Path):
    task = a_task(home)
    journal(home, "task.nudge", at(2), target=task.short_id, args="old")
    journal(home, "task.nudge", at(10), target=task.short_id, args="recent")
    payload = views.agent_interventions(home, "manager", since=timedelta(hours=3), now=at(12))
    assert [r["args"] for r in payload["rows"]] == ["recent"]
    assert payload["cutoff"] == fsio.iso(at(9))
    # the horizon describes the journal tail, not the window: the older entry
    # is still on disk and still readable
    assert payload["horizon"] == fsio.iso(at(2))
    assert payload["summary"]["nudge"]["count"] == 1


def test_a_scrolled_journal_says_how_far_back_it_can_see(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    task = a_task(home)
    for hour in range(2, 12):
        journal(home, "task.nudge", at(hour), target=task.short_id, args=f"nudge {hour}")
    whole = views.agent_interventions(home, "manager")
    assert whole["scrolled"] is False
    assert whole["horizon"] == fsio.iso(at(2))
    assert len(whole["rows"]) == 10
    # a tail that only reaches the last few lines reports the oldest entry it
    # could read, and says it is not the whole journal
    monkeypatch.setattr(views, "HISTORY_JOURNAL_BYTES", 400)
    tail = views.agent_interventions(home, "manager")
    assert tail["scrolled"] is True
    assert len(tail["rows"]) < 10
    assert tail["horizon"] > whole["horizon"]


# -- the agent variant -------------------------------------------------------


def test_a_prompt_agent_has_its_own_journal(home: Path):
    task = a_task(home)
    journal(
        home, "task.nudge", at(2), agent="babysitter", target=task.short_id,
        target_status="pr", args="CI is red on lint",
    )
    journal(home, "task.run", at(3), target=task.short_id, target_status="pr")
    sitter = views.agent_interventions(home, "babysitter")
    assert [r["kind"] for r in sitter["rows"]] == ["nudge"]
    assert sitter["agent"] == "babysitter"
    assert sitter["rows"][0]["actor"] == "babysitter"
    # the manager's journal is a separate file, so the two never mix
    assert [r["kind"] for r in views.agent_interventions(home, "manager")["rows"]] == ["launch"]


# -- the command -------------------------------------------------------------


def test_cli_prints_the_rows_the_summary_and_the_horizon(home: Path):
    task = a_task(home)
    journal(home, "task.nudge", at(2), target=task.short_id, target_status="executing", args="use the helper")
    report(home, task.id, "reviewing", "done with the helper", now=at(2, 5))
    result = runner.invoke(app, ["agent", "interventions", "manager"])
    assert result.exit_code == 0, result.output
    assert "manager interventions, everything the journal tail holds" in result.output
    assert f"nudge -> {task.short_id}" in result.output
    assert "reported reviewing after 5m00s" in result.output
    assert "1 nudge, 1 followed by a report" in result.output
    assert f"the journal starts at {fsio.iso(at(2))}" in result.output


def test_cli_json_carries_the_rows_and_the_summary(home: Path):
    task = a_task(home)
    journal(home, "task.stop", at(2), target=task.short_id, target_status="executing")
    result = runner.invoke(app, ["agent", "interventions", "manager", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent"] == "manager"
    assert payload["summary"]["stop"] == {"count": 1}
    assert payload["rows"][0]["at_text"] == "2026-01-01 02:00:00"


def test_cli_with_a_window_names_the_cutoff(home: Path):
    task = a_task(home, when=fsio.utc_now())
    recent = fsio.utc_now() - timedelta(minutes=5)
    journal(home, "task.nudge", recent, target=task.short_id, target_status="executing", args="now")
    journal(home, "task.nudge", at(2), target=task.short_id, target_status="executing", args="ages ago")
    result = runner.invoke(app, ["agent", "interventions", "manager", "--since", "1h"])
    assert result.exit_code == 0, result.output
    assert "“now”" in result.output
    assert "ages ago" not in result.output
    assert "1 nudge, 0 followed by a report" in result.output


def test_cli_refuses_a_window_that_is_not_one(home: Path):
    result = runner.invoke(app, ["agent", "interventions", "manager", "--since", "yesterday"])
    assert result.exit_code != 0
    assert "invalid window" in result.output


def test_cli_refuses_a_name_that_is_not_a_path_component(home: Path):
    result = runner.invoke(app, ["agent", "interventions", "../escape"])
    assert result.exit_code == 1
    assert "agent name" in result.output


def test_cli_says_so_when_the_agent_has_journaled_nothing(home: Path):
    result = runner.invoke(app, ["agent", "interventions", "manager"])
    assert result.exit_code == 0, result.output
    assert "no interventions recorded" in result.output
    assert "manager has journaled nothing yet" in result.output
