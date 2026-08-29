# quorum × Codex CLI

Adopt a live Codex session into quorum, mid-problem. After adoption the
session shows up as an *attached* task: quorum's manager observes it
(liveness, git state, reports) and can nudge it — guidance queued with
`quorum task nudge` is delivered *inside* the session by the Stop hook the
next time Codex stops, as a continuation prompt.

Codex's hooks system speaks the same protocol as Claude Code's (stdin JSON
with `session_id`/`cwd`, Stop-hook `{"decision": "block", "reason": …}`), so
this adapter is the same three CLI entry points behind a Codex-flavored
config. Requires the `quorum` CLI on PATH and an initialized home
(`quorum init`); if you use a non-default `QUORUM_HOME`, export it in the
environment you launch `codex` from.

## Install

From an installed package (no checkout needed):

```bash
quorum integration install codex
```

Or copy the hooks config by hand — user-wide:

```bash
cp integrations/codex/hooks.json ~/.codex/hooks.json
```

or per-project (only sessions in that checkout are observed):

```bash
mkdir -p <repo>/.codex && cp integrations/codex/hooks.json <repo>/.codex/hooks.json
```

Note: project-scoped hooks need a recent Codex — as of 0.149.x only the
home-level `~/.codex/hooks.json` (or `$CODEX_HOME/hooks.json`) is
discovered; verified against 0.149.1, where the project `.codex/` forms are
silently ignored. If you already have a `hooks.json`, merge the three
entries into it instead.
Codex gates hooks behind one-time **trust**: the first session after
installing will ask you to trust the new hooks (the `/hooks` browser in the
TUI shows what's configured). Optionally add the adopt prompt:

```bash
mkdir -p ~/.codex/prompts && cp integrations/codex/prompts/quorum-adopt.md ~/.codex/prompts/
```

The hooks are global but cheap: for sessions that were never adopted they
read stdin, find no matching attached task, and exit 0 silently.

## Adopt a session

Inside a Codex session, run `/prompts:quorum-adopt refactoring the auth flow`
(the argument becomes the task description), which has Codex run
`quorum task adopt` itself — or from another terminal:

```bash
quorum task adopt "refactoring the auth flow" --dir /path/to/checkout
```

Codex prompts can't see their own session id, so adoption starts id-less;
the SessionStart/Stop hooks match by working directory and record the id on
their next firing (which also makes `codex resume <id>` / `codex exec resume
<id>` possible later). Adoption auto-registers the directory as a quorum
project if it isn't one.

## How it works

- Every **Stop**, `quorum task hook-stop` refreshes the task's liveness
  record (`tasks/<id>/attached.json`) and claims any pending guidance from
  the task's inbox; if there is any, it emits `{"decision": "block",
  "reason": …}` and Codex continues with the guidance as a new user prompt.
  Delivery consumes the guidance, so this can't loop (Codex's own
  `stop_hook_active` guard is redundant with that but harmless).
- **SessionStart** refreshes liveness and (re-)learns the session id — it
  fires on `resume` too, so a reopened session re-associates itself.
- **SessionEnd** records that the session ended; the task stays attached
  (sessions get reopened). Note Codex fires SessionEnd loosely — on close,
  archive, or ~30 minutes of idle — so treat it as a late signal, not a
  prompt one.
- `quorum task detach <id>` hands the task back to the ordinary headless
  runner; the report protocol (`quorum task report`) works from inside the
  session at any time.

The legacy `notify` config is not used (it's fire-and-forget and often
already occupied — e.g. by the ChatGPT desktop app). Codex also has a
`codex queue --thread <id> --message …` injection channel; quorum doesn't
use it yet — the inbox stays the single guidance transport — but it's a
candidate for immediate (rather than next-stop) delivery later.
