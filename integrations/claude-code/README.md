# quorum × Claude Code

Adopt a live Claude Code session into quorum, mid-problem. After adoption
the session shows up as an *attached* task: quorum's manager observes it
(liveness, git state, reports) and can nudge it — guidance queued with
`quorum task nudge` is delivered *inside* the session by the Stop hook the
next time Claude stops, as an instruction to continue.

Requires the `quorum` CLI on PATH and an initialized home (`quorum init`).
If you use a non-default `QUORUM_HOME`, export it in the shell you launch
`claude` from — the hooks resolve the home the same way the CLI does.

## Install as a plugin

From a checkout of this repo:

```bash
claude plugin install /path/to/quorum/integrations/claude-code
```

Then, inside any session you want supervised:

```
/quorum:adopt refactoring the auth flow
```

The argument (optional) becomes the task's description. Adoption
auto-registers the working directory as a quorum project if it isn't one.

## Manual install (no plugin)

Copy the slash command:

```bash
mkdir -p ~/.claude/commands
cp integrations/claude-code/commands/adopt.md ~/.claude/commands/quorum-adopt.md
```

and add the hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "quorum task hook-stop" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "quorum task hook-session-end" }] }
    ]
  }
}
```

The hooks are global but cheap: for sessions that were never adopted they
read stdin, find no matching attached task, and exit 0 silently.

## How it works

- `/quorum:adopt` runs `quorum task adopt --session $CLAUDE_SESSION_ID`,
  which creates an attached task pointing at the session's own directory
  (no worktree, no quorum-spawned runs — the runner refuses attached tasks).
- Every Stop, `quorum task hook-stop` refreshes the task's liveness record
  (`tasks/<id>/attached.json`) and claims any pending guidance from the
  task's inbox; if there is any, it emits `{"decision": "block", "reason":
  …}` so the session continues with the guidance. Delivery consumes the
  guidance, so this can't loop.
- `SessionEnd` records that the session ended; the task stays attached
  (sessions get reopened). `quorum task detach <id>` hands the task back to
  the ordinary headless runner; report protocol (`quorum task report`) works
  from inside the session at any time.
