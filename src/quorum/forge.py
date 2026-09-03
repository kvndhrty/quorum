"""The only module that shells out to a forge CLI (`gh` today).

Three callers need the forge, and they want opposite failure behaviour:

- `ci.pr_state` observes the pull request behind a task's branch on every
  manager tick. It must **fail soft** — a digest may never fail to build
  because a probe could not reach a forge.
- `doctor.check_gh` asks whether a probe would get anywhere at all
  (`auth_status`), which is the same soft question phrased for a human.
- `task add --issue` fetches an issue to compose a prompt from
  (`issue_view`). That one runs in front of a person who typed a flag and
  is waiting, so it **fails loudly**: no `gh`, no auth, an unknown issue or
  a garbled reply each raise `ForgeError` naming the fix. Silently queuing
  a task with an empty prompt would be much worse than an error.

Both shapes share one subprocess site (`_run`) so there is exactly one
place that knows how to invoke a forge CLI unattended, and one place to
extend when a second provider lands.

Quorum only ever *reads* from a forge. There is no write path here — no
labelling, no comments, no closing an issue when a PR merges: the human
owns the issue and the pull request.

Config is the `[ci]` table (`enabled`, `timeout_seconds`), read through
`config.try_load_config`, so an unreadable config.toml means *off* rather
than "defaults" — the table quorum failed to parse may be the one holding
`enabled = false`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 10.0

# The one forge CLI quorum knows how to drive. **This is the seam for #51**
# (a `[ci].provider = gh | glab | none` switch): every subprocess in this
# module asks `cli_name(home)` for the binary, and every caller already
# routes through `available()`, so a second provider is a new branch here
# plus its own argv builders — not a new `gh` call somewhere else.
DEFAULT_PROVIDER_CLI = "gh"

# gh is interactive by default in ways an unattended call must refuse: it
# pages output, colorizes it, and can block on an auth prompt forever.
GH_ENV = {
    "GH_PAGER": "cat",
    "PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
}

# What we ask the forge for about one issue. Deliberately minimal: title and
# body are the prompt, url is what lands on the task record.
ISSUE_FIELDS = "number,title,body,url"

# `62`, `#62`, or a URL ending in `/issues/62` (GitHub) or `/-/issues/62`
# (GitLab). Anything else is refused before a subprocess runs, so a typo
# spends no network call and gets a specific message.
_NUMBER = re.compile(r"^#?(\d+)$")
_ISSUE_URL = re.compile(r"^https?://\S+/issues/(\d+)/?$")


class ForgeError(RuntimeError):
    """A forge call that a person is waiting on failed. The message names
    the fix; CLI callers print it as-is."""


def _config(home: Path):
    """The [ci] table, or None when there is no readable config; never raises.

    None means *disabled*, not "defaults" — see the module docstring and
    `config.try_load_config`.
    """
    from .config import try_load_config

    config = try_load_config(Path(home))
    return config.ci if config is not None else None


def cli_name(home: Path) -> str:
    """The forge CLI to invoke. The #51 seam: one function, one answer."""
    del home  # provider is not configurable yet — see DEFAULT_PROVIDER_CLI
    return DEFAULT_PROVIDER_CLI


def available(home: Path) -> bool:
    """True when a forge call could plausibly work: enabled, and the CLI is
    on PATH."""
    cfg = _config(home)
    if cfg is None or not cfg.enabled:
        return False
    try:
        return shutil.which(cli_name(home)) is not None
    except OSError:
        return False


def _timeout(home: Path) -> float:
    cfg = _config(home)
    return cfg.timeout_seconds if cfg is not None else DEFAULT_TIMEOUT_SECONDS


def _run(home: Path, workdir: Path | None, args: list[str]) -> subprocess.CompletedProcess | None:
    """One forge-CLI call, bounded by `[ci].timeout_seconds`. None when the
    process could not be run to completion at all.

    The single subprocess site of the whole codebase. Callers decide what a
    failure means: `run_json` degrades to None, `run_json_or_raise` raises.
    """
    try:
        return subprocess.run(
            [cli_name(home), *args],
            cwd=str(workdir) if workdir is not None else None,
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


def run_json(home: Path, workdir: Path, *args: str) -> object | None:
    """A `--json` call inside `workdir`; **any** failure → None.

    Nonzero exit is the common, uninteresting case for an observation (no PR
    for this branch, no remote, not logged in) — silence, not an error.
    """
    proc = _run(home, workdir, list(args))
    if proc is None or proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def auth_status(home: Path) -> bool | None:
    """Is the forge CLI authenticated? True / False / **None for "no answer"**.

    The soft entry point for `quorum doctor`: nothing outside this file may
    shell out to a forge CLI, so the question "would a call actually get
    anywhere?" has to be asked here too.

    None is not a failure — it is the probe declining to answer: `[ci]` off
    (or an unreadable config, which means the same thing here), no CLI on
    PATH, or a CLI that did not reply within `[ci].timeout_seconds`. An
    offline machine must not be reported as a broken one, so only an
    explicit non-zero exit from a CLI that *did* answer is False.
    """
    if not available(home):
        return None
    proc = _run(home, None, ["auth", "status"])
    if proc is None:
        return None
    return proc.returncode == 0


def issue_ref(ref: str) -> int:
    """`62` from `62`, `#62`, or an issue URL. Raises `ForgeError` otherwise.

    Pure parsing, so `task add --issue` rejects a typo before spending a
    subprocess — and so a URL for something that is not an issue (a PR, a
    discussion) is refused rather than fetched as one.
    """
    text = (ref or "").strip()
    for pattern in (_NUMBER, _ISSUE_URL):
        m = pattern.match(text)
        if m:
            return int(m.group(1))
    raise ForgeError(
        f"{ref!r} is not an issue: pass a number (62), #62, or a full issue URL "
        "(https://github.com/<owner>/<repo>/issues/62)"
    )


def issue_view(home: Path, ref: str, workdir: Path) -> dict:
    """One issue as `{number, title, body, url}`. **Raises** on every failure.

    `ref` is a number or a URL; a bare number resolves against the remote of
    `workdir` (a registered project's directory), exactly as `gh` does for a
    human standing in that checkout.

    The loud counterpart of `run_json`: this runs for `task add --issue`,
    where the alternative to an error is a task queued with a prompt that is
    missing the work. Every disappointment names its fix.
    """
    number = issue_ref(ref)
    cfg = _config(home)
    if cfg is None:
        raise ForgeError(
            "config.toml could not be read, so forge access is off — fix it "
            "(`quorum doctor`) or paste the issue text with `--prompt-file`"
        )
    if not cfg.enabled:
        raise ForgeError(
            "[ci].enabled = false in config.toml, so quorum will not call the forge — "
            "set it to true, or paste the issue text with `--prompt-file`"
        )
    cli = cli_name(home)
    if shutil.which(cli) is None:
        raise ForgeError(
            f"no `{cli}` on PATH — install it (brew install {cli}) and `{cli} auth login`, "
            "or paste the issue text with `--prompt-file`"
        )
    proc = _run(home, workdir, ["issue", "view", str(number), "--json", ISSUE_FIELDS])
    if proc is None:
        raise ForgeError(
            f"`{cli} issue view {number}` did not finish within "
            f"[ci].timeout_seconds ({cfg.timeout_seconds}s) — raise it or retry"
        )
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:300] or "no output"
        raise ForgeError(
            f"`{cli} issue view {number}` failed in {workdir}: {detail}\n"
            f"check the issue exists and `{cli} auth status` is happy, or pass the full "
            "issue URL"
        )
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        detail = " ".join((proc.stdout or "").split())[:200]
        raise ForgeError(
            f"`{cli} issue view {number}` did not return JSON: {detail!r}"
        ) from None
    if not isinstance(payload, dict) or not str(payload.get("url") or "").strip():
        raise ForgeError(
            f"`{cli} issue view {number}` returned no issue url — quorum needs one to "
            "record where the task came from"
        )
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ForgeError(f"issue #{number} has no title — nothing to make a prompt from")
    return {
        "number": payload.get("number") if isinstance(payload.get("number"), int) else number,
        "title": title,
        "body": str(payload.get("body") or "").strip(),
        "url": str(payload["url"]).strip(),
    }


def issue_prompt(issue: dict) -> str:
    """The task prompt an issue composes to: title, body, and the url.

    The url goes in the text itself (not only on the task record) because
    the harness reads the prompt and nothing else by default, and a task
    that cannot name the issue it came from cannot reference it in a PR.
    """
    parts = [issue["title"]]
    if issue.get("body"):
        parts.append(issue["body"])
    parts.append(f"({issue['url']})")
    return "\n\n".join(parts)
