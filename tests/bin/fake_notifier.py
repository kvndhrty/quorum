#!/usr/bin/env python3
"""A fake notification command for tests, in the tests/bin/ idiom.

`quorum.notify` runs the `[notify].command` template once per new board
message and must survive every way the real thing (terminal-notifier, ntfy,
curl) can disappoint it. Behavior is env-driven:

  FAKE_NOTIFY_MODE
    ok (default)  exit 0
    fail          exit 3 with a line on stderr
    hang          sleep far past any timeout

  FAKE_NOTIFY_LOG   file to append each invocation's argv to, one JSON list
                    per line (so a test can assert what was delivered, in
                    what order, and how many times)
"""

import json
import os
import sys
import time


def main() -> int:
    log = os.environ.get("FAKE_NOTIFY_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")
    mode = os.environ.get("FAKE_NOTIFY_MODE", "ok")
    if mode == "fail":
        print("notifier: no display available", file=sys.stderr)
        return 3
    if mode == "hang":
        time.sleep(120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
