# Quorum architecture

Quorum orchestrates long-running coding tasks executed by user-supplied
harnesses (claude, codex, opencode, …), built around three commitments:

1. **No privileged infrastructure.** One ordinary foreground process
   (`quorum up`) hosts APScheduler. No cron, no systemd, no root, no ports
   (the web dashboard is opt-in and binds to localhost). Background it with
   `nohup` or tmux. Task runs are ordinary detached child processes.
2. **Everything is a plain file.** All state lives under one directory,
   `QUORUM_HOME`, as JSON/JSONL/TOML/Markdown. `ls` and `cat` are debuggers;
   copying the directory migrates the whole system; sandbox profiles reduce
   to "rw on this tree (and per-task worktrees), ro elsewhere".
3. **Fail loudly, recover automatically.** Dashboards and views degrade
   gracefully (pure file readers; they work with the supervisor stopped),
   and a harness that ignores the report protocol is still observed
   passively. Supervision itself, however, is deliberately *not*
   degradable: the manager **is** a harness run, and without a working
   harness its tick raises — visibly, every tick — while `auto_pause =
   false` keeps the schedule firing so the first tick after the LLM
   service returns reads the situation from files and reinvokes whatever
   needs reinvoking. There is no dumbed-down fallback supervisor by
   design.

## Process model

```
quorum up ──► Supervisor
              ├─ APScheduler (BackgroundScheduler, thread pool)
              │   ├─ job: manager  (every 5m)  ── crash-isolated wrapper:
              │   ├─ job: <user plugins…>         heartbeats, error posts,
              │   ├─ job: _control (15s: claims supervisor inbox —
              │   │        agent.pause / agent.resume / agent.run-now)
              │   └─ job: _janitor (hourly: archival, stale-claim recovery)
              └─ supervisor.lock (pid file, touched every 60s = liveness)

manager ──(its harness runs `quorum task run --detach`)──► detached runner
                               ├─ tasks/<id>/runner.lock (pid = liveness)
                               ├─ git worktree in worktrees/<id>/
                               └─ harness subprocess (stdout → transcript.jsonl)

quorum web / quorum tui / quorum status ──► pure readers of QUORUM_HOME
```

Two process shapes on purpose. Agent ticks are short, synchronous, and
idempotent — right for a scheduler thread pool. A harness run lasts minutes
to hours — wrong for a tick, so each run is its own detached process with
its own pid-lock. Consequence: restarting the supervisor never kills a
running task; the manager re-attaches by reading files, exactly like the
dashboards. A per-agent `tick.lock` (same `O_EXCL` pid-lock as everything
else) keeps a scheduled tick and a hand-run `quorum agent run-once` from
interleaving.

## QUORUM_HOME

Resolution: `--home` flag > `$QUORUM_HOME` > `./quorum-home` (if it exists) >
`~/.quorum`.

```
config.toml                       user-owned; quorum never rewrites it
supervisor.lock                   pid + start time; mtime = liveness heartbeat
projects/<slug>.json              canonical project records (machine-owned JSON)
tasks/<id>/task.json              task spec + reported status + session + runs
tasks/<id>/transcript.jsonl       harness stdout, one JSON line per line seen
tasks/<id>/reports.jsonl          `quorum task report` entries
tasks/<id>/runner.lock            pid of the active run
tasks/<id>/runner.log             detached-run bootstrap output
worktrees/<id>/                   git worktree (branch quorum/<short-id>)
prompts/<name>.md                 user-editable prompt templates (re-running
                                  `quorum init` upgrades never-edited seeds)
messages/board/<topic>/*.json     public append-only board
messages/inbox/<name>/new|cur/    direct mail (task-<id>, supervisor, agents)
messages/archive/YYYY-MM.jsonl.gz compacted history
state/agents/<name>/              heartbeat.json + state.json + tick.lock
state/manager/journal.jsonl       auto-recorded manager actions (per-run tagged)
state/manager/transcript.jsonl    the manager harness's own stdout
logs/supervisor.log, actions.jsonl
plugins/                          drop-in custom agent modules
```

## Tasks and the runner

The unit of control for a *generic* harness is the **run**: every CLI
harness supports "run in a directory with a prompt until exit, then be
invoked again", so that is the baseline contract. So a task is a durable
record (`tasks/<id>/task.json`) plus a sequence of runs, and
`quorum.runner.run_task` does exactly one run:

1. take `runner.lock` (O_EXCL pid-lock; one live run per task),
2. resolve the working directory — by default a git worktree under
   `QUORUM_HOME/worktrees/<id>` on branch `quorum/<short-id>`, created on
   first run (parallel tasks on one repo can't collide; the user's checkout
   stays clean; worktrees share the main repo's object store, which is why a
   sandboxed run needs write on the project's `.git`),
3. claim everything in the task's inbox (`messages/inbox/task-<id>/`) — the
   manager's pokes and the user's nudges — and inject it into the prompt,
4. compose the prompt: preamble template (teaches the report/inbox protocol)
   + task prompt + guidance section; pick the harness argv template
   (`resume` when a session id is known, else `start`) and substitute
   `{prompt}`/`{session}`,
5. spawn the harness with `cwd=<workdir>` and `QUORUM_HOME` in its
   environment; stream stdout line-by-line into `transcript.jsonl`,
   capturing a `session_id` (or codex-style `thread_id`) from any JSON
   event that carries one,
6. append the run (exit code, timestamps) to `task.json`; release the lock.

**Mid-run guidance (`inject = "stream-json"`).** A harness whose CLI speaks
the Claude Code stream-json protocol (`--input-format stream-json`
`--output-format stream-json`) can opt into steering *during* a run: the
runner spawns it with a pipe on stdin, and a `GuidancePump` thread polls the
task inbox and writes each claimed message as a stream-json user turn
(`{"type": "user", "message": {...}}`), which the harness queues and picks
up at its next turn boundary. Because a stream-json harness runs until
stdin closes, the pump also owns ending the run: the protocol emits one
`result` event per completed user turn, so the pump closes stdin once every
delivered turn has its result and `new/` is empty — a run extends while
guidance keeps arriving and ends at the first idle turn boundary. A message
that arrives after close, or lands on a harness without `inject`, waits in
`new/` for the next run start, exactly as before; the maildir claim makes
the two delivery points race-free. Delivery is acknowledgment: a message
written to the harness's stdin is acked, the same contract as the
run-start claim.

The runner **never sets task status**. Status is whatever the harness last
said via `quorum task report --status <word>` — a free-form string, recorded
in `reports.jsonl`, mirrored to the board topic `tasks`, and displayed
everywhere. Only `TERMINAL_STATUSES = {done, blocked, cancelled}` mean
anything to quorum: they end the manager's attention. This is deliberate:
quorum ships the manager and the comms substrate, not a workflow engine.

The return channel is quorum's own CLI. The preamble tells the harness to
call `quorum task report` / `quorum task inbox --claim`; since quorum is
just a CLI writing files (and `QUORUM_HOME` is in the run's environment),
any harness that can run shell commands can cooperate — and one that can't
still gets passive monitoring (transcript mtimes, lock liveness, exit
codes). Task ids are ULIDs; the human-facing `short_id` is the ULID's
*random tail* (the head is a timestamp shared by same-instant tasks), and
`TaskStore.resolve` accepts any unique prefix or suffix.

## The manager

The only built-in agent, and it is *itself* harness-driven: supervision
policy is a prompt (`prompts/manager.md`), not Python. Each tick:

1. **Wake condition**: any non-terminal task, or a pending message in the
   manager's inbox. Nothing to manage → no harness run. Dead runners keep
   the condition true, which is precisely what makes post-outage recovery
   automatic.
2. **Digest** (`agents/manager.py::build_digest`, a pure function over
   files): every active task's status, runner liveness, quiet time, recent
   reports and transcript tail, plus a `git:` line when its working
   directory holds uncommitted changes or unpushed commits; recently
   finished tasks, marked `STRANDED-WORK dirty=N unpushed=M` when they
   ended with such state — work a harness left in its worktree without
   delivering it, which the default manager prompt treats as not done and
   relaunches with a nudge to commit and push; the manager's own
   recent **action journal** with then-vs-now status per target (the
   anti-loop memory — see below); and any user directives claimed from
   `messages/inbox/manager/` (`quorum manager tell`). Directives are acked
   only after a successful run; a crash rejects them back to `new/`. If the
   manager's harness sets `inject = "stream-json"`, directives arriving
   *while* a tick's run is in flight are pumped into it as user turns (same
   `GuidancePump` as the task runner, sourced from the `manager` inbox)
   instead of waiting for the next tick.
3. **One harness run** over `prompts/manager.md` + the digest, synchronous,
   cwd = `QUORUM_HOME`, bounded by `run_timeout_seconds`, stdout streamed to
   `state/manager/transcript.jsonl`. The env carries the actor tag
   (`actor.py`): `QUORUM_ACTOR=manager`, a per-run `QUORUM_MANAGER_RUN` id,
   and the resolved action cap in `QUORUM_MANAGER_ACTION_CAP`.

The harness acts with full authority through the quorum CLI — `task
add/run/nudge/cancel`, `agent pause/resume/run-now`, `board post`, `quorum
manager note`. **Every mutating CLI action taken under the manager's env tag
is auto-journaled** (`state/manager/journal.jsonl`: action, target, the
target's status at action time, run id) *before* it executes — ground truth,
not model self-report. The journal serves two purposes: fed back into the
next digest, it lets the manager see which interventions changed nothing and
avoid degenerate loops (its prompt forbids repeating an intervention marked
UNCHANGED); and it enforces the one rail quorum keeps — a per-run action cap
(`max_actions_per_run`), a rate limit that bounds a bad run's blast radius
without ever second-guessing a choice.

Failure story: missing harness config, nonzero exit, or timeout → the tick
raises. Crash isolation records it (heartbeat, board); the manager's
`auto_pause = false` config keeps the schedule firing so recovery needs no
human intervention.

## Messaging protocol

One `Message` schema serves two channels:

```json
{
  "v": 1,
  "id": "01J5R3V7Q8Z9K2M4N6P8R0T2",
  "from": "manager",
  "to": "task-01J5R3…",
  "topic": null,
  "type": "nudge",
  "created_at": "2026-08-20T09:15:01Z",
  "ttl_days": null,
  "payload": {"text": "Your previous run ended without reporting…"}
}
```

- Exactly one of `to` (direct) or `topic` (board) is set. `payload.text` is
  always present so any reader can render any message.
- Filenames are `<UTC-compact>-<ULID>.json`, so lexicographic order is
  chronological order and no index is needed.
- **Atomic writes**: dot-prefixed tmp file in the same directory, fsync,
  `os.rename()`. Readers skip dotfiles, so a partial message is never seen.
- **Board** consumers keep a private cursor (last filename processed) in
  their own state; the board carries no consumption marks, so any number of
  readers coexist without coordination.
- **Inbox** claiming is `os.rename(new/x, cur/x)` — atomic, exactly one
  winner, across processes (which is what makes the task runner's
  guidance-claim safe against a concurrent `task inbox --claim`).
  `ack()` archives + deletes; crash-orphaned `cur/` entries are returned to
  `new/` by the hourly janitor.
- The janitor also compacts board messages older than
  `[quorum].retention_days` (per-message `ttl_days` overrides) into
  `messages/archive/YYYY-MM.jsonl.gz`.

The **control channel** rides the same machinery: `quorum agent
pause|resume|run-now` sends to the `supervisor` inbox, which the supervisor
claims every 15 s and applies to its scheduler jobs. No new transport, no
ports, and commands queue harmlessly while the supervisor is down.

### Design seam: outboxes and a router

v1 delivers directly (writer → recipient's `new/`), because everything
shares one permission domain. If agents are ever sandboxed *from each
other*, the seam is `MessageBus.post()/send()`: swap in an
outbox-spool-plus-router implementation with no agent code changes.

## Projects

Canonical record: `projects/<slug>.json`. A `.quorum.toml` marker inside the
project directory merges over the registry record at read time
(`quorum.projects` is the single merge point), so metadata travels with a
synced repo. Quorum only ever *reads* project directories — the one scoped
exception is task execution, which writes to the task's own worktree (and,
via git's shared object store, the project's `.git`).

## LLM layer

`LLMBackend` is a one-method protocol: prompt in, completion out. The `cli`
backend shells out to any configured executable; `[llm].input` selects stdin
piping or argv substitution. `LLMClient.complete()` never raises — `None`
means "no LLM today" and every caller has a deterministic fallback. Prompts
come from user-editable templates in `prompts/` (`quorum.prompts`). Note the
LLM layer is for *plugin agents'* small completions; neither task harnesses
nor the manager's harness go through it — both are invoked directly as
subprocesses via the `[harness.*]` templates.

### Design seam: managed auth proxy

`[llm].backend = "proxy"` is reserved: a supervisor-managed localhost proxy
injecting API credentials so subprocesses never see raw keys — most likely
over nono-py's `start_proxy`. Everything goes through `LLMBackend`, so no
other module may assume the `cli` backend.

## Sandbox (optional)

Three modes, all fail-closed (sandboxing requested + nono-py missing ⇒
nothing runs unsandboxed); `quorum.sandbox` is the only module that touches
nono-py, always lazily:

1. **Wrap the world**: `nono run --profile quorum -- quorum up` — zero code,
   user-authored profile.
2. **Self-sandbox the supervisor**: `quorum up --self-sandbox` applies
   `build_capabilities` via `nono_py.apply()` before the scheduler starts.
   Because builtins, plugins, and APScheduler triggers import lazily *after*
   apply(), the interpreter's own tree (prefixes, stdlib, site-packages, the
   quorum package dir — derived at runtime from `sys`/`sysconfig`) is granted
   read; nono's `system_read_*` policy groups supply the loader/libc baseline
   without which no child can exec at all.
3. **Per-task / per-LLM-call**: `[sandbox].use_nono = true`. Each task run
   applies `build_task_capabilities` to itself (runner process + harness
   children): write on `QUORUM_HOME`, the worktree, the project's `.git`,
   and `[sandbox].task_write` extras (harness state dirs like `~/.claude`);
   read adds the harness executable, `task_read` extras, and the same
   interpreter/system baseline; network open (harnesses need their APIs).

Users can bring their own nono-style JSON profile via
`[sandbox].profile_file`: its `fs_read`/`fs_write` grants merge *additively*
into both derived capability sets (the derivation stays the floor that keeps
quorum functional), a non-empty `network` list keeps mode 2's network open,
and an unreadable profile raises `SandboxUnavailable` — never a narrower
sandbox than the user asked for. The same file works verbatim with the nono
binary in mode 1.
   Plugin agents' LLM subprocess calls go through `sandboxed_exec` with the
   narrower `build_capabilities` set (network blocked unless `[llm]` is
   configured; stdin prompts staged as ULID-named files under
   `state/llm/` since `sandboxed_exec` cannot pipe stdin).

The asymmetry is the design: a sandboxed quorum can *see* the machine, but
the only durable marks it can leave are `QUORUM_HOME`, the worktrees, and
the grants you added explicitly.

## Testing strategy

`tests/conftest.py` provides `home` (scaffolded `QUORUM_HOME`), `clock`
(injectable `FakeClock`), and `fake_llm`. Two purpose-built fake CLIs live in
`tests/bin/`: `fake_llm.py` (canned completions) and `fake_harness.py`, which
behaves like a real harness — echoes its argv and prompt to stdout, emits a
`session_id`, and in `report` mode calls `python -m quorum task report`
against `$QUORUM_HOME`, exercising the full cooperative loop, and manager
modes (`manager_act` launches/nudges/journals; `manager_flood` slams into
the action cap) — each `[harness.*]` table pins its own mode via `env`, so a
fake task harness and a fake manager harness coexist in one test. Manager
tests run the whole loop for real (the fake manager's `task run` executes
the fake task harness); runner tests build real git repos and assert on
worktrees, transcripts, and session capture.
Sandbox glue is pinned by injecting a fake `nono_py` into `sys.modules`
(`test_sandbox.py`); real kernel enforcement runs under `-m nono_integration`
(dedicated CI job asserts platform support so it can never silently skip).
The example plugin is loaded by file path and tested in
`test_example_steward.py`, so the worked example in the guide stays true.
