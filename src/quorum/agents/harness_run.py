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

from .. import fsio
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


def run_agent_harness(ctx: AgentContext, prompt: str) -> str:
    """Run the agent's configured harness over `prompt`, synchronously,
    cwd = QUORUM_HOME. Raises on timeout or nonzero exit; returns the run id.

    An inject-capable harness can be steered while the run is in flight:
    messages landing in the agent's own inbox are forwarded as user turns by
    the same `GuidancePump` the task runner uses.
    """
    harness = resolve_agent_harness(ctx)
    run_id = fsio.ulid()
    timeout = float(ctx.settings.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS))
    cap = int(ctx.settings.get("max_actions_per_run", DEFAULT_MAX_ACTIONS_PER_RUN))
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
    with guidance_pump(ctx.home, ctx.name, harness, proc, prompt) as pump:
        reader = threading.Thread(
            target=stream_transcript,
            args=(proc, transcript),
            kwargs={
                "extra": {"run": run_id},
                "now": ctx.now,
                "on_event": pump.on_event if pump is not None else None,
            },
            daemon=True,
        )
        reader.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(2)
            raise RuntimeError(
                f"{ctx.name} harness run {run_id} timed out after {int(timeout)}s and was killed"
            ) from None
    reader.join(5)
    if code != 0:
        raise RuntimeError(f"{ctx.name} harness run {run_id} exited {code}")
    return run_id
