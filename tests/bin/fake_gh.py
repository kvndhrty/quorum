#!/usr/bin/env python3
"""A fake `gh` CLI for tests, in the tests/bin/ idiom.

`quorum.ci` invokes `gh pr view --json <fields>` inside a task's workdir and
must survive every way the real thing can disappoint it. Behavior is env-driven:

  FAKE_GH_MODE
    pr (default)  print FAKE_GH_PR_JSON (the `gh pr view --json` payload)
    nopr          exit 1 the way gh does for a branch with no pull request
    unauth        exit 4 with an authentication error
    garbage       exit 0 printing something that is not JSON
    hang          sleep far past any probe timeout

  FAKE_GH_PR_JSON   the JSON body for `pr` mode
  FAKE_GH_LOG       file to append each invocation's argv to, one JSON list
                    per line (so a test can assert the probe ran, or didn't)
"""

import json
import os
import sys
import time


def main() -> int:
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")
    mode = os.environ.get("FAKE_GH_MODE", "pr")
    if mode == "nopr":
        print("no pull requests found for branch", file=sys.stderr)
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
    print(os.environ.get("FAKE_GH_PR_JSON", "{}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
