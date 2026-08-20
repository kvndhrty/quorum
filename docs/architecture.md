# Quorum architecture

Quorum is a collection of always-on specialist agents for a busy researcher,
built around three commitments:

1. **No privileged infrastructure.** One ordinary foreground process
   (`quorum up`) hosts APScheduler. No cron, no systemd, no root, no ports
   (the web dashboard is opt-in and binds to localhost). Background it with
   `nohup` or tmux.
2. **Everything is a plain file.** All state lives under one directory,
   `QUORUM_HOME`, as JSON/JSONL/TOML/Markdown. `ls` and `cat` are debuggers;
   copying the directory migrates the whole system; a kernel sandbox profile
   reduces to "rw on this one tree, ro on project dirs".
3. **Degrade gracefully.** The LLM, the dashboards, and the sandbox are all
   optional. Every agent has deterministic no-LLM behavior; every view works
   with the supervisor stopped.

## Process model

```
quorum up ──► Supervisor
              ├─ APScheduler (BackgroundScheduler, thread pool)
              │   ├─ job: tracker   (every 30m)   ─┐
              │   ├─ job: sentinel  (cron 0 8 …)   ├─ crash-isolated wrapper:
              │   ├─ job: steward   (every 1h)     │  heartbeat files, error
              │   ├─ job: scribe    (cron 30 17 …) │  posts, auto-pause after
              │   ├─ job: scout     (cron 15 9 …) ─┘  5 consecutive failures
              │   └─ job: janitor   (hourly: archival, stale-claim recovery)
              └─ supervisor.lock (pid file, touched every 60s = liveness)

quorum web / quorum tui / quorum status ──► pure readers of QUORUM_HOME
```

`BackgroundScheduler` with synchronous `tick()` (not AsyncIO) is deliberate:
agent work is file scans and subprocess calls — blocking and thread-friendly —
and a synchronous `tick()` keeps third-party agents trivial to write.

`quorum agent run-once <name>` runs any agent without the supervisor; people
who *do* have cron can schedule that instead.

## QUORUM_HOME

Resolution: `--home` flag > `$QUORUM_HOME` > `./quorum-home` (if it exists) >
`~/.quorum`.

```
config.toml                       user-owned; quorum never rewrites it
supervisor.lock                   pid + start time; mtime = liveness heartbeat
projects/<slug>.json              canonical project records (machine-owned JSON)
prompts/<name>.md                 user-editable LLM prompt templates
messages/board/<topic>/*.json     public append-only board
messages/inbox/<agent>/new|cur/   direct mail (maildir-style claiming)
messages/archive/YYYY-MM.jsonl.gz compacted history
state/agents/<name>/              heartbeat.json + private state.json
state/projects/<slug>/status.json tracker output
state/scout/candidates.json       discovered-but-unadopted projects
state/steward/undo.jsonl          every file move, replayable backward
briefs/YYYY-MM-DD.md              scribe output
logs/supervisor.log, actions.jsonl
plugins/                          drop-in custom agent modules
```

## Messaging protocol

One `Message` schema serves two channels:

```json
{
  "v": 1,
  "id": "01J5R3V7Q8Z9K2M4N6P8R0T2",
  "from": "sentinel",
  "to": null,
  "topic": "reminders",
  "type": "deadline.approaching",
  "created_at": "2026-08-20T09:15:01Z",
  "ttl_days": null,
  "payload": {"text": "Big Paper due in 7 day(s) (2026-08-27)", "project": "big-paper"}
}
```

- Exactly one of `to` (direct) or `topic` (board) is set. `payload.text` is
  always present so any reader can render any message.
- Filenames are `<UTC-compact>-<ULID>.json`, so lexicographic order is
  chronological order and no index is needed.
- **Atomic writes**: dot-prefixed tmp file in the same directory, fsync,
  `os.rename()`. Readers skip dotfiles, so a partial message is never seen.
- **Board** consumers keep a private cursor (last filename processed) in
  their own state file. The board itself carries no consumption marks, so
  any number of readers coexist without coordination.
- **Inbox** claiming is `os.rename(new/x, cur/x)` — atomic, exactly one
  winner. `ack()` archives + deletes; crash-orphaned `cur/` entries are
  returned to `new/` by the janitor after a grace period.
- The hourly janitor compacts board messages older than
  `[quorum].retention_days` (per-message `ttl_days` overrides) into
  `messages/archive/YYYY-MM.jsonl.gz`.

### Design seam: outboxes and a router

v1 delivers directly (writer → recipient's `new/`), because all agents share
one process and one permission domain. If agents are ever sandboxed *from
each other*, the seam is `MessageBus.post()/send()`: swap in an
outbox-spool-plus-router implementation (each agent writes only its own
`outbox/`, a router with wider grants fans out) with no agent code changes.

## Projects

Canonical record: `projects/<slug>.json`. If the project directory contains
a `.quorum.toml` marker, its `name`/`deadline`/`tags`/`notes` merge over the
registry record at read time (`quorum.projects` is the single merge point).
Metadata thus travels with a synced repo while quorum needs only read access
to project directories. Discovery is explicit: the scout proposes, the human
adopts (`quorum project adopt`) or declines.

## LLM layer

`LLMBackend` is a one-method protocol: prompt in, completion out. The `cli`
backend shells out to any configured executable; `[llm].input` selects stdin
piping or argv substitution (`{prompt}`). `LLMClient.complete()` never
raises — `None` means "no LLM today" and every LLM-using agent has a
deterministic fallback. Prompts come from user-editable templates in
`prompts/` (see `quorum.prompts`).

### Design seam: managed auth proxy

`[llm].backend = "proxy"` is reserved. The intent: the supervisor boots a
localhost proxy that injects API credentials so agent subprocesses never see
raw keys — most likely implemented over nono-py's `start_proxy`
(ProxyConfig + credential injection + L7 domain filtering). Everything goes
through `LLMBackend`, so no other module may assume the `cli` backend.

## Sandbox (optional)

See [nono.md](nono.md). Three modes: wrap the whole supervisor with the nono
binary (recommended, zero code); `quorum up --self-sandbox` via nono-py
`apply()`; or `[sandbox].use_nono = true` to run only LLM subprocesses under
`sandboxed_exec`. `quorum.sandbox` is the only module that touches nono-py,
imports it lazily, and fails closed: if sandboxing was requested and nono-py
is missing, LLM subprocesses do not run at all.
