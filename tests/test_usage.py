"""Usage extraction: what a harness said a run spent, normalized.

The shapes here are copied from real harness output — a claude `result`
event (taken from an actual quorum transcript), codex's `turn.completed` and
`token_count` — because the whole module is a bet on those shapes.
"""

from __future__ import annotations

from quorum import usage

CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "total_cost_usd": 2.347236,
    "usage": {
        "input_tokens": 92,
        "cache_creation_input_tokens": 41876,
        "cache_read_input_tokens": 2286191,
        "output_tokens": 19983,
        # claude also nests per-message counts here; descending into them
        # would double-count the run.
        "iterations": [{"input_tokens": 2, "output_tokens": 956}],
    },
    "modelUsage": {"claude-opus-5": {"costUSD": 2.3}},
}

CODEX_TURN = {
    "type": "turn.completed",
    "usage": {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 300},
}

CODEX_TOKEN_COUNT = {
    "type": "token_count",
    "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}},
}


def test_claude_result_event_is_normalized():
    u = usage.usage_from_event(CLAUDE_RESULT)
    assert u == {
        "cost_usd": 2.347236,
        "input_tokens": 92,
        "output_tokens": 19983,
        "cache_read_tokens": 2286191,
        "cache_creation_tokens": 41876,
        # everything the model processed, cache reads included
        "total_tokens": 92 + 19983 + 2286191 + 41876,
    }
    assert usage.describe(u) == "$2.35 · 2.3M tok"


def test_codex_shapes_are_normalized_too():
    assert usage.usage_from_event(CODEX_TURN) == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 800,
        # codex's cached_input_tokens is a subset of input_tokens (its own
        # token_count totals exclude it), so the fallback sum must too
        "total_tokens": 1500,
    }
    # a harness-reported total wins over quorum's sum
    assert usage.usage_from_event(CODEX_TOKEN_COUNT)["total_tokens"] == 13


def test_events_that_say_nothing_about_spend_are_none():
    for event in (
        None,
        "a plain line",
        {"type": "assistant", "message": {"text": "hi"}},
        {"type": "result", "subtype": "success"},  # result with no numbers
        {"type": "result", "total_cost_usd": "free"},  # wrong type
        {"type": "result", "total_cost_usd": -1},  # nonsense
        {"type": "result", "usage": {"input_tokens": True}},  # bools are not counts
    ):
        assert usage.usage_from_event(event) is None


def test_per_message_usage_does_not_count_as_a_run_report():
    """claude tags every assistant message with its own usage block; only
    result-shaped events (or anything carrying a cost) are spend reports."""
    assert usage.usage_from_event({"type": "assistant", "usage": {"input_tokens": 5}}) is None
    assert usage.usage_from_event({"type": "custom", "cost_usd": 0.5}) == {"cost_usd": 0.5}


def test_collector_takes_the_max_across_a_runs_events():
    """Harnesses that report usage report run-cumulative totals, and a pumped
    multi-turn run emits one result per turn — so the reduction is max, which
    equals the last report for a cumulative reporter and under-counts (never
    over-counts) a per-turn one."""
    collector = usage.UsageCollector()
    collector.add({"type": "assistant"})  # ignored
    collector.add({"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 100}})
    collector.add({"type": "result", "total_cost_usd": 1.25, "usage": {"input_tokens": 400}})
    assert collector.result() == {
        "cost_usd": 1.25,
        "input_tokens": 400,
        "total_tokens": 400,
        "events": 2,
    }


def test_collector_reports_none_when_the_harness_said_nothing():
    collector = usage.UsageCollector()
    for event in ({"type": "system"}, {"type": "assistant"}, "plain text"):
        collector.add(event)
    assert collector.result() is None


def test_readers_degrade_on_malformed_usage_from_disk():
    """task.json is plain files a human can hand-edit; a garbage value must
    read as "says nothing", never raise out of a view."""
    mangled = {"cost_usd": "lots", "total_tokens": [1, 2], "input_tokens": 5}
    assert usage.describe(mangled) == ""
    assert usage.overages(mangled, max_cost=0.01, max_tokens=1) == []
    assert usage.total([mangled, {"cost_usd": 1.0}, "not-a-dict"]) == {
        "cost_usd": 1.0,
        "input_tokens": 5,
        "runs": 2,
    }


def test_an_explicit_zero_cost_is_omitted_not_rendered():
    assert usage.describe({"cost_usd": 0.0, "total_tokens": 500}) == "500 tok"


def test_task_total_sums_runs_and_drops_event_multiplicity():
    run = {"cost_usd": 1.5, "total_tokens": 1000, "input_tokens": 1000, "events": 3}
    assert usage.total([run, None, run]) == {
        "cost_usd": 3.0,
        "total_tokens": 2000,
        "input_tokens": 2000,
        "runs": 2,
    }
    assert usage.total([]) is None
    assert usage.total([None, None]) is None


def test_formatting_stays_readable_at_every_scale():
    assert usage.format_tokens(950) == "950"
    assert usage.format_tokens(11_000) == "11.0k"
    assert usage.format_tokens(2_300_000) == "2.3M"
    assert usage.format_cost(0.0004) == "$0.0004"  # sub-cent runs still say something
    assert usage.format_cost(12.5) == "$12.50"
    assert usage.describe(None) == ""
    assert usage.describe({"total_tokens": 500}) == "500 tok"


def test_overages_report_both_limits_and_stay_silent_when_off():
    spent = {"cost_usd": 2.0, "total_tokens": 11_000}
    assert usage.overages(spent) == []  # 0 = no budget
    assert usage.overages(spent, max_cost=5.0, max_tokens=20_000) == []
    over = usage.overages(spent, max_cost=1.0, max_tokens=1_000)
    assert over == [
        "cost $2.00 > max_cost_per_run $1.00",
        "tokens 11.0k > max_tokens_per_run 1.0k",
    ]
    # a run that reported nothing can never be over budget
    assert usage.overages(None, max_cost=0.01, max_tokens=1) == []


def test_run_overages_names_the_run():
    class Run:
        def __init__(self, usage):
            self.usage = usage

    runs = [Run({"cost_usd": 0.1}), Run(None), Run({"cost_usd": 9.0})]
    assert usage.run_overages(runs, max_cost=1.0) == ["run 3: cost $9.00 > max_cost_per_run $1.00"]


def test_last_run_overages_reads_only_the_last_run():
    """The budget gate's condition: the last run and nothing else. An earlier
    overage is history; a cheaper or silent later run clears it; no runs and
    no budget never gate."""

    class Run:
        def __init__(self, usage):
            self.usage = usage

    over, under, silent = Run({"cost_usd": 9.0}), Run({"cost_usd": 0.1}), Run(None)
    assert usage.last_run_overages([under, over], max_cost=1.0) == [
        "cost $9.00 > max_cost_per_run $1.00"
    ]
    assert usage.last_run_overages([over, under], max_cost=1.0) == []  # cleared
    assert usage.last_run_overages([over, silent], max_cost=1.0) == []  # silence is not spend
    assert usage.last_run_overages([], max_cost=1.0) == []
    assert usage.last_run_overages([over]) == []  # 0 = off
    assert usage.last_run_overages([Run({"total_tokens": 5_000})], max_tokens=1_000) == [
        "tokens 5.0k > max_tokens_per_run 1.0k"
    ]
# -- the ledger as a run record, not only a spend record (#59) --------------


def test_run_outcomes_and_durations_read_back_off_the_ledger(tmp_path):
    from pathlib import Path

    from quorum import fsio
    from quorum.actor import usage_path

    home = Path(tmp_path)
    path = usage_path(home, "manager")
    path.parent.mkdir(parents=True, exist_ok=True)
    usage.record_agent_run(home, "manager", "r1", {"cost_usd": 0.5}, outcome="ok",
                           duration_seconds=130.4)
    usage.record_agent_run(home, "manager", "r2", None, outcome="timeout",
                           duration_seconds=900)
    usage.record_agent_run(home, "manager", "r3", None, outcome="raised", duration_seconds=3.2)

    written = fsio.read_jsonl(path)
    assert [e["outcome"] for e in written] == ["ok", "timeout", "raised"]
    assert written[0]["duration_seconds"] == 130.4  # rounded, not truncated to int

    runs = usage.agent_runs(home, "manager")
    assert [r["outcome"] for r in runs] == ["ok", "timeout", "raised"]
    assert usage.describe_runs(runs) == "ok 2m10s · TIMEOUT 15m00s · RAISED 0m03s"
    # spend still reads back over the same tail, counting only what reported
    assert usage.agent_usage(home, "manager")["runs"] == 1


def test_a_ledger_line_without_the_new_fields_still_reads(tmp_path):
    """Every home upgrading into #59 has a ledger of lines that predate it —
    they are runs of unknown outcome, never runs that went fine."""
    from pathlib import Path

    from quorum import fsio
    from quorum.actor import usage_path

    home = Path(tmp_path)
    path = usage_path(home, "manager")
    path.parent.mkdir(parents=True, exist_ok=True)
    fsio.append_jsonl(path, {"at": "x", "run": "old", "usage": {"cost_usd": 0.25}})
    fsio.append_jsonl(path, {"at": "y", "run": "half", "usage": None, "outcome": "ok"})
    fsio.append_jsonl(path, {"at": "z", "run": "junk", "usage": None,
                             "outcome": "fine", "duration_seconds": "soon"})
    path.write_text(path.read_text() + '"not even a dict"\n')

    runs = usage.agent_runs(home, "manager")
    assert [r["outcome"] for r in runs] == [None, "ok", None]
    assert [r["duration_seconds"] for r in runs] == [None, None, None]
    assert usage.describe_runs(runs) == "? · ok · ?"
    assert usage.agent_usage(home, "manager")["total"]["cost_usd"] == 0.25


def test_only_the_last_few_runs_are_described(tmp_path):
    from pathlib import Path

    from quorum.actor import usage_path

    home = Path(tmp_path)
    usage_path(home, "manager").parent.mkdir(parents=True, exist_ok=True)
    for i in range(usage.RECENT_RUNS + 4):
        usage.record_agent_run(home, "manager", f"r{i}", None, outcome="ok", duration_seconds=i)

    runs = usage.agent_runs(home, "manager")
    assert len(runs) == usage.RECENT_RUNS
    assert runs[-1]["run"] == f"r{usage.RECENT_RUNS + 3}"  # newest last
    assert usage.describe_runs([]) == ""
    # a caller asking for no runs gets none — `entries[-0:]` would be all of them
    assert usage.agent_runs(home, "manager", limit=0) == []


def test_durations_stay_legible_past_an_hour():
    assert usage.format_duration(0) == "0m00s"
    assert usage.format_duration(52.9) == "0m52s"
    assert usage.format_duration(900) == "15m00s"
    assert usage.format_duration(3600 + 125) == "1h02m"
