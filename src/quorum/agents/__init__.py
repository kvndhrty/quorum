"""Built-in agents, resolved lazily by short name.

Quorum ships two: the manager (harness-driven task supervision) and the
generic prompt agent (a user-written prompt run over the harness on a
schedule). Everything else is user-provided — see docs/guide.md for the
plugin contract and examples/steward.py for a complete worked example.
"""

from __future__ import annotations

import importlib

from ..agent import Agent

BUILTIN_NAMES: dict[str, str] = {
    "manager": "manager.Manager",
    "prompt": "prompt_agent.PromptAgent",
}


def get_builtin(name: str) -> type[Agent]:
    module_name, _, class_name = BUILTIN_NAMES[name].partition(".")
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, class_name)
