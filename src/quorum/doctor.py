"""`quorum doctor`: one pass over everything in a quorum home that fails soft.

Quorum degrades rather than crashes almost everywhere — a `gh` that is
installed but unauthenticated simply stops producing `ci:` lines, an
unreadable config.toml turns the fail-soft probes off, a prompt seed left
behind by an upgrade keeps rendering the old policy, a stale `cur/` claim
just sits there until the janitor's next hour. That is the right behaviour
at runtime and exactly why none of it announces itself. Doctor is the one
place that goes looking.

Three rails:

* **It diagnoses, it never repairs.** Every failing line names the fix in
  the user's own vocabulary (a config key, a shell command); nothing here
  writes to QUORUM_HOME or edits config.toml. `--fix` is deliberately not a
  thing.
* **It is a pure reader**, with exactly one exception: the opt-in
  `--smoke` probe, which actually runs a harness (in a scratch directory,
  never in the user's home) through the real runner path — argv building,
  `inject` stdin delivery, transcript streaming, session capture. That is
  the check that would have caught the 2026-08-30 outage, where a
  stream-json CLI silently ignored its argv prompt and every run hung until
  it timed out (#24).
* **Three states, no fourth.** `ok` (✓), `problem` (✗) and `na` (–, not
  applicable / nothing configured to check). Any ✗ makes the command exit
  non-zero, so `quorum doctor` is usable in a script; a `–` never does.
  Something a user switched off on purpose is a `–`, not a warning to
  train them to ignore.

One small function per check, each taking only what it needs, so every one
of them can be exercised — passing *and* failing — from a test.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import fsio
from . import home as home_mod

if TYPE_CHECKING:
    from .config import Config, HarnessConfig

OK = "ok"
PROBLEM = "problem"
NA = "na"

GLYPH = {OK: "✓", PROBLEM: "✗", NA: "–"}

# `git worktree` — the runner's default working-directory strategy — arrived
# in git 2.5.
MIN_GIT_VERSION = (2, 5)

# The prompt the smoke probe sends. Deliberately trivial and explicitly
# read-only: it costs a handful of tokens and must not touch the scratch
# directory it runs in.
SMOKE_PROMPT = (
    "quorum doctor smoke test. Do not read or write any files, and do not "
    "use any tools. Reply with the single word OK and stop."
)
DEFAULT_SMOKE_TIMEOUT = 60.0
# The pump needs an inbox name; nothing ever posts to this one. Its bus is
# pointed at the probe's throwaway scratch home, so no inbox is created in
# the user's home either.
SMOKE_INBOX = "doctor-smoke"


@dataclass(frozen=True)
class Check:
    """One line of doctor output: what was checked, how it went, what to do."""

    name: str
    status: str
    summary: str
    fix: str = ""

    @property
    def glyph(self) -> str:
        return GLYPH[self.status]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
        }


def ok(name: str, summary: str, fix: str = "") -> Check:
    return Check(name=name, status=OK, summary=summary, fix=fix)


def problem(name: str, summary: str, fix: str = "") -> Check:
    return Check(name=name, status=PROBLEM, summary=summary, fix=fix)


def na(name: str, summary: str, fix: str = "") -> Check:
    return Check(name=name, status=NA, summary=summary, fix=fix)


def _oneline(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- home and config ---------------------------------------------------------


def check_home(home: Path) -> Check:
    """The home directory exists and has been initialized."""
    config = Path(home) / home_mod.CONFIG_NAME
    if config.is_file():
        return ok("home", f"quorum home initialized at {home}")
    return problem(
        "home",
        f"no quorum home at {home} (no {home_mod.CONFIG_NAME})",
        "run `quorum init` (or point --home / $QUORUM_HOME at the right directory)",
    )


def check_config(home: Path) -> tuple[Check, Config | None]:
    """Parse config.toml **strictly**, and hand back what parsed.

    Deliberately not `try_load_config`/`load_config_or_default`: every other
    reader papers a broken config over with defaults so it can keep running,
    which is exactly how a typo goes unnoticed for a week. This is where the
    user gets told.
    """
    from .config import load_config

    try:
        config = load_config(Path(home))
    except Exception as e:  # ConfigError, but tomllib also raises UnicodeDecodeError
        return (
            problem(
                "config",
                f"config.toml does not load: {_oneline(e)}",
                "fix the file named above — until then every reader silently falls back "
                "to defaults and the fail-soft probes ([ci], [herdr]) turn themselves off",
            ),
            None,
        )
    return ok("config", "config.toml parses (strict)"), config


# -- harnesses ---------------------------------------------------------------


def check_harness_binary(name: str, harness: HarnessConfig) -> Check:
    """argv[0] of a `[harness.<name>]` table resolves to something runnable."""
    exe = harness.start[0] if harness.start else ""
    found = shutil.which(exe) if exe else None
    if found:
        return ok(f"harness.{name}.binary", f"harness {name!r}: {exe} on PATH ({found})")
    return problem(
        f"harness.{name}.binary",
        f"harness {name!r}: {exe or '<empty argv>'} not found on PATH",
        f"install it, or give an absolute path in [harness.{name}].start",
    )


def check_harness_template(name: str, harness: HarnessConfig) -> Check:
    """The argv template can actually deliver a prompt (and resume a session).

    The stream-json rule first, because it is the expensive one: a CLI
    invoked with `--input-format stream-json` reads user turns only from
    stdin and ignores an argv prompt entirely, so without
    `inject = "stream-json"` every run sits there with nothing to do until
    something times it out. That is the 2026-08-30 outage, and it is visible
    statically.
    """
    argv = [*harness.start, *harness.resume]
    if any("stream-json" in element for element in argv) and not harness.inject:
        return problem(
            f"harness.{name}.template",
            f"harness {name!r}: argv speaks stream-json but `inject` is unset — "
            "the CLI reads its prompt only from stdin and would ignore the argv one",
            f'add inject = "stream-json" to [harness.{name}] (every run would otherwise '
            "hang until it timed out)",
        )
    if harness.resume and not any("{session}" in element for element in harness.resume):
        return problem(
            f"harness.{name}.template",
            f"harness {name!r}: `resume` has no {{session}} — a resumed run would start "
            "a fresh session instead of continuing the task's",
            f"add {{session}} to [harness.{name}].resume, or drop `resume` entirely "
            "(the worktree already carries prior progress)",
        )
    if harness.inject:
        return ok(
            f"harness.{name}.template",
            f"harness {name!r}: prompt injected over stdin (inject = {harness.inject!r})",
        )
    if any("{prompt}" in element for element in harness.start):
        return ok(f"harness.{name}.template", f"harness {name!r}: {{prompt}} in argv")
    return na(
        f"harness.{name}.template",
        f"harness {name!r}: no {{prompt}} in the template — quorum appends the prompt "
        "as the final argument",
        f"add {{prompt}} to [harness.{name}].start to place it explicitly",
    )


def check_harnesses(config: Config) -> list[Check]:
    """Every `[harness.*]` table — or the one line that says there are none.

    "No harness yet" is a `–`, not a `✗`: it is the state `quorum init`
    leaves behind, and a fresh home reporting two failures for one unmade
    decision teaches the user to ignore the output. It is a `✗` only when
    the config contradicts itself — a `default_harness` naming a table that
    was never written. When there *are* tables, `check_default_harness`
    speaks for the default; when there are none, this line already did.
    """
    if not config.harness:
        default = config.tasks.default_harness
        if default:
            return [
                problem(
                    "harness",
                    f"[tasks].default_harness = {default!r} but config.toml has no "
                    "[harness.<name>] table at all — nothing can run a task",
                    f"add a [harness.{default}] table (or clear default_harness)",
                )
            ]
        return [
            na(
                "harness",
                "no harness configured yet — nothing can run a task until there is one",
                "uncomment one of the [harness.*] examples in config.toml and set "
                "[tasks].default_harness, then `quorum doctor --smoke`",
            )
        ]
    checks: list[Check] = []
    for name, harness in sorted(config.harness.items()):
        checks.append(check_harness_binary(name, harness))
        checks.append(check_harness_template(name, harness))
    return checks


def check_default_harness(config: Config) -> Check:
    default = config.tasks.default_harness
    if not default:
        return problem(
            "harness.default",
            "[tasks].default_harness is unset — every task needs an explicit --harness "
            "and the manager has nothing to launch with",
            'set default_harness = "<name>" under [tasks]',
        )
    if default not in config.harness:
        return problem(
            "harness.default",
            f"[tasks].default_harness = {default!r} names no [harness.{default}] table",
            "fix the name, or add the table (known: "
            + (", ".join(sorted(config.harness)) or "none")
            + ")",
        )
    return ok("harness.default", f"default harness: {default}")


# -- git and projects --------------------------------------------------------


def check_git() -> Check:
    exe = shutil.which("git")
    if not exe:
        return problem(
            "git",
            "git is not on PATH — worktrees, the stranded-work probe and auto-commit "
            "all shell out to it",
            "install git",
        )
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as e:
        return problem("git", f"`git --version` failed: {_oneline(e)}", "check the git install")
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if result.returncode != 0 or not match:
        return problem(
            "git",
            f"`git --version` said {_oneline(result.stdout or result.stderr)!r}",
            "check the git install",
        )
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_GIT_VERSION:
        want = ".".join(str(p) for p in MIN_GIT_VERSION)
        return problem(
            "git",
            f"git {version[0]}.{version[1]} is too old — `git worktree` needs {want}+",
            "upgrade git, or set [tasks].worktree = false to run tasks in the checkout",
        )
    return ok("git", f"git {version[0]}.{version[1]} at {exe}")


def check_projects(home: Path) -> list[Check]:
    from .projects import ProjectRegistry

    projects = ProjectRegistry(home).list()
    if not projects:
        return [
            na(
                "projects",
                "no projects registered",
                "`quorum project add <dir>` registers a repo to work on",
            )
        ]
    checks: list[Check] = []
    for project in projects:
        pdir = project.dir
        if not pdir.is_dir():
            checks.append(
                problem(
                    f"project.{project.slug}",
                    f"project {project.slug}: {pdir} does not exist",
                    f"fix the path in projects/{project.slug}.json, or "
                    f"`quorum project remove {project.slug}`",
                )
            )
        elif not (pdir / ".git").exists():
            checks.append(
                na(
                    f"project.{project.slug}",
                    f"project {project.slug}: {pdir} is not a git repository",
                    "tasks there need `task add --no-worktree` (they run in the "
                    "directory itself)",
                )
            )
        else:
            checks.append(ok(f"project.{project.slug}", f"project {project.slug}: {pdir}"))
    return checks


# -- optional integrations ---------------------------------------------------


def check_gh(home: Path, config: Config) -> Check:
    """`gh` for the CI probe: on PATH *and* authenticated.

    The interesting failure is the middle one. No gh at all is a `–`: the
    probe advertises that it silently does nothing, and plenty of users never
    wanted it. A gh that is installed but unauthenticated is a `✗`, because
    it looks configured and produces nothing — every `ci:` line disappears
    from the digest with no trace anywhere.

    The gh call itself goes through `forge.auth_status` — `forge.py` is the
    only module that shells out to a forge CLI, and asking it means doctor
    asks with the probe's own `[ci]` settings rather than a second opinion.
    Its `None` ("no answer": gh timed out, or the probe declined) is a `–`,
    not a `✗`: a laptop on a plane is not a misconfigured home.
    """
    from .forge import auth_status

    if not config.ci.enabled:
        return na("ci.gh", "[ci].enabled = false — the manager sees no PR/check state")
    exe = shutil.which("gh")
    if not exe:
        return na(
            "ci.gh",
            "[ci] is on but gh is not on PATH — the manager's ci: lines are silently absent",
            "install gh, or set [ci].enabled = false to say so on purpose",
        )
    authenticated = auth_status(Path(home))
    if authenticated is None:
        return na(
            "ci.gh",
            f"gh auth state unknown ({exe} did not answer within "
            "[ci].timeout_seconds) — ci: lines may or may not appear",
            "run `gh auth status` by hand; raise [ci].timeout_seconds if it is just slow",
        )
    if not authenticated:
        return problem(
            "ci.gh",
            "gh is installed but not authenticated — every ci: line silently disappears",
            "run `gh auth login` (or export GH_TOKEN), or set [ci].enabled = false",
        )
    return ok("ci.gh", f"gh authenticated ({exe})")


def check_herdr(config: Config) -> Check:
    if config.herdr is None:
        return na("herdr", "no [herdr] table — pane status and the nudge doorbell are off")
    if not config.herdr.enabled:
        return na("herdr", "[herdr].enabled = false")
    from .herdr import DEFAULT_SOCKET

    # Resolved from the config doctor already parsed strictly, not through
    # herdr.socket_path(): the adapter re-reads config.toml fail-soft, and a
    # doctor line must reflect what the user actually wrote.
    path = Path(config.herdr.socket or DEFAULT_SOCKET).expanduser()
    if path.exists():
        return ok("herdr", f"herdr socket at {path}")
    return problem(
        "herdr",
        f"[herdr] is enabled but {path} does not exist",
        "start herdr, set [herdr].socket to the right path, or set [herdr].enabled = false",
    )


def check_notify(config: Config) -> Check:
    """The `[notify]` template can be run and can carry the text.

    No table is a `–`, not a `✗`: a home whose escalations reach nobody is a
    decision the user may not have made yet, and the line says what it
    costs. A table whose argv[0] is not on PATH is a `✗` — every delivery
    would be a `not found` line in supervisor.log and nothing else, which is
    exactly the failure that looks like silence. A template without
    `{text}` is a `–`: quorum appends the text as the final argument, the
    same convention as a harness template without `{prompt}`, so it works,
    but the user probably meant to place it.
    """
    cfg = config.notify
    if cfg is None:
        return na(
            "notify",
            "no [notify] table — attention posts reach no one until you look "
            "(docs/guide.md#getting-notified)",
        )
    exe = cfg.command[0]
    found = shutil.which(exe)
    topics = ", ".join(cfg.topics)
    if not found:
        return problem(
            "notify",
            f"[notify].command starts with {exe!r}, which is not on PATH — every "
            "notification would fail (one line each in logs/supervisor.log)",
            "install it, give an absolute path in [notify].command, or remove the table",
        )
    if not any("{text}" in element for element in cfg.command):
        return na(
            "notify",
            f"notify: {exe} on PATH ({found}), fires on {topics}; no {{text}} in the "
            "template — quorum appends the text as the final argument",
            "add {text} to [notify].command to place it explicitly",
        )
    return ok("notify", f"notify: {exe} on PATH ({found}), fires on {topics}")


def check_sandbox(config: Config) -> Check:
    """[sandbox].use_nono is backed by an importable, supported nono-py.

    Asked through `sandbox.availability()` rather than importing nono_py
    here: sandbox.py stays the only module that touches it.
    """
    if not config.sandbox.use_nono:
        return na("sandbox", "[sandbox].use_nono = false — task runs are not sandboxed")
    from .sandbox import availability

    available, detail = availability()
    if available:
        return ok("sandbox", f"nono sandbox ready ({detail})")
    return problem(
        "sandbox",
        f"[sandbox].use_nono = true but the sandbox is unavailable: {_oneline(detail)}",
        "install the [nono] extra on a supported platform, or set use_nono = false "
        "(a run refuses to start rather than run unsandboxed)",
    )


# -- prompts -----------------------------------------------------------------


def _prompt_overlays(home: Path) -> list[Check]:
    """`prompts/<name>.local.md` files: reported, never judged.

    An overlay is additive by construction — the user's own words layered on
    top of whatever default ships — so there is no such thing as a stale one
    and nothing here can be a `✗`. It is listed because a prompt that does
    not read the way the file on disk does is otherwise a genuinely baffling
    half hour.
    """
    directory = Path(home) / "prompts"
    try:
        overlays = sorted(p.name for p in directory.glob("*.local.md") if p.is_file())
    except OSError:
        return []
    return [
        na(
            f"prompts.{name.removesuffix('.local.md')}.local",
            f"prompts/{name} overlays the packaged {name.removesuffix('.local.md')}.md",
        )
        for name in overlays
    ]


def check_prompts(home: Path) -> list[Check]:
    """Home prompt copies against the packaged defaults.

    Classification is `home.classify_prompts` — a loop over the same
    `home.classify_prompt` that `quorum init` seeds by and `quorum prompt
    list` displays, seed record and all — so doctor can never disagree with
    either about what "edited" means.
    """
    states = home_mod.classify_prompts(Path(home))
    if not states:
        return [na("prompts", "no packaged prompt defaults found"), *_prompt_overlays(home)]
    checks: list[Check] = []
    for filename, state in sorted(states.items()):
        name = f"prompts.{filename.removesuffix('.md')}"
        if state == "default":
            checks.append(ok(name, f"prompts/{filename} matches the packaged default"))
        elif state == "missing":
            checks.append(
                na(
                    name,
                    f"prompts/{filename} is not seeded — the packaged default is in use",
                    "`quorum init` seeds an editable copy",
                )
            )
        elif state == "upgradable":
            checks.append(
                problem(
                    name,
                    f"prompts/{filename} is an older packaged default, never edited — "
                    "this home is running last release's policy",
                    "run `quorum init`: it upgrades unedited seeds in place",
                )
            )
        else:  # "edited"
            checks.append(
                na(
                    name,
                    f"prompts/{filename} is edited and the packaged default has since changed",
                    "diff it against the packaged default and merge, or delete the file "
                    "to adopt the new one",
                )
            )
    return checks + _prompt_overlays(home)


# -- state hygiene -----------------------------------------------------------


def _supervisor_lock(home: Path) -> dict[str, Any] | None:
    try:
        return fsio.read_json(Path(home) / "supervisor.lock")
    except (OSError, ValueError):
        return None


def check_supervisor(home: Path) -> Check:
    """supervisor.lock: present, owned by a live pid, heartbeat fresh."""
    from .views import SUPERVISOR_STALE_AFTER

    lock = Path(home) / "supervisor.lock"
    if not lock.exists():
        return na(
            "supervisor",
            "supervisor is not running (no supervisor.lock)",
            "`quorum up --detach` starts it in the background",
        )
    meta = _supervisor_lock(home)
    if meta is None:
        return problem(
            "supervisor",
            f"{lock} is unreadable",
            "delete it and run `quorum up --detach`",
        )
    pid = meta.get("pid")
    if not isinstance(pid, int) or not fsio.pid_alive(pid):
        return problem(
            "supervisor",
            f"stale supervisor.lock: pid {pid} is gone (a crash, or a killed process)",
            "delete supervisor.lock, then `quorum up --detach`",
        )
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        age = 0.0
    if age > SUPERVISOR_STALE_AFTER:
        return problem(
            "supervisor",
            f"supervisor (pid {pid}) has not touched its lock in {int(age)}s — it is "
            "alive but its scheduler looks wedged",
            "check logs/supervisor.log, then `quorum down` and `quorum up --detach`",
        )
    return ok("supervisor", f"supervisor running (pid {pid}, since {meta.get('started_at')})")


def check_version(home: Path) -> Check:
    """The installed quorum against the one the running supervisor started with.

    "I re-installed but never restarted" is otherwise invisible: the process
    keeps executing the old code, and nothing in any view says so.
    """
    from . import installed_version

    installed = installed_version()
    meta = _supervisor_lock(home)
    if meta is None:
        return na("version", f"quorum {installed} installed; no supervisor running to compare")
    pid = meta.get("pid")
    if not isinstance(pid, int) or not fsio.pid_alive(pid):
        return na("version", f"quorum {installed} installed; the supervisor lock is stale")
    running = meta.get("version")
    if not running:
        return na(
            "version",
            f"quorum {installed} installed; the running supervisor recorded no version",
            "it was started by an older quorum — restart it (`quorum down && quorum up "
            "--detach`) to record one",
        )
    if running != installed:
        return problem(
            "version",
            f"installed quorum is {installed} but the running supervisor is {running} — "
            "it is still executing the old code",
            "`quorum down` then `quorum up --detach`",
        )
    return ok("version", f"quorum {installed} installed and running")


def check_runner_locks(home: Path) -> list[Check]:
    """runner.lock files whose pid is gone.

    Harmless to the next run (the O_EXCL lock takes a stale one over), but
    not to observation: the lock's mtime feeds `last_activity`, so an orphan
    keeps telling the digest a dead task was busy a moment ago.
    """
    from .tasks import TaskStore, runner_lock_path

    orphans: list[Check] = []
    for task in TaskStore(home).list():
        lock = runner_lock_path(home, task.id)
        if not lock.exists():
            continue
        try:
            pid = int(fsio.read_json(lock).get("pid", -1))
        except (OSError, ValueError, TypeError):
            pid = -1
        if pid > 0 and fsio.pid_alive(pid):
            continue
        orphans.append(
            problem(
                f"state.runner_lock.{task.short_id}",
                f"task {task.short_id} holds a runner.lock from dead pid {pid} "
                "(the run crashed or was killed)",
                f"delete tasks/{task.id}/runner.lock — the next run takes it over "
                "anyway, but quiet-time reads stay skewed until you do",
            )
        )
    return orphans or [ok("state.runner_locks", "no orphaned runner.lock files")]


def check_stale_claims(home: Path) -> Check:
    """Inbox messages claimed into cur/ and never acked past the janitor's grace."""
    from .messages import STALE_CLAIM_GRACE

    inbox_dir = Path(home) / "messages" / "inbox"
    cutoff = time.time() - STALE_CLAIM_GRACE.total_seconds()
    stuck: dict[str, int] = {}
    if inbox_dir.is_dir():
        for inbox in sorted(p for p in inbox_dir.iterdir() if p.is_dir()):
            for path in fsio.sorted_entries(inbox / "cur"):
                try:
                    if path.stat().st_mtime < cutoff:
                        stuck[inbox.name] = stuck.get(inbox.name, 0) + 1
                except OSError:
                    continue
    if not stuck:
        return ok("state.claims", "no stale inbox claims")
    total = sum(stuck.values())
    hours = STALE_CLAIM_GRACE.total_seconds() / 3600
    return problem(
        "state.claims",
        f"{total} message(s) claimed over {hours:g}h ago and never acked "
        f"({', '.join(f'{k}: {v}' for k, v in sorted(stuck.items()))})",
        "the supervisor's hourly janitor returns them to new/ — start it "
        "(`quorum up --detach`) if it is down",
    )


def check_heartbeats(home: Path, config: Config) -> list[Check]:
    """Agent heartbeats: failure streaks, load errors, durable pauses."""
    if not config.agents:
        return [na("agents", "no agents configured")]
    checks: list[Check] = []
    for name in sorted(config.agents):
        acfg = config.agents[name]
        try:
            hb = fsio.read_json(Path(home) / "state" / "agents" / name / "heartbeat.json")
        except (OSError, ValueError):
            hb = {}
        if not acfg.enabled:
            checks.append(na(f"agent.{name}", f"agent {name}: disabled in config"))
            continue
        streak = hb.get("consecutive_failures") or 0
        status = hb.get("status", "never-ran")
        error = _oneline(hb.get("error") or "", limit=120)
        if status == "paused":
            checks.append(
                na(
                    f"agent.{name}",
                    f"agent {name}: paused{f' ({error})' if error else ''}",
                    f"`quorum agent resume {name}` when the cause is fixed",
                )
            )
        elif status == "error" or streak:
            detail = f"{streak} consecutive failure(s)" if streak else "last tick failed"
            checks.append(
                problem(
                    f"agent.{name}",
                    f"agent {name}: {detail}{f' — {error}' if error else ''}",
                    f"`quorum agent run-once {name}` reproduces it in the foreground; "
                    "the manager's failures are almost always its harness",
                )
            )
        elif status == "never-ran":
            checks.append(
                na(f"agent.{name}", f"agent {name}: never ran ({acfg.schedule})")
            )
        else:
            checks.append(ok(f"agent.{name}", f"agent {name}: {status} ({acfg.schedule})"))
    return checks


def check_dials(home: Path, config: Config) -> list[Check]:
    """The trust dials (dials.py) as informational lines: never ✓, never ✗.

    A home's trust posture — how many tasks run at once, how many actions a
    run gets, whether spend is budgeted, how often the manager wakes, who
    launches / decomposes / merges — belongs next to its health, and none of
    it is a fault: a dial at its cautious default and a dial someone loosened
    on purpose are both `–`. The `fix` on the first line points at the guide
    table that says what loosening each one buys and risks.
    """
    from . import dials

    checks: list[Check] = []
    for dial, value in dials.current(home, config):
        checks.append(
            na(
                f"dial.{dial.key}",
                f"dial {dial.label}: {value}",
                f"dials explained: {dials.GUIDE_ANCHOR}"
                if not checks
                else "",
            )
        )
    return checks


# -- the one active probe ----------------------------------------------------


def smoke_checks(
    home: Path,
    config: Config,
    harness_name: str = "",
    timeout: float = DEFAULT_SMOKE_TIMEOUT,
) -> list[Check]:
    """Actually run a harness, the way the runner would. Opt-in; spends tokens.

    Everything static above can be green while the harness still produces
    nothing quorum can use — that is precisely what happened in #24. So this
    goes through the real runner code (`build_harness_argv`,
    `guidance_pump`, `stream_transcript`) rather than a simplified copy, and
    asserts the two things a run is worthless without: a `result` event, and
    a session/thread id to resume from.

    The probe runs in a scratch directory, gives the guidance pump a bus
    rooted there, and points the harness's own `QUORUM_HOME` at a scratch
    home — so it neither writes to the user's home nor touches a real inbox,
    and an installed integration hook (claude-code's SessionStart, say) that
    fires inside the probe acts on the throwaway home instead of the live
    one. `home` names the home whose config is being checked and is
    deliberately never handed to the child.
    """
    name = harness_name or config.tasks.default_harness
    if not name:
        return [
            problem(
                "smoke",
                "no harness to smoke-test",
                "pass `--smoke <name>` or set [tasks].default_harness",
            )
        ]
    harness = config.harness.get(name)
    if harness is None:
        return [
            problem(
                f"smoke.{name}",
                f"no [harness.{name}] table in config.toml",
                "known: " + (", ".join(sorted(config.harness)) or "none"),
            )
        ]
    with tempfile.TemporaryDirectory(prefix="quorum-doctor-") as scratch:
        return _smoke_run(Path(scratch), name, harness, timeout)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL everything the smoke child started, not just the child.

    Harnesses wrap themselves — a node shim, a shell that execs the real
    binary, a CLI that forks a server. `proc.kill()` reaches only the
    process quorum spawned, and its orphaned children keep running (holding
    the stdout pipe) long past `--smoke-timeout`, which is exactly the
    overshoot this probe exists to measure. The child is spawned with
    `start_new_session=True` so its pid is also its process-group id, and
    one `killpg` takes the lot.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except Exception:
        # No killpg (Windows), a group already gone, or a pid we may not
        # signal — fall back to the direct child and take what we can get.
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _smoke_run(scratch: Path, name: str, harness: HarnessConfig, timeout: float) -> list[Check]:
    from .actor import strip_actor_env
    from .runner import _find_session_id, build_harness_argv, guidance_pump, stream_transcript

    workdir = scratch / "run"
    workdir.mkdir(parents=True, exist_ok=True)
    # A real but throwaway QUORUM_HOME for the child: a harness carrying
    # quorum's own integration hooks *will* run `quorum task
    # hook-session-start` during the probe, and it must land here rather
    # than in the user's live home.
    scratch_home = scratch / "home"
    home_mod.scaffold(scratch_home)

    argv = build_harness_argv(harness, SMOKE_PROMPT)
    env = strip_actor_env({**os.environ, **harness.env, "QUORUM_HOME": str(scratch_home)})
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            # DEVNULL for a non-inject harness is the production shape: a real
            # run is a detached child whose stdin is already /dev/null. An
            # inject harness gets the pipe the pump writes its turns to.
            stdin=subprocess.PIPE if harness.inject else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=env,
            # Its own session, so a timeout can kill the whole tree
            # (`_kill_process_group`) instead of one wrapper process.
            start_new_session=True,
        )
    except OSError as e:
        return [
            problem(
                f"smoke.{name}.run",
                f"could not start harness {name!r}: {_oneline(e)}",
                f"run it by hand: {shlex.join(argv)}",
            )
        ]

    session: str | None = None
    saw_result = False
    events = 0
    pump = None  # set by the `with` below, before the reader thread starts

    def on_event(event: object) -> None:
        nonlocal session, saw_result, events
        events += 1
        if isinstance(event, dict):
            if session is None:
                session = _find_session_id(event)
            if event.get("type") == "result":
                saw_result = True
        if pump is not None:
            pump.on_event(event)

    started = time.monotonic()
    timed_out = False
    with guidance_pump(scratch_home, SMOKE_INBOX, harness, proc, SMOKE_PROMPT) as active:
        pump = active
        # stream_transcript blocks on stdout, and an inject harness only ends
        # when the pump closes stdin — so the timeout has to live out here,
        # which is also the only way this probe can observe a hang at all.
        reader = threading.Thread(
            target=stream_transcript,
            args=(proc, scratch / "transcript.jsonl"),
            kwargs={"on_event": on_event},
            daemon=True,
        )
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            timed_out = True
            _kill_process_group(proc)
            reader.join(5)
    elapsed = time.monotonic() - started
    try:
        exit_code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        exit_code = None

    inject_hint = (
        'if this CLI speaks stream-json, set inject = "stream-json" in '
        f"[harness.{name}] — a stream-json CLI ignores an argv prompt and waits "
        "on stdin forever"
    )
    checks: list[Check] = []
    if timed_out:
        checks.append(
            problem(
                f"smoke.{name}.run",
                f"harness {name!r} produced no ending within {timeout:g}s and was killed "
                f"({events} event(s) seen)",
                inject_hint,
            )
        )
    elif exit_code:
        checks.append(
            problem(
                f"smoke.{name}.run",
                f"harness {name!r} exited {exit_code} after {elapsed:.1f}s",
                f"run it by hand to see why: {shlex.join(argv)}",
            )
        )
    else:
        checks.append(
            ok(
                f"smoke.{name}.run",
                f"harness {name!r} ran and exited 0 in {elapsed:.1f}s ({events} event(s))",
            )
        )

    if saw_result:
        checks.append(ok(f"smoke.{name}.result", f"harness {name!r} emitted a result event"))
    else:
        checks.append(
            problem(
                f"smoke.{name}.result",
                f"harness {name!r} emitted no result event — quorum cannot tell when a "
                "run finished, and records no usage for it",
                inject_hint,
            )
        )

    if session:
        checks.append(
            ok(f"smoke.{name}.session", f"harness {name!r} reported session id {session}")
        )
    elif harness.resume:
        checks.append(
            problem(
                f"smoke.{name}.session",
                f"harness {name!r} reported no session/thread id, but [harness.{name}] "
                "has a `resume` template that needs one",
                "check the argv emits JSON events (claude: --output-format stream-json, "
                "codex: --json), or drop `resume`",
            )
        )
    else:
        checks.append(
            na(
                f"smoke.{name}.session",
                f"harness {name!r} reported no session/thread id",
                f"only matters if you add a `resume` template to [harness.{name}]",
            )
        )
    return checks


# -- the whole pass ----------------------------------------------------------


def run_checks(
    home: Path,
    *,
    smoke: str | None = None,
    smoke_timeout: float = DEFAULT_SMOKE_TIMEOUT,
) -> list[Check]:
    """Every static check, in reading order; `smoke` adds the active probe.

    `smoke` is the harness name, `""` for [tasks].default_harness, or None
    for "do not run it".
    """
    home = Path(home)
    home_check = check_home(home)
    if home_check.status == PROBLEM:
        return [home_check]
    config_check, config = check_config(home)
    checks = [home_check, config_check, check_git()]
    if config is None:
        # Nothing below can be trusted: every reader of an unloadable config
        # is looking at defaults, so reporting on those would be fiction —
        # the smoke run included, since there is no harness table to run.
        # Say so out loud: a `--smoke` that silently never ran reads exactly
        # like one that passed.
        if smoke is not None:
            checks.append(
                na(
                    "smoke",
                    "smoke skipped: config failed to load, so there is no harness to run",
                    "fix config.toml (above), then re-run with --smoke",
                )
            )
        return checks
    checks += check_harnesses(config)
    if config.harness:
        # With no [harness.*] table at all, check_harnesses already said the
        # whole of it — a second line about default_harness is the same news
        # twice.
        checks.append(check_default_harness(config))
    checks += check_projects(home)
    checks.append(check_gh(home, config))
    checks.append(check_herdr(config))
    checks.append(check_notify(config))
    checks.append(check_sandbox(config))
    checks += check_prompts(home)
    checks.append(check_supervisor(home))
    checks.append(check_version(home))
    checks += check_runner_locks(home)
    checks.append(check_stale_claims(home))
    checks += check_heartbeats(home, config)
    checks += check_dials(home, config)
    if smoke is not None:
        checks += smoke_checks(home, config, smoke, smoke_timeout)
    return checks


def tally(checks: list[Check]) -> dict[str, int]:
    return {
        "ok": sum(1 for c in checks if c.status == OK),
        "problems": sum(1 for c in checks if c.status == PROBLEM),
        "na": sum(1 for c in checks if c.status == NA),
    }


def report(home: Path, checks: list[Check]) -> dict[str, Any]:
    """The `--json` shape: the same lines a human sees, plus the tally."""
    return {"home": str(home), "checks": [c.as_dict() for c in checks], **tally(checks)}
