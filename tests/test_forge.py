"""The forge seam: the only module that shells out to a forge CLI.

Two contracts in one file, on purpose — they are the reason `forge.py`
exists as a module of its own:

- `auth_status` is **soft** (the doctor's question, herdr's contract): three
  answers, and "no answer" is not a failure.
- `issue_view` is **loud** (`task add --issue` runs in front of a person):
  every disappointment raises `ForgeError` naming a fix, because the
  alternative is a task queued without the work in it.

The same fake `gh` (tests/bin/fake_gh.py, on a stripped PATH) plays both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import install_gh
from quorum import forge

ISSUE = {
    "number": 62,
    "title": "Issue intake: task add --issue",
    "body": "## Problem\n\nquorum does not know about issues.",
    "url": "https://github.com/kvndhrty/quorum/issues/62",
}


def read_log(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


# -- the soft half: auth_status ----------------------------------------------


def test_auth_status_answers_yes_no_or_nothing(home: Path, path_without_gh: Path, monkeypatch):
    """The doctor entry point. Three answers, and the third is not a failure:
    a gh that never replied says nothing about whether it is logged in."""
    assert forge.auth_status(home) is None  # no gh on PATH at all

    install_gh(path_without_gh, monkeypatch)  # exits 0
    assert forge.auth_status(home) is True

    install_gh(path_without_gh, monkeypatch, mode="unauth")
    assert forge.auth_status(home) is False

    (home / "config.toml").write_text("[ci]\ntimeout_seconds = 0.5\n")
    install_gh(path_without_gh, monkeypatch, mode="hang")
    assert forge.auth_status(home) is None  # offline is not unauthenticated


def test_auth_status_honours_the_same_ci_switches_as_the_probe(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)

    (home / "config.toml").write_text("[ci]\nenabled = false\n")
    assert forge.auth_status(home) is None
    assert not log.exists()  # disabled means no subprocess, exactly like pr_state

    (home / "config.toml").write_text("[ci]\nenabled = false\n[harness.broken\noops")
    assert forge.auth_status(home) is None
    assert not log.exists()  # and an unreadable config means off, never fail-open


# -- parsing a reference ----------------------------------------------------


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("62", 62),
        ("#62", 62),
        ("  62  ", 62),
        ("https://github.com/kvndhrty/quorum/issues/62", 62),
        ("https://github.com/kvndhrty/quorum/issues/62/", 62),
        ("https://gitlab.com/g/p/-/issues/7", 7),
    ],
)
def test_a_number_a_hash_or_a_url_all_name_an_issue(ref: str, expected: int):
    assert forge.issue_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "sixty-two",
        "62 and 63",
        "https://github.com/kvndhrty/quorum/pull/62",  # a PR is not an issue
        "https://github.com/kvndhrty/quorum/issues",
    ],
)
def test_anything_else_is_refused_before_a_subprocess(ref: str):
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_ref(ref)
    assert "issue URL" in str(e.value)  # the message names the fix


def test_a_bad_reference_never_spends_a_call(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, issue=ISSUE, log=log)
    with pytest.raises(forge.ForgeError):
        forge.issue_view(home, "not-an-issue", tmp_path)
    assert not log.exists()


# -- the loud half: issue_view ----------------------------------------------


def test_issue_view_reads_title_body_and_url(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, issue=ISSUE, log=log)

    issue = forge.issue_view(home, "62", tmp_path)
    assert issue == {
        "number": 62,
        "title": ISSUE["title"],
        "body": ISSUE["body"],
        "url": ISSUE["url"],
    }
    # one call, the number normalized, and only the four fields we need
    assert read_log(log) == [["issue", "view", "62", "--json", "number,title,body,url"]]


def test_a_url_resolves_to_the_same_call_as_its_number(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, issue=ISSUE, log=log)
    assert forge.issue_view(home, ISSUE["url"], tmp_path)["url"] == ISSUE["url"]
    assert read_log(log)[0][2] == "62"


def test_the_prompt_is_title_body_and_url(home: Path, tmp_path: Path, path_without_gh, monkeypatch):
    install_gh(path_without_gh, monkeypatch, issue=ISSUE)
    text = forge.issue_prompt(forge.issue_view(home, "62", tmp_path))
    assert text == f"{ISSUE['title']}\n\n{ISSUE['body']}\n\n({ISSUE['url']})"

    # a body-less issue still composes: title, then the url
    bodyless = dict(ISSUE, body="")
    install_gh(path_without_gh, monkeypatch, issue=bodyless)
    assert forge.issue_prompt(forge.issue_view(home, "62", tmp_path)) == (
        f"{ISSUE['title']}\n\n({ISSUE['url']})"
    )


def test_no_gh_on_path_is_an_error_naming_the_fix(home: Path, tmp_path: Path, path_without_gh):
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "no `gh` on PATH" in str(e.value)
    assert "--prompt-file" in str(e.value)  # the way to proceed without gh


def test_an_unknown_issue_is_an_error_quoting_the_cli(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch, mode="noissue")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "999999", tmp_path)
    assert "Could not resolve to an Issue" in str(e.value)


def test_an_unauthenticated_gh_is_an_error_not_a_shrug(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch, mode="unauth")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "auth status" in str(e.value)


def test_garbage_output_is_an_error(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch, mode="garbage")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "did not return JSON" in str(e.value)


def test_a_reply_without_a_url_or_title_is_an_error(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """The record has to carry a url, and the prompt has to carry a title —
    a task queued from half an issue is worse than no task."""
    install_gh(path_without_gh, monkeypatch, issue={"number": 62, "title": "t", "body": "b"})
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "issue url" in str(e.value)

    install_gh(path_without_gh, monkeypatch, issue=dict(ISSUE, title="  "))
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "no title" in str(e.value)


def test_a_hung_gh_is_an_error_naming_the_timeout(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    (home / "config.toml").write_text("[ci]\ntimeout_seconds = 0.5\n")
    install_gh(path_without_gh, monkeypatch, mode="hang")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "[ci].timeout_seconds" in str(e.value)


def test_the_ci_switches_turn_issue_intake_off_loudly(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """Same switches as the probe, opposite behaviour: the probe goes quiet,
    this says so, because someone typed --issue and is waiting."""
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, issue=ISSUE, log=log)

    (home / "config.toml").write_text("[ci]\nenabled = false\n")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "[ci].enabled = false" in str(e.value)
    assert not log.exists()  # disabled means no subprocess here either

    (home / "config.toml").write_text("[ci]\nenabled = false\n[harness.broken\noops")
    with pytest.raises(forge.ForgeError) as e:
        forge.issue_view(home, "62", tmp_path)
    assert "could not be read" in str(e.value)
    assert not log.exists()


def test_the_provider_is_one_named_seam(home: Path):
    """#51 (gh | glab | none) lands here and nowhere else: every subprocess
    in the module asks `cli_name` for its binary."""
    assert forge.cli_name(home) == "gh" == forge.DEFAULT_PROVIDER_CLI
