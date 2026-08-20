"""Reserved seam: a supervisor-managed proxy providing agents with managed
LLM API authentication (likely delegating to nono-py's start_proxy credential
injection). Not implemented in v1 — see docs/architecture.md."""

from __future__ import annotations

from pathlib import Path

from ..config import LLMConfig


class NotConfigured(RuntimeError):
    pass


class ProxyBackend:
    def __init__(self, config: LLMConfig, home: Path | None = None):
        raise NotConfigured(
            'backend = "proxy" is reserved for the managed LLM auth proxy and is not '
            'implemented yet — use backend = "cli". See docs/architecture.md.'
        )

    def complete(self, prompt: str, *, timeout: float) -> str:  # pragma: no cover
        raise NotConfigured("proxy backend is not implemented")
