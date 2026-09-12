# Quorum architecture

Quorum orchestrates long-running coding tasks executed by user-supplied
harnesses (claude, codex, opencode, …), built around three commitments:

1. **No privileged infrastructure.** One ordinary process (`quorum up`)
   hosts APScheduler — foreground by default, or detached with `quorum up
   --detach` (the same `start_new_session` pattern task runs use;
   stdout/stderr land in `logs/supervisor.log`, and `quorum down` SIGTERMs
   the pid in `supervisor.lock`, then polls the lock's release). No cron, no
   systemd, no root, and no open ports: quorum has no server and nothing
   listens. Task runs are ordinary detached child processes.
2. **Everything is a plain file.** All state lives under one directory,
   `QUORUM_HOME`, as JSON/JSONL/TOML/Markdown. The system can be inspected
   and repaired with ordinary file tools; copying the directory migrates it;
   sandbox profiles reduce to "rw on this tree (and per-task worktrees), ro
   elsewhere".
3. **Fail loudly, recover automatically.** Views degrade gracefully — their
   reads are pure file reads, so they work with the supervisor stopped — and
   a harness that ignores the report protocol is still observed passively.
   Supervision itself is deliberately *not* degradable: the manager **is** a
   harness run, and without a working harness its tick raises, visibly,
   every tick, while `auto_pause = false` keeps the schedule firing, so the
   first tick after the model service returns reads the situation from files
   and reinvokes whatever needs reinvoking. There is no dumbed-down fallback
   supervisor by design.

These three do not move with model capability, and neither do the smaller
stances recorded per layer below: no decisions in Python, observations are
never rails, a dropped signal is a bug. The settings that *do* move are
dials recording how far the human trusts the model today: how many tasks run
at once, the per-run action cap, the budget, the manager's cadence, who
launches, who decomposes, who merges. They are listed with their loosening
conditions in [guide.md](guide.md#loosening-the-rails-as-trust-is-earned),
facing the list of what does not move, and `dials.py` is the registry behind
that table. A change to either list is argued there and recorded here and in
`CLAUDE.md` in the same commit.

## Process model

```
quorum up ──► Supervisor
              ├─ APScheduler (BackgroundScheduler, thread pool)
              │   ├─ job: manager  (every 5m)  ── crash-isolated wrapper:
              │   ├─ job: <user plugins…>         heartbeats, error posts,
              │   │                               auto-pause or escalation,
              │   ├─ job: _control (15s: claims supervisor inbox —
              │   │        agent.pause / agent.resume / agent.run-now /
              │   │        agent.reload)
              │   └─ job: _janitor (hourly: archival, stale-claim recovery)
              └─ supervisor.lock (pid file, touched every 60s = liveness)

manager ──(its harness runs `quorum task run --detach`)──► detached runner
                               ├─ tasks/<id>/runner.lock (pid = liveness)
                               ├─ git worktree in worktrees/<id>/
                               └─ harness subprocess (stdout → transcript.jsonl)

quorum tui / quorum status ────────────► read QUORUM_HOME's files
                                     (writes: thin shared bus/store calls)
quorum doctor ──────────────────────────► pure reader + one opt-in probe (--smoke)
```

Two process shapes on purpose. Agent ticks are short, synchronous, and
idempotent — right for a scheduler thread pool. A harness run lasts minutes
to hours — wrong for a tick, so each run is its own detached process with
its own pid-lock. Consequence: restarting the supervisor never kills a
running task; the manager re-attaches by reading files, exactly like the
views. A per-agent `tick.lock` keeps a scheduled tick and a hand-run `quorum
agent run-once` from interleaving.

## QUORUM_HOME

Resolution: `--home` flag > `$QUORUM_HOME` > `./quorum-home` (if it exists) >
`~/.quorum`. `--home` is one option on the root app and goes before the
subcommand (`quorum --home /path task list`). The root callback exports the
resolved home as `$QUORUM_HOME`, and every process quorum spawns is handed
it explicitly, so a detached run, a manager harness and a `[notify]` hook
that calls quorum back all read the tree the command line named.

```
config.toml                       user-owned; quorum never rewrites it
agents/<name>.toml                file-defined agents; the one config location
                                  quorum may write (merges over [agents.*])
supervisor.lock                   pid, start time, quorum's version;
                                  mtime = liveness heartbeat
projects/<slug>.json              canonical project records
tasks/<id>/task.json              task spec, reported status, session, runs
                                  (times, exit code, auto-commit note, usage),
                                  attached / perpetual, depends_on, pr_state /
                                  pr_state_at, issue_url
tasks/<id>/attached.json          attached-session liveness (latest hook event)
tasks/<id>/transcript.jsonl       harness stdout, one JSON line per line seen
tasks/<id>/reports.jsonl          `quorum task report` entries
tasks/<id>/notes.jsonl            the task's notebook: standing notes every run
                                  of it reads, plus tombstones
tasks/<id>/handoff.md             what the task left for its dependents
                                  (one per task, last write wins)
tasks/<id>/runner.lock            pid of the active run
tasks/<id>/runner.log             detached-run bootstrap output
tasks/.archive/<id>/              pruned tasks, moved here whole; dot-prefixed
                                  so every scan skips them
worktrees/<id>/                   git worktree (branch quorum/<short-id>)
prompts/<name>.md                 user-editable prompt templates
prompts/.seeded.json              {filename: sha256} of what init last seeded
prompts/<name>.local.md           per-prompt home overlay, merged at {local}
messages/board/<topic>/*.json     public append-only board
messages/inbox/<name>/new|cur/    direct mail (task-<id>, supervisor, agents)
messages/archive/YYYY-MM.jsonl.gz compacted history
state/agents/<name>/              heartbeat.json + state.json + tick.lock
                                  (+ journal.jsonl, notes.jsonl,
                                  transcript.jsonl, usage.jsonl and runs/ for
                                  harness-driven agents)
state/manager/journal.jsonl       the manager's journal: its recorded actions,
                                  tagged with the run that took them
state/manager/notes.jsonl         its notebook: standing notes plus tombstones
state/manager/transcript.jsonl    the manager harness's own stdout
state/manager/usage.jsonl         the usage log: one line per harness run
                                  ({at, run, usage|null, outcome,
                                  duration_seconds})
state/manager/runs/<run>.md       the digest that run was given, written before
                                  its harness starts; head-truncated, and only
                                  the newest SNAPSHOT_KEEP files kept
state/notify.json                 the [notify] hook's private board cursors
logs/supervisor.log, actions.jsonl
plugins/                          drop-in custom agent modules
```

Every one of those files is written atomically (dot-prefixed tmp in the same
directory, fsync, rename) and read back through two functions in `fsio.py`.
`read_json_or(path, default)` answers a dict or the caller's default —
missing, unreadable, not JSON, and JSON that is not an object are all the
default — and `read_pid(path)` answers an int pid or None. Neither raises,
because these reads happen inside scheduler jobs, digest builds and view
refreshes, where an exception is not a diagnostic but a stopped supervisor
or a blank view: a hand-edited `runner.lock` holding `[]` used to raise out
of every manager tick. Fail loudly is about the work quorum was asked to do,
not about a bookkeeping file somebody edited. `agent.read_heartbeat` applies
the same rule to heartbeats, and `MessageBus.archived_records` to
`messages/archive/`, where damage inside a gzip member raises `zlib.error`
rather than an OSError.

## Prompts

`quorum.prompts` resolves a template name three ways, in order: the home
copy `prompts/<name>.md`, else the packaged `default_prompts/<name>.md`,
else `KeyError`. Rendering is `str.format_map` over a missing-key-preserving
dict, so a template may contain braces quorum knows nothing about without
escaping discipline at the call sites.

`quorum init` seeds the packaged defaults and, on re-run, upgrades any copy
whose sha256 still equals what the seed record `prompts/.seeded.json` says
init last wrote there — a pristine seed from an older quorum. Anything else
is a user edit and is never touched, and so is any differing copy with no
record: a lost or malformed record degrades to "not upgraded", never to
"overwritten". Init also re-records a copy it finds identical to the current
default, so a home from before the record existed picks one up while its
copies are pristine. Keeping the fact in the home rather than in a list of
superseded hashes in Python is what lets a change to `default_prompts/` ship
without bookkeeping in `home.py`.

`home.classify_prompts` is that rule as one read-only function — `default`,
`upgradable`, `edited`, `missing`, plus `unreadable` for a copy it could not
decode — and the three surfaces that talk about prompt state all read it:
`quorum init` acts on it, `quorum doctor` reports it, `quorum prompt list`
renders it. Two of them once disagreed, because `prompt list` compared text
to the packaged default and had no third state to call an untouched older
seed (#126); a listing that says "edited" about a file the user never opened
sends them to hand-merge work `init` would have done.

That rule has a cliff: the first edit to `<name>.md`, however small, opts
the home out of every future upgrade to that prompt, silently — a home that
prepends five lines of house policy to `manager.md` keeps running the
manager prompt from the release it edited.

The **home overlay** removes the reason to take the cliff.
`prompts/<name>.local.md` is user-owned, never seeded, never read by `init`,
never upgraded. `render` merges it into the resolved template at the first
unescaped `{local}` slot, which the packaged `manager.md` (before "How to
work", so house rules outrank the general guidance), `task-preamble.md` and
`task-perpetual.md` carry; it is prepended when the template has no slot,
which is the case of a home that rewrote `<name>.md` before the slot
existed, where a silently dropped overlay would be the worse failure; and it
renders as nothing at all when absent or blank, taking the slot's own line
with it so an unused slot leaves no hole.

Reading the overlay is **fail-soft** (`load_local`): one that cannot be read
or decoded renders as no overlay, because `render` is on the manager tick
and every task run, and one stray byte in a user-owned file must not fail
supervision forever. Reading the *template* stays loud — it is the prompt
itself, and silently falling back to the packaged default would hide the
fork. `quorum prompt list` reports either problem, marking an unreadable
file `?` and listing the rest; with `quorum prompt diff <name>` it makes the
state of both levers visible. `local` is otherwise an ordinary placeholder
key: pass it explicitly and it wins over the file, and rewriting `<name>.md`
still overrides a whole template, so the overlay is a second, cheaper lever
rather than a replacement.

The home overlay is home-wide, which is the wrong scope for a home holding
several projects: "base on `develop`, run `just check`" is true of one repo
and wrong for the next. The **`{project}` slot** is the fourth layer, and
task-facing only. `prompts.project_block` assembles the **project overlay**
from the project's registry `notes` (`projects/<slug>.json`, editable with
`quorum project set <slug> --notes` or `--notes-file`), then
`.quorum/<name>.local.md` *inside the project directory* — user-owned,
versioned with the repo if the user wants, and read only. It is read from
the project directory rather than the task's worktree because that is the
copy the user maintains; a worktree holds whatever the task branch happens
to have.

Both reads are fail-soft, for `load_local`'s reason doubled: this one is on
every task run and the file belongs to whoever owns the repo. Both are also
*project-directory* reads, so the runner takes them before it applies the
task sandbox — `build_task_capabilities` grants the worktree and the
project's `.git`, never the project directory itself, and a read taken
afterwards would fail soft into nothing, silently disabling the feature
under `[sandbox].use_nono`. `compose_prompt` takes the project overlay as an
argument for that reason.

`{project}` follows the `{local}` rules with one deliberate difference: the
same empty-slot removal, but **no prepend fallback**. A home overlay is
policy the home already had, so rescuing it into a slotless template is
right; a project overlay is new, and there is no defensible place to put it
in a template the user rewrote. `quorum prompt list` says so instead: it
lists every project that contributes one, marks one it cannot decode `?`,
and warns when the home's `task-preamble.md` has no `{project}` slot.

The full order for the task preamble is therefore: packaged default → home
copy (wins outright) → home overlay (at `{local}`) → project notes plus
`<project>/.quorum/task-preamble.local.md` (at `{project}`). No new state
file: the notes were already in the registry, and the project file is the
user's.

## Tasks and the runner

The unit of control for a *generic* harness is the **run**: every CLI
harness supports "run in a directory with a prompt until exit, then be
invoked again", so that is the baseline contract. A task is therefore a
durable record (`tasks/<id>/task.json`) plus a sequence of runs, and
`quorum.runner.run_task` does exactly one run:

1. take `runner.lock` (O_EXCL pid-lock; one live run per task),
2. resolve the working directory — by default a git worktree under
   `QUORUM_HOME/worktrees/<id>` on branch `quorum/<short-id>`, created on
   first run, so parallel tasks on one repo can't collide and the user's
   checkout stays clean; worktrees share the main repo's object store, which
   is why a sandboxed run needs write on the project's `.git`,
3. claim everything in the task's inbox (`messages/inbox/task-<id>/`) — the
   guidance the manager and the user have sent it,
4. compose the prompt: preamble template (which teaches the
   report/inbox/memory protocol) + task prompt + dependency note + the
   task's notebook + guidance section; pick the harness argv template
   (`resume` when a session id is known, else `start`) and substitute
   `{prompt}`/`{session}` — except for an inject harness, whose prompt
   travels over stdin and whose `{prompt}` elements are dropped,
5. spawn the harness with `cwd=` the working directory, `QUORUM_HOME` and
   its own actor tag (`QUORUM_ACTOR=task-<id>` — identity only, no run id,
   no cap; the launcher's tag is stripped first); stream stdout into
   `transcript.jsonl`, capturing a `session_id` (or codex-style
   `thread_id`) from any JSON event that carries one, and whatever usage
   its result events report,
6. optionally auto-commit,
7. append the run (exit code, timestamps, usage) to `task.json`; release
   the lock.

**Auto-commit (`[tasks].auto_commit`, default off).** The delivery protocol
in the task preamble and the `STRANDED-WORK` flag in views and the digest
are advisory: neither *guarantees* work survives a harness that crashes
mid-edit or ignores its instructions. This setting is the hard guarantee —
after the harness exits, if the working tree is dirty, the runner does `git
add -A` and commits it. Branches outlive worktrees, so the work can then
only be found, never lost.

It is deliberately narrow. It fires only inside a task's *own* worktree
(paths compared `resolve()`d, so a symlinked home spelling can't disable
it), never in a `--no-worktree` task's checkout, which quorum does not own,
and never on a task whose harness already reported a terminal status, since
sweeping scratch files into a finished branch would re-flag a done task as
stranded and push junk toward its PR. It never pushes: that would assume a
remote and credentials, and an unpushed branch is already reported as
stranded work. It is mechanical, not a judgement — the runner still never
sets status. For messy crash states, `status` and staging use
`--untracked-files=all`, so a repo-level `status.showUntrackedFiles no`
cannot hide an untracked-only crash, and the commit runs `--no-verify` with
signing off, because a failing pre-commit hook or a pinentry prompt would
defeat the guarantee in exactly the unattended case it exists for. Two
states it refuses to conclude, leaving the tree dirty and flagged: a
detached HEAD (the commit would belong to no branch and die with the
worktree) and an in-progress merge/rebase/cherry-pick (`git add -A` plus
commit would finish it, conflict markers and all); under
`[sandbox].use_nono` it cannot run git at all, so it skips with a transcript
note. What happened is recorded twice — a transcript line and `auto_commit`
on the run's entry in `task.json` — and a failure is recorded the same two
ways rather than raised: the tree stays dirty, which is the state
`workdir_git_state` already reports.

**Token/cost usage (`usage.py`).** Harnesses already say what a run spent —
claude's terminal `result` event carries `total_cost_usd` and a `usage`
block, codex's `turn.completed` and `token_count` carry token counts — and
the runner is already parsing every stdout event on its way to the
transcript. So capture is one more look at each parsed event, and the result
lands as `usage` on the run's entry in `task.json`. No new file, no new
store.

- **Extraction is loose, storage is canonical.** Any event typed `result` /
  `turn.completed` / `token_count`, or carrying a top-level cost key so an
  unknown harness still gets its cost recorded, is a spend report, and the
  various key spellings normalize to one small set, so no reader branches on
  which harness ran.
- **Silence is unknown, never zero.** A harness that reports nothing records
  `usage = null`, and every reader omits the field rather than showing
  `$0.00`. Nothing in the module raises on a malformed event.
- **Within a run the reduction is elementwise max, across runs a sum.** The
  harnesses that report usage report *run-cumulative* totals, and a pumped
  multi-turn run emits one such event per turn, so summing them would
  multiply the spend. Max under-counts a hypothetical per-turn reporter,
  which is the honest direction for a number a budget may be judged against.
- **Surfacing** is pure file reading: `views.task_rows` carries `usage`,
  `usage_text` (rendered once, so the CLI and the TUI agree) and
  `budget_overages`, and the digest gets a `usage:` line per task. The
  figure is the harness CLI's own — quorum prices nothing — which matters
  because a subscription claude session reports a notional API-rate cost,
  not a bill.
- **An agent's runs go to a usage log.** The manager's tick and every prompt
  agent run through `agents/harness_run.py`, which captures usage off the
  same events; an agent has no `task.json` to hang it on, so each run
  appends one line to `state/manager/usage.jsonl` (or
  `state/agents/<name>/usage.jsonl`): `{at, run, usage|null, outcome,
  duration_seconds}`. Every run is recorded, timeouts and nonzero exits
  included — a spent-and-then-died run still spent — and `outcome` and
  `duration_seconds` ride that line rather than a file of their own (#59),
  because the run that reports no usage at all is the one an agent most
  needs to see about itself. Writing it can never fail a tick. The file is
  append-only and unbounded, so readers take a bounded tail
  (`usage.agent_usage`, `usage.agent_runs`, `AGENT_USAGE_TAIL` = 200 runs)
  and report the window alongside the figure. The digest opens with that
  figure for the manager itself, the one recurring cost nothing else in the
  digest accounts for.
- **The budget gate is a rail of the rate-limit class — the next run, never
  the current one.** `[tasks] max_cost_per_run` / `max_tokens_per_run` (0 =
  off) turn an over-budget run into a `BUDGET-EXCEEDED` digest line and a
  `$!` mark in the views, and a task whose **last** run went over is refused
  its next run: `run_task` raises before taking `runner.lock` or spending
  anything, and `quorum task run` mirrors the check so `--detach` fails in
  the parent. `--force` waives it for one run. Only the last run counts, so
  a later run under budget — or one that reported nothing, since silence is
  not evidence of spend — clears the gate on its own. It is the fourth
  substrate refusal, beside `runner.lock`, the attached-task refusal and the
  dependency refusal, and like the per-run action cap it bounds a bad task's
  blast radius and vetoes no particular choice; what to do instead of
  relaunching lives in `prompts/manager.md`, and the digest line says why a
  relaunch failed. It is deliberately not a mid-run kill: that is only
  expressible for pumped runs, and a detached run past budget finishes its
  turn and is gated afterwards. The gate never sets status and never
  cancels; the views' `$! GATED` mark is the same read rendered, so nobody
  learns of the gate only from a refused launch.

**Aggregate statistics (`stats.py`, `quorum usage`).** The figures above
answer "what did this run cost"; the questions asked after a week are
aggregate — what did this project cost, is one harness cheaper per merged
PR, how long from queue to merge. `quorum usage [--by
project|harness|week|agent] [--since 7d] [--json]` answers them as a **pure
reader with no cache** (#88): it opens `tasks/<id>/task.json`, each task's
`reports.jsonl` and the agent usage logs, recomputed on every call, so it
works with the supervisor stopped. `reports.jsonl` is read for one fact —
the instant a task last said `done`, which `task.json` does not hold. The
reduction is reused, not re-derived: a row's spend is one `usage.total` over
every run in the group. A task whose runs reported nothing is counted and
adds nothing to `cost` or `tokens`, and because a harness may report tokens
but no cost, the `reported` column renders `tasks_with_cost` wherever a cost
is shown and `tasks_with_usage` only where there is none — the `$` is the
figure a reader takes for the whole row, so the column has to be the `$`'s
own coverage. Nothing is estimated. Delivery figures come only from the
merged observation: `share_merged` is measured over the tasks with *any*
`pr_state`, never over every done task, because absence of a `pr_state`
means no `gh`, `[ci]` off or a supervisor that was never up while the PR was
open, and must not read as "not merged". A task belongs to the moment it was
queued: `--since` (`fsio.parse_window`, the window grammar shared with
`board read --since`, `board clear --before` and `task prune --older-than`)
and the `week` dimension both read `created_at`, so a window is a set of
tasks, never runs sliced mid-task.

**Mid-run guidance (`inject = "stream-json"`).** A harness whose CLI speaks
the Claude Code stream-json protocol can be steered *during* a run: the
runner spawns it with a pipe on stdin, and a `GuidancePump` thread writes
the run's composed prompt as the opening user turn, then polls the task
inbox and writes each claimed message as a further turn, which the harness
picks up at its next turn boundary. Stdin is the whole prompt channel here —
a stream-json CLI ignores an argv prompt and blocks until a turn arrives, so
an inject harness that only got its prompt via argv would hang silently
until the run timeout. Because such a harness runs until stdin closes, the
pump also owns ending the run: the protocol emits one `result` event per
completed user turn (the prompt turn is the first), so the pump closes stdin
once every delivered turn has its result and `new/` is empty — a run extends
while guidance keeps arriving and ends at the first idle turn boundary. The
claim of a message and its count as a delivered turn happen under the same
lock the close check takes, so a `result` arriving mid-claim sees the
message either still pending or already owed an answer, never neither.
Guidance that arrives after close, or that lands on a harness without
`inject`, waits in `new/` for the next run start; the maildir claim makes
the two delivery points race-free. Delivery is acknowledgement: a message
written to the harness's stdin is archived, the same contract as the
run-start claim.

The runner **never sets task status**. Status is whatever the harness last
said via `quorum task report --status <word>` — a free-form string, recorded
in `reports.jsonl`, mirrored to the board topic `tasks`, and displayed
everywhere. Only `TERMINAL_STATUSES = {done, blocked, cancelled}` mean
anything to quorum: they end the manager's attention. Quorum ships the
manager and the comms substrate, not a workflow engine.

The return channel is quorum's own CLI. The preamble tells the harness to
call `quorum task report` / `quorum task inbox --claim`; since quorum is
just a CLI writing files, any harness that can run shell commands can
cooperate, and one that can't still gets passive monitoring (transcript
mtimes, lock liveness, exit codes). Task ids are ULIDs; the human-facing
`short_id` is the ULID's *random tail*, since the head is a timestamp shared
by same-instant tasks. `fsio.resolve_handle` is the one implementation of
the id grammar — full id first, then a unique prefix or suffix,
case-insensitive, `KeyError` for nothing and `ValueError` for more than one
— and it is the same call behind board messages, notes, archived tasks and
agent runs, so a person who has learned to type six characters at a task has
learned them all.

**Where the prompt comes in.** `task add` takes the prompt from one place:
the positional argument, which is `-` to read stdin instead. A file goes in
as `quorum task add <project> - < plan.md`, so there is no second file
option to keep in step with the first. Empty or whitespace-only input is
refused before anything is written — a task with nothing to do would still
queue, launch, and spend a run. Stdin is read as *bytes* and decoded here
rather than through `sys.stdin.read`, so what lands in `task.json` is
byte-for-byte its source: the prompt is quoted verbatim into the harness's
context, and newline translation or a stripped trailing newline would make a
queued task differ from the issue it was piped from.

**From an issue (`--issue`).** Piping issue text queues the text and nothing
else, so the task carries no link back and no view can say `#62`. `task add
<project> --issue <number|url>` closes that (#62): it fetches title and body
through `forge.issue_view`, composes the prompt from them plus the url, and
records the url the forge reported as `issue_url`. A prompt given as well is
*appended* — the issue is the work, anything typed alongside it is
instructions about the work. `issue_url` is written once and never touched
again: it says where the task came from, not what happened to it, so nothing
re-probes it. Three readers share one renderer (`tasks.issue_ref`, `#62`
from the url): the CLI listing's `issue` column (dropped whole on a home
that uses none), the TUI's table, and the digest's `issue=#62` mark. `quorum
task show` prints the full url, and the preamble's `{issue}` slot tells the
harness which issue it is working from, to reference in its commits and PR —
and not to touch the issue itself.

Unlike the manager's PR probe, `--issue` **fails loudly**: a person typed a
flag and is waiting, so no `gh`, no auth, an unknown issue, a timeout or a
reply without a url is an error naming the fix, and no task is queued. A
task with an empty prompt, launched and spending a run, is much worse than
an error. Quorum only ever *reads* from a forge: nothing labels, comments on
or closes an issue.

### Stopping and restarting a run

(User-facing how-to: [guide.md](guide.md#when-a-run-hangs).)

Harness sessions hang: a stream-json CLI blocked forever on stdin (#24), a
provider turn that never returns, a wedged tool. The process is alive and
the lock is fresh, so every liveness signal quorum has says "working", while
the only kill quorum used to offer was `task cancel --kill` — terminal,
losing the task along with the hung run. Three pieces, split between
mechanism and judgement.

**`quorum task stop <id>` (`runner.stop_run`)** ends the *run* and nothing
else: status untouched (the runner never sets one; neither does this),
worktree untouched, the task still queued exactly where it was. The signal
goes to the runner's **process group**, because `launch_detached` starts a
run with `start_new_session` and that group is the only handle reaching the
harness and everything it spawned. SIGTERM, then SIGKILL after
`STOP_GRACE_SECONDS`; liveness is asked of the group (`fsio.group_alive`),
not the runner's pid, because SIGTERM kills the runner instantly while a
harness ignoring it keeps running, and a pid check would call that a clean
stop. A run sharing quorum's *own* process group (a foreground `task run`)
is signalled by pid instead, since killing that group would take the caller
with it.

**Zombies are not runs.** A process that has exited but that its parent has
not waited on is still a process-table entry, so `kill(pid, 0)` succeeds and
`killpg(pgid, 0)` answers "alive" for a group holding nothing but a corpse.
That is what every killed run looks like to a caller that stays alive after
`launch_detached`, and read as "alive" it makes `task stop` report a run
that survived SIGKILL and makes the next `task run` refuse to start. Both
ends are fixed: `launch_detached` waits on its child from a daemon thread,
and `fsio.pid_alive` / `fsio.group_alive` ask `ps` for the state letter
(`Z`) whenever the cheap signal probe says something is there.
`fsio._ps_rows` is the only place quorum shells out to `ps`, and it fails
soft *conservatively*: no `ps`, no answer, and the caller keeps the process
table's word rather than calling a live run dead.

The killed runner never gets to write its own record, so `stop` writes it: a
`quorum: run.stopped` transcript line, a `TaskRun` with `stopped = true`,
the signal as a negative exit code and the killed run's own `fresh_session`
(recorded in the lock at acquisition, since this record is that run's only
trace and the digest counts fresh restarts off it), and the now-stale lock
removed. `fsio.clear_stale_pid_lock` re-reads the pid immediately before
unlinking, which narrows but does not close the window: `acquire_pid_lock`
takes a stale lock over by unlink-and-create, so a new runner can still
claim the file in between. There is no compare-and-unlink without the flock
the pid-lock deliberately avoids, and the residue — a live run whose lock
file is gone, recreated by the next acquisition — is not worth one. If the
runner did record the run itself, that record stands and nothing is
duplicated. A lock whose runner is *already* dead gets the same tidying
without a signal; only a task with no lock at all has "no live run to stop".
An **attached** task is refused outright: the same substrate rail as the
runner's, since the "runner" of an attached task is the user's own session.

**`quorum task run <id> --fresh-session`** clears the captured
`session`/`thread_id` before composing the argv, so the harness starts a new
session instead of resuming a damaged one. The worktree — the actual durable
state — is untouched; the session was only ever a convenience. The new
session remembers nothing, so the caller is expected to send a summary as
guidance, and the run records `fresh_session = true`.

**`[tasks].run_stall_timeout_seconds`** (0 = off, the default) is the
mechanical version, and needs no manager at all: `runner.StallWatchdog`
watches the stdout stream the runner is already reading, and when no line
arrives for N seconds it notes the stall in the transcript, SIGTERMs the
harness (SIGKILL after the same grace) and lets the run end the ordinary way
— so the run record, auto-commit and lock release all still happen, with
`stalled = true` on the record. That turns a hang into a dead runner with a
non-terminal status, which supervision already handles well. It counts
silence, not progress, so the threshold has to sit above the longest silent
step a real run takes; that is why it is off by default and why quorum never
picks a value. The watchdog signals the **harness only**, never the group:
the runner leads that group, so a `killpg` from inside would kill the run's
own bookkeeping. One limitation follows — a grandchild that inherited the
harness's stdout and outlived it holds the pipe open, so the runner stays
blocked and the watchdog's kill does not by itself end the run. The cure
there is the group-wide `quorum task stop`, which is why the watchdog does
not replace it.

All three are visible in the digest as `stopped=N` / `fresh_sessions=N` /
`last-run=stalled` on the task line, which is how the manager knows what it
has already tried without relying on its bounded journal window.

### Perpetual tasks

(User-facing how-to: [guide.md](guide.md#perpetual-tasks).)

`quorum task add --perpetual` sets `perpetual = true` on the task record.
Nothing about the substrate changes: the runner still does one run, status
is still a free-form reported word, and the manager still relaunches any
task whose runner died with a non-terminal status — which is *already* an
endless loop for a task that never reports one. The flag exists because
three readings of that substrate were wrong for a task not trying to finish:

- **the run preamble.** `compose_prompt` substitutes the preamble's
  `{perpetual}` placeholder with `prompts/task-perpetual.md` (empty for an
  ordinary task): work in cycles, commit and push *every* cycle rather than
  "before finishing", report a changing status word per cycle so an
  unchanging one still means something, and never report `done` or
  `cancelled`. Both files are ordinary user-editable prompts.
- **the digest.** The task line carries `perpetual=true` (only when true, so
  ordinary lines are untouched), and the `possible-loop` observation is
  **suppressed** for it: that flag reads repetition in a live run as a
  symptom, and for a task whose job is a repeating cycle it would fire every
  tick, teaching the manager to ignore a signal that still means something
  everywhere else.
- **the manager prompt.** `prompts/manager.md` is told to relaunch it
  forever, to never read a long `runs=` count or a cycling status as stuck,
  to never cancel it (only the user ends it, with `task cancel`), and to
  judge it on its per-cycle reports and git state.

Views badge it (`∞`) so "still running after 40 runs" reads as working. Two
consequences are worth knowing before queuing one. Runs reuse the task's
worktree and its captured session id, so a `resume` template hands the
harness an ever-growing context; expect a session reset eventually, which is
clearing `session` in `tasks/<id>/task.json` and costs no work, since the
worktree keeps it. And the manager's tick cadence is the floor on cycle
latency: nothing relaunches a perpetual task between ticks, so with the
default `every 5m` schedule a cycle that ends is idle for up to five
minutes. Tighten the schedule if the loop needs to be tighter; there is
deliberately no self-relaunch path in the runner, which would be a second
scheduler.

### Task dependencies

(User-facing how-to: [guide.md](guide.md#dependencies-and-handoffs).)

`quorum task add … --after <id>` (repeatable) records `depends_on` — a list
of **full** task ids — in `task.json`. It is deliberately *not* a DAG
engine: nothing schedules on it, nothing topologically sorts, nothing fans
out. The manager still makes every launch decision; dependencies only tell
it when a launch would be premature. Cross-project chains work by
construction, since ids are global.

- **Validation happens once, at `task add`** (`tasks.resolve_dependencies`):
  handles resolve through the same grammar as everything else and are stored
  expanded, an unknown or ambiguous handle fails the command, and a task
  cannot depend on itself. Depending on a **perpetual** task is refused too:
  it never reaches a terminal status, so the dependent would wait forever. A
  dependency must already exist, so a cycle is only reachable by
  hand-editing `task.json`.
- **Reading is total** (`tasks.dependency_state`, pure over an
  already-loaded task listing, so every reader stays a file reader):
  `waiting_on` = dependencies that have not reached a terminal status;
  `failed` = dependencies that ended `blocked` or `cancelled`; `missing` =
  dependencies whose task record is gone; and `cycle`, detected rather than
  recursed into. A hand-edited `depends_on` never raises.
- **Only a dependency that still might finish blocks.** `failed` and
  `missing` are upstreams that can never reach `done`, and both are
  deliberately kept *out* of `waiting_on`: calling an unsatisfiable
  dependency "waiting" would hide the decision behind a task that silently
  never runs. They are reported instead, and the manager or the user decides.
- **The digest observes** (`waiting-on=<short ids>`; `DEP-FAILED` /
  `DEP-MISSING` / `DEP-CYCLE` with a line of explanation). The manager
  judges them and quorum does nothing on its own.
- **One narrow substrate refusal**: `run_task` (and `quorum task run`, so
  `--detach` fails in the parent too) refuses a task with unfinished
  dependencies unless `--force`. This is the third rail of that class, next
  to `runner.lock` and the attached-task refusal, justified the same way: a
  dependent launched early is pure waste (it reviews a PR that does not
  exist yet), and the manager is the only caller that would do it by
  accident. It refuses the launch; it never cancels, re-queues or reorders.
- **Views** render `waiting_on` / `dep_failed` / `dep_missing` / `dep_cycle`
  straight off `views.task_rows`. Nothing is materialized to disk for them,
  unlike the merged observation ([below](#the-merged-observation)):
  dependencies are derivable from files quorum already holds.
- **Reading the upstream outcome**: the dependent's composed prompt gains a
  *Tasks this one depends on* block listing each dependency's short id,
  status and `pr_url` (`runner.dependency_note`), plus the upstream's
  handoff when it wrote one, and points at `quorum task show <id>` for the
  full record. That is deliberately the whole mechanism: no `{depends.*}`
  substitution, no result-passing channel beyond the handoff file.

#### The handoff

Status and `pr_url` answer "may I start"; they do not answer "what do I
build on" — what changed, what was left undone, what to look at first. The
handoff is the upstream's answer, and the one piece of new durable state in
this section (#92).

`quorum task report <id> --status done --handoff <file|->` stores the body
whole at `tasks/<id>/handoff.md` (`tasks.write_handoff`, an
`atomic_write_text`, so a dependent composing its prompt never reads a
partial file). It is written *before* the status changes, so a dependent
that sees `done` sees the handoff that came with it. One per task, not a
log: a later `--handoff` replaces it, because it describes the finished
state and the finished state has no history worth keeping. Any status may
carry one — a `blocked` task can leave notes for whoever picks it up — and
an empty body is refused before anything is journaled or written.

It is capped when rendered and whole when asked for. `dependency_note`
appends a `## Handoff from <id>` section per dependency that has one, cut to
`runner.HANDOFF_MAX_BYTES` (8 KiB) with a line saying how many bytes were
dropped and that `task show` has the rest. The cap is *per dependency*,
following the notebook's rule that each read into a prompt has a budget
nothing else spends: a long-winded upstream cannot crowd out a terse one.
The digest says only `handoff=true` on the finished task's line — existence
is all the manager needs, the body is for the dependent — and reading fails
soft, since both readers are on the prompt-composition and digest paths.

It is asked for, never generated. The preamble tells a task to leave one
when `quorum task show <its id>` lists `dependents:` — the reverse read of
`depends_on`, computed from the listing `task show` already loads — and what
to put in it. Nothing in Python writes a handoff for a task that did not,
and nothing summarizes one: what to keep is the model's decision, the file
is where it keeps it.

### The task notebook

A task's only memory between runs used to be its harness session, and the
session is exactly what compaction, a `--fresh-session` restart or a crash
throws away. The task notebook is the manager's notebook (`notes.py`)
generalized to a task, on the same substrate and under the same rules:

- **Layout.** `tasks/<id>/notes.jsonl`, append-only, one entry per line with
  the same schema as `state/manager/notes.jsonl`: `{id, ts, run_id, sender,
  text, ttl_days?}` for a note, `{…, retired: true}` for the tombstone
  `quorum task forget <id> <note>` appends. It lives in the task directory
  rather than under `state/`, so `quorum task prune` moves it with the task.
- **Owner.** A task's actor identity is `task-<full id>` — the same string
  as its inbox name — so one name addresses a task on the bus and in the
  CLI. The runner sets `QUORUM_ACTOR=task-<id>` on the harness it spawns for
  *identity only*: `_actor_guard` treats a task actor like a human, with no
  run id, no journal and no action cap, because reports.jsonl and the
  transcript are a task's record and the runner is its rail. One side
  effect: a task's `task nudge` and `board post` carry `task-<id>` as
  sender, not `user`.
- **Fence.** `notes.Notebook.may_write` admits the owner, the manager (a
  standing instruction for a task's next run is the natural complement to
  one-shot guidance) and an untagged human; any other task and any prompt
  agent is refused with a pointer to `task nudge`. "The manager" is read
  from config by *type* (`notes.manager_writers`), so a renamed manager is
  admitted; a config quorum cannot parse falls back to the literal name
  rather than raising, because this is read on every task run. The fence
  reads `QUORUM_ACTOR`, which any process that can run the CLI can set, so
  it is a **convention against accidental crowding, not a security
  boundary** — the sandbox is. Tagging a task's harness is also what keeps
  it out of the manager's notebook; a task reaches the manager with `quorum
  task report` and the board.
- **Reader.** `runner.compose_prompt` renders the notebook into every
  composed prompt — a resumed session and a fresh one alike, because the
  fresh one is the run that needs it — after the task body and the
  dependency note and before the guidance section, under its own
  `TASK_NOTES_MAX_ENTRIES` / `TASK_NOTES_MAX_BYTES`; over the cap the newest
  notes are kept and a line says how many older ones were dropped, and a
  file grown past `NOTES_SCAN_BYTES` says how many bytes went unread. An
  empty notebook renders nothing. `quorum task show` prints the same
  rendering; the digest deliberately does not, because the manager reads
  reports and the notebook is the task's own.
- **Attached tasks are the exception.** An attached session does not go
  through the runner, so nothing composes a prompt for it and **its notebook
  is never rendered into the session**; `quorum task show <id>` is the read
  path there. The writes work — it is an ordinary file — but the session has
  to be handed the content, by a `task nudge` or by the user pasting what
  `task show` printed. The identity differs too: an attached session runs
  under the user's own shell with no `QUORUM_ACTOR` set, so its `task
  remember` is admitted as a human and its notes carry `sender: user`.
  Closing this would mean `task hook-session-start` injecting the notebook
  the way `hook-stop` injects pending guidance; that is not done.
- **Policy.** The preamble says what the notebook is for — state worth
  having after a restart, not a log — and to rewrite one superseding note
  rather than append when the list grows. Nothing consolidates in Python;
  expiry (`--ttl`) is the only automatic retirement.

`notes.py` carries the two notebooks as one `Notebook` value (path, owner,
extra writers, header, budget, the command names its rendering teaches),
with `agent_notebook` and `task_notebook` the only entry points, and `quorum
task remember|forget` share one `cli._notebook_write` with `manager
remember|forget`, so the fence decision, the journaled refusal and its
wording exist once.

### Attached tasks: adopting a live session

(User-facing how-to: [guide.md](guide.md#adopting-a-live-session).)

`quorum task adopt` inverts the ownership: instead of quorum spawning runs,
an *existing interactive session* (Claude Code, or anything with hooks) is
recorded as a task with `attached = true`, `workdir` = the session's own
directory, no worktree, and the harness's session id when known. Quorum
never spawns runs for it — `run_task` refuses attached tasks outright, a
substrate rail in the same class as `runner.lock`, protecting the user's
live checkout from a racing headless run. `quorum task detach` lifts it.

Liveness for a run quorum didn't spawn comes from `tasks/<id>/attached.json`,
rewritten by harness-side hooks (`quorum task hook-session-start`,
`hook-stop`, `hook-session-end`) with the latest lifecycle event. The hook
entry points are harness-agnostic — JSON with `session_id`/`cwd` on stdin,
matched to an attached task by exact session id first, then working
directory. The cwd fallback is how an id-less adoption *learns* its session
id, and it fires only while the task has no live session of its own, so a
second concurrent session in the same checkout can't steal guidance or the
session id. `integrations/` ships an adapter per harness: `claude-code/` and
`codex/` wire native Stop/SessionEnd(/SessionStart) hooks straight to the
CLI, both speaking the same stdin payload and `{"decision": "block"}`
continuation protocol, while `opencode/` (no hook commands; an in-process
plugin bus instead) ships a fail-soft JS plugin that calls `hook-stop
--format text` on idle events and injects whatever the CLI prints as a user
turn. Either way the digest renders attached tasks in their own section, and
guidance flows through the ordinary task inbox: the stop/idle hook claims
pending messages and continues the session with them, so `task nudge`
reaches the human's live session at its next stop. Delivery consumes the
guidance, so continuation can't loop, and the maildir claim keeps the
delivery point race-free against a headless run after detach.

**herdr (optional).** When the session runs inside a
[herdr](https://herdr.dev) pane (`task adopt --herdr-pane <id>`), `herdr.py`
— the one module speaking herdr's local socket API — adds the pane's
detected agent status (`herdr: state=working|blocked|idle` in the digest; a
busy session fires no hooks, so this is a better signal than mtimes) and a
doorbell on `task nudge`, which is how a session with no quorum adapter
installed learns that guidance is waiting. The adapter fails *soft* by
design, the opposite of sandbox.py's fail-closed, because observation
enrichment must never break a digest. The inbox remains the single
transport: the doorbell never carries the payload, so delivery stays
exactly-once across all delivery points.

### Pruning: on-demand cleanup

Quorum accumulates: a finished task keeps its directory, its worktree, and
its `quorum/<short-id>` branch forever, and the board grows until the hourly
janitor's retention window catches up. `quorum task prune`, `quorum board
clear <topic>`, `quorum board ack <message-id>` and `quorum task inbox <id>
--clear` are the hand-driven tidies, and all follow the bus's rule:
**archive, never delete.** A pruned task's directory is *moved* to
`tasks/.archive/<id>/` by one `os.rename`; the name is dot-prefixed on
purpose, because `TaskStore.list` already skips dot-entries and every reader
goes through it, so an archived task leaves all of them with no code change
anywhere, and restoring one is `mv` in the other direction. Cleared board
and inbox messages go into the same `messages/archive/YYYY-MM.jsonl.gz` the
janitor writes, keeping their `created_at`; `board ack` takes one message id
and `board clear` a whole topic, one spelling each, and `inbox --clear`
touches `new/` only, because a message in `cur/` has a claimant.

`prune.py` splits into total readers and two doers — `select()` (pure, over
an already-loaded task list), `refusal()`, `dependents_first()` (pure batch
ordering), `plan()`, `worktree_plan()`, then `remove_task_worktree()` and
`archive_task()` — so the selection is reusable rather than tangled into the
command.

The refusals are substrate rails of the runner's class, not manager policy:
a **live runner** would keep writing into a directory that moved out from
under it; an **attached** task's working directory is the user's own
checkout; a task **something else still depends on** would leave a dangling
`depends_on` (a dependent pruned in the same pass is not a reason to keep
it); and **stranded work** — uncommitted or unpushed commits in the
worktree, read with the same `workdir_git_state` probe the digest uses — is
exactly what the rest of quorum works to keep visible, so archiving the one
record that surfaces it would hide it. Only the last is overridable, with
`--force`; the other three name an action the user can take instead. The
stranded-work check is skipped for a `use_worktree = false` task, whose
working directory is the user's own checkout: the dirt there is theirs.

`--worktrees` adds `git worktree remove` plus branch deletion, and treats
the two asymmetrically because git does. **`--force` is never passed to `git
worktree remove`**: uncommitted and untracked files in a worktree are the
stranded work the rest of quorum surfaces, and no flag on a tidy-up command
should destroy them, so a removal git refuses leaves the worktree alone
*and* the task unarchived — the record is the only thing that would have
said the work was there. `--force` therefore has exactly two meanings: waive
the stranded-work refusal, and upgrade `git branch -d` to `-D`. The second
does lose data, which is why it is behind the flag and said out loud in the
confirm prompt; unforced, an unmerged branch is kept with a note and the
task archived anyway, its commits still in the repo.

The archive loop re-derives `refusal()` for each task immediately before
archiving it, because `plan()` ran before an interactive confirm a runner
could have started during, and because a task skipped mid-sweep leaves the
batch: an upstream that passed the dependency check only because its
dependent was going too is refused again rather than archived into a
dangling `depends_on`. `plan()` returns the batch `dependents_first()`,
which is what makes one in-order pass enough. `--dry-run` prints the same
plan and touches nothing. A prune journals one entry through `_actor_guard`,
not one per task: it is a single decision, and per-task entries would burn
an agent's action cap mid-sweep and leave the tidy half-finished.

### Exporting a task: one archive for sharing a run

Everything about a task is on disk, in three places: `tasks/<id>/`,
`messages/inbox/task-<id>/` (guidance waiting or claimed) and — once a run
has archived it — `messages/archive/YYYY-MM.jsonl.gz`, where delivered
guidance sits mixed with every other message. `quorum task export <id>
[--out <path>] [--with-worktree-diff] [--redact]` collects them into one
`.tar.gz`, so sharing a run or attaching it to a bug report does not mean
knowing that layout: the task directory whole (tmp files skipped), an
`export.json` manifest, `inbox/new/` and `inbox/cur/`, an
`inbox/delivered.jsonl` of archived messages addressed to this task (read
from the months of its creation onward, since a message can only be archived
at or after it), and optionally `worktree.diff`.

It is a reader of the same class as `prune.py`'s plan half, and the theme's
stances (#88) hold: **no new state** — the manifest and `delivered.jsonl`
are composed in memory; **read-only** apart from the output file, refused
inside the home (an archive under `tasks/<id>/` would be swept into the next
export of that task) and refused over an existing file; **nothing from the
project directory**. That last one is why the diff is refused, loudly, for
an attached or `--no-worktree` task: its working directory is the user's
checkout, and the person asked for the diff, so silently omitting it would
be the wrong kind of quiet. For a task with a quorum-made worktree,
`worktree.diff` is `git diff` against the merge base with the same base
`worktree_changed_paths` uses, plus one `git diff --no-index` per untracked
file — read-only plumbing, no fetch. `runner.lock` is the one file left out:
it is a pid on this machine, not a fact about the task, and an unpacked
archive must not look like it holds a live run. Member ownership is
stripped, and the archive is written beside its final name and renamed into
place.

`--redact` exists because tool results are where transcripts carry file
contents, command output and secrets read off disk. It rewrites
`transcript.jsonl` in the archive (never on disk), keeping the assistant's
text, its thinking, and every tool *call* — name and arguments, which is how
a reader follows what the run did — and replacing each tool *result* with a
marker. The walk is structural and loose in the mold of `loop_signal`'s
tool-call extraction: a result-kind dict loses its output fields and keeps
its ids, its remaining keys are walked rather than copied so a payload filed
under a name this module does not know is still reached, and past a depth
bound a node is replaced rather than kept, because a redaction's failure
direction has to be "dropped". A plain-text harness's `line` entries have no
structure to redact and are kept verbatim — the command says how many, so
nobody mistakes a `--redact` of an opencode transcript for a clean one.
There is no `--redact` of reports or guidance, which are what people wrote.

## The manager

(User-facing how-to: [guide.md](guide.md#the-manager).)

The flagship built-in agent, and it is *itself* harness-driven: supervision
policy is a prompt (`prompts/manager.md`), not Python. Each tick:

1. **Wake condition**: any non-terminal task, or a pending message in the
   manager's inbox. Nothing to manage → no harness run. Dead runners keep
   the condition true, which is what makes post-outage recovery automatic.
2. **Digest** (`agents/manager.py::build_digest`, a pure function over
   files): every active task's status, runner liveness, quiet time, recent
   reports and transcript tail, plus a `git:` line when its working
   directory holds uncommitted changes or unpushed commits, and
   `perpetual=true` on a task not meant to finish; attached sessions in
   their own clearly-labeled section (last hook event age, git state,
   reports — never runner liveness, which they don't have); recently
   finished tasks, marked `STRANDED-WORK dirty=N unpushed=M` when they ended
   with work left undelivered in the worktree, which the default manager
   prompt treats as not done. A task line then carries, as they apply: a
   `ci:` line and `CI-FAILING`; `waiting-on=` and the `DEP-*` flags;
   `possible-loop:`; `STALLED`, with the `stopped=` / `fresh_sessions=` /
   `last-run=stalled` counts of what has already been done about it; a
   `usage:` line and `BUDGET-EXCEEDED`; `overlaps=` with an `overlap:` line.
   Above the tasks sit a three-line **self-observation header** — what the
   manager's own recent runs have cost, how its last N runs ended, and
   `Actions this run: 0 of <cap>` (always `0`: the digest is built before
   the run starts, so the line reports the budget, and what happened to it
   lands in the journal as `cap.hit`) — its **notebook**, its recent
   **journal** with then-vs-now status per target (the anti-loop memory),
   and any user guidance claimed from `messages/inbox/manager/` (`quorum
   manager tell`). Every one of those is an observation the prompt judges;
   none is a rail — nothing pauses, throttles or changes the cap. Claimed
   guidance is acknowledged only after a successful run; a crash rejects it
   back to `new/`. If the manager's harness sets `inject = "stream-json"`,
   guidance arriving *while* a tick's run is in flight is pumped into it as
   user turns instead of waiting for the next tick.
3. **One harness run** over `prompts/manager.md` + the digest, synchronous,
   cwd = `QUORUM_HOME`, bounded by `run_timeout_seconds`, stdout streamed to
   `state/manager/transcript.jsonl`. The env carries the actor tag
   (`actor.py`): `QUORUM_ACTOR=manager`, a per-run `QUORUM_ACTOR_RUN` id,
   and the resolved action cap in `QUORUM_ACTOR_CAP`. The tag is
   name-generic — any harness-driven agent identifies itself the same way,
   and the CLI journals it under `state/agents/<name>/journal.jsonl`, the
   manager keeping its historical `state/manager/` spot.

The harness acts with full authority through the quorum CLI — `task
add/run/nudge/cancel`, `agent pause/resume/run-now`, `board post`, and
`quorum manager note` to journal a reason. **Every mutating CLI action taken
under the manager's env tag is journaled automatically** (action, target,
the target's status at action time, run id) *before* it executes — ground
truth, not model self-report. The journal serves two purposes: fed back into
the next digest, it lets the manager see which interventions changed nothing
and avoid degenerate loops (its prompt forbids repeating an intervention
marked UNCHANGED); and it enforces the one supervision rail quorum keeps, a
per-run action cap (`max_actions_per_run`) that bounds a bad run's blast
radius without ever second-guessing a choice. A refused action appends one
`cap.hit` entry per run, naming the action refused, so the run that ran out
of budget is visible in the *next* digest rather than only as an error the
model saw mid-run; that entry is not itself an action and does not count
against the cap. The task budget gate is the only other rail of that class.

**The notebook (`notes.py`).** The journal is what the manager *did* this
run, read back as a bounded tail; a note meant for next week is pushed out
of that window by the next busy tick. The notebook is the other memory:
`state/manager/notes.jsonl` (per-agent `state/agents/<name>/notes.jsonl`),
append-only, one entry per line — `{id, ts, run_id, sender, text,
ttl_days?}` for a note and `{…, retired: true}` for the tombstone `quorum
manager forget` appends. It is written with `quorum manager remember "…"
[--ttl N]`, which goes through `_actor_guard` like every other mutating
command, so a note is journaled, attributed and counted against the run's
action cap.

It is a **separate buffer** on both sides, and that is the whole design. It
is not a board topic, so nothing posts into it in the ordinary course of
things: only the notebook's own agent, its declared extra writers and an
untagged human may write, and a call tagged as a task or another agent is
refused with a pointer to `task report` and `board post attention`. That
fence reads `QUORUM_ACTOR`, which any process that can run the CLI can set,
so it keeps honest callers out of each other's memory and stops accidental
crowding; the real boundary around a notebook is the filesystem the run is
given (`sandbox.py`). On the read side `Notebook.render` renders it
**before** the task section, under `NOTES_MAX_ENTRIES` / `NOTES_MAX_BYTES`,
which nothing else in the digest spends, so ten live tasks with long report
tails cannot shrink it. Over the cap the newest notes are kept and the
digest says how many older ones it dropped; when the file has outgrown
`NOTES_SCAN_BYTES` the section says how many bytes went unscanned, so a
truncated memory is visible rather than silent. Nothing is compacted or
summarized in Python: expiry (`ttl_days`) is the only automatic retirement,
and consolidation — one superseding note, then `forget` the ones it replaced
— is policy in `prompts/manager.md`.

The four signals below are **observations, not rails**: Python makes no
decision on any of them, their thresholds are plain module constants tuned
to prefer false negatives (a flag that fires on healthy work teaches the
manager to ignore it), and the only rails stay the rate limits that never
read them.

**Loop observation (`possible-loop`).** The journal remembers what the
*manager* did; nothing else sees the other loop class — a task harness
spinning inside a single run, repeating the same failing tool call while
`runner.lock` stays live and the transcript keeps growing, which to every
other signal looks like healthy work. `loop_signal` (pure over files, no
state file) scans a transcript tail bounded twice — `LOOP_SCAN_LINES` (120)
entries from at most `LOOP_SCAN_BYTES` (2 MiB), the byte cap being the
binding one on payload-heavy transcripts — extracts tool calls, and scores
the last `LOOP_WINDOW_CALLS` (12) of them. The evidence must be current:
only a live runner is scored (the transcript is append-only; a dead task
would stay flagged forever) and only entries newer than the last *completed*
run, so a relaunch is not indicted by its predecessor's spinning. A
perpetual task is skipped entirely — repetition is its job.

Extraction is deliberately loose: a recursive walk for any nested dict
tagged `tool_use` / `tool_call` / `function_call` / `command_execution` /
`local_shell_call`, or carrying a string `tool_name`, counted once per call
id so harnesses that pair started/completed events don't double-count, with
each call becoming `name + sha256(arguments)[:12]`. It sees only structured
JSON events: a harness that prints plain text is unobservable here, and
absence of the flag is not evidence of health. The hash keeps argument
payloads off the flag line itself, but it is not a secrecy boundary — the
digest's adjacent `out|` tail lines still quote raw events.

A flag needs **both** a call repeated `LOOP_REPEAT_THRESHOLD` (4) times *and*
that repetition dominating the window (`distinct/total <=
LOOP_DISTINCT_RATIO`, 0.5). The double gate keeps polling interleaved with
real work, retries whose arguments change, and short tails quiet, at the
cost of missing a loop that only repeats three times. That is the deliberate
divergence from OpenHands' stuck detector, which auto-halts; here the
default manager prompt tells the manager to read the tail and judge.

**Overlap observation (`overlaps=`).** The motivating incident: the manager
launched two queued tasks in one tick, both forked from the same base, and
both PRs came back `MERGE-CONFLICT`; the digest showed each task's git state
in isolation, so it had nothing to judge overlap *with*.
`overlap_signal(live)` closes that gap at digest build: for every pair of
live worktree tasks on the same project it intersects the path sets their
worktrees have changed, read by `tasks.worktree_changed_paths` — the working
tree against the merge-base with the base branch, so committed work, staged
and unstaged edits and untracked files all count, since a live task has
usually not committed what it is touching right now. The base is the branch
checked out in the repository's main worktree, because that is what the
runner forked the task branch from. It has to come first: a checkout one
unpushed commit ahead of the remote, measured against `origin/HEAD` instead,
would put that commit's paths into *every* live task's changed set and
report an overlap on a file neither task wrote. `refs/remotes/origin/HEAD`
is the fallback, and the branch's own upstream the one after that; with none
of those the task is unobservable. Read-only git plumbing, no network, never
`git fetch`.

A non-empty intersection renders ` overlaps=<short-id> paths=N` on *both*
task lines plus an `overlap:` line naming at most `OVERLAP_MAX_PATHS` (3) of
the shared paths. Attached sessions and `--no-worktree` tasks are never
compared: that directory is the human's checkout. Cost is bounded by
`OVERLAP_MAX_PAIRS` (20) pairs per digest, spent in digest order, so a home
with more concurrency than budget still sees its first pairs. Parallel edits
to one file are sometimes exactly the job, so `prompts/manager.md` tells the
manager to judge — steer both to fetch and rebase before pushing, or
serialize the two — and the task preamble's delivery protocol carries the
matching step on the other side: fetch and rebase onto the base branch
before pushing, push again with `--force-with-lease` (never a bare
`--force`, never off the task's own branch) when a rebase leaves an
already-pushed branch unable to fast-forward, and report `blocked` naming
the conflicting files when the rebase cannot complete. Views never show it:
the read happens at digest build only, and nothing is materialized.

**Stall observation (`STALLED`).** The half of *Stopping and restarting a
run* that judges. `stall_minutes` reads the mtime of a task's transcript —
deliberately not `last_activity`, which also counts the runner lock and the
reports file, both of which a hung run leaves fresh — falling back to when
the live run acquired its lock when there is no transcript at all, because a
*first* run that hangs before printing anything (#24's stdin block) is the
loudest hang there is and the one with nothing to age. A live runner silent
for longer than `STALL_QUIET_MINUTES` (30) gets the flag; a dead one is
simply a task to relaunch. `prompts/manager.md` holds the policy: look at
the tail once, `task stop` then relaunch, then relaunch `--fresh-session`
with a summary sent as guidance, then escalate on the `attention` topic
after two fresh restarts, reading which step it is at off the task line's
own counts. The rail-shaped answer to the same failure is the runner's stall
watchdog, which is opt-in config rather than supervision.

**CI observation (`ci:`).** `workdir_git_state` follows work as far as
"pushed" and stops; `ci.py` — the digest-facing half over `forge.py` —
carries it one step further, to whether what was pushed actually works. For
each digested task with a working directory it runs one `gh pr view --json
number,url,state,isDraft,mergeable,statusCheckRollup` *inside that
directory*, so gh resolves the repository from the remote and the PR from
the checked-out branch. The rollup mixes CheckRun and StatusContext shapes;
`_verdict` classifies each as pass/fail/pending and treats anything it does
not recognize as pending, because an unknown shape must never read as a
pass. The line is `key=value` like the rest of the digest — the PR's own
state (normalized by `ci.normalize_state`), check counts, up to five failing
check *names*, `MERGE-CONFLICT` when `mergeable` is `CONFLICTING`, and the
PR url. What to *do* about red CI lives in `prompts/manager.md`, and the
shipped `prompts/babysitter.md` is a whole reactive policy written as prompt
text.

It **fails soft**, the herdr contract rather than the sandbox one: no `gh`,
no auth, no remote, no PR for the branch, a timeout, or unparseable output
all degrade to `None`, and the digest is byte-identical to one built with
the probe off (a test asserts that). A missing `ci:` line therefore carries
no information at all — including under a self-sandboxed supervisor, where
the blocked network makes every probe return `None`. Cost is bounded twice,
because digest build blocks the tick: `[ci].timeout_seconds` (10) per call,
and `CI_MAX_PROBES` (12) probes per digest, spent in digest order.
`[ci].enabled = false` skips the probe — and so does a config.toml quorum
cannot read at all: the table it failed to parse may be the one holding that
switch, so an unreadable config means *disabled*, never "defaults". That is
the general policy for the two fail-soft probes (`ci.py`, `herdr.py`): they
read config through `config.try_load_config`, whose `None` means "no usable
config" and therefore off, while the read-only views degrade through
`config.load_config_or_default` — the single fallback helper the CLI, views
and digest share — which fills in defaults but is never allowed to *enable*
something a user may have switched off.

#### The forge seam (`forge.py`)

One rule: **exactly one module shells out to a forge CLI.** It holds both CI
observation and issue intake (#62), which is why it is `forge.py` rather
than `ci.py`; `ci.py` keeps the digest half. Three entry points, two
contracts:

- `run_json(home, workdir, *args)` — the **soft** call behind
  `ci.pr_state`: any failure is `None`, because a digest must build.
- `auth_status(home)` — `True` / `False` / `None` for `quorum doctor`'s gh
  line, soft in the same way, so the diagnostic never grows a gh subprocess
  of its own (`None` is "no answer", not "broken").
- `issue_view(home, ref, workdir)` — the **loud** call behind `task add
  --issue`: it raises `ForgeError` with the fix in the message. Its `ref`
  parsing (`62`, `#62`, or a full issue url) is pure and happens *before*
  the subprocess, so a typo costs nothing and a pull-request url is refused
  rather than fetched as an issue. A url is handed to the CLI whole rather
  than reduced to its number: it may name an issue in a different repository
  than the project's, and only the url says which.

Both contracts share `_invoke`, the single `subprocess.run` of the whole
codebase for a forge, so the unattended-invocation details (`GH_PAGER=cat`,
no prompts, no colour, stdin closed, `[ci].timeout_seconds`) are stated
once; the soft half degrades every exception to `None`, and the loud half
tells a timeout apart from a call that never started, because those have
different fixes. Provider selection is `cli_name(home)`, today a constant:
that one function is where #51's `[ci].provider = gh | glab | none` switch
lands, and because every subprocess asks it, a second backend cannot
re-introduce a `gh` call somewhere else.

There is no write path. Not "not yet" — reading an issue to make a task out
of it does not imply permission to comment on, label or close it, and a
supervisor that edits a forge behind a human's back is exactly the kind of
privileged infrastructure quorum refuses.

#### The merged observation

A task's lifecycle ends at the harness's word (`done`); its work is
delivered when the PR merges. Quorum keeps the two apart — `done` is the
harness's word, merged is the forge's, and **nothing in Python turns one
into the other** — but the second fact has to survive the probe that saw it,
because the surfaces that would show it never make network calls.

So there is exactly one materialized probe result. When `build_digest`'s
probe returns a state in `tasks.PR_STATES` (`open` / `merged` / `closed`),
`tasks.record_pr_state` writes it — with `pr_state_at` — onto
`tasks/<id>/task.json`, and every reader then gets it off the file:
`quorum status` / `task list` / `task show` and the TUI badge `✔` merged and
`⊘` closed-unmerged straight out of `views.task_rows`, while `views.py`
still never acquires a `gh` subprocess. The section used to say that nothing
materializes a probe result; #57 narrowed that to "nothing but a durable
one", because a merge is final in a way a check rollup (true only as of now)
is not. The exception is fenced by five properties, and a second
materialized probe result would have to earn all of them again:

- **one writer**, `record_pr_state`, called from **one place**, the digest's
  probe closure — and `open` only for a task whose status is terminal. A
  live task's `task.json` is being written by its own detached runner (the
  one file race quorum has no lock for), and `open`, the state a PR sits in
  for the whole of a task's working life, is rendered by no surface, so
  taking that race for it buys nothing. A merge or a close is written
  wherever it is seen, live task included: it is durable, every surface
  badges it, and a PR can land while its task is still running. A task
  already recorded `merged` is not probed again — merge is final.
- **closed vocabulary**: only the three known states are written. `unknown`
  writes nothing, so a forge shape quorum does not understand can never
  badge a task as delivered.
- **never a status**: `status` and `pr_state` are separate fields, and no
  code path derives one from the other.
- **`updated_at` untouched**. It means "someone acted on this task", and the
  digest's recently-finished window is measured from it; a probe that bumped
  it would pin every merged task in that section forever. The record is
  re-read from disk rather than dumped from the in-memory `Task`, so a
  status a live run reported meanwhile is never rolled back.
- **fail-soft to the end**: an unreadable or unwritable `task.json` is a
  lost observation, re-made on the next tick, never a failed digest.

Absence stays uninformative, exactly as the missing `ci:` line is: no
`pr_state` means no PR, no `gh`, `[ci]` off, or a supervisor that was never
up while the PR was open — never "not merged". A recorded state is only as
fresh as `pr_state_at`, which every surface that has room prints next to it.
Two consequences elsewhere: a merged PR suppresses `CI-FAILING` explicitly
(a forge may keep serving a stale red rollup after the merge, and that must
never send the manager to relaunch finished work), and the manager prompt
gains the reading — a merged task needs nothing; a `done` task whose PR was
closed unmerged is a human decision quorum cannot interpret, worth one line
to the human and nothing else.

Failure story: missing harness config, nonzero exit, or timeout → the tick
raises. Crash isolation records it (heartbeat, board); the manager's
`auto_pause = false` config keeps the schedule firing so recovery needs no
human intervention, and because a paused agent is the loud signal the
manager does not have, a sustained failure streak escalates on `attention`
instead — see [Messaging protocol](#messaging-protocol).

### Prompt agents

The generic sibling of the manager: `type = "prompt"` runs a user-written
prompt (`prompts/<name>.md`, or `settings.prompt` to point elsewhere) over
the configured harness on a schedule, sharing the manager's exact run
mechanics (`agents/harness_run.py`): actor-tagged env, per-agent journal and
action cap, transcript at `state/agents/<name>/transcript.jsonl`, and
mid-run guidance from the agent's own inbox when the harness supports
injection (rendered at the template's `{directives}` placeholder). There is
deliberately no wake condition and no digest — a prompt agent runs every
scheduled tick, and anything conditional belongs in its prompt. A template
that writes `{notes}` gets its notebook *and*, above it, the same
self-observation header the manager's digest opens with: there is
deliberately no second `{self}` placeholder, so an agent's memory of itself
is one block. Prompt agents are usually file-defined (`agents/<name>.toml`,
created by `quorum agent create`, hot-added via `agent.reload`) but a
`[agents.<name>]` table in config.toml works identically.

Quorum packages one worked example, `default_prompts/babysitter.md` — the CI
babysitter, seeded into `prompts/` by `quorum init` and inert until an agent
is created over it (`quorum agent create babysitter --schedule "every 10m"`,
or `--prompt babysitter` to run it under another name). It polls
quorum-created PRs with `gh`, waits for a task's runner to go idle, sends
the *specific* failing check as guidance and relaunches, and gives up to the
human after two failed relaunches. Every bit of that is prompt text under
the ordinary prompt-agent rails, which is why `agent create` accepts a
prompt agent with no prompt text at all when the template already resolves.

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
  winner, across processes, which is what makes the runner's guidance-claim
  safe against a concurrent `task inbox --claim`. `ack()` archives and
  deletes; crash-orphaned `cur/` entries are returned to `new/` by the
  hourly janitor.
- The janitor also compacts board messages older than
  `[quorum].retention_days` (per-message `ttl_days` overrides) into
  `messages/archive/YYYY-MM.jsonl.gz`. `MessageBus.archived_records` is the
  one reader of those files — raw dicts addressed to one recipient, bounded
  by a starting month, skipping a line that will not parse and a month that
  will not decompress. `archived_direct` is its validated face and
  `export.delivered_guidance` takes the raw records, so an export keeps a
  message the current schema would reject.
- The same archive is where **on-demand** clearing goes: `MessageBus`
  exposes the janitor's per-message path as `archive_board_message`, with
  `ack_board_message`, `archive_topic` and `clear_inbox` on top of it,
  behind `quorum board ack`, `board clear` and `task inbox --clear`. All of
  them archive rather than set a flag on the message, so the board keeps
  carrying no read-state.
- **Archiving one message is what dismisses an escalation.**
  `views.attention_summary` is a seven-day window over the `attention`
  topic, so without `board ack` an escalation the human has already handled
  sits in `quorum status` and the TUI header for a week; archiving it drops
  it from every view while the history keeps it with its original
  `created_at`. `resolve_board_message` accepts a full message id, a unique
  prefix, or the unique suffix `Message.short_id` prints, and raises for
  unknown and ambiguous, because silently archiving the wrong message would
  dismiss someone else's escalation. `board read` prints that short id so
  there is something to type. In the TUI it is one shared bus call with no
  view-local write logic: `a` opens the attention list and acks the
  highlighted line through `_write`. That list carries
  `views.ATTENTION_LIST_LIMIT` entries rather than the banner's handful,
  since every line it renders is one the reader may want to dismiss.

The **control channel** rides the same machinery: `quorum agent
pause|resume|run-now|reload` sends to the `supervisor` inbox, which the
supervisor claims every 15 s and applies to its scheduler jobs. No new
transport, no ports, and commands queue harmlessly while the supervisor is
down. `agent.reload` is the hot-add path: it re-reads config and creates,
replaces, or removes that one agent's job — the file is the source of truth,
the message is only a poke, so one command covers create, edit, and delete.
A pause is durable: it lands in the agent's heartbeat, and a restarting
supervisor schedules any agent whose heartbeat says `paused` with its job
paused rather than silently resuming it.

**Failure escalation** rides the board rather than the control channel. Every
failed tick posts `agent.error` to `system` and records the streak on the
heartbeat (`consecutive_failures`, `error`); at `MAX_CONSECUTIVE_FAILURES`
(5) the agent is auto-paused with an `agent.paused` post — also on `system`.
Nothing in that story reaches a banner: `views.attention_summary` reads the
`attention` topic alone. For an ordinary agent the pause is itself the loud
signal (it stops, and `quorum agent list` says `paused`), but an agent whose
config sets `auto_pause = false` — the manager, which must keep firing so it
self-recovers — is exempt from the pause and would just fail all night in a
channel nobody watches, so the supervisor posts one `agent.failing` to
`attention` for such an agent instead. **That post is the only failure path
in quorum that reaches `attention`**; everything else the supervisor says,
recovery included, stays on `system`.

The dedupe is a third heartbeat field, `escalated_at`: written *after* the
post lands (a stamp-first escalation whose post threw would suppress the
banner for the rest of the streak), checked before posting (so a ten-hour
outage is one post, not one per tick), and cleared by every success path — a
scheduled tick, `quorum agent run-once`, and `quorum agent resume` all clear
it alongside `error` and `consecutive_failures`, which is also what makes
the closing `agent.recovered` post (on `system`, since a self-healed outage
is informational) fire exactly once. Nothing here pauses, retries or
throttles the agent; the post is an observation.

### Board consumers: the notification hook

The board carries no read marks, so *reaching* someone is a consumer's job,
and `notify.py` is the one consumer quorum ships for a person rather than an
agent. An optional `[notify]` table holds an argv template — the same shape
as `[harness.<name>]`, substituted element-wise (`{text}`, `{from}`,
`{topic}`, `{type}`, `{id}`; a template with no `{text}` gets it appended,
like a harness template with no `{prompt}`), so there is no shell and
nothing to quote — plus the topics that fire it (default `attention`, the
one topic meant for a human). The supervisor runs `_notify` on the control
cadence (15 s, and once at startup, that startup catch-up running *before*
the scheduler and the janitor so it can neither race the job's first fire
nor lose an escalation the janitor is about to archive): it reads each
listed topic past a private cursor in `state/notify.json` — the last on-disk
filename processed, per topic, via `MessageBus.entries_after_cursor`, which
hands back real filenames because a message's own `filename()` is only what
`post()` happened to write — and runs the template once per message, oldest
first, advancing and persisting the cursor *before* each delivery.

Delivery is therefore **at-most-once** by design: a crash, or a failed
cursor write, loses one notification, where the other order would repeat it
every 15 seconds for as long as the disk stayed full. Nothing is ever
delivered twice, including across a restart or between the startup drain and
the job (`drain` takes a process-wide lock, since APScheduler's
`max_instances` guards a job only against itself). There is no queue, no
retry store and no second transport; a message posted while the supervisor
is down goes out on the next start.

Three stances hold the rest in shape. **It fires on topic membership, never
on content**: what is escalation-worthy stays prompt policy. **It fails
soft** in herdr's mold: a missing binary, a nonzero exit or a hang past
`[notify].timeout_seconds` is one line in `logs/supervisor.log` and an
advanced cursor — a notification that cannot be delivered must not block the
ones behind it, and nothing here can fail a tick, a board post or the
supervisor. **Enabling it starts from now**: the first drain arms each
topic's cursor at its current tail without delivering, so turning the hook
on does not replay a month of old escalations. A per-tick cap
(`MAX_PER_TICK`) keeps a suddenly busy topic from wedging the job thread,
and shutdown is bounded by the one delivery in flight rather than a whole
batch: `quorum down` ends in `scheduler.shutdown(wait=True)`, so the
supervisor calls `notify.request_stop()` first and the drain stops between
messages. Nothing is lost — the cursor advanced only past what went out, and
the startup catch-up delivers the rest. The template runs with the
supervisor's environment, as a harness does; it is not a security boundary.

`quorum notify test "…"` runs the template once, directly and loudly (exit 1
with the reason), without touching the board or the cursor; `quorum doctor`
reports the table statically.

### Design seam: outboxes and a router

v1 delivers directly (writer → recipient's `new/`), because everything
shares one permission domain. If agents are ever sandboxed *from each
other*, the seam is `MessageBus.post()/send()`: swap in an
outbox-spool-plus-router implementation with no agent code changes.

## Views and their write affordances

`views.py` assembles the read model out of files alone — no locks, no
network, no supervisor required — and `quorum status` and the TUI are both
readers of that one model, which is why they never disagree.

The CLI's listings render that model as Rich tables rather than concatenated
lines — the shape that grew a clause per feature until a row with a report
and a PR URL wrapped mid-cell past column 80 (#52). One table builder per
row kind in `cli/_common.py` turns the `views.*_rows` dicts into cells —
rendering only, never re-deriving — and one `_print_table` renders the
result two ways.

The marks *inside* those cells are views', not the CLI's: `task_marker` (the
character before the short id — `⚭` attached, `▶` running, `✓` done, `✗`
blocked, `·` anything else), `task_badges` (`∞` perpetual, then `✔` or `⊘`
for what the forge last said about the PR), `task_flags` (`⚠` stranded work,
`waiting-on <ids>`, `DEP-*`) and `usage_badge` (the spend plus `$!` or `$!
GATED`). The CLI task table and the TUI task table call all four, which is
what stops the two from disagreeing about where a dependency mark goes;
`task show` calls `task_badges` for the two marks it has room for and spells
the rest out in words, and `quorum status --legend` describes the set. A
surface may choose where it puts a mark — the TUI has no flags column, so it
appends the flags to the status cell — but not how it is spelled.

On a terminal the table is fitted to the window: one give-way column per
listing absorbs the shortfall with an ellipsis so the id, status, harness,
pr and usage columns stay whole, and below the width at which it has nothing
left to give only the id — the handle you retype into `task run` — holds a
`min_width`, so it is the last cell to be cut. A table of nothing but fixed
columns is rendered at its natural width rather than expanded, or a wide
window's slack would be spread evenly and leave the fields far apart. Off a
terminal it is laid out at its natural width, plain text, no ANSI and no
trailing padding, so every id, status and `#N` reference is whole and
greppable. `_print_table(width=80)` is the test seam: an 80-column render
must have exactly one line per row.

The reads are pure; the writes are deliberately not absent. The TUI
(`tui/app.py`) carries a small set of *write affordances*, and the rule is
that each is a thin call into the same code path the CLI uses — a
`MessageBus` send, a `TaskStore.update`, `runner.launch_detached`,
`config.create_agent` — never write logic that lives in a view: send a task
guidance (`n`), send the manager guidance (`m` — the `manager` inbox,
exactly `quorum manager tell`, and the reason the TUI needs no task-add
form: the manager runs `task add` itself, journaled and capped), start a
detached run (`s`), cancel a task (`c`). `t` is a read, not a write: the
detail pane's second tab, the task's history. `s` refuses an attached task
and one whose runner is alive, mirroring the runner's own substrate rails;
`c` is the one destructive binding, so it goes through a yes/no
`ConfirmScreen` and, like `quorum task cancel` without `--kill`, marks the
status without signalling a live runner. All four resolve their target the
same way (`_target_task`): the *highlighted* row while the task table has
focus, falling back to the open task when the reader is down in its detail —
`enter` opens a transcript for reading and nothing more, since a selection
made once must not silently become the target of every later keystroke. And
every one runs through `_write`, which turns an `OSError` into an error
notification: an unwritable QUORUM_HOME is exactly when a reader needs the
dashboard most, so no keystroke may take it down.

No view holds a lock, spawns an agent tick, or writes state of its own
invention. This revises the earlier "the views are pure readers whose one
write affordance is a task nudge" stance (issue #11); the invariant that
survived it is *thin, shared, no view-local write logic*.

### Task history

`views.task_history(home, task)` is the first of the post-hoc readers (#88):
one list, oldest first, of everything that happened to a task, assembled
from the files that already record it and nothing else. Every row is `{at,
at_text, kind, text, …}` — `at` the ISO-8601 UTC stamp as written, `at_text`
that stamp as a surface prints it, `kind` one of `queued`, `action`,
`guidance`, `run.started`, `report`, `run.ended`, `pr_state`, `archived`,
and `text` the rest of the line every surface prints (`views.history_line`).
`quorum task history` and the TUI's `t` tab render the same rows, so they
cannot disagree. The sources: `task.json` (`queued`, and a `run.started` /
`run.ended` pair per run carrying exit code, usage, `stopped`, `stalled`,
`fresh_session` and the `auto_commit` note, plus `pr_state` from
`pr_state_at`); `runner.lock` (a `run.started` marked `live` for the run in
progress, which has no record yet, and only while the pid is alive);
`reports.jsonl`; the task's inbox `new/` and `cur/` (`guidance` still
`waiting` or `claimed`); the message archive (`guidance` that was consumed —
`task inbox --clear` archives without delivering and leaves an identical
record, so the row cannot tell the two apart, and the guide says so); every
agent journal (`action` entries whose `target` is the task's short id, plus
a `task.prune` whose args list it, read over `HISTORY_JOURNAL_BYTES`, a
completeness bound rather than a tail); and `tasks/.archive/<id>/`
(`archived`, stamped from the directory's ctime, the one event with no
record of its own). The transcript contributes no row: everything the runner
notes there is also on the run record.

What the reader does *not* do is as much the design as what it does. It
records nothing: guidance is stamped when it was sent because delivery
writes no time, and the honest fix for a missing fact is to record it where
it happens (as `pr_state_at` was, #79), never to compute and cache it here.
It is bounded and fail-soft in the read model's way — the journals over a
byte budget, the archive from the task's own month on, a torn line costing
the rows it held and never the list. A stamp that will not parse sorts after
every real row instead of by string comparison, and `at_text` marks it with
a `?`, so an event quorum cannot place in time is visible rather than
silently misfiled. Guidance is deduplicated by message id, furthest-along
state winning, because `ack()` archives before it unlinks the `cur/` copy
and a consumer that dies between the two leaves the message in both places
for good.

Even bounded it is too expensive for a polling loop: on a home with four
agents' journals at the byte budget and a year of archives it takes about
four tenths of a second. So the TUI's tab is rebuilt on `t`, on `r`, on
opening a different task and after any write the dashboard made, and reused
on the two-second tick. And it outlives pruning: `quorum task history`
resolves a handle out of `tasks/.archive/` when the live listing has
nothing, because archival is the last thing that happens to a task and the
answer to "what happened to it" must not vanish with the move. That is the
one reader that looks into the dot-prefixed directory on purpose; every
listing, view and digest keeps skipping it.

### Reading a run

A transcript is complete and illegible: a claude run is a few hundred events
of nested `tool_use` payloads and echoed tool output, and answering "what did
it try, what came back, why did it stop" took `jq`. `transcript.py` renders
those files as a narrative, and it is the *only* renderer — `quorum task
log`, `quorum agent log` and the TUI's transcript pane all call it, so the
surfaces cannot drift into different readings of one file. There is one
command per transcript rather than a `log`/`tail` pair: `-n N` bounds the
output to the last N entries and `-f` follows a live one.

What it renders: the run's start (session id, working directory), assistant
text in full, one line per tool call with its first argument trimmed, each
tool result collapsed to `N lines · size`, quorum's own `quorum:` transcript
notes, and a closing `result` line whose numbers come from
`usage.usage_from_event` rather than a second reading of the same event.
Reasoning blocks and the events that carry no story are folded away; `-v`
unfolds all of it plus the full payload of every line.

Three properties fence it. **One place for harness shapes**: `tool_call`,
`session_id` and `normalize` are the only code that knows how each harness
spells an event, on the seam `usage.py` already owns for result events, so
teaching quorum a new harness is one file. **Fail-soft, always**: an
unrecognized event renders as its raw JSON line and a malformed entry as its
`repr`, because this runs in a dashboard refresh loop and in `-f` tails,
where a raise is a dead surface. **`--raw` is a promise**: it prints the
transcript's own lines, byte for byte, so anything grepping a transcript
keeps working.

`quorum agent log <name> [--last N | --run <id>]` reads one *tick* rather
than one file, out of the four the tick leaves behind: the digest it was
given (`state/<agent>/runs/<run>.md`), its transcript entries, the actions
the CLI journaled for it — each with the then-vs-now target status the next
digest would show — and the usage-log line saying how it ended. The manager
is an agent here like any other (`agent log manager`); a prompt agent's
digest file is its rendered prompt. A tick happening right now has no
usage-log line yet, so `-n`/`-f` fall back to reading the transcript file
directly, and every section degrades to a note rather than an error. That
per-run digest file is the one thing here that writes, and it is
deliberately not state: nothing reads it back to decide anything, it is
head-truncated on write and only the newest `SNAPSHOT_KEEP` files are kept,
and losing one costs a reader some history and the system nothing. Without
it a tick's reasoning was unreconstructable — the digest was rendered, sent
to the harness and dropped.

## Projects

Canonical record: `projects/<slug>.json`. A `.quorum.toml` marker inside the
project directory merges over the registry record at read time
(`quorum.projects` is the single merge point), so metadata travels with a
synced repo. Quorum only ever *reads* project directories — the one scoped
exception is task execution, which writes to the task's own worktree (and,
via git's shared object store, the project's `.git`).

Two files in a project directory are quorum's by convention, both read-only:
the `.quorum.toml` marker above, and `.quorum/task-preamble.local.md`, which
fills the task preamble's `{project}` slot (see [Prompts](#prompts)).

## Model calls

There is one way to reach a model: a `[harness.<name>]` argv template, run
as a subprocess. Task runs go through `quorum.runner`; the manager and
prompt agents go through `agents/harness_run.run_agent_harness`, and a
plugin agent that wants a model call uses the same function — it takes the
agent's `AgentContext`, resolves the agent's harness, and returns the run id
after streaming the harness output to the agent's transcript. A second path
(a `[llm]` table and an `LLMClient` for plugin agents' small completions)
was removed once nothing in shipped code called it; a leftover `[llm]` table
in config.toml is an unknown table and pydantic ignores it.

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
   quorum package dir, derived at runtime from `sys`/`sysconfig`) is granted
   read; nono's `system_read_*` policy groups supply the loader/libc
   baseline without which no child can exec at all.
3. **Per-task**: `[sandbox].use_nono = true`. Each task run applies
   `build_task_capabilities` to itself (runner process + harness children):
   write on `QUORUM_HOME`, the worktree, the project's `.git`, and
   `[sandbox].task_write` extras (harness state dirs like `~/.claude`); read
   adds the harness executable, `task_read` extras, and the same
   interpreter/system baseline; network open (harnesses need their APIs).

Users can bring their own nono-style JSON profile via
`[sandbox].profile_file`: its `fs_read`/`fs_write` grants merge *additively*
into both derived capability sets (the derivation stays the floor that keeps
quorum functional), a non-empty `network` list keeps mode 2's network open,
and an unreadable profile raises `SandboxUnavailable` — never a narrower
sandbox than the user asked for. The same file works verbatim with the nono
binary in mode 1.

Mode 2's network rule is worth stating plainly: `build_capabilities` blocks
the network unless the profile file grants it. Mode 2 applies to the
supervisor process and therefore to every child it spawns, so under `quorum
up --self-sandbox` the manager's harness has no network either, and a
harness-driven manager needs a `profile_file` whose `network` list is
non-empty. Blocking by default is what keeps the mode fail-closed: opening
the network is the user's explicit act, not an inference quorum makes. The
asymmetry is the design: a sandboxed quorum can *see* the machine, but the
only durable marks it can leave are `QUORUM_HOME`, the worktrees, and the
grants added explicitly.

## Diagnostics: `quorum doctor`

`doctor.py` is the counterweight to how much of quorum fails soft. Every
degradation elsewhere in this document is deliberate — an unreadable
config.toml disables the optional probes rather than killing a tick, an
unauthenticated `gh` yields `None`, a stale seed in `prompts/` keeps
rendering, a crashed run leaves a `runner.lock` nobody trips over — and each
one is invisible by construction. Doctor is the single place that goes and
looks. Three rails, and they are the whole design:

1. **Diagnose, never repair.** Each line names its own fix; nothing in the
   module writes to QUORUM_HOME. `--fix` is not a planned feature — an
   autofix would have to guess which of two defensible states the user
   wanted.
2. **A pure reader plus exactly one opt-in probe.** The static checks are
   file reads and `shutil.which`, in the same family as `views.py`. The
   exception is `--smoke`, which runs a harness for real, in a
   `TemporaryDirectory`, through the runner's own `build_harness_argv` /
   `guidance_pump` / `stream_transcript` — including `inject` stdin delivery
   — and asserts a `result` event and a captured session id inside a short
   timeout. It reuses the runner's code rather than a simplified copy
   because a copy would drift away from the bug it exists to catch (#24: a
   stream-json CLI ignoring an argv prompt, so every run hung until it timed
   out). Everything the probe touches is scratch, including the child's own
   `QUORUM_HOME`, because a harness with quorum's integration hooks
   installed runs `quorum task hook-session-start` on startup and must not
   write to the live home. The child is spawned with
   `start_new_session=True` and the timeout `killpg`s the group, since
   killing only the process quorum spawned leaves grandchildren holding the
   pipe.
3. **Three states, no fourth.** `ok` / `problem` / `na` (✓ / ✗ / –), where
   `na` covers "you turned this off" and "there is nothing configured to
   check". Only `problem` sets a non-zero exit, which is what makes `quorum
   doctor --json` usable in a script and keeps a `–` from training anyone to
   ignore the output. A fresh `quorum init` home — no `[harness.*]` table,
   no `default_harness` — is one `–` line rather than two ✗ for one unmade
   decision, and a `gh` that never answered is `–` too, because an offline
   laptop says nothing about whether anyone is logged in.

Doctor asks other modules rather than reimplementing them, which keeps its
answers from drifting from the code it reports on: `gh` through
`forge.auth_status`, prompt staleness through `home.classify_prompt`,
sandbox support through `sandbox.availability()`, the exposed-surface counts
through `surfaces.py`, and the trust dials through `dials.current` — the
registry the guide's table is tested against, rendered as `dial.*` lines
that are `–` by construction, since a cautious default and a deliberately
loosened value are both facts rather than faults.

One small function per check, each taking only what it needs (a `Config`, a
`HarnessConfig`, a home path), so every check has both a passing and a
failing test. `check_config` is the one caller in the codebase that uses
strict `load_config` on purpose: everywhere else papers a broken file over
with defaults so work can continue, and this is where the user finally hears
about it. Two things elsewhere exist to feed it: `supervisor.lock` records
the version of quorum that started the process, so "you upgraded but never
restarted" is a line rather than a memory, and `home.classify_prompts` is
the read-only half of the seeding logic `quorum init` acts on.

## Testing strategy

`tests/conftest.py` provides `home` (scaffolded `QUORUM_HOME`) and `clock`
(injectable `FakeClock`). Two purpose-built fake CLIs live in `tests/bin/`:
`fake_gh.py` (a GitHub CLI installed onto a PATH stripped down to real git,
so the CI probe's no-gh / no-auth / no-PR / garbage / hung branches are all
reachable, and so a developer's real `gh` can never reach the network from a
test), and `fake_harness.py`, which behaves like a real harness: it echoes
its argv and prompt to stdout, emits a `session_id`, and in `report` mode
calls `python -m quorum task report` against `$QUORUM_HOME`, exercising the
full cooperative loop, plus manager modes (`manager_act` launches, sends
guidance and journals; `manager_flood` slams into the action cap). Each
`[harness.*]` table pins its own mode via `env`, so a fake task harness and
a fake manager harness coexist in one test. Manager tests run the whole loop
for real (the fake manager's `task run` executes the fake task harness);
runner tests build real git repos and assert on worktrees, transcripts, and
session capture. Sandbox glue is pinned by injecting a fake `nono_py` into
`sys.modules`; real kernel enforcement runs under `-m nono_integration`,
with a dedicated CI job asserting platform support so it can never silently
skip. The example plugin is loaded by file path and tested in
`test_example_steward.py`, so the worked example in the guide stays true.
