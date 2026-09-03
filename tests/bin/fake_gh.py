#!/usr/bin/env python3
"""A fake `gh` CLI for tests, in the tests/bin/ idiom.

`quorum.forge` is the only module that invokes a forge CLI, and it does so
in two shapes that must both survive every way the real thing can
disappoint them: the fail-soft `pr view` probe behind `quorum.ci`, and the
loud `issue view` behind `task add --issue`. Behavior is env-driven:

  FAKE_GH_MODE
    ok (default)  print the payload for the subcommand: FAKE_GH_PR_JSON for
                  `pr view`, FAKE_GH_ISSUE_JSON for `issue view`. `pr` is
                  kept as an alias, since most callers only want the probe.
    nopr          exit 1 the way gh does for a branch with no pull request
    noissue       exit 1 the way gh does for an issue that does not exist
    unauth        exit 4 with an authentication error
    garbage       exit 0 printing something that is not JSON
    hang          sleep far past any probe timeout

  FAKE_GH_PR_JSON      the JSON body for `pr view`
  FAKE_GH_ISSUE_JSON   the JSON body for `issue view`
  FAKE_GH_LOG          file to append each invocation's argv to, one JSON
                       list per line (so a test can assert the call ran, or
                       didn't, and with which arguments)
"""

import json
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    mode = os.environ.get("FAKE_GH_MODE", "ok")
    if mode == "nopr":
        print("no pull requests found for branch", file=sys.stderr)
        return 1
    if mode == "noissue":
        number = argv[2] if len(argv) > 2 else "?"
        print(
            f"GraphQL: Could not resolve to an Issue with the number of {number}. (repository.issue)",
            file=sys.stderr,
        )
        return 1
    if mode == "unauth":
        print("gh: To use GitHub CLI in a GitHub Actions workflow, set GH_TOKEN", file=sys.stderr)
        return 4
    if mode == "garbage":
        print("Showing 1 of 1 pull request")
        return 0
    if mode == "hang":
        time.sleep(120)
        return 0
    if argv[:1] == ["issue"]:
        print(os.environ.get("FAKE_GH_ISSUE_JSON", "{}"))
        return 0
    print(os.environ.get("FAKE_GH_PR_JSON", "{}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
