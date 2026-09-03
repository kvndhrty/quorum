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

These three do not move with model capability, and neither do the smaller
stances recorded per layer below: no decisions in Python, observations are
never rails, a dropped signal is a bug. The settings that *do* move are
dials that record how far the human trusts the model today: how many tasks
run at once, the per-run action cap, the budget, the manager's cadence, who
launches, who decomposes, who merges. They are listed with their loosening
conditions in [guide.md](guide.md#loosening-the-rails-as-trust-is-earned),
facing the list of what does not move, and `dials.py` is the registry
behind that table (`quorum doctor` reads the current values from it). A
change to either list is argued there and recorded here and in `CLAUDE.md`
in the same commit.

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

quorum web / quorum tui / quorum status ──► read QUORUM_HOME's files
                                     (writes: thin shared bus/store calls)
quorum doctor ──────────────────────────► pure reader + one opt-in probe (--smoke)
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
supervisor.lock                   pid + start time + the version of quorum that
                                  started it; mtime = liveness heartbeat
projects/<slug>.json              canonical project records (machine-owned JSON)
tasks/<id>/task.json              task spec + reported status + session + runs
                                  (each run: times, exit code, auto-commit
                                   note, reported token/cost usage) + the
                                  attached / perpetual / held flags +
                                  priority + depends_on:
                                  full ids this task waits on + pr_state /
                                  pr_state_at: what the forge last said about
                                  the PR, the one materialized probe result +
                                  issue_url: the forge issue this task was
                                  queued from (`task add --issue`), written
                                  once and never re-read from the forge
tasks/<id>/attached.json          adopted-session liveness (latest hook event)
tasks/<id>/transcript.jsonl       harness stdout, one JSON line per line seen
tasks/<id>/reports.jsonl          `quorum task report` entries
tasks/<id>/runner.lock            pid of the active run
tasks/<id>/runner.log             detached-run bootstrap output
tasks/.archive/<id>/              pruned tasks, moved here whole; dot-prefixed
                                  so every scan skips them (see "Pruning")
worktrees/<id>/                   git worktree (branch quorum/<short-id>)
prompts/<name>.md                 user-editable prompt templates (re-running
                                  `quorum init` upgrades never-edited seeds)
prompts/.seeded.json              {filename: sha256} of what init last seeded;
                                  what makes "never edited" a local fact
prompts/<name>.local.md           per-prompt overlay: user-owned, never
                                  seeded, merged at the template's {local}
                                  slot (else prepended) — see "Prompts"
messages/board/<topic>/*.json     public append-only board
messages/inbox/<name>/new|cur/    direct mail (task-<id>, supervisor, agents)
messages/archive/YYYY-MM.jsonl.gz compacted history
state/agents/<name>/              heartbeat.json + state.json + tick.lock
                                  (+ journal.jsonl, notes.jsonl,
                                  transcript.jsonl and usage.jsonl for
                                  harness-driven agents other than the
                                  manager)
state/manager/journal.jsonl       auto-recorded manager actions (per-run tagged)
state/manager/notes.jsonl         the notebook: standing notes a future run
                                  reads, plus retirement tombstones
state/manager/transcript.jsonl    the manager harness's own stdout
state/manager/usage.jsonl         one line per manager harness run: what it
                                  spent and how it went ({at, run,
                                  usage|null, outcome, duration_seconds})
state/notify.json                 the [notify] hook's private board cursors
                                  (last filename delivered, per topic)
logs/supervisor.log, actions.jsonl
plugins/                          drop-in custom agent modules
```

## Prompts

`quorum.prompts` resolves a template name three ways, in order: the home
copy `prompts/<name>.md`, else the packaged `default_prompts/<name>.md`,
else `KeyError`. Rendering is `str.format_map` over a missing-key-preserving
dict, so a template may contain braces quorum knows nothing about
(`{{escaped}}` documentation in the header comments, JSON shapes in
examples) without any escaping discipline at the call sites.

`quorum init` seeds the packaged defaults and, on re-run, upgrades any copy
whose sha256 still equals what the seed record `prompts/.seeded.json` says
init last wrote there — i.e. a pristine seed from an older quorum. Anything
else is a user edit and is never touched, and so is any differing copy with
no record: a lost or malformed record degrades to "not upgraded", never to
"overwritten". The record is written by init only (atomically, once per
run) and also re-records a copy it finds identical to the current default,
so a home from before the record existed picks one up while its copies are
pristine. Keeping the fact in the home, not in a list of superseded hashes
in Python, is what lets a change to `default_prompts/` ship without
bookkeeping in `home.py`.
That rule has a cliff: the first edit to `<name>.md`, however small, opts
the home out of every future upgrade to that prompt, silently. A home that
prepended five lines of house policy to `manager.md` kept running the
manager prompt from the release it edited, with no policy for any digest
observation added since.

The **overlay** removes the reason to take the cliff. `prompts/<name>.local.md`
is user-owned, never seeded, never read by `init`, never upgraded. `render`
merges it into the resolved template:

- at the first unescaped `{local}` slot, which the packaged `manager.md`
  (before "How to work", so house rules outrank the general guidance),
  `task-preamble.md` (after the delivery protocol) and `task-perpetual.md`
  (after the cycle conventions) carry — `{{local}}` in a header comment is
  documentation, not a slot;
- prepended, when the template has no slot — the case of a home that
  rewrote `<name>.md` before the slot existed, where a silently dropped
  overlay would be the worse failure;
- as nothing at all when the overlay is absent or blank, taking the slot's
  own line with it so an unused slot leaves no hole.

Reading the overlay is **fail-soft** (`load_local`): an overlay that cannot
be read or decoded renders as no overlay, because `render` is on the manager
tick and every task run, and one stray byte in a user-owned file must not
fail supervision forever. Reading the *template* stays loud — it is the
prompt itself, and silently falling back to the packaged default would hide
the fork. `quorum prompt list` is where either problem is reported: it marks
an unreadable file `?` and keeps listing the rest.

`local` is otherwise an ordinary placeholder key: pass it explicitly and it
wins over the file. Overriding a whole template by rewriting `<name>.md`
keeps working exactly as before — the overlay is a second, cheaper lever,
not a replacement. `quorum prompt list` and `quorum prompt diff <name>`
(home copy vs packaged default) make the state of both levers visible, and
`init`'s `edited` line points at them.

The overlay is home-wide, which is the wrong scope for a home holding
several projects: "base on `develop`, run `just check`" is true of one repo
and wrong for the next. The **`{project}` slot** is the fourth layer, and
task-facing only (the manager digest does not change). `prompts.project_block`
assembles it from two sources, in that order:

1. the project's registry `notes` — `projects/<slug>.json`, already merged
   with the `.quorum.toml` marker, editable with `quorum project set <slug>`
   and either `--notes` or `--notes-file` (`-` for stdin, read as bytes and
   decoded as UTF-8 like a task prompt, and read only after the slug checks
   out so a typo cannot eat piped text);
2. `.quorum/<name>.local.md` *inside the project directory*
   (`.quorum/task-preamble.local.md` in practice) — user-owned, versioned
   with the repo if the user wants, and **read only**, like every other
   project-dir read. It is read from the project directory rather than the
   task's worktree because that is the copy the user maintains; a worktree
   holds whatever the task branch happens to have.

Both reads are fail-soft, for `load_local`'s reason and more so: this one is
on every task run and the file belongs to whoever owns the repo. An
unreadable block is no block; the run goes ahead.

Both are also *project-directory* reads, so the runner takes them before it
applies the task sandbox: `build_task_capabilities` grants the worktree and
the project's `.git`, never the project directory itself, and a read taken
afterwards would fail soft into no block at all — the feature would silently
do nothing under `[sandbox].use_nono`. `compose_prompt` takes the block as
an argument for that reason.

`{project}` follows the `{local}` rules with one deliberate difference:
same empty-slot removal (nothing to say leaves no hole), but **no prepend
fallback**. An overlay is policy the home already had, so rescuing it into a
slotless template is right; a project block is new, and there is no
defensible place to put it in a template the user rewrote. `quorum prompt
list` says so instead — it lists every project that contributes a block,
marks one it cannot decode `?`, and warns when the home's `task-preamble.md`
has no `{project}` slot to render them into.

So the full order for the task preamble is: packaged default →
`prompts/task-preamble.md` (home copy, wins outright) →
`prompts/task-preamble.local.md` (home overlay, at `{local}`) →
project notes + `<project>/.quorum/task-preamble.local.md` (at `{project}`).
No new state file: the notes were already in the registry, and the project
file is the user's.

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
  agree) and `budget_overages`; `quorum status` / `task list` show
  `$0.42 · 11.0k tok` in a headed `usage` column, `task show` breaks it
  out, and the manager digest gets a `usage:` line per task. The figure is
  the harness CLI's own — quorum prices nothing — and the guide says so
  next to its status example, since a subscription claude session reports
  a notional API-rate cost, not a bill.
- **Agent runs are ledgered, not embedded.** The manager's tick and every
  prompt agent run through `agents/harness_run.py`, which captures usage off
  the same parsed events — but an agent has no `task.json` to hang it on, so
  each run appends one line to `state/manager/usage.jsonl` (or
  `state/agents/<name>/usage.jsonl`, the same split as `journal_path`):
  `{"at": ..., "run": <run id>, "usage": {...}|null, "outcome":
  "ok"|"raised"|"timeout", "duration_seconds": <float>}`. Every run is
  recorded, including the ones that reported nothing and the ones that
  timed out or exited nonzero — a spent-and-then-died run still spent, and a
  run count only means something if it counts every run. The line is
  therefore the record of *how a run went*, not only of what it cost:
  `outcome` and `duration_seconds` ride it deliberately (#59) rather than
  getting a file of their own, because the run that reports no usage at all
  — the timeout — is exactly the one an agent most needs to see about
  itself. A line written before they existed reads back as outcome `None`
  (rendered `?`), never as `ok`. Writing it can
  never fail a tick (`usage.record_agent_run` swallows `OSError`). The file
  is append-only and unbounded, so readers take a bounded tail
  (`usage.agent_usage`, `AGENT_USAGE_TAIL` = 200 runs; a total over a full
  tail is labelled "recent runs", never presented as all-time) and report the window
  alongside the figure: `views.agent_rows` carries `usage` (`last` / `total`
  / `runs` / `window`) and a rendered `usage_text`, which `quorum status`,
  the TUI's agent table and the web agent row show when it is known. The
  manager digest opens with the same figure for the manager itself — the one
  recurring cost nothing else in the digest accounts for, and in a live home
  usually the largest.
- **The budget gate is a rail of the rate-limit class — the next run,
  never the current one.** `[tasks] max_cost_per_run` /
  `max_tokens_per_run` (validated non-negative, 0 = off) turn an
  over-budget run into a `BUDGET-EXCEEDED` digest line and a `$!` mark in
  the views, and — the enforcement half of issue #19 — a task whose
  **last** run went over is refused its next run: `run_task` raises before
  taking `runner.lock` or spending anything, `quorum task run` mirrors the
  check so `--detach` fails in the parent, and the TUI's `s` key shows the
  refusal as a notice. `--force` waives it for one run. Only the last run
  counts (`usage.last_run_overages` → `runner.budget_blockers`): a later
  run that came in under budget, or reported nothing (silence is not
  evidence of spend), clears the gate on its own — a rate limit on
  relaunching a task that just blew its budget, not a sentence for one that
  once did. It is the fourth substrate refusal, beside `runner.lock`, the
  attached-task refusal, the dependency refusal (*Task dependencies*
  below) and the hold refusal (*Priority and hold*, below), and the
  second rail of the **rate-limit family** the per-run
  action cap belongs to: it bounds a bad task's blast radius and never
  vetoes a particular choice. What to do instead of relaunching — a sharper
  nudge, a decomposition, an escalation — lives in `prompts/manager.md`,
  and the digest line says `(next run gated; --force to override)` on the
  last run (`(an earlier run; a later one cleared the gate)` on older ones)
  so the manager knows why a relaunch failed. Deliberately **not** a
  mid-run kill: that is only even expressible for pumped runs (stdin closed
  at a turn boundary), and a detached run past budget finishes its turn
  and is gated afterwards. The gate never sets status, never cancels, and
  never touches the views' `$!` mark (`budget_gated` on `task_rows` is the
  same read, rendered — as `$! GATED` in `task list`, the TUI's spend column
  and the web dashboard alike, so nobody learns of the gate from a refused
  launch).
  usually the largest. `usage.agent_runs` reads the outcomes back over the
  same bounded tail, separately from `agent_usage` (which stays `None` when
  no run in the window reported spend, and a timed-out run never does).

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
first idle turn boundary. The claim of a message (its rename out of
`new/`) and its count as a delivered turn happen under the same lock the
close check takes, so a `result` arriving mid-claim sees the message
either still pending or already owed an answer, never neither — the gap
that once let a run end with a nudge in flight. A message
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

**Where the prompt comes in.** `task add` takes the prompt from exactly one
of three places: the positional argument, stdin (`-`), or `--prompt-file
<path>`. Two at once is an error rather than a precedence rule, and empty
(or whitespace-only) input is refused before anything is written — a task
with nothing to do would still queue, launch, and spend a run. Both
indirect paths read *bytes* and decode UTF-8 themselves instead of going
through `read_text`, so what lands in `task.json` is byte-for-byte its
source: the prompt is quoted verbatim into the harness's context, and
universal-newline translation or a stripped trailing newline would make a
queued task differ from the issue it was piped from. `gh issue view N
--json title,body | quorum task add <project> -` therefore still works, and
still leaves the forge on the user's side of the pipe.

**From an issue (`--issue`).** The pipe recipe queues the *text* of an issue
and nothing else: the task carries no link back, so no view can say `#62`
and the manager cannot tell a human "the task for #62 is done". `task add
<project> --issue <number|url>` closes that (#62). It fetches title and
body through `forge.issue_view`, composes the prompt as
`<title>\n\n<body>\n\n(<url>)`, and records the url the forge itself
reported as `issue_url` on `task.json`. A prompt given as well is *appended*
— the issue is the work, anything typed alongside it is instructions about
the work.

`issue_url` is written once, at `add`, and never touched again: it says
where the task came from, not what happened to it, so it is not an
observation and nothing re-probes it. Four readers share one renderer
(`tasks.issue_ref`, `#62` from the url): the CLI listing's `issue` column
(dropped whole on a home that uses none), the TUI and web tables, and the
digest's `issue=#62` mark on a task line. `quorum task show` prints the full
url, and the run preamble's `{issue}` slot tells the harness which issue it
is working from, to reference in its commits and PR — and not to touch the
issue itself.

Unlike the manager's PR probe, `--issue` **fails loudly**: a person typed a
flag and is waiting, so no `gh`, no auth, an unknown issue, a timeout or a
reply without a url is an error naming the fix, and no task is queued. The
alternative — a task with an empty prompt, launched and spending a run — is
much worse than an error. Quorum only ever *reads* from a forge: nothing
labels, comments on or closes an issue, before or after a merge.
### Stopping and restarting a run

(User-facing how-to: [guide.md](guide.md#when-a-session-hangs).)

Harness sessions hang: a stream-json CLI blocked forever on stdin (#24), a
provider turn that never returns, a wedged tool. The process is alive and
the lock is fresh, so every liveness signal quorum has says "working", and
the only kill quorum used to offer was `task cancel --kill` — **terminal**,
losing the task along with the hung run. Three pieces, deliberately split
between mechanism and judgement:

**`quorum task stop <id>` (`runner.stop_run`)** ends the *run* and nothing
else. Status untouched (the runner never sets one; neither does this),
worktree untouched, the task still queued exactly where it was. The signal
goes to the runner's **process group**: `launch_detached` starts a run with
`start_new_session`, so the runner leads a group that contains the harness
and everything it spawned, and the group is the only handle that reaches the
whole tree. SIGTERM, then SIGKILL after `STOP_GRACE_SECONDS` for a harness
that ignores it — and liveness is asked of the group (`fsio.group_alive`),
not the runner's pid, because SIGTERM kills the runner instantly while the
harness ignoring it keeps running; a pid check would call that a clean stop.
A run sharing quorum's *own* process group (a foreground `task run`) is
signalled by pid instead, since killing that group would take the caller
with it.

**Zombies are not runs.** A process that has exited but that its parent has
not waited on is still a process-table entry, so `kill(pid, 0)` succeeds and
`killpg(pgid, 0)` answers "alive" (EPERM on macOS, plain success on Linux)
for a group holding nothing but a corpse. That is not exotic: it is what
every killed run looks like to a caller that stays alive after
`launch_detached` (the TUI's `s` binding), and read as "alive" it makes
`task stop` report a run that *survived SIGKILL* and makes the next
`task run` refuse to start. Both ends are fixed: `launch_detached` waits on
its child from a daemon thread, so nothing lingers unreaped in the first
place, and `fsio.pid_alive` / `fsio.group_alive` ask `ps` for the state
letter (`Z`) whenever the cheap signal probe says something is there, so a
zombie counts as dead either way. `fsio._ps_rows` is the only place quorum
shells out to `ps`, and it fails soft *conservatively*: no `ps`, no answer,
and the caller keeps the process table's word rather than calling a live run
dead.

The killed runner never gets to write its own record, so `stop` writes it: a
`quorum: run.stopped` transcript line, a `TaskRun` with `stopped = true`,
the signal as a negative exit code and the killed run's own `fresh_session`
(recorded in the lock at acquisition, since this record is that run's only
trace and the digest counts fresh restarts off it), and the
now-provably-stale lock removed. `fsio.clear_stale_pid_lock` re-reads the
pid immediately before unlinking, which *narrows but does not close* the
window — `acquire_pid_lock` takes a stale lock over by unlink-and-create, so
a new runner can still claim the file in between; there is no
compare-and-unlink without the flock the pid-lock deliberately avoids, and
the residue (a live run whose lock file is gone, recreated by the next
acquisition) is not worth one. If the runner did manage to record the run
itself, that record stands and nothing is duplicated. A lock whose runner is
*already* dead gets the same tidying without a signal — only a task with no
lock at all has "no live run to stop". An **attached** task is refused
outright — the same substrate rail as the runner's, and the sharpest one:
the "runner" of an attached task is the user's own interactive session.

**`quorum task run <id> --fresh-session`** clears the captured
`session`/`thread_id` before composing the argv, so the harness starts a new
session instead of resuming a damaged one (a thread that errors on every
turn, a context the provider will not take back). The worktree — the actual
durable state — is untouched; the session was only ever a convenience. The
new session remembers nothing, so the caller is expected to nudge in a
summary, and the run records `fresh_session = true`.

**`[tasks].run_stall_timeout_seconds`** (0 = off, the default) is the
mechanical version, and needs no manager at all: `runner.StallWatchdog`
watches the stdout stream the runner is already reading, and when no line
arrives for N seconds it notes the stall in the transcript, SIGTERMs the
harness (SIGKILL after the same grace) and lets the run end the ordinary way
— so the run record, auto-commit and lock release all still happen, with
`stalled = true` on the record. That turns a hang into a **dead runner with
a non-terminal status**, which supervision already handles well. It counts
silence, not progress, so the threshold has to sit above the longest silent
step a real run takes (a full test suite, a cold build); that is why it is
off by default and why quorum never picks a value.

The watchdog signals the **harness only**, never the group: the runner leads
that group, so a `killpg` from inside would kill the run's own bookkeeping.
That leaves one known limitation — a *grandchild* that inherited the
harness's stdout and outlived it holds the pipe open, so the runner's
`stream_transcript` stays blocked and the watchdog's kill does not by itself
end the run (closing the read end under a thread already blocked in `read()`
does not reliably wake it). The cure for that case is the group-wide one,
`quorum task stop`, which is why the mechanical watchdog does not replace it.

All three are visible in the digest as `stopped=N` / `fresh_sessions=N` /
`last-run=stalled` on the task line, which is how the manager knows what it
has already tried without relying on its bounded journal window.

### Perpetual tasks

(User-facing how-to: [guide.md](guide.md#perpetual-tasks).)

`quorum task add --perpetual` sets `perpetual = true` on the task record.
Nothing about the substrate changes: the runner still does one run, status
is still a free-form reported word, `TERMINAL_STATUSES` still means what it
means, and the manager still relaunches any task whose runner died with a
non-terminal status — which is *already* an endless loop for a task that
never reports one. The flag exists because three readings of that same
substrate were wrong for a task that is not trying to finish:

- **the run preamble.** `compose_prompt` substitutes the preamble's
  `{perpetual}` placeholder with `prompts/task-perpetual.md` (empty for an
  ordinary task): work in cycles, commit and push *every* cycle rather than
  "before finishing", report a changing status word per cycle (`cycle-7`,
  `idle`) so an unchanging one still means something, and never report
  `done` or `cancelled`. Both files are ordinary user-editable prompts.
- **the digest.** The task line carries `perpetual=true` (only when true, so
  ordinary lines are untouched), and the `possible-loop` observation is
  **suppressed** for it. That flag reads repetition in a live run as a
  symptom; for a task whose job is a repeating cycle it would fire every
  tick, and a flag that is always on teaches the manager to ignore a signal
  that still means something everywhere else.
- **the manager prompt.** `prompts/manager.md` is told to relaunch it
  forever, to never read a long `runs=` count or a cycling status as stuck,
  to never cancel it (only the user ends it, with `task cancel`), and to
  judge it instead on its per-cycle reports and git state — escalating to a
  human when the same report repeats verbatim, when it reports `blocked`,
  or when spend climbs with nothing to show.

Views badge it (`∞` in `quorum status`, `task list` and the TUI; a titled
`∞` in the web row) so "still running after 40 runs" reads as working.

Two consequences worth knowing before queuing one:

- **One worktree and one resumed session, indefinitely.** Runs reuse the
  task's worktree (fine — that is where its work accumulates) and its
  captured session id, so a `resume` template hands the harness an
  ever-growing context. Expect a session reset eventually: clearing
  `session` in `tasks/<id>/task.json` (plain files, no schema migration)
  makes the next run start fresh, and the worktree keeps the work.
- **The manager's tick cadence is the floor on cycle latency.** Nothing
  relaunches a perpetual task between ticks, so with the default
  `every 5m` manager schedule a cycle that ends is idle for up to five
  minutes before the next one starts. Tighten the schedule if the loop
  needs to be tighter; there is deliberately no self-relaunch path in the
  runner (that would be a second scheduler).
### Task dependencies

(User-facing how-to: [guide.md](guide.md#chaining-tasks-with---after).)

`quorum task add … --after <id>` (repeatable) records `depends_on` — a list
of **full** task ids — in `task.json`. It is the one new piece of durable
state, and it is deliberately *not* a DAG engine: nothing schedules on it,
nothing topologically sorts, nothing fans out. The manager still makes every
launch decision; dependencies only tell it when a launch would be premature.
Cross-project chains work by construction, since ids are global.

- **Validation happens once, at `task add`** (`tasks.resolve_dependencies`):
  handles resolve through the same prefix/suffix `resolve()` as everything
  else and are stored expanded, an unknown or ambiguous handle fails the
  command (nothing is queued), and a task cannot depend on itself. Depending
  on a **perpetual** task is refused too: it never reaches a terminal status,
  so the dependent would wait forever. A dependency must already exist, so a
  cycle is only reachable by hand-editing `task.json`.
- **Reading is total** (`tasks.dependency_state`, pure over an
  already-loaded task listing, so every reader stays a file reader):
  `waiting_on` = dependencies that have not reached a terminal status;
  `failed` = dependencies that ended `blocked` or `cancelled`; `missing` =
  dependencies whose task record is gone; and `cycle`, detected rather than
  recursed into. A hand-edited `depends_on` never raises.
- **Only a dependency that still might finish blocks.** `failed` and
  `missing` are both upstreams that can never reach `done`, and both are
  deliberately kept *out* of `waiting_on`: continuing to call an
  unsatisfiable dependency "waiting" would hide the decision behind a task
  that silently never runs. They are reported instead, and the manager (or
  the user) decides — launch it anyway, cancel it, escalate.
- **The digest observes** (`waiting-on=<short ids>` on the task line while a
  dependency is unfinished; `DEP-FAILED` / `DEP-MISSING` / `DEP-CYCLE` flags
  with a line of explanation). These are observations of the same class as
  `possible-loop` and a `BUDGET-EXCEEDED` on an earlier run — the manager
  judges them (nudge the dependency, cancel the dependent, escalate) and
  quorum does nothing on its own.
- **One narrow substrate refusal**: `run_task` (and `quorum task run`, so
  `--detach` fails in the parent too) refuses a task with unfinished
  dependencies unless `--force`. This is the third rail of that class, next
  to `runner.lock` and the attached-task refusal (the budget gate under
  *Token/cost usage* is the fourth, and the hold refusal under *Priority and
  hold* the fifth) — a deliberate bend of "the action cap
  is the only rail", justified the same way: a dependent launched
  early is pure waste (it reviews a PR that does not exist yet), and the
  manager is the only caller that would ever do it by accident. It refuses
  the launch; it never cancels, re-queues or reorders anything.
- **Views** (`quorum status` / `task list` / `task show`, TUI, web) render
  `waiting_on` / `dep_failed` / `dep_missing` / `dep_cycle` straight off
  `views.task_rows`. Nothing is materialized to disk for them (unlike the
  merged observation, [below](#the-merged-observation) — dependencies are
  derivable from files quorum already holds, so materializing them would buy
  nothing).
- **Reading the upstream outcome**: the dependent task's composed prompt
  gains a *Tasks this one depends on* block listing each dependency's short
  id, status and `pr_url` (`runner.dependency_note` — fields already in
  `task.json`, one read each, no new state), and points at
  `quorum task show <id>` for the full record, reports and branch. That is
  deliberately the whole mechanism: no `{depends.*}` template substitution,
  no result-passing channel.

### Priority and hold

(User-facing how-to: [guide.md](guide.md#priority-and-holding-a-task).)

Two fields on `task.json`, and the same stance as `depends_on`: neither is a
scheduler. `priority: int = 0` (`task add --priority N`, `task set-priority
<id> N`) is the user's ordering signal; `held: bool = False` (`task hold` /
`task release`) is their parking brake.

- **Priority is data the manager reads, and Python orders nothing by it.**
  No sort anywhere: `TaskStore.list` stays chronological, `views.task_rows`
  hands the browser and the TUI the rows in that order, and the digest lists
  active tasks in it. The number reaches a decision only through
  `prompts/manager.md`, which is told to prefer the higher priority among
  the tasks it *could* launch this tick. That is the whole mechanism —
  changing the policy is editing a prompt, not the queue. Negative values
  are legal and mean "push this to the back".
- **The digest renders `priority=N` only when it is not 0**, so an ordinary
  task's line is unchanged and the mark reads as the exception it is (the
  same rule `perpetual=true` follows). Views badge `↑N` / `↓N` for the same
  reason.
- **Hold is not a status.** `task hold` never touches `status` — that stays
  the harness's word — and never touches the worktree, the branch, the
  session or the queue position. It is the parking brake `task cancel` is
  not: `task release` puts the task back exactly as it was. The digest
  renders `held=true` *always* (a held task otherwise reads as launchable —
  `runner=dead`, `status queued` — and nothing else on its line would say
  differently) plus one line telling the manager not to launch or release it.
  It gates the next *launch* and nothing else: a run already in flight keeps
  going (`task stop` ends it) and an adopted session is untouched (the
  runner refuses those outright), both of them invisible from the word
  "held". `runner.hold_note` is the one line that says which, printed by
  `quorum task hold` and by the TUI's `h`.
- **The hold refusal is the fifth substrate rail**, next to `runner.lock`,
  the attached-task refusal, the dependency refusal ([above](#task-dependencies))
  and the budget gate. `run_task` raises `held_refusal(task)` before taking
  the lock or spending anything; `quorum task run` mirrors it so `--detach`
  fails in the parent too, and the TUI's `s` key shows it as a notice.
  `--force` waives it for one run and **does not release the hold** — the
  same shape as the other four, justified the same way: a launch the human
  explicitly parked is pure waste, and the manager is the only caller that
  would ever do it by accident.
- **Only a human releases a hold.** Nothing in Python lifts it — not a
  finished dependency, not a `--force` run, not the janitor — and
  `prompts/manager.md` says outright never to run `quorum task release`,
  escalating with `board post attention` instead. The prompt is the fence
  here, not a check: `task release` is an ordinary CLI verb and a harness
  that ignores its instructions can call it, which is exactly the
  convention-not-boundary line `notes.may_write` draws.
- **Every verb is an ordinary `TaskStore.update` behind `_actor_guard`**, so
  `task.hold` / `task.release` / `task.set-priority` are journaled and count
  against an agent's per-run action cap like anything else. The TUI's `h`
  (toggle) and `+` / `-` (±1) are the same thin store calls through
  `_write`; `h` does not confirm, because unlike `c` nothing is lost by
  pressing it twice.

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

### Pruning: on-demand cleanup

Quorum accumulates: a finished task keeps its directory, its worktree, and
its `quorum/<short-id>` branch forever, and the board grows until the hourly
janitor's retention window catches up with it. `quorum task prune`,
`quorum board clear <topic>`, `quorum board ack <message-id>` and
`quorum task inbox <id> --clear` are the hand-driven tidies, and all of them
follow the bus's rule: **archive, never delete.**

- A pruned task's `tasks/<id>/` directory is *moved* to `tasks/.archive/<id>/`
  by one `os.rename`. The name is dot-prefixed on purpose: `TaskStore.list`
  already skips dot-entries (`fsio.is_tmp`), and every reader in the codebase
  — `quorum status`, `task list`, the TUI, the web dashboard, the manager
  digest, `doctor` — goes through it, so an archived task leaves all of them
  with no code change anywhere. Restoring one is `mv` in the other direction.
- Cleared board and inbox messages go into the same
  `messages/archive/YYYY-MM.jsonl.gz` the janitor writes, keeping their
  `created_at`. `board clear` is `archive_old`'s per-message path run
  immediately for one topic (and `quorum board ack --all <topic>` is the same
  sweep under the name a reader who has been acking one at a time reaches
  for — one command implemented on top of the other, so the alias cannot
  drift, and since its argument is the topic, a `--topic` alongside `--all`
  is an error rather than a silently ignored flag); `inbox --clear` touches
  `new/` only, because a message in `cur/` has a claimant.

`prune.py` splits into total readers and two doers — `select()` (pure, over
an already-loaded task list), `refusal()`, `dependents_first()` (pure batch
ordering), `plan()`, `worktree_plan()` (the `--dry-run` preview of the git
half), then `remove_task_worktree()` and `archive_task()` — so the selection
is reusable rather than tangled into the command.

The refusals are substrate rails of the same class as the runner's, not
manager policy: a **live runner** would keep writing into a directory that
moved out from under it; an **attached** task's workdir is the user's own
checkout; a task **something else still depends on** would leave a dangling
`depends_on` (a dependent pruned in the same pass is not a reason to keep
it); and **stranded work** — uncommitted or unpushed commits in the
worktree, read with the same `workdir_git_state` probe the digest uses — is
exactly what the rest of quorum works to keep visible, so archiving the one
record that surfaces it would hide it. Only the last is overridable, with
`--force`; the other three name an action the user can take instead (wait,
detach, prune the dependent too). The stranded-work check is skipped
entirely for a `use_worktree = false` task: that workdir is the user's own
checkout, where the dirt is theirs and says nothing about the task record —
the same reasoning that keeps an attached task's checkout off limits.

`--worktrees` adds `git worktree remove` plus branch deletion, and treats the
two asymmetrically because git does. **`--force` is never passed to `git
worktree remove`**: uncommitted and untracked files in a worktree are exactly
the stranded work the rest of quorum surfaces, and no flag on a tidy-up
command should destroy them, so a removal git refuses leaves the worktree
alone *and* the task unarchived — the record is the only thing that would
have said the work was there. `--force` therefore has exactly two meanings:
waive the stranded-work refusal, and upgrade `git branch -d` to `-D`. The
second one does lose data — an unmerged branch's commits go with it — which
is why it is behind the flag and said out loud in the confirm prompt;
unforced, an unmerged branch is kept with a note and the task is archived
anyway, its commits still in the repo.

The archive loop re-derives `refusal()` for each task immediately before
archiving it, because `plan()` ran before an interactive confirm that a
runner could have started during, and because a task skipped mid-sweep
leaves the batch: an upstream that passed the dependency check only because
its dependent was going too is refused again rather than archived into a
dangling `depends_on`. `plan()` returns the batch `dependents_first()`, which
is what makes one in-order pass enough.

`--dry-run` prints the same plan and touches nothing — with `--worktrees` it
also prints the git half per task (`would remove worktree …`, `would delete
branch … (-d/-D)`), including the dirty worktree it would leave and the task
that would stay unarchived because of it. A prune journals one entry through
`_actor_guard`, not one per task: it is a single decision, and per-task
entries would burn an agent's action cap mid-sweep and leave the tidy
half-finished.

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
   directory holds uncommitted changes or unpushed commits, and
   `perpetual=true` on a task that is not meant to finish (above); attached
   sessions in their own clearly-labeled section (last hook event age, git
   state, reports — never runner liveness, which they don't have); recently
   finished tasks, marked `STRANDED-WORK dirty=N unpushed=M` when they
   ended with such state — work a harness left in its worktree without
   delivering it, which the default manager prompt treats as not done and
   relaunches with a nudge to commit and push; a `ci:` line carrying the
   pull request behind the branch, `CI-FAILING` on a finished task whose
   checks are red (see below); `waiting-on=` and the `DEP-*` flags for a
   task with declared dependencies (see *Task dependencies* above); a
   `possible-loop:` line
   when a task's transcript tail is dominated by one repeated tool call
   (see below); `STALLED` when a live runner has printed nothing for
   `STALL_QUIET_MINUTES`, with `stopped=` / `fresh_sessions=` /
   `last-run=stalled` counting what has already been done about it (see
   below); `overlaps=<short-id> paths=N` on both lines of a pair of
   live worktree tasks on one project whose branches change the same files,
   with an `overlap:` line naming up to three of them (see below); a `usage:` line with what the task has spent when its
   harness reported usage at all, plus `BUDGET-EXCEEDED` per run past a
   configured `[tasks]` budget, the last run's marked `next run gated` (see
   *Token/cost usage* above); a three-line **self-observation header** (`agents/harness_run.py
   ::self_observations`) — what the manager's own recent runs have cost,
   `Your last N runs: ok 2m10s · TIMEOUT 15m00s · …` off the same ledger,
   and `Actions this run: 0 of <cap> (cap)` (always `0`: the digest is built
   before the run starts, so the line reports the budget, and what happened
   to it lands in the journal as `cap.hit`) — all three observations the
   prompt judges, none of them a rail: nothing pauses, throttles or changes
   the cap; its **notebook** (see below); the manager's own
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
UNCHANGED); and it enforces the one supervision rail quorum keeps — a per-run action cap
(`max_actions_per_run`), a rate limit that bounds a bad run's blast radius
without ever second-guessing a choice. A refused action appends one
`cap.hit` entry per run (its `args` naming the action refused), so the run
that ran out of budget is visible in the *next* digest's journal section
rather than only as an error the model saw mid-run; the entry is not itself
an action and does not count against the cap. The task budget
gate (*Token/cost usage* above) is the only other rail of that class.

**The notebook (`notes.py`).** The journal is what the manager *did* this
run, read back as a bounded tail; a note meant for next week is pushed out
of that window by the next busy tick. The notebook is the other memory:
`state/manager/notes.jsonl` (per-agent `state/agents/<name>/notes.jsonl`,
via `actor.notes_path`), append-only, one entry per line —
`{id, ts, run_id, sender, text, ttl_days?}` for a note and
`{id, ts, run_id, sender, retired: true}` for the tombstone `quorum manager
forget` appends. Written with `quorum manager remember "…" [--ttl N]`,
which goes through `_actor_guard` like every other mutating command, so a
note is journaled, attributed via `current_actor()` and counted against the
run's action cap.

It is a **separate buffer** on both sides, and that is the whole design:

- *Write side.* Not a board topic, so no reporting task or chatty agent
  posts into it in the ordinary course of things. Only the notebook's own
  agent and an untagged human may write (`notes.may_write`); a call tagged
  as a task or another agent is refused with a pointer to `task report` and
  `board post attention`. Be honest about what that fence is: `may_write`
  reads `QUORUM_ACTOR` from the environment, and any process that can run
  the quorum CLI can set it. The runner stripping the actor tag from task
  runs, and this check, are **conventions that keep honest callers out of
  each other's memory** — they stop accidental crowding, not a harness that
  decides to impersonate the manager. The real boundary around a notebook
  is the filesystem the run is given (`sandbox.py`), not this check.
- *Read side.* `notes.digest_section` renders the notebook **before** the
  task section, under `NOTES_MAX_ENTRIES` / `NOTES_MAX_BYTES`, which nothing
  else in the digest spends. Ten live tasks with long report tails cannot
  shrink it. Over the cap the newest notes are kept and the digest says how
  many older ones it dropped; when the file itself has outgrown
  `NOTES_SCAN_BYTES` the section also says how many bytes went unscanned, so
  a truncated memory is visible rather than silent; a single very long note
  is truncated
  (`NOTE_MAX_CHARS`) rather than allowed to evict the rest.

Nothing is compacted or summarized in Python: expiry is the only automatic
retirement (`ttl_days`), and consolidation — one superseding note, then
`forget` the ones it replaced — is policy in `prompts/manager.md`. Readers
are pure file readers (`quorum manager notes`, `views.agent_detail` →
the TUI's agent pane and the web agent detail), and a prompt agent's
template gets the same rendering wherever it writes `{notes}`.

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
A perpetual task is skipped entirely — repetition is its job, so the read
has nothing to say about it (above).
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
ignore), and the only rails stay rate limits that never read the flag (the
per-run action cap, the task budget gate).

**Overlap observation (`overlaps=`).** The motivating incident: the manager
launched two queued tasks in one tick, both forked from the same base, and
both PRs came back `MERGE-CONFLICT` — its own escalation named the fix
("scope concurrent tasks to non-overlapping files"), but the digest showed
each task's git state in isolation, so it had nothing to judge overlap
*with*. `overlap_signal(live)` (`agents/manager.py`) closes that gap at
digest build: for every pair of live worktree tasks on the same project it
intersects the path sets their worktrees have changed, read by
`tasks.worktree_changed_paths` — the working tree against the merge-base
with the base branch (so committed work, staged and unstaged edits, and
untracked files all count: a live task has usually not committed what it is
touching right now). The base is the branch checked out in the repository's
main worktree, because that is exactly what the runner forked the task
branch from (`git worktree add <path> -b quorum/<id>` takes no start-point).
It has to come first: a checkout one unpushed commit ahead of the remote,
measured against `origin/HEAD` instead, would put that commit's paths into
*every* live task's changed set and report an overlap on a file neither task
wrote. `refs/remotes/origin/HEAD` (set by `git clone`) is the fallback — the
base for a `--no-worktree` task sitting on the main branch itself — and the
branch's own upstream the one after that; with none of those the task is
simply unobservable. Read-only git plumbing, no network, never `git fetch`:
the comparison is against whatever the repository already knows.

A non-empty intersection renders ` overlaps=<short-id> paths=N` on *both*
task lines plus an `overlap:` line naming at most `OVERLAP_MAX_PATHS` (3)
of the shared paths. Attached sessions and tasks run with `--no-worktree`
are never compared: that directory is the human's checkout, and its diff is
theirs. Cost is bounded by `OVERLAP_MAX_PAIRS` (20) pairs per digest — pairs
are what grows quadratically, while the git subprocesses are per task (each
worktree read once, memoized) and digest build blocks the tick — spent in
digest order, so a home with more
concurrency than budget still sees its first pairs and the rest go
unobserved. That is the same *prefer false negatives* setting as
`possible-loop`, and the same contract: an observation, never a rail.
Parallel edits to one file are sometimes exactly the job, so Python decides
nothing; `prompts/manager.md` tells the manager to judge — nudge both to
fetch and rebase before pushing, or serialize the two — and the task
preamble's delivery protocol now carries the matching step on the other
side: fetch and rebase onto the base branch before pushing, push again with
`--force-with-lease` (never a bare `--force`, never off the task's own
branch) when a rebase leaves an already-pushed branch unable to
fast-forward, and report `blocked` naming the conflicting files when the
rebase cannot complete.
Views never show it (they stay pure file readers; the read happens at digest
build only, alongside the CI probe, and nothing is materialized to disk).

**Stall observation (`STALLED`).** The other half of *Stopping and
restarting a run* (above), and the half that judges. `stall_minutes` reads
the mtime of a task's transcript — deliberately not `last_activity`, which
also counts the runner lock and the reports file, both of which a hung run
leaves fresh — falling back to when the live run acquired its lock when
there is no transcript at all, because a *first* run that hangs before
printing anything (#24's stdin block) is the loudest hang there is and it is
the one with nothing to age. A live runner silent for longer than
`STALL_QUIET_MINUTES` (30) gets the flag. Only a *live* runner: a dead one
is simply a task to relaunch, which the manager already handles. Like
`possible-loop` the threshold is a plain module constant tuned to prefer
false negatives, because a flag that fires on a long test run teaches the
manager to ignore it, and like `possible-loop` it is an **observation, not a
rail** — quorum ends no run on its account. `prompts/manager.md` holds the
policy: look at the tail once, `task stop` then relaunch, then relaunch
`--fresh-session` with a summarizing nudge, then escalate to `attention`
after two fresh restarts, reading which step it is at off the task line's
own `stopped=` / `fresh_sessions=` counts. The *rail-shaped* answer to the
same failure is the runner's stall watchdog, which is opt-in config rather
than supervision.

**CI observation (`ci:`).** `workdir_git_state` follows work as far as
"pushed" and stops; `ci.py` — the digest-facing half over `forge.py`, the
one module that shells out to a forge CLI — carries it one step further, to
whether what was pushed actually works. For
each digested task with a workdir it runs one `gh pr view --json
number,url,state,isDraft,mergeable,statusCheckRollup` *inside that
directory*, so gh resolves the repository from the remote and the PR from
the checked-out branch (`quorum/<short-id>` for a worktree task, whatever
the human is on for an adopted one). The rollup mixes CheckRun (Actions:
`status` + `conclusion`) and StatusContext (classic: `state`) shapes;
`_verdict` classifies each as pass/fail/pending and treats anything it does
not recognize as pending, because an unknown shape must never read as a
pass. The line is `key=value` like the rest of the digest — the PR's own
state (`state=open` / `merged` / `closed`, normalized by
`ci.normalize_state` from whatever the forge calls it), check counts,
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
entirely — and so does a config.toml quorum cannot read at all: the table it
failed to parse may be the one holding that switch, so an unreadable config
means *disabled*, never "defaults". That is the general policy for the two
fail-soft probes (`ci.py`, `herdr.py`): they read config through
`config.try_load_config`, whose `None` means "no usable config" and
therefore off, while the read-only views degrade through
`config.load_config_or_default` — the single fallback helper the CLI, views
and the manager digest share — which fills in defaults but is never allowed
to *enable* something a user may have switched off. Those are the only
knobs, and none of them changes what anything *does* about the result.

Which is the point: like `possible-loop`, this is an observation, not a
rail. Python never nudges, relaunches, or blocks on red CI. `prompts/manager.md`
says how to read the line, and the shipped `prompts/babysitter.md` (below)
is a whole reactive policy written as prompt text.

#### The forge seam (`forge.py`)

One rule, unchanged since the probe landed: **exactly one module shells out
to a forge CLI.** It used to be `ci.py`, which was honest while every call
was about a pull request. Issue intake (#62) made that a lie in the other
direction — `task add --issue` is not CI observation — so the subprocess
moved to `forge.py` and `ci.py` kept the digest half. There are three entry
points and two contracts:

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
once; the soft half degrades every exception it raises to `None`, and the
loud half tells a timeout apart from a call that never started, because
those have different fixes. Config is read through `try_load_config` for both, so an unreadable
config.toml means *off* — the soft half goes quiet and the loud half says
so. Provider selection is `cli_name(home)`, today a constant: that one
function is where #51's `[ci].provider = gh | glab | none` switch lands, and
because every subprocess asks it, a second backend cannot re-introduce a
`gh` call somewhere else.

There is no write path. Not "not yet" — reading an issue to make a task out
of it does not imply permission to comment on, label or close it, and a
supervisor that edits a forge behind a human's back is exactly the kind of
privileged infrastructure quorum refuses.

#### The merged observation

A task's lifecycle ends at the harness's word (`done`); its work is
delivered when the PR merges. Quorum keeps the two apart — `done` is the
harness's word, merged is the forge's, and **nothing in Python turns one
into the other**. But the second fact has to survive the probe that saw it,
because the surfaces that would show it never make network calls.

So there is exactly one materialized probe result. When `build_digest`'s
probe returns a state in `tasks.PR_STATES` (`open` / `merged` / `closed`),
`tasks.record_pr_state` writes it — with `pr_state_at` — onto
`tasks/<id>/task.json`. Every reader then gets it for free off the file:
`quorum status` / `task list` / `task show`, the TUI and the web dashboard
badge `✔` merged and `⊘` closed-unmerged straight out of
`views.task_rows`, and `views.py` still never acquires a `gh` subprocess.

This is a deliberate revision of the rule this section used to state
outright ("nothing materializes its result to disk"). The rule was there to
stop views from growing network calls; it did not anticipate a fact that is
*durable* — a merge is final, unlike a check rollup, which is only ever true
as of now. The narrow exception is fenced by five properties, and a second
materialized probe result would have to earn all of them again:

- **one writer**, `record_pr_state`, called from **one place**, the
  digest's probe closure — and `open` only for a task whose status is
  terminal. A live task's `task.json` is being written by its own detached
  runner (the one file race quorum has no lock for), and `open`, the state
  a PR sits in for the whole of a task's working life, is rendered by no
  surface, so taking that race for it buys nothing. A merge or a close is
  written wherever it is seen, live task included: it is durable, every
  surface badges it, a PR can land while its task is still running, and a
  perpetual task never reaches a terminal status at all. A task already
  recorded `merged` is not probed again at all: merge is final, so the
  remaining ticks of its 24h window would spend a subprocess to re-learn
  it. Its line in the digest carries `pr_state=merged` instead of a `ci:`
  line.
- **closed vocabulary**: only the three known states are written. `unknown`
  writes nothing, so a forge shape quorum does not understand can never
  badge a task as delivered — and nothing re-probes a task once its worktree
  is gone.
- **never a status**: `status` and `pr_state` are separate fields, and no
  code path derives one from the other.
- **`updated_at` untouched**. It means "someone acted on this task", and the
  digest's recently-finished window is measured from it; a probe that bumped
  it would pin every merged task in that section forever. The record is also
  re-read from disk rather than dumped from the in-memory `Task`, so a
  status a live run reported meanwhile is never rolled back.
- **fail-soft to the end**: an unreadable or unwritable `task.json` is a
  lost observation, re-made on the next tick, never a failed digest.

Absence stays uninformative, exactly as the missing `ci:` line is: no
`pr_state` means no PR, no `gh`, `[ci]` off, or a supervisor that was never
up while the PR was open — never "not merged". And a recorded state is only
as fresh as `pr_state_at`, which every surface that has room prints next to
it.

Two consequences elsewhere: a merged PR suppresses `CI-FAILING`
explicitly (a forge may keep serving a stale red rollup after the merge, and
that must never send the manager to relaunch finished work), and the manager
prompt gains the reading — a merged task needs nothing; a `done` task whose
PR was closed unmerged is a human decision quorum cannot interpret, worth
one line to the human and nothing else.

Failure story: missing harness config, nonzero exit, or timeout → the tick
raises. Crash isolation records it (heartbeat, board); the manager's
`auto_pause = false` config keeps the schedule firing so recovery needs no
human intervention. Every other supervisor announcement — tick errors,
auto-pause — lands on `system`, which no banner reads; a normally-pausing
agent at least *stops*, but the manager keeps firing, so a *sustained*
streak escalates on its own: at `MAX_CONSECUTIVE_FAILURES` the supervisor
posts one `agent.failing` to `attention` (the banner `quorum status`, the
TUI and the web header all read). Recovery is announced on `system`, not
`attention`. See [Messaging protocol](#messaging-protocol) for the dedupe.

### Prompt agents

The generic sibling of the manager: `type = "prompt"` runs a user-written
prompt (`prompts/<name>.md`, or `settings.prompt` to point elsewhere) over
the configured harness on a schedule, sharing the manager's exact run
mechanics (`agents/harness_run.py`): actor-tagged env, per-agent journal and
action cap, transcript at `state/agents/<name>/transcript.jsonl`, mid-run
directives via the agent's own inbox when the harness supports injection.
There is deliberately no wake condition and no digest — a prompt agent runs
every scheduled tick, and anything conditional belongs in its prompt. A
template that writes `{notes}` gets its notebook *and*, above it, the same
self-observation header the manager's digest opens with (spend, recent run
outcomes, action budget): there is deliberately no second `{self}`
placeholder, so an agent's memory of itself is one block and a template
that already writes `{notes}` gets the header without being rewritten. A
template that writes neither sees neither — including the shipped
`babysitter.md`, which keeps its policy in prompt text and asks for no
notebook. Prompt
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
- The same archive is where **on-demand** clearing goes: `MessageBus`
  exposes the janitor's per-message path as `archive_board_message`, with
  `ack_board_message` (behind `quorum board ack <id>`), `archive_topic`
  (behind `quorum board clear` and its `board ack --all <topic>` alias) and
  `clear_inbox` (behind `quorum task inbox --clear`) on top of it. Clearing
  and acking are archival, not a flag on the message, so the board keeps
  carrying no read-state and any number of readers still coexist.
- **Acknowledgement** is that per-message path aimed at the attention banner.
  `views.attention_summary` is a seven-day window over the `attention` topic,
  so without an ack an escalation the human has already handled sits in
  `quorum status`, the TUI header and the web header for a week; acking
  archives that one message, which drops it from every view (they are all
  pure readers of the live topic) while the history keeps it with its
  original `created_at`. `resolve_board_message` accepts a full message id, a
  unique prefix, or the unique suffix `Message.short_id` prints — the grammar
  `TaskStore.resolve` already taught — and raises `KeyError`/`ValueError` for
  unknown and ambiguous, because a silently-wrong ack archives someone else's
  escalation. `board read` prints that short id so there is something to type.
  The affordance repeats in both dashboards as one shared bus call and no
  view-local write logic: the TUI's `a` opens the attention list and acks the
  highlighted line through `_write` (an unwritable home notifies, it never
  takes the dashboard down), and the web dashboard's per-escalation **Ack**
  button posts to `/api/board/{topic}/ack/{message_id}`. Both lists carry
  `views.ATTENTION_LIST_LIMIT` entries rather than the banner's handful:
  every line one of them renders is one the reader may want to ack, so a
  count the list cannot reach would be an escalation nobody can dismiss
  from that surface.

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

**Failure escalation** rides the board rather than the control channel. Every
failed tick posts `agent.error` to `system` and records the streak on the
heartbeat (`consecutive_failures`, `error`); at `MAX_CONSECUTIVE_FAILURES`
(5) the agent is auto-paused with an `agent.paused` post — also on `system`.
Nothing in that story reaches a banner: `views.attention_summary` reads the
`attention` topic alone. For an ordinary agent the pause is itself the loud
signal (it stops, and `quorum agent list` says `paused`), but an agent whose
config sets `auto_pause = false` — the manager, which must keep firing so it
self-recovers — is exempt from the pause and would just fail all night in a
channel nobody watches. So the supervisor posts one `agent.failing` to
`attention` for such an agent instead. **That post is the only failure path
in quorum that reaches `attention`**; everything else the supervisor says,
recovery included, stays on `system`.

The dedupe is a third heartbeat field, `escalated_at`: written *after* the
post lands (a stamp-first escalation whose post threw would suppress the
banner for the rest of the streak), checked before posting (so a ten-hour
outage is one post, not one per tick), and cleared by every success path —
a scheduled tick, `quorum agent run-once`, and `quorum agent resume` all
clear it alongside `error` and `consecutive_failures`, which is also what
makes the closing `agent.recovered` post (on `system` — a self-healed
outage is informational, and the time-windowed banner has no read-state to
dismiss) fire exactly once. Nothing here pauses, retries or throttles the
agent; the post is an observation, in the same class as `possible-loop`
and `ci:`.

### Board consumers: the notification hook

The board carries no read marks, so *reaching* someone is a consumer's job,
and `notify.py` is the one consumer quorum ships for a person rather than
an agent. An optional `[notify]` table holds an argv template — the same
shape as `[harness.<name>]`, substituted element-wise (`{text}`, `{from}`,
`{topic}`, `{type}`, `{id}`; a template with no `{text}` gets it appended,
like a harness template with no `{prompt}`), so there is no shell and
nothing to quote — and the topics that fire it (default `attention`, the
one topic meant for a human). The supervisor runs `_notify` on the control
cadence (15 s, and once at startup, that startup catch-up running *before*
the scheduler and the janitor so it can neither race the job's first fire
nor lose an escalation the janitor is about to archive): it reads each
listed topic past a private cursor kept in `state/notify.json` (the last
on-disk filename processed, per topic — `MessageBus.entries_after_cursor`
hands back real filenames, because a message's own `filename()` is only
what `post()` happened to write) and runs the template once per message,
oldest first, advancing and persisting the cursor *before* each delivery.
Delivery is therefore **at-most-once** by design: a crash — or a failed
cursor write — mid-hook loses one notification, where the other order
would repeat it every 15 seconds for as long as the disk stayed full.
Nothing is ever delivered twice, including across a restart or between
the startup drain and the job (`drain` takes a process-wide lock, since
APScheduler's `max_instances` guards a job only against itself). That is
the documented board-consumer pattern and nothing more: no queue, no retry
store, no second transport, and a message posted while the supervisor is
down goes out on the next start.

Three stances hold it in shape. **It fires on topic membership, never on
content**: what is escalation-worthy stays prompt policy, and the hook
would deliver a `note` on `attention` as readily as an `escalation`. **It
fails soft** in herdr's mold, not sandbox.py's: a missing binary, a
nonzero exit or a hang past `[notify].timeout_seconds` is one line in
`logs/supervisor.log` and an advanced cursor — a notification that cannot
be delivered must not block the ones behind it, and nothing here can fail
a tick, a board post or the supervisor (`drain` catches everything,
including an unwritable cursor file; an unreadable one is re-initialized
with a log line rather than raised at every tick). **Enabling it starts
from now**: the first drain arms each topic's cursor at its current tail
without delivering, so turning the hook on does not replay a month of
old escalations the banner already showed. A per-tick cap
(`MAX_PER_TICK`) keeps a suddenly busy listed topic from wedging the job
thread; the rest waits for the next tick. Shutdown shortens that further:
`quorum down` ends in `scheduler.shutdown(wait=True)`, which waits for the
running job, so the supervisor calls `notify.request_stop()` first and the
drain stops between messages — `down` is bounded by the one delivery in
flight rather than by a whole batch (`MAX_PER_TICK` × `timeout_seconds`).
Nothing is lost: the cursor advanced only past what actually went out, and
the startup catch-up delivers the rest. The template runs with the
supervisor's environment, as a harness does — not a security boundary.

`quorum notify test "…"` runs the template once, directly and loudly (exit
1 with the reason), without touching the board or the cursor; `quorum
doctor` reports the table (`–` when absent, `✗` when `command[0]` is not
on PATH, `–` when the template has no `{text}`).

### Design seam: outboxes and a router

v1 delivers directly (writer → recipient's `new/`), because everything
shares one permission domain. If agents are ever sandboxed *from each
other*, the seam is `MessageBus.post()/send()`: swap in an
outbox-spool-plus-router implementation with no agent code changes.

## Views and their write affordances

`views.py` assembles the read model out of files alone — no locks, no
network, no supervisor required — and `quorum status`, the TUI and the web
app are all readers of that one model, which is why they never disagree.

The CLI's listings (`quorum status`, `task list`, `agent list`, `project
list`) render that model as Rich tables (rich is already typer's dependency)
rather than concatenated lines — the shape that grew a clause per feature
until a row with a report and a PR URL wrapped mid-cell past column 80
(#52). One table builder per row kind in `cli.py` (`_task_table`,
`_agent_table`, `_project_table`) turns the `views.*_rows` dicts into cells
— rendering only, never re-deriving — and one `_print_table` renders the
result two ways. On a terminal the table is fitted to the window: the
report and flags (agents: error; projects: tags) columns absorb the
shortfall with an ellipsis, so the id, status, harness, pr and usage
columns stay whole down to the width at which the give-way column has
nothing left to give (around 60 columns for a task listing). Below that
floor Rich clips the fixed columns too, and only the id — the handle you
retype into `task run` — holds a `min_width` (`ID_MIN_WIDTH`), so it is the
last cell to be cut. Fitting is conditional on a give-way column having
survived the drop: a table of nothing but fixed columns (the usual `agent
list`) is rendered at its natural width rather than expanded, or a wide
window's slack would be spread evenly over the columns and leave the fields
acres apart. Off a terminal (a pipe, a file, `CliRunner`) it is laid out at
its natural width, plain text, no ANSI and no trailing padding, so every
id, status and `#N` reference is whole and greppable. Columns empty on
every row are dropped; a PR URL is shortened to `#N` (`!N` for a GitLab
merge request, the URL as given otherwise) with the full URL kept for `task
show`; a report is folded to one line and clipped at `REPORT_MAX_CHARS`.
`_print_table(width=80)` is the test seam: an 80-column render must have
exactly one line per row.

The reads are pure; the writes are deliberately not absent. Both dashboards
carry a small set of *write affordances*, and the rule is that each is a
thin call into the same code path the CLI uses — a `MessageBus` send, a
`TaskStore.update`, `runner.launch_detached`, `config.create_agent` — never
write logic that lives in a view:

- **TUI** (`tui/app.py`): nudge a task (`n`), send the manager a directive
  (`m` — the `manager` inbox, exactly `quorum manager tell`, and the reason
  the TUI needs no task-add form: the manager runs `task add` itself,
  journaled and capped), start a detached run (`s`), cancel a task (`c`),
  hold/release a task (`h`) and nudge its priority (`+` / `-`).
  `s` refuses an attached task, a held task and a task whose runner is
  alive, mirroring the runner's own substrate rails; `c` is the one
  destructive binding, so
  it goes through a yes/no `ConfirmScreen` and, like `quorum task cancel`
  without `--kill`, marks the status without signalling a live runner.
  `h` and `+`/`-` do not confirm — hold is not a status and both are
  reversible by pressing the key again — and the table is never reordered
  by priority, since a row that jumped under the cursor would make the next
  keystroke act on something else.
  They all resolve their target the same way (`_target_task`): the
  *highlighted* row while the task table has focus, falling back to the open
  task when the reader is down in its detail. `enter` opens a transcript for
  reading and nothing more — a selection made once must not silently become
  the target of every later keystroke. And every one runs through `_write`,
  which turns an `OSError` into an error notification: an unwritable
  QUORUM_HOME is exactly when a reader needs the dashboard most, so no
  keystroke may take it down.
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

Two files in a project directory are quorum's by convention, both read-only:
the `.quorum.toml` marker above, and `.quorum/task-preamble.local.md`, which
fills the task preamble's `{project}` slot (see [Prompts](#prompts)).

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

## Diagnostics: `quorum doctor`

`doctor.py` is the counterweight to how much of quorum fails soft. Every
degradation elsewhere in this document is deliberate — an unreadable
config.toml disables the optional probes rather than killing a tick, an
unauthenticated `gh` yields `None`, a stale seed in `prompts/` keeps
rendering, a crashed run leaves a `runner.lock` nobody trips over — and each
one is invisible by construction. Doctor is the single place that goes and
looks.

Three rails, and they are the whole design:

1. **Diagnose, never repair.** Each line names its own fix (a config key, a
   shell command); nothing in the module writes to QUORUM_HOME. `--fix` is
   not a planned feature — an autofix would have to guess which of two
   defensible states the user wanted.
2. **A pure reader plus exactly one opt-in probe.** The static checks are
   file reads and `shutil.which`, in the same family as `views.py`. The
   exception is `--smoke`, which runs a harness for real, in a
   `TemporaryDirectory`, through the runner's own `build_harness_argv` /
   `guidance_pump` / `stream_transcript` — including `inject` stdin
   delivery — and asserts a `result` event and a captured session id inside
   a short timeout. It reuses the runner's code rather than a simplified
   copy because a copy would drift away from the very bug it exists to
   catch (#24: a stream-json CLI ignoring an argv prompt, so every run hung
   until it timed out). Everything the probe touches is scratch: the
   guidance pump's bus, the working directory, and the child's own
   `QUORUM_HOME` — that last one because a harness with quorum's
   integration hooks installed runs `quorum task hook-session-start` on
   startup, and the probe must not let it write to the live home. The child
   is spawned with `start_new_session=True` and the timeout `killpg`s the
   group: harnesses wrap themselves, and killing only the process quorum
   spawned leaves grandchildren holding the pipe well past the budget the
   probe is there to measure.
3. **Three states, no fourth.** `ok` / `problem` / `na` (✓ / ✗ / –), where
   `na` covers "you turned this off" and "there is nothing configured to
   check". Only `problem` sets a non-zero exit, which is what makes
   `quorum doctor --json` usable in a script and keeps a `–` from training
   anyone to ignore the output. Two consequences worth stating: a fresh
   `quorum init` home — no `[harness.*]` table, no `default_harness` — is
   one `–` line ("no harness configured yet") rather than two ✗ for one
   unmade decision, and a `gh` that never answered is `–` too, because an
   offline laptop says nothing about whether anyone is logged in.

Doctor asks other modules rather than reimplementing them, which is what
keeps its answers from drifting from the code it reports on: `gh` through
`forge.auth_status` (the module that owns every forge-CLI subprocess), prompt
staleness through `home.classify_prompt` (the classification `quorum init`
seeds by), sandbox support through `sandbox.availability()`, the trust dials
through `dials.current` (the registry the guide's table is tested
against, rendered as `dial.*` lines that are `–` by construction: a
cautious default and a deliberately loosened value are both facts, not
faults). The
`[notify]` line is static (argv[0] on PATH, `{text}` in the template);
actually running the template is `quorum notify test`, which is loud
where the supervisor's delivery is deliberately not.

One small function per check, each taking only what it needs (a `Config`, a
`HarnessConfig`, a home path), so every check has both a passing and a
failing test. `check_config` is the one caller in the codebase that uses
strict `load_config` on purpose: everywhere else papers a broken file over
with defaults so work can continue, and this is where the user finally
hears about it.

Two things elsewhere exist to feed it: `supervisor.lock` records the version
of quorum that started the process (so "you upgraded but never restarted" is
a line rather than a memory), and `home.classify_prompts` is the read-only
half of the seeding logic `quorum init` acts on.

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
