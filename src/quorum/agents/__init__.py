"""Built-in agents, resolved lazily by short name."""

from __future__ import annotations

import importlib

from ..agent import Agent

BUILTIN_NAMES: dict[str, str] = {
    "tracker": "tracker.Tracker",
    "sentinel": "sentinel.Sentinel",
    "steward": "steward.Steward",
    "scribe": "scribe.Scribe",
    "scout": "scout.Scout",
}


def get_builtin(name: str) -> type[Agent]:
    module_name, _, class_name = BUILTIN_NAMES[name].partition(".")
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, class_name)
