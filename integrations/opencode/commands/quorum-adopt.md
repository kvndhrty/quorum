---
description: Adopt this session into quorum as an attached task (its manager can then observe and nudge you here)
---

Adopt the current session into quorum by running this shell command:

    quorum task adopt "$ARGUMENTS" --json

Report the outcome to the user in one or two sentences: the attached task's
short id (from the JSON output), and that quorum's manager will now observe
this session and may inject guidance here whenever you go idle — follow such
guidance like a user message. If the command failed, show its error instead
and suggest checking that `quorum init` has been run and `QUORUM_HOME` (if
customized) is set in the environment opencode was launched from.

(When the quorum plugin is installed it intercepts this command and runs the
adoption itself, with the session id attached; this body is the fallback —
adoption then starts id-less and the id is learned at the next idle.)
