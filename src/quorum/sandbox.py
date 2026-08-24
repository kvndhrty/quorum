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

Why the capability set is wider than "QUORUM_HOME plus project dirs": a
process cannot exec *anything* without reading the loader, the system
libraries it links against, and the binary itself, and modes 2 and 3 both
need that. Mode 2 additionally keeps importing Python after apply() — the
builtin agents and APScheduler's trigger plugins are imported lazily — so it
needs the interpreter's own tree readable. Every one of those additions is
READ-only and comes from nono's own `system_read_*` policy groups (the same
baseline the `nono run` binary uses in mode 1) or is derived from this
interpreter at runtime. Write access stays exactly where it was: QUORUM_HOME, plus any
watch/dest directories agents declare in their settings.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING

from . import fsio

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("quorum.sandbox")

# nono ships a policy describing the read-only paths an executable needs in
# order to run at all (loader, libc, /dev, trust stores...). Both platform
# variants are requested; resolve_groups keeps the ones that apply here.
SYSTEM_READ_GROUPS = ("system_read_macos", "system_read_linux")


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


def _add_system_reads(nono_py, caps) -> None:
    """Layer nono's own baseline of system read paths onto `caps`.

    Without this a sandboxed child cannot exec at all: Linux reports
    `nono: exec failed: Permission denied`, macOS fails to open the dynamic
    loader. Fails closed — a sandbox that cannot run the program it is
    wrapping is a misconfiguration worth surfacing, not one to paper over.
    """
    try:
        policy = nono_py.load_embedded_policy()
        policy.resolve_groups(list(SYSTEM_READ_GROUPS), caps)
    except Exception as e:
        raise SandboxUnavailable(
            f"could not resolve nono's system read policy ({e}); without it no "
            "sandboxed process can exec. Check the installed nono-py version."
        ) from e


def _python_runtime_paths() -> set[str]:
    """Directories this interpreter must read to keep importing after apply().

    Mode 2 sandboxes the supervisor before it resolves agents, and both the
    builtin agents and APScheduler's trigger plugins import lazily, so the
    venv, the base interpreter and the stdlib all have to stay readable.
    Derived rather than hardcoded so it follows venvs, uv-managed CPythons
    and editable checkouts (where the package sits outside sys.prefix).
    """
    candidates = {
        sys.prefix,
        sys.base_prefix,
        sysconfig.get_path("stdlib"),
        sysconfig.get_path("purelib"),
        sysconfig.get_path("platlib"),
        # The tree holding the quorum package itself — for an editable install
        # that is the checkout's src/, which no prefix above covers.
        str(Path(__file__).resolve().parent.parent),
    }
    executable = Path(sys.executable)
    if executable.exists():
        candidates.add(str(executable.resolve().parent))
    return {c for c in candidates if c and Path(c).is_dir()}


def _apply_profile_file(nono_py, caps, config: Config) -> dict:
    """Merge the user's own nono-style profile into `caps`.

    `[sandbox].profile_file` points at the same JSON shape a `nono run`
    profile uses ({"fs_read": [...], "fs_write": [...], "network": [...]}),
    so one hand-written profile serves all three sandbox modes. Grants are
    *added* to what quorum derives — the derivation stays the floor that
    keeps quorum itself functional. Fails closed on an unreadable or invalid
    file: a profile the user asked for that cannot load must not silently
    narrow (or skip) the sandbox they expected.
    """
    if not config.sandbox.profile_file:
        return {}
    path = Path(config.sandbox.profile_file).expanduser()
    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SandboxUnavailable(f"could not load [sandbox].profile_file {path}: {e}") from e
    if not isinstance(profile, dict):
        raise SandboxUnavailable(f"[sandbox].profile_file {path} must be a JSON object")
    for key, mode in (("fs_write", nono_py.AccessMode.READ_WRITE), ("fs_read", nono_py.AccessMode.READ)):
        for entry in profile.get(key, []):
            p = Path(str(entry)).expanduser()
            if p.is_file():
                caps.allow_file(str(p), mode)
            elif p.is_dir():
                caps.allow_path(str(p), mode)
            else:
                log.warning("sandbox profile %s: %s path %s does not exist; skipped", path, key, p)
    return profile


def _llm_executable(config: Config) -> Path | None:
    """The configured LLM CLI as an absolute path, or None if unset/not found.

    `[llm].executable` is usually a bare name (`claude`, `codex`), so it is
    resolved against PATH here: the sandbox grants access to paths, and a
    name the child cannot resolve is a name it cannot run.
    """
    if config.llm is None or not config.llm.executable:
        return None
    found = shutil.which(config.llm.executable)
    return Path(found).resolve() if found else None


def build_capabilities(home: Path, config: Config):
    """A least-privilege CapabilitySet derived from the resolved config.

    Writable: QUORUM_HOME and any watch/dest directories agents declare.
    Readable: project dirs, the configured LLM executable, this interpreter's
    tree, and nono's system-read baseline. Network is blocked unless an LLM
    CLI is configured.
    """
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

    _add_system_reads(nono_py, caps)
    for path in _python_runtime_paths():
        caps.allow_path(path, nono_py.AccessMode.READ)
    executable = _llm_executable(config)
    if executable is not None:
        # allow_file, not allow_path: the latter rejects non-directories, and
        # granting the whole containing directory would be needlessly wide.
        caps.allow_file(str(executable), nono_py.AccessMode.READ)
    profile = _apply_profile_file(nono_py, caps, config)

    needs_network = (config.llm is not None and config.llm.executable) or profile.get("network")
    if not needs_network:
        caps.block_network()
    caps.deduplicate()
    return caps


def self_sandbox(home: Path, config: Config) -> None:
    """Irreversibly sandbox the current process (and all children)."""
    nono_py = _import_nono()
    caps = build_capabilities(home, config)
    nono_py.apply(caps)
    log.info("self-sandbox applied via nono-py (Landlock/Seatbelt)")


def build_task_capabilities(home: Path, config: Config, task, workdir: Path):
    """A CapabilitySet for one task run.

    Writable: QUORUM_HOME (reports, transcript, locks), the run's working
    directory, the project's own .git (a worktree shares the main repo's
    object store, so commits write there), and any [sandbox].task_write
    extras. Readable: the interpreter tree, nono's system-read baseline, the
    first argv element of the harness when it resolves on PATH, and
    [sandbox].task_read extras. Network stays open — a coding harness is
    assumed to need its API.
    """
    from .projects import ProjectRegistry

    nono_py = _import_nono()
    caps = nono_py.CapabilitySet()
    caps.allow_path(str(home), nono_py.AccessMode.READ_WRITE)
    caps.allow_path(str(workdir), nono_py.AccessMode.READ_WRITE)
    project = ProjectRegistry(home).get(task.project)
    if project is not None and (project.dir / ".git").is_dir():
        caps.allow_path(str(project.dir / ".git"), nono_py.AccessMode.READ_WRITE)
    for extra in config.sandbox.task_write:
        p = Path(extra).expanduser()
        if p.exists():
            caps.allow_path(str(p), nono_py.AccessMode.READ_WRITE)
    for extra in config.sandbox.task_read:
        p = Path(extra).expanduser()
        if p.exists():
            caps.allow_path(str(p), nono_py.AccessMode.READ)
    harness = config.harness.get(task.harness)
    if harness is not None:
        found = shutil.which(harness.start[0])
        if found:
            caps.allow_file(str(Path(found).resolve()), nono_py.AccessMode.READ)
    _apply_profile_file(nono_py, caps, config)
    _add_system_reads(nono_py, caps)
    for path in _python_runtime_paths():
        caps.allow_path(path, nono_py.AccessMode.READ)
    caps.deduplicate()
    return caps


def apply_task_sandbox(home: Path, config: Config, task, workdir: Path) -> None:
    """Irreversibly sandbox the current task-runner process and its harness.

    Called by the runner when [sandbox].use_nono is set; fails closed via
    SandboxUnavailable when nono-py is missing.
    """
    nono_py = _import_nono()
    caps = build_task_capabilities(home, config, task, workdir)
    nono_py.apply(caps)
    log.info("task sandbox applied for %s (workdir %s)", task.short_id, workdir)


def _text(stream) -> str:
    """nono-py hands back bytes; the subprocess.run contract we advertise is text."""
    if isinstance(stream, bytes | bytearray):
        return bytes(stream).decode("utf-8", "replace")
    return stream or ""


def make_sandboxed_runner(home: Path, config: Config):
    """A subprocess.run-compatible callable that executes the command under a
    nono-py child sandbox (nono_py.sandboxed_exec: the parent stays
    unsandboxed, the child gets Landlock/Seatbelt restrictions).

    nono-py is imported lazily at call time, so a misconfiguration is loud in
    logs but never causes an unsandboxed execution.

    sandboxed_exec has no stdin piping, so stdin-mode prompts are staged as a
    private file under QUORUM_HOME and redirected via /bin/sh. argv-mode
    ([llm].input = "argv") avoids the shell hop and is slightly cheaper.

    cwd is pinned to QUORUM_HOME: sandboxed_exec otherwise inherits the
    parent's working directory, which is normally outside the capability set,
    and a shell that cannot getcwd() starts by printing errors to stderr.
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
                # ULID, not pid: scheduler threads share one pid, and two
                # agents prompting in the same tick window must not clobber
                # (or unlink) each other's staged prompt.
                prompt_file = staging / f"prompt-{fsio.ulid()}.txt"
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
                caps,
                command,
                cwd=str(home),
                timeout_secs=timeout,
                env=env_list,
                inherit_env=inherit,
            )
        finally:
            if prompt_file is not None:
                prompt_file.unlink(missing_ok=True)
        return subprocess.CompletedProcess(
            list(argv), result.exit_code, stdout=_text(result.stdout), stderr=_text(result.stderr)
        )

    return run
