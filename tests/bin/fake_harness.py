#!/usr/bin/env python3
"""A fake coding-harness CLI for tests.

Invoked the way quorum invokes any harness: the prompt arrives as the last
argv element (we take the longest argument). Behavior is controlled by env
vars — in tests each [harness.<name>] table pins its own mode via its `env`
field, so a fake *task* harness and a fake *manager* harness coexist:

  FAKE_HARNESS_MODE
    echo (default)  print argv as JSON, a session_id event, the prompt
                    line by line, and the cwd
    report          echo + extract the task id from the prompt's "Task ID:"
                    line and call `python -m quorum task report` — the full
                    cooperative return channel
    manager_act     echo + act like a manager: find the first queued task in
                    the digest, `task run` it (foreground, for determinism),
                    nudge it, and journal a note
    manager_flood   echo + nudge the first task repeatedly until the CLI's
                    per-run action cap refuses; print the refusal
    hang            sleep far past any test timeout (exercises run timeouts)
    fail            exit 3 without output

  FAKE_HARNESS_STATUS / FAKE_HARNESS_PR_URL   report-mode knobs
"""

import json
import os
import re
import subprocess
import sys
import time


def quorum(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "quorum", *args], capture_output=True, text=True
    )


def main() -> int:
    mode = os.environ.get("FAKE_HARNESS_MODE", "echo")
    if mode == "fail":
        return 3
    if mode == "hang":
        time.sleep(120)
        return 0
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
        argv = ["task", "report", m.group(1),
                "--status", os.environ.get("FAKE_HARNESS_STATUS", "done"),
                "finished by fake harness"]
        pr_url = os.environ.get("FAKE_HARNESS_PR_URL")
        if pr_url:
            argv[-1:-1] = ["--pr-url", pr_url]
        done = quorum(*argv)
        if done.returncode != 0:
            print(f"report failed: {done.stderr}", file=sys.stderr)
            return 5

    elif mode == "manager_act":
        queued = re.findall(r"- \[queued\] (\S+)", prompt)
        if queued:
            target = queued[0]
            ran = quorum("task", "run", target)
            print(f"ACT| task run {target} -> exit {ran.returncode}")
            nudged = quorum("task", "nudge", target, "keep going, you are on track")
            print(f"ACT| task nudge {target} -> exit {nudged.returncode}")
            noted = quorum("manager", "note", f"launched and nudged {target}")
            print(f"ACT| note -> exit {noted.returncode}")

    elif mode == "manager_flood":
        m = re.search(r"- \[\w+\] (\S+)", prompt)
        if not m:
            print("no task found to flood", file=sys.stderr)
            return 6
        target = m.group(1)
        for i in range(10):
            r = quorum("task", "nudge", target, f"redundant nudge {i}")
            if r.returncode != 0:
                print(f"REFUSED| {r.stderr.strip().splitlines()[0] if r.stderr.strip() else 'refused'}")
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
