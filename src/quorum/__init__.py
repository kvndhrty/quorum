"""Quorum: an agentic ecosystem of specialists.

Always-on, file-based agents that make a busy researcher's life easier.
"""

__version__ = "0.2.0"


def installed_version() -> str:
    """The version of the installed quorum-orchestrator distribution.

    Falls back to `__version__` for a source tree that was never installed.
    Recorded on supervisor.lock so `quorum doctor` can tell a user who
    upgraded but never restarted that the running process is still the old
    code — otherwise an invisible state.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quorum-orchestrator")
    except PackageNotFoundError:
        return __version__
