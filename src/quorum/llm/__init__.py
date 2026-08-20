"""Optional LLM intelligence for agents.

Quorum is not tied to any provider or CLI: the configured backend turns a
prompt string into a completion string, nothing more. The default backend
shells out to a user-selected executable (`claude -p`, `codex exec`, `llm`,
`ollama run …`). A "proxy" backend name is reserved for a future
supervisor-managed LLM auth proxy.

LLMClient.complete() NEVER raises — it returns None on any failure
(unconfigured, missing executable, timeout, nonzero exit), and every
LLM-using agent must implement its no-LLM behavior for that case.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import LLMConfig

log = logging.getLogger("quorum.llm")


class LLMBackend(Protocol):
    def complete(self, prompt: str, *, timeout: float) -> str: ...


class LLMClient:
    def __init__(self, backend: LLMBackend | None, config: "LLMConfig | None"):
        self._backend = backend
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._backend is not None

    @classmethod
    def from_config(
        cls,
        llm_config: "LLMConfig | None",
        agent_settings: dict | None = None,
        home: Path | None = None,
    ) -> "LLMClient":
        """Build a client from the global [llm] section merged with per-agent
        overrides. settings.llm may be false (opt out), true (defaults), or a
        table of [llm]-shaped overrides."""
        from ..config import LLMConfig

        agent_settings = agent_settings or {}
        override = agent_settings.get("llm", True)
        if override is False or (llm_config is None and override in (True, None)):
            return cls(None, None)
        base = llm_config.model_dump() if llm_config else LLMConfig().model_dump()
        if isinstance(override, dict):
            base.update(override)
        config = LLMConfig.model_validate(base)
        if not config.executable:
            return cls(None, None)
        if config.backend == "proxy":
            from .proxy_backend import ProxyBackend

            return cls(ProxyBackend(config, home=home), config)
        from .cli_backend import CliBackend

        return cls(CliBackend(config), config)

    def complete(self, prompt: str) -> str | None:
        """Prompt -> completion, or None. Truncates oversized prompts
        (keeping head and tail) and never raises."""
        if self._backend is None or self._config is None:
            return None
        limit = self._config.max_prompt_chars
        if len(prompt) > limit:
            half = limit // 2
            prompt = prompt[:half] + "\n[... truncated ...]\n" + prompt[-(limit - half) :]
        try:
            result = self._backend.complete(prompt, timeout=self._config.timeout_seconds)
        except Exception as e:
            log.warning("LLM call failed (%s): %s", type(e).__name__, e)
            return None
        result = (result or "").strip()
        return result or None
