"""Optional CI/PR observation: the only module that shells out to `gh`.

Quorum's ground-truth instinct used to stop at "pushed": `workdir_git_state`
sees a dirty or unpushed worktree, and nothing looked at what happened
*after* the push. A task can report `done` over a branch whose checks are
red — the failure mode the field literature keeps finding, because agents do
not reliably self-detect failure. This module closes that gap by *observing*
the pull request a task's branch belongs to: its state, its check rollup,
and whether it has a merge conflict.

It is observation enrichment, nothing more. Deciding what to do about red CI
stays in prompts (`prompts/manager.md`, or a babysitter prompt agent) —
there is no Python here that nudges, relaunches, or blocks anything.

Like `herdr.py` and unlike `sandbox.py`, this module **fails soft**: no `gh`
on PATH, no authentication, no remote, no GitHub, no PR for the branch, a
slow network, or an unexpected JSON shape all degrade to `None`. A digest
must never fail to build because a probe could not reach a forge.

Cost note: each probe is one `gh` subprocess making a network call, run once
per digested task per manager tick. `[ci].enabled = false` turns the whole
thing off; `[ci].timeout_seconds` bounds one call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 10.0
# The digest line names failing checks; a PR with fifty red checks must not
# turn one line into a wall.
MAX_FAILING_NAMES = 5

# What we ask GitHub for. `mergeable` (MERGEABLE/CONFLICTING/UNKNOWN) is the
# merge-conflict signal; `statusCheckRollup` carries both CheckRun (Actions)
# and StatusContext (classic status API) entries, which report their verdict
# in different fields — see `_verdict`.
PR_FIELDS = "number,url,state,isDraft,mergeable,statusCheckRollup"

FAILING_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
FAILING_STATES = frozenset({"FAILURE", "ERROR"})
PENDING_STATES = frozenset({"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"})

# gh is interactive by default in ways an unattended probe must refuse: it
# pages output, colorizes it, and can block on an auth prompt forever.
GH_ENV = {
    "GH_PAGER": "cat",
    "PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
}


def _config(home: Path):
    """The [ci] table, or None when there is no readable config; never raises.

    None means *disabled*, not "defaults". A config.toml quorum cannot parse
    may well be the one carrying `[ci].enabled = false`, and this probe is
    the kind that spends a subprocess and a network call: when in doubt it
    must degrade toward doing less, never toward doing more behind the
    user's back (`config.try_load_config` documents the policy).
    """
    from .config import try_load_config

    config = try_load_config(Path(home))
    return config.ci if config is not None else None


def available(home: Path) -> bool:
    """True when a probe could plausibly work: enabled, and `gh` on PATH."""
    cfg = _config(home)
    if cfg is None or not cfg.enabled:
        return False
    try:
        return shutil.which("gh") is not None
    except OSError:
        return False


def _timeout(home: Path) -> float:
    cfg = _config(home)
    return cfg.timeout_seconds if cfg is not None else DEFAULT_TIMEOUT_SECONDS


def _gh(home: Path, workdir: Path, *args: str) -> object | None:
    """One `gh ... --json` call inside `workdir`; any failure → None.

    Nonzero exit is the common, uninteresting case (no PR for this branch,
    no remote, not logged in) — silence, not an error.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_timeout(home),
            env={**os.environ, **GH_ENV},
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        # Fail-soft like herdr.py's bare except: timeouts and exec errors are
        # the usual suspects, but text=True can also raise UnicodeDecodeError
        # on non-UTF-8 output, and none of them may break a digest.
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def _verdict(check: dict) -> str:
    """Classify one rollup entry as pass / fail / pending.

    Two shapes arrive in the same list: a CheckRun reports `status`
    (QUEUED/IN_PROGRESS/COMPLETED) plus `conclusion`, a StatusContext reports
    only `state`. Anything not recognizably finished counts as pending — an
    unknown shape must never be read as a pass.
    """
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or "").upper()
    state = str(check.get("state") or "").upper()
    if conclusion in FAILING_CONCLUSIONS or state in FAILING_STATES:
        return "fail"
    if state in PENDING_STATES:
        return "pending"
    if status and status != "COMPLETED":
        return "pending"
    if not conclusion and not state:
        return "pending"
    return "pass"


def _summarize(pr: object) -> dict | None:
    if not isinstance(pr, dict):
        return None
    counts = {"pass": 0, "fail": 0, "pending": 0}
    failing: list[str] = []
    rollup = pr.get("statusCheckRollup")
    for check in rollup if isinstance(rollup, list) else []:
        if not isinstance(check, dict):
            continue
        verdict = _verdict(check)
        counts[verdict] += 1
        if verdict == "fail" and len(failing) < MAX_FAILING_NAMES:
            failing.append(str(check.get("name") or check.get("context") or "check"))
    if counts["fail"]:
        summary = "failing"
    elif counts["pending"]:
        summary = "pending"
    elif counts["pass"]:
        summary = "passing"
    else:
        summary = "none"
    mergeable = str(pr.get("mergeable") or "UNKNOWN").upper()
    number = pr.get("number")
    return {
        "pr": number if isinstance(number, int) else None,
        "url": str(pr.get("url") or ""),
        "state": str(pr.get("state") or "UNKNOWN").upper(),
        "draft": bool(pr.get("isDraft")),
        "summary": summary,
        "counts": counts,
        "failing": failing,
        "mergeable": mergeable,
        "conflict": mergeable == "CONFLICTING",
    }


def probeable(task) -> Path | None:
    """The task's workdir as an existing directory, or None.

    A queued task has no workdir yet and a cleaned-up one may have lost it;
    neither is worth a subprocess (or a slot of the digest's probe budget).
    """
    workdir = getattr(task, "workdir", "") or ""
    if not workdir:
        return None
    directory = Path(workdir)
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    return directory


def pr_state(home: Path, task) -> dict | None:
    """The pull request for the task's current branch, or None.

    `gh pr view` resolves the repository from the workdir's remote and the
    PR from its checked-out branch, which is exactly what a task worktree
    (branch `quorum/<short-id>`) or an adopted checkout provides. None means
    "nothing to say" for every reason — probe disabled, no gh, no auth, no
    remote, no PR yet, timeout, garbage output.
    """
    directory = probeable(task)
    if directory is None:
        return None
    if not available(home):
        return None
    return _summarize(_gh(home, directory, "pr", "view", "--json", PR_FIELDS))


def describe(state: dict) -> str:
    """One greppable `key=value` line's worth of PR/check state."""
    parts = [f"pr=#{state['pr']}" if state.get("pr") else "pr=?", f"state={state['state']}"]
    if state.get("draft"):
        parts.append("draft")
    counts = state["counts"]
    parts.append(
        f"checks={state['summary']} pass={counts['pass']} "
        f"fail={counts['fail']} pending={counts['pending']}"
    )
    if state["failing"]:
        parts.append("failing=" + ",".join(state["failing"]))
    if state["conflict"]:
        parts.append("MERGE-CONFLICT")
    if state["url"]:
        parts.append(f"url={state['url']}")
    return " ".join(parts)
