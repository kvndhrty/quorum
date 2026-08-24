#!/usr/bin/env python3
"""A fake coding-harness CLI for tests.

Invoked the way quorum invokes any harness: the prompt arrives as the last
argv element (or after a flag; we just take the longest argument). Behavior
is controlled by env vars:

  FAKE_HARNESS_MODE    = echo (default) | report | fail
  FAKE_HARNESS_STATUS  = status word used in report mode (default "done")
  FAKE_HARNESS_PR_URL  = optional --pr-url used in report mode

* echo:   prints its argv as JSON (so tests can assert on start vs resume
          templates), a session_id event, and the prompt line by line.
* report: additionally extracts the task id from the prompt's "Task ID:"
          line and calls `python -m quorum task report` against
          $QUORUM_HOME — exercising the full cooperative return channel.
* fail:   exits 3 without output.
"""

import json
import os
import re
import subprocess
import sys


def main() -> int:
    mode = os.environ.get("FAKE_HARNESS_MODE", "echo")
    if mode == "fail":
        return 3
    prompt = max(sys.argv[1:], key=len) if len(sys.argv) > 1 else ""
    print(json.dumps({"argv": sys.argv[1:]}))
    print(json.dumps({"type": "system", "session_id": "sess-fake-123"}))
    for line in prompt.splitlines():
        print(f"PROMPT| {line}")
    print(f"CWD| {os.getcwd()}")
    if mode == "report":
        m = re.search(r"Task ID: (\S+)", prompt)
        if not m:
            print("no task id found in prompt", file=sys.stderr)
            return 4
        argv = [
            sys.executable, "-m", "quorum", "task", "report", m.group(1),
            "--status", os.environ.get("FAKE_HARNESS_STATUS", "done"),
            "finished by fake harness",
        ]
        pr_url = os.environ.get("FAKE_HARNESS_PR_URL")
        if pr_url:
            argv[-1:-1] = ["--pr-url", pr_url]
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            print(f"report failed: {done.stderr}", file=sys.stderr)
            return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
