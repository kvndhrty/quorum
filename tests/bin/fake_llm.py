#!/usr/bin/env python3
"""A fake LLM CLI for tests.

Behavior is controlled by env vars:
  FAKE_LLM_MODE   = ok (default) | fail | timeout | echo
  FAKE_LLM_OUTPUT = text to print in ok mode
Prompt is read from stdin, or from argv when arguments are present.
"""

import os
import sys
import time


def main() -> int:
    mode = os.environ.get("FAKE_LLM_MODE", "ok")
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    if mode == "fail":
        print("synthetic failure", file=sys.stderr)
        return 2
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "echo":
        print(prompt)
        return 0
    print(os.environ.get("FAKE_LLM_OUTPUT", "This is a canned narrative summary."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
