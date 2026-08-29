# quorum × opencode

Adopt a live opencode session into quorum, mid-problem. After adoption the
session shows up as an *attached* task: quorum's manager observes it
(liveness, git state, reports) and can nudge it — guidance queued with
`quorum task nudge` is injected *into* the session as a user turn the next
time it goes idle.

Unlike Claude Code and Codex, opencode has no hook-command protocol; its
extension surface is an in-process JS plugin with an event bus and an SDK
client. The shipped plugin is a dumb pipe: every decision lives in the same
quorum CLI entry points the other harnesses use (`task adopt`,
`task hook-stop --format text`, `task hook-session-end`) — the plugin only
translates events and injects what the CLI prints. Requires the `quorum`
CLI on PATH and an initialized home (`quorum init`); if you use a
non-default `QUORUM_HOME`, export it in the shell you launch `opencode`
from.

## Install

From an installed package (no checkout needed):

```bash
quorum integration install opencode
```

Or copy the plugin and command by hand — global:

```bash
mkdir -p ~/.config/opencode/plugins ~/.config/opencode/commands
cp integrations/opencode/plugin/quorum.js ~/.config/opencode/plugins/
cp integrations/opencode/commands/quorum-adopt.md ~/.config/opencode/commands/
```

or per-project (only sessions in that checkout are observed): same files
under `<repo>/.opencode/plugins/` and `<repo>/.opencode/commands/`.

## Adopt a session

Inside an opencode session:

```
/quorum-adopt refactoring the auth flow
```

The argument (optional) becomes the task's description. The plugin
intercepts the command, runs `quorum task adopt --session <id> --dir <cwd>`
itself, and replaces the prompt with a short report instruction. Adoption
auto-registers the working directory as a quorum project if it isn't one.
(Without the plugin the command still works as plain instructions to the
agent, minus the session id — it's then learned at the next idle by
working-directory match.)

## How it works

- On every `session.status` **idle** event (the "agent finished a turn"
  signal), the plugin calls `quorum task hook-stop --format text` with the
  session id and directory. That refreshes the task's liveness record
  (`tasks/<id>/attached.json`) and claims pending guidance from the task's
  inbox; whatever the CLI prints, the plugin injects as a new user turn via
  `client.session.prompt` — the injected turn's own idle then finds an empty
  inbox, so delivery can't loop.
- Plugin `dispose()` (instance shutdown) records session-end; the task stays
  attached (sessions get reopened). There is no true SessionEnd event in
  opencode, so treat liveness as best-effort between idles.
- `quorum task detach <id>` hands the task back to the ordinary headless
  runner; the report protocol (`quorum task report`) works from inside the
  session at any time.

The plugin runs in-process, so none of this needs the opencode HTTP server
exposed (`--port` / `opencode serve`) — external processes still can't
reach a default TUI session, and don't need to.
