"""One synchronous harness run on a harness-driven agent's behalf.

The manager and any prompt agent share this exact mechanics: resolve the
agent's harness, tag the env with the actor protocol so the CLI journals and
caps the run's actions, stream stdout to the agent's transcript, and pump
mid-run guidance from the agent's inbox when the harness supports it. Policy
stays in the caller's prompt; nothing here decides anything.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .. import fsio, usage
from ..actor import DEFAULT_MAX_ACTIONS_PER_RUN, actor_env, transcript_path
from ..agent import AgentContext
from ..runner import build_harness_argv, guidance_pump, resolve_harness, stream_transcript

DEFAULT_RUN_TIMEOUT_SECONDS = 300


def resolve_agent_harness(ctx: AgentContext):
    config = ctx.config
    name = ctx.settings.get("harness") or (config.tasks.default_harness if config else "")
    if not config or not name:
        raise RuntimeError(
            f"agent {ctx.name!r} has no usable harness (set [agents.{ctx.name}.settings].harness "
            "or [tasks].default_harness) — its runs are halted until then"
        )
    return resolve_harness(config, name)


def agent_cap(ctx: AgentContext) -> int:
    """The per-run action cap this agent's runs are tagged with (actor.py)."""
    return int(ctx.settings.get("max_actions_per_run", DEFAULT_MAX_ACTIONS_PER_RUN))


def self_observations(home: Path, name: str, cap: int) -> list[str]:
    """What an agent can see about *itself*: spend, recent run outcomes, budget.

    Three lines, all read from files the agent's own runs already wrote (the
    usage ledger) or from the run it is about to make (the cap). Nothing here
    pauses, throttles or changes anything — like `possible-loop` and
    `BUDGET-EXCEEDED`, these are observations the prompt judges.

    The action count is `0` by construction: the header is rendered *before*
    the harness starts, so what it reports is the budget, and what happened to
    it lands in the journal (`cap.hit`) for the next run to read.
    """
    lines = []
    spent = usage.describe_agent(usage.agent_usage(home, name))
    if spent:
        lines.append(f"Your own runs have cost: {spent}")
    runs = usage.agent_runs(home, name)
    recent = usage.describe_runs(runs)
    if recent:
        label = f"Your last {len(runs)} runs" if len(runs) > 1 else "Your last run"
        lines.append(f"{label}: {recent}")
    lines.append(f"Actions this run: 0 of {cap} (cap)")
    return lines


def run_agent_harness(ctx: AgentContext, prompt: str) -> str:
    """Run the agent's configured harness over `prompt`, synchronously,
    cwd = QUORUM_HOME. Raises on timeout or nonzero exit; returns the run id.

    An inject-capable harness can be steered while the run is in flight:
    messages landing in the agent's own inbox are forwarded as user turns by
    the same `GuidancePump` the task runner uses.

    Whatever the harness said the run cost is captured off the same parsed
    events (`usage.py`, fail-soft) and appended to the agent's usage ledger
    — the agent-side counterpart of a task run's `usage` field — together
    with how the run ended (`ok`/`raised`/`timeout`) and how long it took,
    which is what lets a *later* run of the same agent see that its recent
    ticks have been timing out.
    """
    harness = resolve_agent_harness(ctx)
    run_id = fsio.ulid()
    timeout = float(ctx.settings.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS))
    cap = agent_cap(ctx)
    env = {
        **os.environ,
        **harness.env,
        "QUORUM_HOME": str(ctx.home),
        **actor_env(ctx.name, run_id, cap),
    }
    argv = build_harness_argv(harness, prompt)
    transcript = transcript_path(ctx.home, ctx.name)
    proc = subprocess.Popen(
        argv,
        cwd=str(ctx.home),
        stdin=subprocess.PIPE if harness.inject else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",  # a stray non-UTF-8 byte must not kill the tick
        bufsize=1,
        env=env,
    )
    spend = usage.UsageCollector()
    # Real elapsed time, not ctx.now(): this measures how long the process
    # actually took (the same real seconds `proc.wait(timeout=...)` counts),
    # and nothing decides anything from it.
    started = time.monotonic()
    outcome = "raised"  # every exit that is not a clean 0 is one of these
    try:
        with guidance_pump(ctx.home, ctx.name, harness, proc, prompt) as pump:

            def on_event(event: object) -> None:
                spend.add(event)
                if pump is not None:
                    pump.on_event(event)

            reader = threading.Thread(
                target=stream_transcript,
                args=(proc, transcript),
                kwargs={"extra": {"run": run_id}, "now": ctx.now, "on_event": on_event},
                daemon=True,
            )
            reader.start()
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                reader.join(2)
                outcome = "timeout"
                raise RuntimeError(
                    f"{ctx.name} harness run {run_id} timed out after {int(timeout)}s "
                    "and was killed"
                ) from None
        reader.join(5)
        if code == 0:
            outcome = "ok"
    finally:
        # A run that timed out or crashed still spent what it spent, and a
        # run count only means something if every run is in the ledger.
        usage.record_agent_run(
            ctx.home,
            ctx.name,
            run_id,
            spend.result(),
            ctx.now(),
            outcome=outcome,
            duration_seconds=time.monotonic() - started,
        )
    if code != 0:
        raise RuntimeError(f"{ctx.name} harness run {run_id} exited {code}")
    return run_id
