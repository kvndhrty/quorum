---
description: Adopt this session into quorum as an attached task (its manager can then observe and nudge you here)
allowed-tools: Bash(quorum:*)
---

Adopt the current session into quorum by running:

!`quorum task adopt "$ARGUMENTS" --session "${CLAUDE_SESSION_ID:-}" --json`

Report the outcome to the user in one or two sentences: the attached task's
short id (from the JSON above), and that quorum's manager will now observe
this session and may deliver guidance here whenever you stop — such guidance
arrives as an instruction to continue and should be followed like a user
message. If the command failed, show its error instead and suggest checking
that `quorum init` has been run and `QUORUM_HOME` (if customized) is
exported in this shell's environment.

If the user provided arguments, they were passed as the task's description;
otherwise quorum described the task from the working directory.
