# Quorum architecture

Quorum orchestrates long-running coding tasks executed by user-supplied
harnesses (claude, codex, opencode, …), built around three commitments:

1. **No privileged infrastructure.** One ordinary process (`quorum up`)
   hosts APScheduler — foreground by default, or detached into the
   background with `quorum up --detach` (the same `start_new_session`
   pattern task runs use; stdout/stderr land in `logs/supervisor.log`, and
   `quorum down` SIGTERMs the pid recorded in `supervisor.lock`, then polls
   the lock's release). No cron, no systemd, no root, no ports (the web
   dashboard is opt-in and binds to localhost). Task runs are ordinary
   detached child processes.
2. **Everything is a plain file.** All state lives under one directory,
   `QUORUM_HOME`, as JSON/JSONL/TOML/Markdown. `ls` and `cat` are debuggers;
   copying the directory migrates the whole system; sandbox profiles reduce
   to "rw on this tree (and per-task worktrees), ro elsewhere".
3. **Fail loudly, recover automatically.** Dashboards and views degrade
   gracefully (their reads are pure file reads; they work with the
   supervisor stopped),
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
              │   │        agent.pause / agent.resume / agent.run-now /
              │   │        agent.reload)
              │   └─ job: _janitor (hourly: archival, stale-claim recovery)
              └─ supervisor.lock (pid file, touched every 60s = liveness)

manager ──(its harness runs `quorum task run --detach`)──► detached runner
                               ├─ tasks/<id>/runner.lock (pid = liveness)
                               ├─ git worktree in worktrees/<id>/
                               └─ harness subprocess (stdout → transcript.jsonl)

quorum web / quorum tui / quorum status ──► read QUORUM_HOME's files
                                     (writes: thin shared bus/store calls)
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
agents/<name>.toml                file-defined agents (the one config location
                                  quorum may write: `agent create` and the web
                                  dashboard; merges over [agents.*], file wins)
supervisor.lock                   pid + start time; mtime = liveness heartbeat
projects/<slug>.json              canonical project records (machine-owned JSON)
tasks/<id>/task.json              task spec + reported status + session + runs
                                  (each run: times, exit code, auto-commit
                                   note, reported token/cost usage)
tasks/<id>/attached.json          adopted-session liveness (latest hook event)
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
                                  (+ journal.jsonl and transcript.jsonl for
                                  harness-driven agents other than the manager)
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
   `{prompt}`/`{session}` — except for an inject harness, whose prompt
   travels over stdin instead (below) and whose `{prompt}` elements are
   dropped from the argv,
5. spawn the harness with `cwd=<workdir>` and `QUORUM_HOME` in its
   environment; stream stdout line-by-line into `transcript.jsonl`,
   capturing a `session_id` (or codex-style `thread_id`) from any JSON
   event that carries one, and whatever token/cost usage its result events
   report (below),
6. optionally auto-commit (below),
7. append the run (exit code, timestamps, usage) to `task.json`; release
   the lock.

**Auto-commit (`[tasks].auto_commit`, default off).** The delivery protocol
in the task preamble and the `STRANDED-WORK` flag in views and the digest
are advisory and corrective: neither *guarantees* work survives a harness
that crashes mid-edit or ignores its instructions. This flag is the hard
guarantee — after the harness exits, if the working tree is dirty, the
runner does `git add -A` and commits it as "quorum: auto-commit uncommitted
work after run". Branches outlive worktrees, so the work can then only be
found, never lost. Deliberately narrow:

- it only ever fires inside a task's *own* worktree (paths compared
  `resolve()`d, so a symlinked home spelling can't silently disable it) — a
  `--no-worktree` task runs in the user's checkout on whatever branch they
  had out, which quorum does not own,
- it never pushes: that would assume a remote and credentials, and an
  unpushed branch is already reported as stranded work,
- it is mechanical, not a judgement — the runner still never sets status,
  and a tree the harness committed itself gets no empty extra commit,
- it is built for messy crash states: `status`/staging use
  `--untracked-files=all` (a repo-level `status.showUntrackedFiles no` must
  not hide an untracked-only crash — `workdir_git_state` sees through it
  too), and the commit runs `--no-verify` with signing off, because a
  failing pre-commit hook or a pinentry prompt would defeat the net in
  exactly the unattended case it exists for,
- two states it refuses to conclude, leaving the tree dirty and flagged:
  a detached HEAD (the commit would belong to no branch and die with the
  worktree) and an in-progress merge/rebase/cherry-pick (`git add -A` +
  commit would finish it, conflict markers and all),
- a task whose harness already reported a terminal status keeps its tree as
  the harness left it — sweeping stray scratch files into a *finished*
  branch would re-flag a done task as stranded and push junk toward its PR,
- under `[sandbox].use_nono` the runner is already inside the kernel
  sandbox when the run ends and cannot run git at all, so the net skips
  with an explicit transcript note instead of failing cryptically,
- what happened is recorded twice: a `quorum: auto-committed N path(s) as
  <sha>` transcript line, and durably as `auto_commit` on the run's entry
  in `task.json` — the record that quorum, not the harness, made that
  commit,
- a failure (no git identity, a stale index lock) is recorded the same two
  ways rather than raised: the tree simply stays dirty, which is the state
  `workdir_git_state` already reports for the manager to chase.

**Token/cost usage (`usage.py`).** Harnesses already say what a run spent —
claude's terminal `result` event carries `total_cost_usd` and a `usage`
block, codex's `turn.completed` and `token_count` carry token counts — and
the runner is already parsing every stdout event on its way to the
transcript. So capture is one more look at each parsed event
(`UsageCollector` on the runner's `on_event` hook) and the result lands as
`usage` on the run's entry in `task.json`. No new file, no new store.

- **Extraction is loose, storage is canonical.** Any event typed `result` /
  `turn.completed` / `token_count` / ... (or carrying a top-level cost key,
  so an unknown harness still gets its cost recorded) is a spend report;
  the key spellings the field actually uses (`input_tokens` /
  `inputTokens` / `prompt_tokens`, `cache_read_input_tokens` /
  `cached_input_tokens`, ...) normalize to one small set of keys, so no
  reader branches on which harness ran. `total_tokens` is the harness's own
  figure when it reports one, else input + output + cache-read +
  cache-creation: everything the model processed.
- **Silence is unknown, never zero.** A harness that reports nothing (the
  shipped opencode template, most custom scripts, anything printing plain
  text) records `usage = null`, and every reader omits the field rather
  than showing `$0.00`. Nothing in the module raises on a malformed event.
- **Within a run the reduction is elementwise max, across runs it is a
  sum.** The harnesses that report usage report *run-cumulative* totals
  (verified against real claude transcripts), and a pumped multi-turn run
  emits one such event per turn, so summing them would multiply the spend.
  Max equals "the last event that reported anything" for a cumulative
  reporter and merely under-counts a hypothetical per-turn one — the same
  prefer-false-negatives tradeoff as `possible-loop`, and the honest
  direction for a number a budget may be judged against. Separate runs are
  separate spends, so a task total sums them.
- **Surfacing** is pure file reading: `views.task_rows` carries `usage`
  (task total), `usage_text` (rendered once, so the CLI, TUI and browser
  agree) and `budget_overages`; `quorum status` / `task list` append
  `$0.42 · 11.0k tok` to the row, `task show` breaks it out, and the
  manager digest gets a `usage:` line per task.
- **The budget is an observation, not (yet) a rail.** `[tasks]
  max_cost_per_run` / `max_tokens_per_run` (validated non-negative, 0 =
  off) turn an over-budget run into a `BUDGET-EXCEEDED` digest line and a
  `$!` mark in the views. Quorum kills nothing and refuses nothing for
  cost; the manager reads the flag and decides, exactly as it does with
  `possible-loop`. Enforcement — gating the *next* run of a task that blew
  its budget — is a deliberate follow-up (issue #19 step 3): it would join
  the **rate-limit family** the per-run action cap belongs to (bound a
  bad run's blast radius, never veto a particular choice), and mid-run
  enforcement is only even expressible for pumped runs, where stdin can be
  closed at a turn boundary.

**Mid-run guidance (`inject = "stream-json"`).** A harness whose CLI speaks
the Claude Code stream-json protocol (`--input-format stream-json`
`--output-format stream-json`) can opt into steering *during* a run: the
runner spawns it with a pipe on stdin, and a `GuidancePump` thread writes
the run's composed prompt as the opening stream-json user turn (`{"type":
"user", "message": {...}}`), then polls the task inbox and writes each
claimed message as a further turn, which the harness queues and picks up at
its next turn boundary. Stdin is the whole prompt channel here — a
stream-json CLI ignores an argv prompt and blocks until a turn arrives on
stdin, so an inject harness that only got its prompt via argv would hang
silently until the run timeout. Because a stream-json harness runs until
stdin closes, the pump also owns ending the run: the protocol emits one
`result` event per completed user turn (the prompt turn is the first), so
the pump closes stdin once every delivered turn has its result and `new/`
is empty — a run extends while guidance keeps arriving and ends at the
first idle turn boundary. A message
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

### Attached tasks: adopting a live session

(User-facing how-to: [guide.md](guide.md#adopting-a-live-session).)

`quorum task adopt` inverts the ownership: instead of quorum spawning runs,
an *existing interactive session* (Claude Code, or anything with hooks) is
recorded as a task with `attached = true`, `workdir` = the session's own
directory, no worktree, and the harness's session id when known. Quorum
never spawns runs for it — `run_task` refuses attached tasks outright. That
refusal is a deliberate, narrow bend of "the action cap is the only rail":
it is a *substrate* rail in the same class as `runner.lock`, protecting the
user's live checkout from a racing headless run, not supervision policy.
`quorum task detach` lifts it.

Liveness for a run quorum didn't spawn comes from `tasks/<id>/attached.json`,
rewritten by harness-side hooks (`quorum task hook-session-start`,
`hook-stop`, `hook-session-end`) with the latest lifecycle event. The hook
entry points are harness-agnostic — JSON with `session_id`/`cwd` on stdin,
matched to an attached task by exact session id first, then working
directory. The cwd fallback is how an id-less adoption *learns* its session
id, and it only fires while the task has no live session of its own (none
recorded, or the recorded one ended — a resume under a fresh id), so a
second concurrent session in the adopted checkout can't steal guidance or
the session id —
and `integrations/` ships an adapter per harness: `claude-code/` and
`codex/` wire native Stop/SessionEnd(/SessionStart) hooks straight to the
CLI, both speaking the same stdin payload and `{"decision": "block"}`
continuation protocol, while `opencode/` (no hook commands; an in-process
plugin bus instead) ships a fail-soft JS plugin that calls
`hook-stop --format text` on idle events and injects whatever the CLI
prints as a user turn via the SDK. Either way the digest renders attached
tasks in their own section (never as `runner=dead`-launchable), and
guidance flows through the ordinary task inbox: the stop/idle hook claims
pending messages and continues the session with them, so `task nudge` —
from the CLI, manager, TUI, or web — reaches the human's live session at
its next stop. Delivery consumes the guidance, so continuation can't loop,
and the maildir claim keeps the delivery point race-free against a future
headless run after detach.

**herdr (optional).** When the session runs inside a
[herdr](https://herdr.dev) pane (`task adopt --herdr-pane <id>`), `herdr.py`
— the one module speaking herdr's local socket API — adds two things:
the pane's detected agent status (`herdr: state=working|blocked|idle` in
the digest; a busy session fires no hooks, so this outclasses mtimes) and a
doorbell on `task nudge` (the pane is poked that guidance is waiting;
sessions with no quorum adapter installed get their delivery prompt this
way). The adapter fails *soft* by design — the mirror image of
sandbox.py's fail-closed — because observation enrichment must never break
a digest. The inbox remains the single transport: the doorbell never
carries the payload, so delivery stays exactly-once across all delivery
points.

## The manager

(User-facing how-to: [guide.md](guide.md#the-manager).)

The flagship built-in agent, and it is *itself* harness-driven: supervision
policy is a prompt (`prompts/manager.md`), not Python. Each tick:

1. **Wake condition**: any non-terminal task, or a pending message in the
   manager's inbox. Nothing to manage → no harness run. Dead runners keep
   the condition true, which is precisely what makes post-outage recovery
   automatic.
2. **Digest** (`agents/manager.py::build_digest`, a pure function over
   files): every active task's status, runner liveness, quiet time, recent
   reports and transcript tail, plus a `git:` line when its working
   directory holds uncommitted changes or unpushed commits; attached
   sessions in their own clearly-labeled section (last hook event age, git
   state, reports — never runner liveness, which they don't have); recently
   finished tasks, marked `STRANDED-WORK dirty=N unpushed=M` when they
   ended with such state — work a harness left in its worktree without
   delivering it, which the default manager prompt treats as not done and
   relaunches with a nudge to commit and push; a `ci:` line carrying the
   pull request behind the branch, `CI-FAILING` on a finished task whose
   checks are red (see below); a `possible-loop:` line
   when a task's transcript tail is dominated by one repeated tool call
   (see below); a `usage:` line with what the task has spent when its
   harness reported usage at all, plus `BUDGET-EXCEEDED` per run past a
   configured `[tasks]` budget (both observations — see *Token/cost usage*
   above); the manager's own
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
   (`actor.py`): `QUORUM_ACTOR=manager`, a per-run `QUORUM_ACTOR_RUN` id,
   and the resolved action cap in `QUORUM_ACTOR_CAP`. The tag is
   name-generic — any harness-driven agent identifies itself the same way,
   and the CLI journals it under `state/agents/<name>/journal.jsonl` (the
   manager keeps its historical `state/manager/` spot).

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

**Loop observation (`possible-loop`).** The action journal remembers what the
*manager* did; nothing else sees the other loop class — a task harness
spinning inside a single run, repeating the same failing tool call while
`runner.lock` stays live and the transcript keeps growing, which to every
other signal looks like healthy work. `loop_signal` (still pure over files,
no state file) scans a transcript tail bounded twice — `LOOP_SCAN_LINES`
(120) entries from at most `LOOP_SCAN_BYTES` (2 MiB), the byte cap being
the binding one on payload-heavy transcripts — extracts tool calls, and
scores the last `LOOP_WINDOW_CALLS` (12) of them. The evidence must be
current: only a live runner is scored (the transcript is append-only; a
dead task would stay flagged forever) and only entries newer than the last
*completed* run (a relaunch is not indicted by its predecessor's spinning).
Extraction is deliberately loose — a recursive walk for any nested dict
tagged `tool_use` / `tool_call` / `function_call` / `command_execution` /
`local_shell_call` (or carrying a string `tool_name`), counted once per
call id, so harnesses that pair started/completed events (codex) don't
double-count — and each call becomes `name + sha256(arguments)[:12]`.
It sees only structured JSON events: a harness that prints plain text (the
shipped opencode template, most custom scripts) is unobservable here, and
absence of the flag is not evidence of health. The hash keeps argument
payloads (paths, secrets, file contents) off the *flag line itself* — a
tool name and counts, never a payload — but it is not a secrecy boundary:
the digest's adjacent `out|` tail lines still quote raw events, truncated
to 160 characters.

A flag needs **both** a call repeated `LOOP_REPEAT_THRESHOLD` (4) times *and*
that repetition dominating the window (`distinct/total <= LOOP_DISTINCT_RATIO`,
0.5). The double gate sets the tradeoff at *prefer false negatives*: polling
interleaved with real work, retries whose arguments change, and short tails
stay quiet, at the cost of missing a loop that only repeats three times. These
are plain module constants in `agents/manager.py`, not config — a wrong
threshold costs a noisy digest line, never a killed run.

That last part is the point, and the deliberate divergence from OpenHands'
stuck detector (which auto-halts): `possible-loop` is an **observation, not a
rail**. Python makes no decision; the flag is data, the default manager prompt
tells the manager to read the tail and judge (nudge, relaunch, cancel, or
ignore), and the per-run action cap remains the only rail.

**CI observation (`ci:`).** `workdir_git_state` follows work as far as
"pushed" and stops; `ci.py` — the only module that shells out to `gh` —
carries it one step further, to whether what was pushed actually works. For
each digested task with a workdir it runs one `gh pr view --json
number,url,state,isDraft,mergeable,statusCheckRollup` *inside that
directory*, so gh resolves the repository from the remote and the PR from
the checked-out branch (`quorum/<short-id>` for a worktree task, whatever
the human is on for an adopted one). The rollup mixes CheckRun (Actions:
`status` + `conclusion`) and StatusContext (classic: `state`) shapes;
`_verdict` classifies each as pass/fail/pending and treats anything it does
not recognize as pending, because an unknown shape must never read as a
pass. The line is `key=value` like the rest of the digest — check counts,
up to five failing check *names*, `MERGE-CONFLICT` when `mergeable` is
`CONFLICTING`, and the PR url.

It **fails soft**, the herdr contract rather than the sandbox one: no `gh`,
no auth, no remote, no PR for the branch, a timeout, or unparseable output
all degrade to `None`, and the digest is byte-identical to one built with
the probe off (a test asserts exactly that). A missing `ci:` line therefore
carries no information at all — including under a self-sandboxed supervisor,
where the blocked network simply makes every probe return `None` rather than
earning `gh` a capability grant. Cost is bounded twice, because digest build
blocks the tick: `[ci].timeout_seconds` (10) per call, and `CI_MAX_PROBES`
(12) probes per digest, spent in digest order so a home with more tasks than
budget still covers its live work. `[ci].enabled = false` skips the probe
entirely. Those are the only knobs, and none of them changes what anything
*does* about the result.

Which is the point: like `possible-loop`, this is an observation, not a
rail. Python never nudges, relaunches, or blocks on red CI. `prompts/manager.md`
says how to read the line, and the shipped `prompts/babysitter.md` (below)
is a whole reactive policy written as prompt text. Views stay pure file
readers — the probe runs during digest build only, and nothing materializes
its result to disk, so `views.py` never acquires a network call.

Failure story: missing harness config, nonzero exit, or timeout → the tick
raises. Crash isolation records it (heartbeat, board); the manager's
`auto_pause = false` config keeps the schedule firing so recovery needs no
human intervention.

### Prompt agents

The generic sibling of the manager: `type = "prompt"` runs a user-written
prompt (`prompts/<name>.md`, or `settings.prompt` to point elsewhere) over
the configured harness on a schedule, sharing the manager's exact run
mechanics (`agents/harness_run.py`): actor-tagged env, per-agent journal and
action cap, transcript at `state/agents/<name>/transcript.jsonl`, mid-run
directives via the agent's own inbox when the harness supports injection.
There is deliberately no wake condition and no digest — a prompt agent runs
every scheduled tick, and anything conditional belongs in its prompt. Prompt
agents are usually file-defined (`agents/<name>.toml`, created by
`quorum agent create` or the web dashboard, hot-added via `agent.reload`)
but a `[agents.<name>]` table in config.toml works identically.

Quorum packages one worked example, `default_prompts/babysitter.md` — the
CI babysitter, seeded into `prompts/` by `quorum init` and inert until an
agent is created over it (`quorum agent create babysitter --schedule
"every 10m"`, or `--prompt babysitter` to run it under another name). It
polls quorum-created PRs with `gh`, waits for a task's runner to go idle,
nudges + relaunches with the *specific* failing check, and gives up to the
human after two failed relaunches. Every bit of that is prompt text under
the ordinary prompt-agent rails (journal + per-run action cap): the shape
Sculptor and Jules grew in Python, quorum ships as a file you can edit.
`agent create` therefore accepts a prompt agent with no `--prompt-text`
when the template already resolves (user file or packaged default).

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
pause|resume|run-now|reload` sends to the `supervisor` inbox, which the
supervisor claims every 15 s and applies to its scheduler jobs. No new
transport, no ports, and commands queue harmlessly while the supervisor is
down. `agent.reload` is the hot-add path: it re-reads config (config.toml
plus `agents/*.toml`) and creates, replaces, or removes that one agent's
job — the file is the source of truth, the message is only a poke, so one
command covers create, edit, and delete. A pause is durable: it lands in
the agent's heartbeat, and a restarting supervisor schedules any agent whose
heartbeat says `paused` with its job paused rather than silently resuming
it.

### Design seam: outboxes and a router

v1 delivers directly (writer → recipient's `new/`), because everything
shares one permission domain. If agents are ever sandboxed *from each
other*, the seam is `MessageBus.post()/send()`: swap in an
outbox-spool-plus-router implementation with no agent code changes.

## Views and their write affordances

`views.py` assembles the read model out of files alone — no locks, no
network, no supervisor required — and `quorum status`, the TUI and the web
app are all readers of that one model, which is why they never disagree.

The reads are pure; the writes are deliberately not absent. Both dashboards
carry a small set of *write affordances*, and the rule is that each is a
thin call into the same code path the CLI uses — a `MessageBus` send, a
`TaskStore.update`, `runner.launch_detached`, `config.create_agent` — never
write logic that lives in a view:

- **TUI** (`tui/app.py`): nudge a task (`n`), send the manager a directive
  (`m` — the `manager` inbox, exactly `quorum manager tell`, and the reason
  the TUI needs no task-add form: the manager runs `task add` itself,
  journaled and capped), start a detached run (`s`), cancel a task (`c`).
  `s` refuses an attached task and a task whose runner is alive, mirroring
  the runner's own substrate rails; `c` is the one destructive binding, so
  it goes through a yes/no `ConfirmScreen` and, like `quorum task cancel`
  without `--kill`, marks the status without signalling a live runner.
- **Web** (`web/app.py`): the same task nudge, plus board posts, project
  deadline/notes edits, agent create and pause/resume/run-now/reload.

Neither view holds a lock, spawns an agent tick, or writes state of its own
invention; a dashboard that vanishes mid-keystroke leaves nothing behind but
the message it already queued. This revises the earlier "the views are pure
readers whose one write affordance is nudging a task" stance (issue #11) —
the invariant that survived it is *thin, shared, no view-local write logic*.

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
(injectable `FakeClock`), and `fake_llm`. Three purpose-built fake CLIs live
in `tests/bin/`: `fake_llm.py` (canned completions), `fake_gh.py` (a GitHub
CLI installed onto a PATH stripped down to real git, so the CI probe's
no-gh / no-auth / no-PR / garbage / hung branches are all reachable — and
so a developer's real `gh` can never reach the network from a test), and
`fake_harness.py`, which
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
