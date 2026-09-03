"""`quorum usage`: aggregate spend and delivery read off a synthetic home.

Two harnesses with known figures — `claude` reports cost and tokens, `codex`
tokens only — plus a task that reported nothing, so every rule the report
makes (count, never estimate; empty cost cell; share merged over observed
PRs only) has a row that exercises it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import stats, tasks, usage
from quorum.cli import app
from quorum.tasks import TaskRun, TaskStore

runner = CliRunner()

T0 = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)  # a Monday: ISO week 2026-W36


def _run(started: datetime, minutes: int, spent: dict | None) -> dict:
    return TaskRun(
        started_at=tasks.fsio.iso(started),
        ended_at=tasks.fsio.iso(started + timedelta(minutes=minutes)),
        exit_code=0,
        usage=spent,
    ).model_dump()


CLAUDE_RUN = {"cost_usd": 1.5, "input_tokens": 100, "output_tokens": 400, "total_tokens": 500}
CODEX_RUN = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}


def build_home(home: Path) -> dict[str, str]:
    """Five tasks over two projects, two harnesses and two weeks.

    alpha/claude  `a1`: 2 runs (1 rerun), $3.00 · 1000 tok, done at +1h,
                        merged seen at +1h30m
    alpha/claude  `a2`: 1 run, done at +2h, PR observed open (never merged)
    alpha/codex   `c1`: 1 run, tokens only, done at +30m; merge seen at
                        +20m — before done — so done→merged reads 0
    beta/codex    `c2`: no runs, no usage, still queued; nothing observed
    beta/claude   `w2`: queued the next week, 1 run, done, hand-edited
                        status (no done report), so no queue→done figure
    """
    store = TaskStore(home)
    ids = {}

    a1 = store.add("alpha", "a1", "claude", now=T0)
    store.update(
        a1.id,
        runs=[_run(T0 + timedelta(minutes=10), 20, CLAUDE_RUN), _run(T0 + timedelta(minutes=40), 15, CLAUDE_RUN)],
    )
    tasks.report(home, a1.id, "done", "shipped", now=T0 + timedelta(hours=1))
    tasks.record_pr_state(home, store.get(a1.id), "merged", now=T0 + timedelta(hours=1, minutes=30))
    ids["a1"] = a1.id

    a2 = store.add("alpha", "a2", "claude", now=T0 + timedelta(minutes=5))
    store.update(a2.id, runs=[_run(T0 + timedelta(minutes=35), 30, CLAUDE_RUN)])
    tasks.report(home, a2.id, "done", "shipped", now=T0 + timedelta(hours=2, minutes=5))
    tasks.record_pr_state(home, store.get(a2.id), "open", now=T0 + timedelta(hours=3))
    ids["a2"] = a2.id

    c1 = store.add("alpha", "c1", "codex", now=T0)
    store.update(c1.id, runs=[_run(T0 + timedelta(minutes=5), 10, CODEX_RUN)])
    tasks.report(home, c1.id, "done", "shipped", now=T0 + timedelta(minutes=30))
    tasks.record_pr_state(home, store.get(c1.id), "merged", now=T0 + timedelta(minutes=20))
    ids["c1"] = c1.id

    c2 = store.add("beta", "c2", "codex", now=T0 + timedelta(hours=1))
    ids["c2"] = c2.id

    w2 = store.add("beta", "w2", "claude", now=T0 + timedelta(days=7))
    store.update(w2.id, runs=[_run(T0 + timedelta(days=7, minutes=3), 5, CLAUDE_RUN)], status="done")
    ids["w2"] = w2.id
    return ids


def rows_by_key(payload: dict) -> dict[str, dict]:
    return {r["key"]: r for r in payload["rows"]}


def test_parse_since_accepts_units_and_rejects_the_rest():
    assert stats.parse_since("7d") == timedelta(days=7)
    assert stats.parse_since(" 36h ") == timedelta(hours=36)
    assert stats.parse_since("2w") == timedelta(weeks=2)
    assert stats.parse_since("90m") == timedelta(minutes=90)
    for bad in ("", "7", "d", "0d", "-1d", "3x", "1.5d", "7 days"):
        with pytest.raises(ValueError, match="--since wants"):
            stats.parse_since(bad)


def test_by_project_counts_every_task_and_sums_only_what_was_reported(home: Path):
    build_home(home)
    payload = stats.report(home, by="project", now=lambda: T0 + timedelta(days=8))
    rows = rows_by_key(payload)
    assert list(rows) == ["alpha", "beta"]

    alpha = rows["alpha"]
    assert (alpha["tasks"], alpha["tasks_with_usage"], alpha["runs"], alpha["reruns"]) == (3, 3, 4, 1)
    # usage.total's reduction: a sum across the four runs, `runs` = runs that reported
    assert alpha["usage"]["cost_usd"] == pytest.approx(4.5)
    assert alpha["usage"]["total_tokens"] == 500 * 3 + 1200
    assert alpha["usage"]["runs"] == 4
    assert (alpha["done"], alpha["observed"], alpha["merged"]) == (3, 3, 2)
    assert alpha["share_merged"] == pytest.approx(2 / 3)
    # medians over the tasks that have the figure: a1 10m, a2 30m, c1 5m
    assert alpha["queue_to_run"] == {"n": 3, "median_seconds": 600.0, "mean_seconds": 900.0}
    assert alpha["queue_to_done"]["n"] == 3 and alpha["queue_to_done"]["median_seconds"] == 3600.0
    # a1 30m; c1's merge was seen before done and is a zero wait, never negative
    assert alpha["done_to_merged"] == {"n": 2, "median_seconds": 900.0, "mean_seconds": 900.0}

    beta = rows["beta"]
    # c2 reported nothing and never ran: counted, not estimated
    assert (beta["tasks"], beta["tasks_with_usage"], beta["runs"], beta["reruns"]) == (2, 1, 1, 0)
    assert beta["usage"]["cost_usd"] == pytest.approx(1.5) and beta["usage"]["runs"] == 1
    assert beta["observed"] == 0 and beta["merged"] == 0 and beta["share_merged"] is None
    # w2 is done by a hand-edited status with no `done` report: done, no figure
    assert beta["done"] == 1 and beta["queue_to_done"] is None and beta["done_to_merged"] is None
    assert beta["queue_to_run"]["n"] == 1

    total = payload["total"]
    assert total["key"] == "total" and total["tasks"] == 5 and total["runs"] == 5
    assert total["usage"]["cost_usd"] == pytest.approx(6.0)
    assert payload["since_seconds"] is None and payload["cutoff"] is None


def test_by_harness_gives_the_tokens_only_harness_tokens_and_no_cost(home: Path):
    build_home(home)
    rows = rows_by_key(stats.report(home, by="harness"))
    assert list(rows) == ["claude", "codex"]
    assert rows["claude"]["usage"]["cost_usd"] == pytest.approx(6.0)
    assert "cost_usd" not in rows["codex"]["usage"]
    assert rows["codex"]["usage"]["total_tokens"] == 1200
    assert (rows["codex"]["tasks"], rows["codex"]["tasks_with_usage"]) == (2, 1)


def test_by_week_dates_a_task_by_when_it_was_queued(home: Path):
    build_home(home)
    rows = rows_by_key(stats.report(home, by="week"))
    assert list(rows) == ["2026-W36", "2026-W37"]
    assert rows["2026-W36"]["tasks"] == 4 and rows["2026-W37"]["tasks"] == 1


def test_since_keeps_the_tasks_queued_in_the_window(home: Path):
    build_home(home)
    now = T0 + timedelta(days=7, hours=1)
    payload = stats.report(home, by="project", since=timedelta(days=2), now=now)
    assert payload["cutoff"] == "2026-09-05T10:00:00Z"
    assert payload["since_seconds"] == 2 * 86400
    assert [(r["key"], r["tasks"]) for r in payload["rows"]] == [("beta", 1)]
    # one row: the total equals it rather than being None
    assert payload["total"]["tasks"] == 1
    # an empty window is an empty report, not an error
    empty = stats.report(home, by="project", since=timedelta(minutes=1), now=now + timedelta(days=30))
    assert empty["rows"] == [] and empty["total"] is None


def test_done_at_reads_the_last_done_report_of_a_task_still_done(home: Path):
    store = TaskStore(home)
    t = store.add("p", "x", "claude", now=T0)
    assert stats.done_at(home, store.get(t.id)) is None  # queued
    tasks.report(home, t.id, "done", now=T0 + timedelta(minutes=10))
    tasks.report(home, t.id, "executing", "relaunched", now=T0 + timedelta(minutes=20))
    # said done once, then went back to work: not delivered
    assert stats.done_at(home, store.get(t.id)) is None
    tasks.report(home, t.id, "done", now=T0 + timedelta(minutes=50))
    assert stats.done_at(home, store.get(t.id)) == T0 + timedelta(minutes=50)
    # a torn timestamp on the report says nothing rather than raising
    tasks.fsio.append_jsonl(tasks.reports_path(home, t.id), {"at": "not a time", "status": "done"})
    assert stats.done_at(home, store.get(t.id)) is None


def test_unreadable_timestamps_and_records_degrade_to_no_figure(home: Path):
    store = TaskStore(home)
    t = store.add("p", "x", "claude", now=T0)
    data = json.loads(tasks.task_json_path(home, t.id).read_text())
    data["created_at"] = "garbage"
    data["runs"] = [{"started_at": "also garbage", "usage": {"cost_usd": 2.0}}]
    tasks.task_json_path(home, t.id).write_text(json.dumps(data))
    facts = stats.task_facts(home, store.get(t.id))
    assert facts["week"] == "unknown" and facts["queue_to_run"] is None
    assert facts["runs"] == 1  # counted all the same
    payload = stats.report(home, by="week", since=timedelta(days=1), now=T0)
    assert payload["rows"] == []  # no readable queue time: outside every window
    # a task.json that does not parse is skipped by the store, not the report
    (home / "tasks" / "01BROKEN000000000000000000" / "task.json").parent.mkdir()
    (home / "tasks" / "01BROKEN000000000000000000" / "task.json").write_text("{")
    assert stats.report(home, by="week")["total"]["tasks"] == 1


def test_by_agent_reads_every_ledger_line_and_its_outcome(home: Path):
    at = T0
    for i, (spent, outcome, secs) in enumerate(
        [
            (CLAUDE_RUN, "ok", 10.0),
            (CLAUDE_RUN, "ok", 30.0),
            (None, "timeout", 900.0),
            (CLAUDE_RUN, "raised", 20.0),
        ]
    ):
        usage.record_agent_run(
            home, "manager", f"run{i}", spent, now=at + timedelta(hours=i), outcome=outcome,
            duration_seconds=secs,
        )
    # a prompt agent that has since been removed from config keeps its ledger
    # (the writer fails soft, so the agent's state dir has to exist first)
    usage.actor.usage_path(home, "babysitter").parent.mkdir(parents=True)
    usage.record_agent_run(home, "babysitter", "b1", CODEX_RUN, now=at)
    # a line from before outcomes existed, and a torn one
    tasks.fsio.append_jsonl(usage.actor.usage_path(home, "babysitter"), {"at": tasks.fsio.iso(at), "run": "b0", "usage": None})
    with open(usage.actor.usage_path(home, "babysitter"), "a") as f:
        f.write('{"at": "tor')

    payload = stats.report(home, by="agent")
    rows = rows_by_key(payload)
    assert list(rows) == ["manager", "babysitter"]
    m = rows["manager"]
    assert (m["runs"], m["runs_with_usage"]) == (4, 3)
    assert m["outcomes"] == {"ok": 2, "raised": 1, "timeout": 1, "unknown": 0}
    assert m["usage"]["cost_usd"] == pytest.approx(4.5)
    assert m["duration"] == {"n": 4, "median_seconds": 25.0, "mean_seconds": 240.0}
    b = rows["babysitter"]
    assert (b["runs"], b["runs_with_usage"]) == (2, 1)
    assert b["outcomes"]["unknown"] == 2 and "cost_usd" not in b["usage"] and b["duration"] is None
    assert payload["total"]["runs"] == 6

    # --since filters on the line's own `at`
    later = stats.report(home, by="agent", since=timedelta(hours=1, minutes=30), now=at + timedelta(hours=3))
    assert [(r["key"], r["runs"]) for r in later["rows"]] == [("manager", 2)]
    # no ledgers at all
    for path in stats.agent_ledgers(home).values():
        path.unlink()
    assert stats.report(home, by="agent") == {
        "by": "agent", "since_seconds": None, "cutoff": None, "rows": [], "total": None,
    }


def test_report_refuses_an_unknown_dimension(home: Path):
    with pytest.raises(ValueError, match="--by wants one of"):
        stats.report(home, by="model")
    with pytest.raises(ValueError, match="--by wants one of"):
        stats.task_rows([], by="agent")


def test_format_span_and_summary_cells():
    assert stats.format_span(59) == "0m"
    assert stats.format_span(600) == "10m"
    assert stats.format_span(3600 * 3 + 60 * 12) == "3h12m"
    assert stats.format_span(86400 * 2 + 3600 * 4) == "2d04h"
    assert stats.describe_summary(None) == ""
    assert stats.describe_summary({"n": 1, "median_seconds": 1800.0, "mean_seconds": 1800.0}) == "30m"


# -- the CLI ---------------------------------------------------------------


def test_cli_table_is_plain_when_piped_and_drops_what_nothing_fills(home: Path):
    build_home(home)
    r = runner.invoke(app, ["usage", "--by", "harness", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "\x1b[" not in r.output
    lines = r.output.split("\n")
    assert lines[0] == "usage by harness, all time"
    header = lines[1].split()
    assert header == [
        "harness", "tasks", "reported", "runs", "reruns", "cost", "tokens", "done", "merged",
        "queue→run", "queue→done", "done→merged",
    ]
    claude = next(line for line in lines if line.startswith("claude "))
    codex = next(line for line in lines if line.startswith("codex "))
    total = next(line for line in lines if line.startswith("total "))
    # four claude runs at 500 tokens; queue→done is the median of a1 (1h) and a2 (2h)
    assert claude.split() == ["claude", "3", "4", "1", "$6.00", "2.0k", "3", "1/2", "(50%)", "10m", "1h30m", "30m"]
    # tokens but no cost: an empty cost cell, and the share over observed PRs only
    assert codex.split() == ["codex", "2", "1/2", "1", "1.2k", "1", "1/1", "(100%)", "5m", "30m", "0m"]
    assert total.split()[:7] == ["total", "5", "4/5", "5", "1", "$6.00", "3.2k"]
    assert "medians" in r.output and "harness's own figures" in r.output

    # no observation anywhere: the delivery columns are gone, not zero
    store = TaskStore(home)
    for t in store.list():
        data = json.loads(tasks.task_json_path(home, t.id).read_text())
        data.update(pr_state=None, pr_state_at=None)
        tasks.task_json_path(home, t.id).write_text(json.dumps(data))
    r = runner.invoke(app, ["usage", "--by", "project", "--home", str(home)])
    assert r.exit_code == 0, r.output
    header = r.output.split("\n")[1].split()
    assert "merged" not in header and "done→merged" not in header and "queue→done" in header


def test_cli_by_agent_and_since_and_json(home: Path):
    usage.record_agent_run(home, "manager", "r1", CLAUDE_RUN, now=T0, outcome="ok", duration_seconds=12)
    usage.record_agent_run(home, "manager", "r2", None, now=T0 + timedelta(hours=1), outcome="timeout", duration_seconds=900)
    r = runner.invoke(app, ["usage", "--by", "agent", "--home", str(home)])
    assert r.exit_code == 0, r.output
    lines = r.output.split("\n")
    assert lines[1].split() == ["agent", "runs", "reported", "timeout", "cost", "tokens", "duration"]
    assert lines[2].split() == ["manager", "2", "1/2", "1", "$1.50", "500", "7m36s"]

    r = runner.invoke(app, ["usage", "--by", "agent", "--since", "30m", "--json", "--home", str(home)])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["by"] == "agent" and payload["since_seconds"] == 1800
    assert payload["rows"] == [] and payload["total"] is None  # T0 is years before now

    build_home(home)
    r = runner.invoke(app, ["usage", "--since", "1w", "--json", "--home", str(home)])
    payload = json.loads(r.output)
    assert payload["by"] == "project" and payload["cutoff"] is not None
    assert all(row["queue_to_run"] is None or "median_seconds" in row["queue_to_run"] for row in payload["rows"])


def test_cli_says_when_nothing_is_recorded(home: Path):
    r = runner.invoke(app, ["usage", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert r.output.split("\n")[1] == "nothing recorded"
    r = runner.invoke(app, ["usage", "--by", "agent", "--since", "1d", "--home", str(home)])
    assert r.exit_code == 0 and "nothing recorded in that window" in r.output


def test_cli_rejects_a_bad_since_and_an_unknown_dimension(home: Path):
    r = runner.invoke(app, ["usage", "--since", "3x", "--home", str(home)])
    assert r.exit_code == 1 and "--since wants a positive count" in r.output
    r = runner.invoke(app, ["usage", "--by", "model", "--home", str(home)])
    assert r.exit_code != 0 and "project" in r.output
    r = runner.invoke(app, ["usage", "--by", "WEEK", "--home", str(home)])
    assert r.exit_code == 0 and r.output.startswith("usage by week")
