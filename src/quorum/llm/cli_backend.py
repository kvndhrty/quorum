"""Shell out to a user-selected CLI for completions.

input = "stdin": the prompt is piped to the process's stdin.
input = "argv":  the prompt replaces a "{prompt}" placeholder in args (or is
                 appended as the final argument if no placeholder is present).
stdout (stripped) is the completion. Output is capped at 1 MB.
"""

from __future__ import annotations

import logging
import os
import subprocess

from ..config import LLMConfig

log = logging.getLogger("quorum.llm")

MAX_OUTPUT_BYTES = 1_000_000


class CliBackend:
    def __init__(self, config: LLMConfig, sandboxed_runner=None):
        self.config = config
        # Optional hook: sandbox.py supplies a nono-wrapped runner when
        # [sandbox].use_nono is enabled.
        self._run = sandboxed_runner or subprocess.run

    def complete(self, prompt: str, *, timeout: float) -> str:
        cfg = self.config
        argv = [cfg.executable]
        stdin_data: str | None = None
        if cfg.input == "stdin":
            argv += cfg.args
            stdin_data = prompt
        else:
            if any("{prompt}" in a for a in cfg.args):
                argv += [a.replace("{prompt}", prompt) for a in cfg.args]
            else:
                argv += [*cfg.args, prompt]
        env = {**os.environ, **cfg.env} if cfg.env else None
        proc = self._run(
            argv,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:500]
            raise RuntimeError(f"{cfg.executable} exited {proc.returncode}: {stderr}")
        out = proc.stdout or ""
        if len(out.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
            out = out[: MAX_OUTPUT_BYTES // 4]
            log.warning("LLM output exceeded %d bytes; truncated", MAX_OUTPUT_BYTES)
        return out
