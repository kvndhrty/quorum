---
description: Adopt this session into quorum as an attached task (its manager can then observe and nudge you here)
argument-hint: [what this session is working on]
---

Adopt the current session into quorum by running this shell command:

    quorum task adopt "$ARGUMENTS" --json

Report the outcome to the user in one or two sentences: the attached task's
short id (from the JSON output), and that quorum's manager will now observe
this session and may deliver guidance here whenever you stop — such guidance
arrives as an instruction to continue and should be followed like a user
message. If the command failed, show its error instead and suggest checking
that `quorum init` has been run and `QUORUM_HOME` (if customized) is set in
the environment Codex was launched from.

Codex prompts cannot see their own session id; quorum's SessionStart/Stop
hooks associate it with the adopted task automatically (matched by working
directory) the next time they fire.
