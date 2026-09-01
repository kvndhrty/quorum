#!/usr/bin/env python3
"""A fake coding-harness CLI for tests.

Invoked the way quorum invokes any harness: the prompt arrives as the last
argv element (we take the longest argument), except in inject mode, where —
like the real stream-json CLIs — it arrives on stdin. Behavior is controlled by env
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
    agent_act       echo + act like a generic prompt agent: post a board note,
                    then journal a reasoning note — two capped actions, so a
                    cap of 1 provably refuses the second
    manager_chain   echo + act like a manager that obeys the dependency rule
                    in prompts/manager.md: launch every queued task whose
                    digest line has no `waiting-on=`, and print a SKIP line
                    for the ones that have
    manager_flood   echo + nudge the first task repeatedly until the CLI's
                    per-run action cap refuses; print the refusal
    manager_remember  echo + write one standing note into the manager's
                    notebook (FAKE_HARNESS_NOTE), which the *next* tick's
                    digest must render back
    inject          speak the real stream-json protocol, like claude does:
                    the prompt arrives as the *first user turn on stdin*
                    (never via argv — the real CLI ignores an argv prompt in
                    this mode, the regression that motivated this fidelity);
                    echo it, emit a `result` event per turn, and echo every
                    further stdin line back as an event; exits when the
                    runner closes stdin. Before the first result it can seed
                    its own mid-run message (FAKE_HARNESS_INJECT_POST
                    below), which makes pump tests deterministic: the
                    message provably lands *during* the run, yet before the
                    runner could close an idle stdin.
    hang            sleep far past any test timeout (exercises run timeouts)
    fail            exit 3 without output

  FAKE_HARNESS_USAGE   cost in USD (e.g. "0.42"); makes the harness report
                       token/cost usage the way a real one does — a `result`
                       event carrying total_cost_usd and a usage block
                       (11,000 tokens' worth). Unset (the default) is the
                       harness that reports nothing at all, which quorum
                       must keep supporting.
  FAKE_HARNESS_STATUS / FAKE_HARNESS_PR_URL   report-mode knobs
  FAKE_HARNESS_WRITE   name of a file to create in the cwd before exiting,
                       i.e. leave the working tree dirty the way a harness
                       that crashed (or ignored the delivery protocol) does
  FAKE_HARNESS_INJECT_POST   inject-mode knob: "nudge" sends `task nudge` to
                             its own task, "tell" sends `manager tell`
  FAKE_HARNESS_NOTE    manager_remember mode: the text to remember
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

# The token split of a usage-reporting run: 11,000 tokens all told, so a
# test can set a budget on either side of it.
USAGE_TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_read_input_tokens": 9000,
    "cache_creation_input_tokens": 500,
}


def usage_block() -> dict | None:
    """The usage fields of a `result` event, or None when the harness is
    playing a harness that reports nothing."""
    cost = os.environ.get("FAKE_HARNESS_USAGE")
    if not cost:
        return None
    return {"total_cost_usd": float(cost), "usage": dict(USAGE_TOKENS)}


def quorum(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "quorum", *args], capture_output=True, text=True
    )


def task_id_from(prompt: str) -> str | None:
    m = re.search(r"Task ID: (\S+)", prompt)
    return m.group(1) if m else None


def inject_main() -> int:
    print(json.dumps({"argv": sys.argv[1:]}))
    print(json.dumps({"type": "system", "session_id": "sess-fake-123"}), flush=True)
    # If the close logic ever regresses, die loudly instead of wedging CI.
    watchdog = threading.Timer(30, lambda: os._exit(7))
    watchdog.daemon = True
    watchdog.start()
    first = sys.stdin.readline().strip()
    if not first:
        print("no prompt turn arrived on stdin", file=sys.stderr)
        return 4
    content = json.loads(first).get("message", {}).get("content", [])
    prompt = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    for line in prompt.splitlines():
        print(f"PROMPT| {line}")
    print(f"CWD| {os.getcwd()}")

    post = os.environ.get("FAKE_HARNESS_INJECT_POST", "")
    if post == "nudge":
        task_id = task_id_from(prompt)
        if not task_id:
            print("no task id found in prompt", file=sys.stderr)
            return 4
        quorum("task", "nudge", task_id, "switch to the fallback plan")
    elif post == "tell":
        quorum("manager", "tell", "pause new launches until tests pass")
    result = {"type": "result", "subtype": "success", **(usage_block() or {})}
    print(json.dumps(result), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        print(json.dumps({"type": "stdin", "received": json.loads(line)}), flush=True)
        print(json.dumps(result), flush=True)
    return 0


def main() -> int:
    mode = os.environ.get("FAKE_HARNESS_MODE", "echo")
    if mode == "fail":
        return 3
    if mode == "hang":
        time.sleep(120)
        return 0
    if mode == "inject":
        return inject_main()
    prompt = max(sys.argv[1:], key=len) if len(sys.argv) > 1 else ""
    scratch = os.environ.get("FAKE_HARNESS_WRITE")
    if scratch:
        with open(scratch, "w") as fh:
            fh.write("work the harness never committed\n")
    print(json.dumps({"argv": sys.argv[1:]}))
    print(json.dumps({"type": "system", "session_id": "sess-fake-123"}))
    for line in prompt.splitlines():
        print(f"PROMPT| {line}")
    print(f"CWD| {os.getcwd()}")
    reported_usage = usage_block()
    if reported_usage:
        print(json.dumps({"type": "result", "subtype": "success", **reported_usage}))

    if mode == "report":
        task_id = task_id_from(prompt)
        if not task_id:
            print("no task id found in prompt", file=sys.stderr)
            return 4
        argv = ["task", "report", task_id,
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

    elif mode == "manager_chain":
        for line in re.findall(r"- \[queued\] .*", prompt):
            target = line.split()[2]
            if "waiting-on=" in line:
                print(f"SKIP| {target} waiting on its dependencies")
                continue
            ran = quorum("task", "run", target)
            print(f"ACT| task run {target} -> exit {ran.returncode}")
            if ran.returncode != 0:
                print(f"REFUSED| {ran.stderr.strip().splitlines()[-1] if ran.stderr.strip() else 'refused'}")

    elif mode == "agent_act":
        posted = quorum("board", "post", "notes", "hello from the prompt agent")
        print(f"ACT| board post -> exit {posted.returncode}")
        noted = quorum("manager", "note", "prompt agent reasoning note")
        print(f"ACT| note -> exit {noted.returncode}")
        if noted.returncode != 0 and noted.stderr.strip():
            print(f"REFUSED| {noted.stderr.strip().splitlines()[0]}")

    elif mode == "manager_remember":
        note = os.environ.get("FAKE_HARNESS_NOTE", "a standing fact for next time")
        r = quorum("manager", "remember", note)
        print(f"ACT| remember -> exit {r.returncode}")
        if r.returncode != 0 and r.stderr.strip():
            print(f"REFUSED| {r.stderr.strip().splitlines()[0]}")

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
