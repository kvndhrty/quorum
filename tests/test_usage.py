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
        "total_tokens": 2300,
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
