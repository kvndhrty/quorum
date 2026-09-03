"""Optional CI/PR observation: the digest's read of a task's pull request.

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

The subprocess lives in `forge.py`, the one module that invokes a forge CLI;
this file is the digest-facing half over it. Like `herdr.py` and unlike
`sandbox.py`, that half **fails soft**: no `gh` on PATH, no authentication,
no remote, no GitHub, no PR for the branch, a slow network, or an unexpected
JSON shape all degrade to `None`. A digest must never fail to build because
a probe could not reach a forge.

Cost note: each probe is one forge-CLI subprocess making a network call, run
once per digested task per manager tick. `[ci].enabled = false` turns the
whole thing off; `[ci].timeout_seconds` bounds one call.
"""

from __future__ import annotations

from pathlib import Path

# The forge CLI itself is `forge.py`'s business: `available` answers "is a
# probe worth a subprocess" for the digest's budget, `run_json` is the
# fail-soft call. Re-exported rather than wrapped so there is one
# implementation and callers (the manager tick) keep one import.
from .forge import available, run_json

# The digest line names failing checks; a PR with fifty red checks must not
# turn one line into a wall.
MAX_FAILING_NAMES = 5

# What we ask GitHub for. `mergeable` (MERGEABLE/CONFLICTING/UNKNOWN) is the
# merge-conflict signal; `statusCheckRollup` carries both CheckRun (Actions)
# and StatusContext (classic status API) entries, which report their verdict
# in different fields — see `_verdict`.
PR_FIELDS = "number,url,state,isDraft,mergeable,statusCheckRollup"

# The forge's word for the pull request itself, normalized to the small
# vocabulary `tasks.PR_STATES` persists (#57). GitHub says OPEN / MERGED /
# CLOSED; GitLab (#51) says opened / merged / closed. The names are
# forge-neutral on purpose — a second backend fills the same field rather
# than teaching every reader a second dialect. Anything unrecognized stays
# "unknown" and is never written to disk: a shape we do not understand must
# not be quietly read as one of the three.
PR_STATE_WORDS = {
    "OPEN": "open",
    "OPENED": "open",
    "MERGED": "merged",
    "CLOSED": "closed",
    "LOCKED": "closed",
}
UNKNOWN_PR_STATE = "unknown"

FAILING_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
FAILING_STATES = frozenset({"FAILURE", "ERROR"})
PENDING_STATES = frozenset({"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"})


def normalize_state(raw: object) -> str:
    """A forge's PR state as one of `open` / `merged` / `closed`.

    `unknown` for anything else, including absence: this is the value that
    reaches `task.json`, and a wrong guess there would badge an unfinished
    PR as delivered forever (nothing re-probes a task once its worktree is
    gone).
    """
    return PR_STATE_WORDS.get(str(raw or "").strip().upper(), UNKNOWN_PR_STATE)


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
    state = normalize_state(pr.get("state"))
    return {
        "pr": number if isinstance(number, int) else None,
        "url": str(pr.get("url") or ""),
        "state": state,
        # The delivered/not-delivered read, hoisted out of the string so no
        # caller has to know the vocabulary: `done` is the harness's word,
        # merged is the forge's, and only the second one means shipped.
        "merged": state == "merged",
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
    return _summarize(run_json(home, directory, "pr", "view", "--json", PR_FIELDS))


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
