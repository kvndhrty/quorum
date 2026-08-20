"""Optional nono (nolabs-ai) sandbox integration.

Quorum runs fine without nono. When it's wanted, there are three modes,
documented in docs/nono.md:

1. Wrap the world (recommended, zero code): `nono run --profile quorum -- quorum up`.
2. Self-sandbox: `quorum up --self-sandbox` applies a nono-py CapabilitySet to
   this process (irreversibly, children included) before the scheduler starts.
3. Child-only: [sandbox].use_nono = true runs LLM CLI subprocesses through
   nono-py's sandboxed_exec while the supervisor itself stays unsandboxed.

This is the only module that imports nono-py, and only inside functions, so
installations without the [nono] extra never pay for it. If the user asked
for sandboxing and nono-py is missing, we fail loud rather than silently
running unsandboxed.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("quorum.sandbox")


class SandboxUnavailable(RuntimeError):
    pass


def _import_nono():
    try:
        import nono_py  # type: ignore

        return nono_py
    except ImportError as e:
        raise SandboxUnavailable(
            "nono-py is not installed — install the [nono] extra "
            "(uv tool install 'quorum[nono]') or run under the nono binary instead: "
            "`nono run --profile quorum -- quorum up` (see docs/nono.md)"
        ) from e


def build_capabilities(home: Path, config: Config):
    """A least-privilege CapabilitySet derived from the resolved config:
    rw on QUORUM_HOME, ro on project dirs, rw on steward watch/dest dirs,
    network blocked unless an LLM CLI is configured."""
    nono_py = _import_nono()
    from .projects import ProjectRegistry

    caps = nono_py.CapabilitySet()
    caps.allow_path(str(home), nono_py.AccessMode.READ_WRITE)
    for project in ProjectRegistry(home).list():
        if project.dir.is_dir():
            caps.allow_path(str(project.dir), nono_py.AccessMode.READ)
    for acfg in config.agents.values():
        for w in acfg.settings.get("watch", []):
            p = Path(w).expanduser()
            if p.is_dir():
                caps.allow_path(str(p), nono_py.AccessMode.READ_WRITE)
        for rule in acfg.settings.get("rules", []):
            dest = rule.get("dest")
            if dest:
                caps.allow_path(str(Path(dest).expanduser()), nono_py.AccessMode.READ_WRITE)
    if config.llm is None or not config.llm.executable:
        caps.block_network()
    return caps


def self_sandbox(home: Path, config: Config) -> None:
    """Irreversibly sandbox the current process (and all children)."""
    nono_py = _import_nono()
    caps = build_capabilities(home, config)
    nono_py.apply(caps)
    log.info("self-sandbox applied via nono-py (Landlock/Seatbelt)")


def make_sandboxed_runner(home: Path, config: Config):
    """A subprocess.run-compatible callable that executes the command under a
    nono-py child sandbox (nono_py.sandboxed_exec: the parent stays
    unsandboxed, the child gets Landlock/Seatbelt restrictions).

    nono-py is imported lazily at call time, so a misconfiguration is loud in
    logs but never causes an unsandboxed execution.

    sandboxed_exec has no stdin piping, so stdin-mode prompts are staged as a
    private file under QUORUM_HOME and redirected via /bin/sh. argv-mode
    ([llm].input = "argv") avoids the shell hop and is recommended under nono.
    """
    home = Path(home)

    def run(argv, input=None, timeout=None, env=None, capture_output=True, text=True):
        nono_py = _import_nono()
        caps = build_capabilities(home, config)
        command = [str(a) for a in argv]
        prompt_file: Path | None = None
        try:
            if input:
                staging = home / "state" / "llm"
                staging.mkdir(parents=True, exist_ok=True)
                prompt_file = staging / f"prompt-{os.getpid()}.txt"
                prompt_file.write_text(input, encoding="utf-8")
                command = [
                    "/bin/sh", "-c",
                    f'exec "$0" "$@" < {shlex.quote(str(prompt_file))}',
                    *command,
                ]
            if env is None:
                env_list, inherit = None, True
            else:
                env_list, inherit = [(k, str(v)) for k, v in env.items()], False
            result = nono_py.sandboxed_exec(
                caps, command, timeout_secs=timeout, env=env_list, inherit_env=inherit
            )
        finally:
            if prompt_file is not None:
                prompt_file.unlink(missing_ok=True)
        return subprocess.CompletedProcess(
            list(argv), result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    return run
