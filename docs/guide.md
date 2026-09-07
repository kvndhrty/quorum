# The quorum guide

Quorum queues coding tasks and hands each one to the coding-agent CLI you
already use. This guide is how to run it. The first section gets you from an
empty machine to a running task; **Watching** and **Steering** are what you
do in the first week; everything after the [Reference](#reference) heading is
there when you need it, starting with a [glossary](#glossary) of every word
quorum uses.

Design rationale — why each piece works the way it does — is in
[architecture.md](architecture.md).

- [Start here](#start-here)
- [Watching](#watching)
- [Steering](#steering)
- [Reference](#reference)

## Start here

Five steps: install, register a project, queue a task, start the supervisor,
read status.

### 1. Install and configure a harness

Python 3.11 or newer.

```bash
uv tool install quorum-orchestrator     # the `quorum` command and its TUI
quorum init                             # scaffolds ~/.quorum and a config.toml
```

`quorum init` writes `~/.quorum/config.toml`. It is yours: quorum reads it
and never writes it back. The one thing you must do is tell quorum how to
invoke your **harness** — the coding-agent CLI that will do the work. The
scaffold ships ready-to-uncomment blocks for `claude`, `codex` and `opencode`,
plus a template for any other agentic binary:

```toml
[tasks]
default_harness = "claude"      # uncomment the matching [harness.*] block too
```

A harness block is an argv template with `{prompt}` and `{session}`
substituted into it. Runs are unattended, so the harness needs permission to
act without asking: one that stops for an interactive prompt stalls silently.
The shipped blocks grant a scoped tool allowlist rather than a blanket bypass,
covering file edits, git and quorum's own CLI, which is how the harness
reports progress. Full detail in [Harnesses](#harnesses).

Then check the setup against reality:

```bash
quorum doctor          # one line per check; exit 1 if anything is ✗
```

### 2. Register a project

A project is a directory, usually a git repo. Tasks run against projects.

```bash
quorum project add ~/work/my-api
quorum project list
```

The slug is a slugified form of the directory name, or of `--name` when you
pass one.

### 3. Queue a task

A task is a prompt pointed at a project.

```bash
quorum task add my-api "add rate limiting to the public endpoints, then open a PR"
```

That writes a record and stops. Nothing runs yet. `quorum task list` shows it
queued, with a short id (`a3f2k9`) you can use anywhere a task id is wanted —
any unique prefix or suffix of the full id works too.

You can run it by hand right now, without a supervisor:

```bash
quorum task run a3f2k9             # one run, in the foreground
quorum task run a3f2k9 --detach    # the same, in the background
```

A **run** is one invocation of your harness in the task's working directory,
which by default is a fresh git worktree under `~/.quorum/worktrees/<id>` on
branch `quorum/<short-id>`. Your checkout is never touched and parallel tasks
cannot collide. Every run's prompt starts with a preamble teaching the
harness to report progress back:

```
quorum task report <id> --status <word> "<note>"
quorum task inbox <id> --claim
```

Status is whatever word the harness reports — the conventional flow is
`planning → executing → reviewing → pr → done`, and only `done`, `blocked`
and `cancelled` mean anything to quorum itself. The preamble also tells the
harness to commit and push before reporting `done`, so the deliverable is a
pushed branch, with a pull request when a forge CLI happens to be installed.

### 4. Start the supervisor

`quorum up` is one ordinary process that runs agents on a schedule. There is
no daemon, no root, no cron, and no open port.

```bash
quorum up                # foreground; Ctrl-C stops it
quorum up --detach       # background; `quorum down` stops it
```

The one agent it ships with is the **manager**, and the manager is itself
driven by your harness. Every five minutes it builds a **digest** — every
task's status, whether its run is alive, how long it has been quiet, its
recent output, plus what the manager itself did last time and whether that
changed anything — renders `~/.quorum/prompts/manager.md` over it, and runs
your harness. The harness then acts through the same CLI you use: launch a
queued task, nudge a stuck one, relaunch a dead one, queue follow-up work, or
escalate to you.

Supervision policy is that prompt file, not code. Nothing in quorum decides
what to launch.

### 5. Read status

```bash
quorum status
```

```
supervisor: running (pid 4711, since 2026-08-30T22:10:04Z)

agents:
name       status  schedule  last                  usage
● manager  idle    every 5m  2026-08-30T22:35:02Z  $0.31 · 84.0k tok

tasks:
id        project  status     harness  report                             pr   usage
· xrxapw  my-api   pr         claude   opened the PR                      #53  $0.42 · 11.0k tok
▶ 75htqx  my-api   executing  claude   running the suite before pushing

projects:
slug    due
my-api  2026-09-10 (8d)
```

The mark before a task's id is its liveness (`▶` running, `✓` done, `✗`
blocked, `·` anything else); `quorum status --legend` explains every glyph.
Columns nothing fills are dropped, and each table is fitted to your terminal —
piped or redirected it comes out plain and full width, so
`quorum task list | grep <id>` works.

That is the whole loop: register, queue, supervise, read. Everything below is
detail on one of those four.

## Watching

Six commands answer "what is happening", and all of them are pure readers of
`~/.quorum` — they work with the supervisor stopped, over SSH, and never hold
a lock.

```bash
quorum status                 # supervisor, agents, tasks, projects
quorum task list              # every task, one line each
quorum task show a3f2k9       # one task in full (--json for the raw record)
quorum task log a3f2k9        # one run, rendered readably (-f follows a live one)
quorum task history a3f2k9    # one task's whole life, oldest first
quorum tui                    # all of it, live, in the terminal
```

**`task log`** turns a transcript — a few hundred JSON events per run — into
something readable: assistant text in full, one line per tool call with its
first argument, each result collapsed to a size or an exit code.

```
$ quorum task log 5yqg9f
[03:14:08] ▶ run started (session 6a183734 · ~/.quorum/worktrees/01M1JAAX…)
[03:14:10] 💬 I'll start by reading the issue and orienting in the codebase.
[03:14:12] 🔧 Bash  gh issue view 82 --comments
[03:14:13]   ↳ 214 lines · 8.1 kB
[03:16:41] 🔧 Edit  src/quorum/transcript.py  (+41 −0)
[03:24:11] ■ result: success · 148 turns · 24m13s · $13.79 · 20.2M tok
```

`-n 40` bounds it to the last forty entries, `-f` follows a live run, `-v`
unfolds everything it folded (reasoning, full arguments, full results), and
`--raw` prints the transcript's own JSON lines for `jq`. An event quorum does
not recognize prints as its raw line rather than disappearing, so a harness it
has never seen is still readable, just less pretty.

**`task history`** answers "what happened to this task" from every file that
records part of it — the task record, its reports, its guidance, the manager's
journal:

```
$ quorum task history a3f2k9
task a3f2k9  (01M1…A3F2K9)  9 event(s), oldest first
[2026-09-03 09:00:12] queued on my-api · harness claude · from #62
[2026-09-03 09:00:40] manager: task.run — a3f2k9 (status then queued)
[2026-09-03 09:00:41] run 1 started
[2026-09-03 09:01:05] reported planning: reading the issue and the auth module
[2026-09-03 09:14:30] guidance from user: use the existing retry helper
[2026-09-03 09:31:02] reported pr: Add rate limiting · https://…/pull/71
[2026-09-03 09:31:04] reported done: opened #71
[2026-09-03 09:31:09] run 1 ended · exit 0 · $4.12 · 1.8M tok
[2026-09-03 11:00:03] pr state observed: merged · https://…/pull/71
```

Guidance is stamped when it was *sent*, since delivery writes no time of its
own; one the task has not consumed yet says `(waiting)`. `--json` gives the
same rows with their raw fields. The list still answers for a task you have
archived with `task prune`.

**`quorum tui`** is the live dashboard: tasks on top, agents and projects
below, a detail pane at the bottom. Arrow to a task and press `enter` for its
transcript or `t` for its history. The write keys act on the row you are
pointing at, or on the open task while you are reading one.

| key | does |
| --- | --- |
| `enter` | open the highlighted task's transcript and reports |
| `t` | open the highlighted task's history; again for the transcript |
| `n` | send guidance to the highlighted task |
| `m` | send the manager guidance — no task needed |
| `s` | start a detached run of the highlighted task |
| `c` | cancel the highlighted task (asks first) |
| `a` | open the escalation list and archive the one you pick |
| `esc` | back to the board feed (or cancel what you are typing) |
| `r` | refresh now |
| `q` | quit |

`s` refuses a task that is already running or attached to a live session; `c`
only marks the task cancelled — to also stop a live run use
`quorum task cancel <id> --kill`. `m` is the widest of them, because the
manager can do anything you ask it to, including queueing tasks, which is why
the dashboard has no task form. If the home has gone unwritable they all say
so and carry on rather than taking the dashboard down.

The transcript pane follows a live run on its own; the history tab is a
snapshot, rebuilt when you press `t` or `r`, when you open another task, and
after anything you do from the dashboard.

**Reading a manager tick** is a different question — not "what did it type"
but "why did it do that" — and `quorum agent log` answers it from the four
files a tick leaves behind:

```
$ quorum agent log manager
=== manager run 01M1JN0ZB0GZG6WKNG6FH8VKNR — 2026-09-03T03:20:05Z

--- what it saw (01M1JN0ZB0GZG6WKNG6FH8VKNR.md)
# Situation digest — 2026-09-03T03:15:00Z
## Active tasks
- [queued] 5yqg9f readable logs (#82)
… (48 more lines; -v for all)

--- what it said
[03:15:02] 💬 5yqg9f is queued, nothing is running, and no dependency is
           unfinished — launching it.
[03:15:03] 🔧 Bash  quorum task run 5yqg9f --detach

--- what it did
[03:15:04] task.run -> 5yqg9f  [queued -> executing]

--- how it ended
ok · 1m12s · $0.31 · 84.0k tok
```

"What it saw" is the digest that tick was given, kept on disk per run. "What
it did" is quorum's own journal of the actions the CLI recorded as they ran —
not the model's account of them — each with what its target's status was then
and is now. `--last 5` reads the last five ticks, `--run <id>` one by id, `-f`
follows the tick running now, and the same command reads any agent
(`quorum agent log babysitter`). A prompt agent has no digest, so what it saw
is its rendered prompt.

## Steering

Three channels, and you and the manager use all three identically.

**Guidance for one task** goes into that task's inbox. The next run starts
with it in the prompt, under a "Guidance received" heading; a harness that
checks `quorum task inbox <id> --claim` mid-run sees it sooner, and one
configured with `inject = "stream-json"` gets it delivered into the run in
progress.

```bash
quorum task nudge a3f2k9 "use the middleware approach, not decorators"
```

**Guidance for the manager** goes into the manager's inbox and is read at the
start of its next tick. Its prompt is told to follow your guidance above
everything else.

```bash
quorum manager tell "prioritize the api task; park the docs work"
```

**Standing notes** are the third channel, and the difference matters:
guidance is read once and consumed, a note stays until it expires or someone
retires it. Both the manager and each task have a **notebook**
([below](#notebooks)).

```bash
quorum manager remember "task a3f2k9's PR is waiting on my review"
quorum task remember a3f2k9 "the flaky auth test is quarantined, not fixed"
```

Then there are the direct controls:

```bash
quorum task run a3f2k9 --detach       # launch or relaunch it yourself
quorum task stop a3f2k9               # end the RUN, keep the task
quorum task cancel a3f2k9             # end the TASK (--kill also stops a live run)
quorum board read attention           # what the manager escalated to you
quorum board ack 7c1af2               # say you have handled one escalation
```

`quorum status` and the TUI show a banner while anything posted to the
`attention` topic in the last seven days is unarchived:

```
⚠ 1 on #attention in the last 7d — `quorum board read attention`, then
`quorum board ack <id>` for each one you have handled
```

The board has no read-state, so `board ack` is what makes an escalation leave
the banner. It archives the message rather than flagging it, so the history
still says what was escalated when. Message ids resolve like task ids — a full
id, a unique prefix, or the short suffix `board read` prints.

## Reference

### Glossary

One name per idea. These are the words the CLI, the prompts, the digest and
this guide all use.

- **home** — the one directory holding every durable byte quorum touches
  (`~/.quorum` by default; `$QUORUM_HOME`, or `quorum --home <dir>` before the
  subcommand, overrides it, and a `quorum-home/` directory in the current
  directory wins over `~/.quorum`). Plain JSON, JSONL, TOML and Markdown, so
  ordinary file tools read and repair it and copying the directory moves the
  whole setup.
- **project** — a directory registered with `quorum project add`, usually a
  git repo. Tasks run against projects; quorum only ever reads them.
- **task** — a prompt pointed at a project, plus everything recorded about
  it. Identified by a ULID whose short tail (`a3f2k9`) is what you type.
- **run** — one invocation of the harness for one task, until it exits. A
  task is a sequence of runs.
- **runner** — the detached process that performs one run: it takes the task's
  lock, prepares the working directory, composes the prompt, spawns the
  harness and records what happened.
- **harness** — any coding-agent CLI (`claude`, `codex`, `opencode`, your own
  script), described by a `[harness.<name>]` argv template. Quorum never calls
  a model API itself.
- **session** — the harness's own conversation, identified by an id quorum
  captures from its output so a later run can resume it.
- **working directory** — where a run happens. By default a **worktree**: a
  git worktree quorum creates under `worktrees/<id>` on branch
  `quorum/<short-id>`. `--no-worktree` runs in the project directory instead.
- **status** — the free-form word the harness last reported. Only `done`,
  `blocked` and `cancelled` mean anything to quorum: they end the manager's
  attention.
- **report** — what a harness sends back with `quorum task report`: a status,
  a note, optionally a PR url or a handoff.
- **handoff** — one file a finishing task leaves for the tasks that depend on
  it, written with `task report --handoff`.
- **dependency** — a task listed with `task add --after <id>`, which must
  finish before this one starts.
- **perpetual task** — one queued with `--perpetual`, not meant to finish.
- **attached task** — a task whose work is a live interactive session you are
  driving, created with `quorum task adopt`. Quorum observes it and never runs
  it.
- **notebook** — an agent's or a task's standing memory. Written with
  `remember`, retired with `forget`; its entries are **notes**.
- **guidance** — a message steering one recipient, read once and then
  consumed. Sent to a task with `quorum task nudge`, to the manager with
  `quorum manager tell`.
- **message** — the one record type on both channels. It lands either on a
  **board topic** (append-only, public, any number of readers) or in a
  recipient's **inbox** (claimed by exactly one reader).
- **escalation** — a message the manager posts on the `attention` topic to ask
  a person for something. It sits in the banner for seven days or until
  archived.
- **archive** — quorum never deletes a record. `board ack` archives one
  message, `board clear` a topic, `task inbox --clear` waiting guidance, and
  `task prune` a whole task directory.
- **agent** — anything quorum runs on a schedule. Three kinds: the **manager**
  (built in, supervises tasks), a **prompt agent** (a prompt file plus a
  schedule, created with `quorum agent create`), and a **plugin agent** (a
  Python class in `plugins/`).
- **supervisor** — the `quorum up` process that runs agents on their
  schedules. It holds no privilege and listens on nothing.
- **tick** — one scheduled agent run.
- **digest** — the situation summary a manager tick is built from. Each tick's
  digest is kept on disk so `agent log` can show what it saw.
- **journal** — what an agent did, recorded by quorum as each command executes
  rather than reported by the model. `quorum manager journal` prints it, and
  the recent entries go back into the next digest.
- **transcript** — everything the harness printed during a run, one JSON line
  per event. Read it with `task log` or `agent log`.
- **usage log** — one line per agent run saying what it cost and how it ended.
  `quorum usage` aggregates it and the per-task figures.
- **budget** — `[tasks].max_cost_per_run` / `max_tokens_per_run`. A run over
  budget is flagged, and the task's next run is refused until one comes in
  under.
- **observation** — something quorum computes and puts in the digest for the
  manager to judge (`possible-loop`, `overlaps=`, `CI-FAILING`,
  `STRANDED-WORK`, `waiting-on=`). Quorum never acts on one itself.
- **rail** — one of the few things quorum refuses outright: the per-run action
  cap, the budget gate, and the substrate refusals that protect a checkout (a
  live run's lock, an attached task, unfinished dependencies). A rail limits a
  rate; it never vetoes a particular choice.
- **preamble** — the template prepended to every task run's prompt
  (`prompts/task-preamble.md`), which teaches the report, memory and delivery
  protocols.
- **overlay** — text merged into a prompt template without replacing it. The
  **home overlay** is `prompts/<name>.local.md`, merged at the template's
  `{local}` slot; the **project overlay** is a project's registry notes plus
  its `.quorum/task-preamble.local.md`, merged at `{project}`.
- **dial** — a setting that records how far you currently trust the model, as
  opposed to an invariant. They are tabled under
  [Loosening the rails](#loosening-the-rails-as-trust-is-earned).
- **stranded work** — uncommitted or unpushed changes in a task's working
  directory. Every view flags it, and the manager's digest marks a finished
  task in that state `STRANDED-WORK`.

### Harnesses

A `[harness.<name>]` table tells quorum how to invoke your tool. `{prompt}`
and `{session}` are substituted into the argv; a template with no `{prompt}`
gets the prompt appended as the final argument.

```toml
[harness.claude]
start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]
resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]
inject = "stream-json"

[harness.codex]
start  = ["codex", "exec", "--json", "{prompt}"]
resume = ["codex", "exec", "resume", "{session}", "--json", "{prompt}"]

[harness.opencode]
start = ["opencode", "run", "{prompt}"]

[harness.myscript]
start = ["/usr/local/bin/my-agent.sh"]        # prompt appended as last arg
env   = { MY_AGENT_MODE = "headless" }
```

**Sessions.** Quorum scans the harness's stdout for JSON carrying a
`session_id` (claude) or `thread_id` (codex `exec --json`) and stores it on
the task. With a `resume` template and a known session, later runs continue
the same conversation. Without either, every run starts fresh — which still
works, because the working directory and everything the last run wrote there
persist between runs.

**Guidance mid-run (`inject`).** By default guidance reaches the harness at
the start of its next run. A harness whose CLI speaks the Claude Code
stream-json protocol can do better: set `inject = "stream-json"` and pair
`--input-format stream-json` with `--output-format stream-json` in its
templates. Both flags matter — the runner writes user turns to the harness's
stdin and watches stdout `result` events to know when it is idle. With
`inject` set, stdin becomes the only prompt channel: the composed prompt is
delivered as the opening user turn and any `{prompt}` element is dropped from
the argv. Guidance sent while the run is live then reaches the agent at its
next turn boundary, in the same session, and the run ends at the first idle
turn with an empty inbox. Setting `inject` without the flags hangs the run on
stdin, and setting the flags without `inject` hangs the CLI waiting for a
prompt; `quorum doctor` checks for both.

**Autonomy flags.** Runs are unattended. A harness that stops to ask for
permission stalls silently at its first denied tool call, and the manager will
send it guidance and relaunch it to no effect. Prefer a scoped allowlist like
the one above — file edits, git, `gh` for the PR step, and `Bash(quorum:*)`,
which is what lets the harness call `quorum task report` and
`quorum task inbox` — over a blanket bypass. Pair whatever you grant with the
[sandbox](#sandboxing) to confine what a run can touch to the worktree.

**Environment.** The run inherits your environment plus the harness's `env`
table, with `QUORUM_HOME` set — that is how `quorum task report` inside the
run finds the right home.

### Checking your setup

Nearly everything optional in quorum fails soft on purpose — an
unauthenticated `gh` just stops producing CI observations, an unparseable
`config.toml` turns the optional probes off, a stale prompt keeps rendering
last release's policy. Those are the right runtime behaviours, and they are
exactly why none of them announce themselves. `quorum doctor` is the one place
that goes and looks.

```bash
quorum doctor                  # one line per check; exits 1 if anything is ✗
quorum doctor --json           # the same checks, for scripts
quorum doctor --smoke          # ...and run the default harness once, for real
quorum doctor --smoke codex    # ...that one instead
```

```
home: /Users/you/.quorum
  ✓ config.toml parses (strict)
  ✓ git 2.49 at /opt/homebrew/bin/git
  ✓ harness 'claude': claude on PATH (/Users/you/.local/bin/claude)
  ✓ harness 'claude': prompt injected over stdin (inject = 'stream-json')
  ✓ default harness: claude
  ✓ project my-api: /Users/you/work/my-api
  ✗ gh is installed but not authenticated — every ci: line silently disappears
      → run `gh auth login` (or export GH_TOKEN), or set [ci].enabled = false
  – [sandbox].use_nono = false — task runs are not sandboxed
  ✗ prompts/manager.md is an older packaged default, never edited — this home
    is running last release's policy
      → run `quorum init`: it upgrades unedited seeds in place
  ✓ supervisor running (pid 40680, since 2026-08-31T23:40:50Z)
  – dial concurrent launches: house rule in prompts/manager.local.md (not parsed)
  – dial actions per agent run: manager 20 (default)
  – dial manager cadence: every 5m
```

`✓` is fine, `✗` is a problem, `–` is everything that is neither: something
you switched off, never configured, or that could not answer (a `gh` that
timed out is `–`, not a failure — offline says nothing about whether you are
logged in). The `dial` lines report a current setting rather than check it.
Only `✗` affects the exit code, so a freshly `init`ed home with no harness
chosen yet says so in one `–` line and exits 0. Doctor diagnoses and never
repairs: every `✗` names its fix, and applying it stays your call.

What it looks at: a strict parse of `config.toml`, every `[harness.*]` table
(the binary on PATH, a template that can carry a prompt, `{session}` wherever
a `resume` template promises one, argv that speaks stream-json without
`inject` set), git and each registered project, every optional integration
that is switched on, each `prompts/` file against the packaged default, and
the state earlier runs left behind — a dead supervisor lock, orphaned run
locks, messages claimed and never acked, agents with a failure streak. It ends
with the current value of every
[dial](#loosening-the-rails-as-trust-is-earned).

**`--smoke` is the one thing doctor does actively**, and the only one that
spends tokens: it runs your harness once, in a scratch directory, through the
runner's own code — the same argv building, the same stdin injection, the same
transcript streaming — and asserts that it produced a `result` event and a
session id before a short timeout (`--smoke-timeout`, 60s by default). The
probe gets a scratch working directory *and* a scratch home, so a harness with
quorum's integration hooks installed cannot touch your real tasks. Everything
static can be green while the harness still answers with nothing quorum can
use; that is the outage this command exists for.

### Tasks

```bash
quorum task add my-api "migrate the test suite to pytest" --harness codex
quorum task add my-api - < ~/notes/migration-plan.md    # prompt from stdin
quorum task add my-api --issue 62                       # prompt from an issue
```

The prompt has one home, the positional argument; `-` makes that argument
stdin, read verbatim, which covers a file too. Empty input is an error.

**From an issue.** `--issue` takes a number or a full URL, fetches the issue's
title and body through `gh`, and makes them the prompt. The URL is recorded on
the task, so views show `#62`, `task show` prints the full link, and the run
preamble tells the harness which issue it is working from so it can reference
it in the pull request. A prompt given as well is appended as extra
instructions. A number resolves against the project checkout's own remote; a
full URL is fetched as written, so it can name an issue in another repository.

This needs `gh` on PATH, authenticated, with `[ci].enabled` left on (it is on
by default). Unlike the manager's PR probe, `--issue` fails loudly: a missing
`gh`, an expired login or an unknown issue is an error naming the fix, and
nothing is queued. Quorum only reads from a forge — it never comments on,
labels or closes your issues.

**Worktrees.** Each task gets `~/.quorum/worktrees/<id>`, a git worktree on
branch `quorum/<short-id>` of your project. Abandoning a task is
`git worktree remove` plus `git branch -D`; nothing in your repo moved. Use
`--no-worktree` to run in the project directory itself.

**Running tasks concurrently.** Separate worktrees keep concurrent tasks out
of each other's directories, not out of each other's diffs: two tasks forked
from one commit that both edit a file will open two conflicting PRs. Quorum
handles that as something the manager sees and the harness is told about,
never as a lock. The preamble tells every task to fetch and rebase onto the
base branch before pushing, and the manager's digest marks any two live tasks
on one project whose branches touch the same paths with
`overlaps=<id> paths=N`, so it can nudge them to rebase or let one land
first.

**Delivery.** The preamble teaches plain git — commit everything and
`git push -u origin HEAD` before reporting `done` — with no assumption that
`gh`, `glab` or any forge CLI is installed; opening a PR is a bonus, otherwise
the pushed branch is the deliverable. Quorum verifies: every view flags a task
whose working directory holds uncommitted changes or unpushed commits
(`⚠ 2 uncommitted, 1 unpushed`), and the manager's digest marks a finished
task in that state `STRANDED-WORK`, which the default manager prompt answers
by relaunching it with guidance to commit and push.

**Auto-commit** is the safety net under that protocol, for the harness that
crashes mid-edit and obeys nothing:

```toml
[tasks]
auto_commit = true
```

The runner then commits whatever a run left uncommitted onto the task branch,
so it can be reviewed or reset later instead of vanishing with the worktree.
It never pushes, never touches a `--no-worktree` task, leaves a task alone
once its harness reported `done`, and declines a detached HEAD or a
half-finished merge rather than commit something misleading — the tree then
stays dirty and flagged as stranded. Each rescue, and each failure, is noted
in the transcript and on the run's record. Under `[sandbox].use_nono = true`
the sandboxed runner cannot run git at all after the harness exits, so the net
skips with a note; rely on the stranded-work flag there.

**Finishing and undoing.** A task that opens a PR reports the URL, which shows
up in every view. `quorum task cancel <id>` ends the manager's attention
(`--kill` also stops a live run, and asks first on an interactive shell —
`--yes` skips). The work lives on the `quorum/<short-id>` branch either way.

#### When a run hangs

Harness sessions hang: blocked on stdin, waiting on a provider turn that never
returns, stuck in a tool. The process stays alive and the lock stays fresh, so
nothing looks wrong from outside; the run just stops producing.

```bash
quorum task stop a3f2k9                           # end the RUN, keep the task
quorum task run a3f2k9 --detach                   # resume the same session
quorum task run a3f2k9 --detach --fresh-session   # start a new session instead
```

`task stop` is the non-terminal kill: it signals the run's whole process
group, records the interrupted run, and leaves the task's status, queue
position and working directory exactly as they were. That is the difference
from `task cancel --kill`, which ends the task. It is also the tidy-up for a
run that died without closing itself — a crashed runner, a killed terminal:
the run gets its record, the stale lock goes, and the task is runnable again.

`--fresh-session` is for when resuming is what keeps failing — a session the
provider now errors on every turn. It forgets the stored session id and starts
a new one in the same working directory, so the work on disk survives and so
does the task's notebook, but the new session remembers nothing else: send
guidance first, summarizing whatever the notebook does not already say.

You rarely do this by hand, because the manager does it for you. Its digest
marks a live-but-silent run `STALLED`, and its prompt walks one step per
tick — read the tail, stop and resume, then restart with a fresh session and a
summarizing nudge, then escalate to you rather than restart a third time. The
`stopped=` and `fresh_sessions=` counts on the task's digest line are how it
knows what it already tried.

If you would rather not wait for a tick, set the watchdog:

```toml
[tasks]
run_stall_timeout_seconds = 1800   # 30 minutes of silence ends the run
```

The runner then stops a harness that has printed nothing for that long, marks
the run `stalled` and exits non-zero, which turns a hang into an ordinary dead
run the manager relaunches. It counts *silence, not progress*, so set it well
above the longest quiet stretch a healthy run has — a full test suite, a cold
build, a long provider turn. It is off by default for that reason.

### Perpetual tasks

Some jobs never finish: watch CI and fix what breaks, keep the changelog
current, groom the backlog. Queue those with `--perpetual`:

```bash
quorum task add my-api "watch CI on open PRs; fix what breaks, one at a time" --perpetual
```

Nothing about the machinery changes — it is still a task, still runs in a
worktree, still reports free-form statuses. What changes is how three things
read it. Its prompt gains a block (`prompts/task-perpetual.md`, yours to edit)
telling it to work in **cycles**, commit and push at the end of *each* cycle
rather than "before finishing", report a changing word per cycle (`cycle-4`,
`idle`) so an unchanging one still means something, and never report `done`.
The manager relaunches it whenever its run dies, and its prompt tells it never
to read a long run count or a cycling status as stuck, and never to cancel it.
And quorum withholds the `possible-loop` observation for it, since repetition
is the job. Every view badges it `∞`.

You end it, with `quorum task cancel <id>`. Three things to expect:

- **it reuses one working directory and one session forever**, so the
  harness's context grows every cycle. When that starts to bite, clear
  `"session"` in `~/.quorum/tasks/<id>/task.json` and the next run starts
  fresh in the same directory, keeping the work;
- **the manager's schedule is the floor on cycle latency**, since nothing else
  relaunches it — tighten the schedule if you need a tighter loop;
- **it keeps the manager awake**, because a home with a perpetual task is
  never idle, so expect one manager run per tick for as long as it lives.

### Dependencies and handoffs

Some work only makes sense once other work has landed. `--after` says so:

```bash
quorum task add my-api "add rate limiting, open a PR"
# → queued task a3f2k9 on my-api

quorum task add my-api "review the rate-limiting PR and fix what you find" \
    --after a3f2k9
# → queued task b7c1x4 on my-api
#   waits on: a3f2k9 — `task run` refuses until they finish (--force overrides)
```

`--after` is repeatable and takes the same short ids as everything else; ids
are global, so a task in one project may wait on a task in another. An unknown
id fails the command — nothing is queued — and so does `--after` a
[perpetual task](#perpetual-tasks), which never finishes.

While a dependency has not reached a terminal status, the dependent shows
`waiting-on a3f2k9` in every view, the manager's digest marks the same thing
and its prompt tells it not to launch such a task, and `quorum task run`
refuses it outright (`--force` if you disagree). Once every dependency is
`done` it is an ordinary queued task the manager picks up on its next tick.

This is not a workflow engine. Nothing schedules on dependencies, nothing runs
the instant an upstream finishes, and quorum never cancels or reorders
anything for you.

**Only a dependency that still might finish holds a task back.** One that
ended `blocked` or `cancelled`, or whose record was deleted, can never be
satisfied — so quorum stops calling the dependent "waiting" and flags
`DEP-FAILED` / `DEP-MISSING` instead, in the digest and in every view. The
task becomes runnable and the decision is a visible one: nudge the dependency,
launch the dependent anyway if its premise still holds, or cancel it. A
dependency that silently blocked forever would have hidden exactly that
choice.

**How the dependent reads the upstream's result.** Its prompt gains a block
naming each dependency with its status and PR url:

```
# Tasks this one depends on

- a3f2k9: status=done pr=https://github.com/you/my-api/pull/42
  it was asked to: add rate limiting, open a PR
```

`quorum task show <id>` is the rest of the channel: reports, branch, runs,
spend. The harness has the CLI and `QUORUM_HOME`, so nothing else is needed.

**The handoff** is what a finishing task says beyond status and PR url — what
it changed, what it left undone, what to check first:

```bash
cat <<'EOF' | quorum task report a3f2k9 --status done --handoff - "PR #42"
Added a token-bucket limiter in `api/limits.py`, wired into the public
router only. Not done: the admin endpoints still have no limit, and the
config knob is hard-coded to 100/min. Check first: the new tests in
`tests/test_limits.py` mock the clock — if they flake, that is why.
EOF
```

The body is stored whole as `tasks/a3f2k9/handoff.md`. One file per task: a
later `--handoff` replaces it, because it describes the finished state rather
than logging progress. Every task that waits on `a3f2k9` finds it in its
prompt under a `## Handoff from a3f2k9` heading, cut at 8 KiB per dependency
with a note saying how much was dropped. `quorum task show` prints it in full;
the manager's digest shows only `handoff=true`, because the body is for the
dependent, not the manager.

The preamble asks for one: a task whose `task show` output has a `dependents:`
line is told to leave a handoff with its `done` report, naming what changed,
what is not done and what to look at first. Quorum never writes or summarizes
one on a task's behalf.

The common recipe is a review task queued behind an implementation task
("review the PR opened by the task you depend on: read its diff with
`gh pr diff`, fix what you find on its branch, and report done"). The reviewer
cannot start before the PR exists, so it never spends a run reviewing nothing.

### Notebooks

A session is not durable: models compact their own context, a resumed session
can be days old, and a fresh session starts with nothing but the working
directory. A **notebook** is what survives that. The manager and every task
have one, on the same substrate, with the same verbs.

```bash
quorum task remember a3f2k9 "parser done and committed; tests not started"
quorum task remember a3f2k9 "the flaky test is quarantined" --ttl 2
quorum task show a3f2k9                     # the notebook, as a run sees it
quorum task forget a3f2k9 k7f2ab            # retire one (id from `remember`)

quorum manager remember "codex is rate-limited today" --ttl 2
quorum manager notes                        # what it currently remembers
quorum manager forget k7f2ab
```

Every run quorum starts for a task renders that task's notebook into the
prompt, resumed or fresh. The manager's notebook is the first thing in every
digest. The preamble and the manager prompt both say what a notebook is for:
state, not a log — what is done, what is left, what was tried and failed.

Each notebook has a byte budget in the prompt it is rendered into, and nothing
else spends that budget, so a busy home cannot crowd it out. When it overflows
the prompt keeps the newest notes and says how many older ones it dropped, and
the prompts tell the reader to consolidate — write one note that supersedes
several, then forget the rest. Nothing in quorum summarizes a notebook.

Who may write: a task's notebook takes writes from that task's own harness,
from the manager (a standing instruction for the task's next run, where
guidance is read once and gone) and from you; the manager's takes writes from
the manager and from you. Another task or a prompt agent is refused and
pointed at `task nudge`, so no amount of task chatter crowds your notes out.
That refusal is a convention against accidental crowding, not a security
boundary — it reads an environment variable a determined harness could set.
The sandbox is what actually confines a run.

Two things worth knowing. The manager never sees a task's notebook: it reads
the task's reports, and the notebook is the task's own. And an
[attached task](#adopting-a-live-session) is the exception to the reading
half — quorum does not compose the prompt for a session it did not start, so
its notebook is written but never rendered into the session. `task show`
prints it, and you can send what matters with `task nudge`.

### What runs cost

Most harnesses report their token and cost usage when a turn finishes
(claude's `result` event, codex's `turn.completed`), and quorum records
whatever it sees on the run's record. It then shows up wherever tasks do —
`$0.42 · 11.0k tok` in the `usage` column of `quorum status` and `task list`,
broken out by `task show`, summed per task in the digest. A harness that
reports nothing is fully supported: you see nothing, never a misleading
`$0.00`.

**Quorum prices nothing.** The `$` figure is the harness CLI's own reported
cost, copied as-is. codex reports tokens only, so its rows show tokens and no
`$`. For a subscription (OAuth) claude session the number is the CLI's
notional API-rate cost, not what you were billed — read it as relative spend,
never as an invoice.

**The budget.** Set `max_cost_per_run` or `max_tokens_per_run` under `[tasks]`
and a run that reported more gets marked (`$!` in the views,
`BUDGET-EXCEEDED` in the digest). The budget also gates the *next* run: while
a task's last run is over budget, `quorum task run` refuses it (`$! GATED` in
`task list` and the TUI) until you pass `--force` or a run comes in under — a
run that reports no usage counts as under, since silence is not spend. Quorum
never kills a run in progress: a run past its budget finishes, and only the
relaunch is held. The digest tells the manager the gate is on, and its prompt
tells it to sharpen its guidance, split the task, or escalate to you before
reaching for `--force`. With no budget set (the default) nothing is ever
gated.

**Aggregates.** `quorum status` shows spend one task or one agent at a time.
The questions you ask after a week are aggregate — what did this project cost,
is one harness cheaper per merged PR, how long from queue to merge — and
`quorum usage` answers them off the same files:

```
$ quorum usage --by harness --since 7d
usage by harness, tasks queued since 2026-08-27T09:00:00Z (7d)
harness       tasks  reported  runs  reruns    cost  tokens  done     merged  queue→run  queue→done  done→merged
claude            4       3/4     5       1  $11.31   15.8M     4  2/3 (67%)      2h25m       2h50m          35m
codex             3            3               6.2M     2  1/2 (50%)        40m       3h10m        1d02h
total             7       3/7     8       1  $11.31   22.0M     6  3/5 (60%)      1h38m       2h52m          40m
```

- `--by project` (the default), `harness`, `week` (ISO week) or `agent` (the
  manager and every prompt agent, off their usage logs: runs, how many raised
  or timed out, spend, median run time). `--since 7d` / `36h` / `2w` keeps the
  tasks queued in that window — a task belongs to the moment it was queued, so
  a window is a set of tasks, never runs sliced mid-task. `--json` gives every
  figure unrounded.
- The `reported` column says how many of the row's tasks the `$` figure
  covers, and is shown only when that differs from `tasks`: a row mixing
  claude with codex has fewer tasks behind its `$` than behind its tokens.
- The delivery columns come only from what was recorded, each as a median:
  `queue→run`, `queue→done`, and `done→merged` from the `done` report to the
  tick that first saw the PR merged, so it is late by up to one tick and never
  early. `merged` is measured over the PRs the manager *observed* in any
  state, not over every done task, because no observation is not "not merged".
  Columns nothing fills are dropped.
- It is a pure reader with no cache and no network, so it works with the
  supervisor stopped. An archived task leaves the figures the moment
  `task prune` moves it.

### The manager

Supervision in quorum is not a set of thresholds — it is your harness reading
the situation and deciding. On its schedule (default every five minutes, and
only when there is something to manage) the manager compiles a digest:

Its own notebook comes first, then every active task — status, whether its
run is alive, how long it has been quiet, its recent reports and the tail of
its output — then its own recent actions with their observed outcomes ("you
nudged a3f2k9 at 14:02; status UNCHANGED since", recorded by quorum as the
commands ran, so it never loops on an intervention that is not working), what
its own runs have cost, and your guidance from `quorum manager tell`.

A task's line carries whichever of these marks apply:

| mark | means |
| --- | --- |
| `possible-loop` | this run's output is dominated by one repeated tool call — the kind of stuck that looks busy from outside. Only a harness that streams JSON events can be read this way, so its absence means nothing for a plain-text one |
| `overlaps=<id> paths=N` | two live tasks on one project have changed the same files, with an `overlap:` line naming up to three. Attached and `--no-worktree` tasks are never compared: that checkout is yours |
| `ci:` | the pull request behind the branch — state, check counts, failing check names, `MERGE-CONFLICT` when it no longer merges. Needs an authenticated `gh`; without one the line is simply absent. `[ci].enabled = false` turns the probe off |
| `CI-FAILING` | a task reported `done` over red checks. A merged PR never earns it, because merged is delivered |
| `STRANDED-WORK` | a finished task never delivered its work |
| `STALLED` | a live run has printed nothing for a long time; `stopped=` and `fresh_sessions=` say what has already been tried |
| `BUDGET-EXCEEDED` | a run went past a budget you set, `(next run gated)` when it was the last one |
| `perpetual=true`, `waiting-on=`, `DEP-FAILED`, `DEP-MISSING`, `handoff=true` | the marks the sections above describe |

Every one of those is an observation. Nothing in quorum acts on one — a task
ends at the harness's word, and no status is ever changed because a PR merged.
What the tick *does* record is the PR state it saw, so every view can badge it
without touching the network: `✔` merged, `⊘` closed unmerged, spelled out
with its timestamp by `task show`. No badge means nothing was ever observed —
no PR yet, no `gh`, `[ci]` off, or a supervisor that was never up while the PR
was open — and never "not merged".

The manager then runs your harness over that digest with `prompts/manager.md`,
and that prompt file *is* the supervision policy. Edit it to change how your
manager behaves: how patient it is, when it escalates, how it words its
guidance. Delete it to restore the default. For a few lines of house policy prefer the
overlay ([Prompts and overlays](#prompts-and-overlays)).

The manager acts through the same CLI you use — launching tasks, nudging them,
cancelling them, queueing follow-up work — and every action is journaled:

```bash
quorum manager tell "prioritize the api task; park the docs work"
quorum manager journal                    # everything it has done, and why
quorum agent log manager                  # one tick end to end
quorum manager notes                      # its notebook
```

Guidance is normally read at the start of the next tick; if the manager's
harness sets `inject = "stream-json"`, guidance sent while a tick's run is in
flight is delivered into that run instead of waiting.

Two things bound a bad run, neither of which second-guesses a decision: a
per-run action cap (`max_actions_per_run`, default 20) and your own eyes on
the journal. The cap is not silent — when a run reaches it the refusal is
journaled as `cap.hit`, so the *next* run sees that the last one ran out of
budget, and the default prompt tells it to escalate rather than try a third
time.

**When the model service is down, supervision halts loudly and then recovers
on its own.** There is no reduced-capability fallback: the manager's tick fails (visibly, in
`quorum status` and on the board) but its schedule keeps firing
(`auto_pause = false`), so the first tick after service returns reads the
world from files and relaunches whatever died. You do not have to do anything.

You do get told. Individual failures go to the `system` topic, which nothing
nags you about — but after five consecutive failed ticks the supervisor posts
`agent.failing` to `attention`, which is the banner. An agent that is never
paused would otherwise fail all night in a channel nobody watches. It is one
post per outage, not per tick, and when the manager ticks again a matching
`agent.recovered` lands on `system`, where it does not add to the banner.

### Loosening the rails as trust is earned

Quorum's constraints are of two kinds, and the difference matters when a
better model arrives. Some encode distrust of the *environment*: rate limits,
money, a laptop with no root and no open ports, a signal that must never be
dropped silently. Those do not change with model capability, and they are
listed under "What does not move" below. The rest are dials. Each one records
how far you currently trust the model, and each should loosen as that trust is
earned. A dial left at its cautious default forever costs throughput for
nothing; an invariant loosened by mistake costs the property it protected.
`quorum doctor` prints the current value of each dial as a `–` line, so a
home's posture is visible in the same place as its health.

| dial | lives in | default | move it when |
| --- | --- | --- | --- |
| concurrent launches | `prompts/manager.local.md` house rule | none | rate-limit headroom, `overlaps=` rare |
| actions per agent run (`max_actions_per_run`) | `[agents.<name>.settings]` | 20 | `cap.hit` on legitimate work |
| seconds per agent run (`run_timeout_seconds`) | `[agents.<name>.settings]` | 300 | `TIMEOUT` on runs that progressed |
| per-run budget (`max_cost_per_run` / `max_tokens_per_run`) | `[tasks]` | 0 (off) | set on a metered account; before #43 (not built) |
| stall watchdog (`run_stall_timeout_seconds`) | `[tasks]` | 0 (off) | a healthy run is silent for longer |
| manager cadence | `[agents.manager]` `schedule` | `every 5m` (dogfood: `every 1h`) | events carry the facts (#83, not built) |
| who launches | `prompts/manager.md` | the manager | tasks self-schedule (#83, not built) |
| who decomposes | a person | a person | a spawn cap exists (#43, not built) |
| merge gate | a person | a person | never removed; may move later |

Four of the rows need a word more than the table has room for. Quorum counts
no **concurrent launches** at all — the cap is a house rule you write into the
manager's overlay, and the dogfood home says two. The **stall watchdog**
measures the harness rather than your trust in it, since it counts silence and
not progress, so it belongs above the longest quiet step a healthy run has.
**Manager cadence** is spend: every tick is one harness run. And the last three
rows are not settings at all — who launches is a sentence in
`prompts/manager.md`, and who decomposes and who merges are conventions a
person keeps, the merge gate permanently so, because quorum has no forge write
path.

**What does not move.** These are the constraints the dials sit inside. They
encode the environment, not the model, and a more capable model does not
change what a laptop, a rate limit or a lost message is.

- **No privileged infrastructure.** One ordinary process hosts the scheduler,
  runs are detached child processes, and there is no cron, systemd, root or
  open port.
- **All state is plain files** under the home. No database, so `ls` and `cat`
  remain the debugger and copying the directory remains the migration.
- **Fail loudly, recover automatically.** Views degrade to reading files.
  Supervision does not degrade at all: without a working harness the manager's
  tick raises every time and keeps firing until the service returns.
- **No decisions in Python.** Every launch, nudge, cancel and escalation is
  the harness reading a digest and typing a command. A threshold in the code
  is rendered into the digest, never acted on by the code.
- **Observations are never rails.** The only rails are rate limits (the action
  cap, the budget gate) and the substrate refusals that protect a checkout,
  and none of them second-guesses a decision.
- **A dropped signal is a bug.** A report, a piece of guidance or a board
  message that goes missing is a defect, not acceptable degradation. The
  notification hook is the one deliberate exception: it loses a message on a
  crash rather than repeat it every fifteen seconds, and logs that it did.

These two lists are where a change to either is argued; the design record
([architecture.md](architecture.md)) and the contributor notes (`CLAUDE.md`)
both point here. To loosen a dial, change the value where the table says it
lives. To move an invariant, open an issue: that is a design change, and it
updates `CLAUDE.md` and `docs/architecture.md` in the same commit.

### Prompts and overlays

Every prompt quorum uses is a file in `~/.quorum/prompts/`: the manager's
policy (`manager.md`), the task preamble (`task-preamble.md`), the perpetual
block (`task-perpetual.md`), and one per prompt agent. `quorum init` seeds
them, and deleting one restores the packaged default. Re-run `quorum init`
after upgrading quorum: a prompt you never edited is refreshed to the new
packaged default (init keeps a record of what it seeded, so an untouched copy
is recognized even after the default moves on), and one you did edit is left
alone.

Four layers resolve in this order — each optional, each added on top of the
last:

1. the **packaged default** quorum ships;
2. the **home copy** `prompts/<name>.md`, which replaces it outright;
3. the **home overlay** `prompts/<name>.local.md`, merged at the template's
   `{local}` slot;
4. for the task preamble only, the **project overlay** at its `{project}`
   slot: the project's registry notes, then `.quorum/task-preamble.local.md`
   inside the project directory.

The difference between layers 2 and 3 is the one that matters:

- **An overlay is yours alone**: never seeded, never read by `quorum init`,
  never upgraded. Its text is merged at the `{local}` slot — the packaged
  `manager.md` puts that slot right before "How to work", so house rules land
  above the general guidance; `task-preamble.md` puts it after the delivery
  protocol. A template with no slot, one you rewrote yourself, gets the
  overlay prepended instead. An absent, empty or unreadable overlay renders as
  nothing, and a bad one never breaks a run — `quorum prompt list` flags it.
- **Editing `<name>.md` still wins outright**, but an edited file is *yours*
  from then on: `quorum init` will never upgrade it, so every later
  improvement to the packaged default stops reaching this home. Init says so,
  and `quorum prompt diff <name>` shows what you are missing.

House rules ("run one task at a time", "always open draft PRs") belong in an
overlay. Rewriting how supervision fundamentally works belongs in the file.

```bash
quorum prompt list                # each template: default, seeded, or edited (+ overlay)
quorum prompt diff manager        # your copy vs the packaged default
```

**Migrating a home that already edited a prompt** — one step, and worth doing,
because an edited `manager.md` from a few releases ago has no policy for
whatever the digest has learned to report since:

```bash
quorum prompt diff manager                      # see what the upgrade brings
$EDITOR ~/.quorum/prompts/manager.local.md      # paste ONLY your own lines here
rm ~/.quorum/prompts/manager.md && quorum init  # take the current default back
```

After that, `quorum init` keeps `manager.md` current and your
`manager.local.md` is merged into every future version of it.

**Per-project conventions.** A home overlay is the wrong scope for "base
branches on `develop`" or "run `just check` before pushing" — true of one
repo, wrong for the next. The project overlay fills the preamble's `{project}`
slot from two places:

```bash
quorum project set api --notes "Base branches on develop; run just check."
quorum project set api --notes-file CONVENTIONS.md    # or from a file ('-' for stdin)
$EDITOR ~/work/api/.quorum/task-preamble.local.md     # ...or from inside the repo
```

Registry notes come first, then the project's own file, so short metadata can
stay in quorum while longer conventions live with the code and travel with the
repo. Quorum only *reads* that file. Neither source is required: with nothing
to say, the slot leaves no trace in the prompt. A file quorum cannot read is
dropped rather than failing the run, exactly like a home overlay. And the
block needs the `{project}` slot: if you rewrote `task-preamble.md` before
this existed, it has nowhere to go. `quorum prompt list` reports both — it
names every project that contributes an overlay and warns when your preamble
has no slot for them:

```
  task-preamble    seeded, matches the packaged default
  per-project overlay in task-preamble ({project} slot):
    api            notes (registry) + .quorum/task-preamble.local.md
```

### Adopting a live session

Sometimes the work is already underway — you are deep in a problem inside an
interactive coding session and want quorum's manager watching over it. Adopt
the session instead of re-queuing the work:

```bash
quorum task adopt "refactoring the auth flow"    # from the session's directory
```

Or do it from inside the session itself, with the shipped adapter for your
harness. Install one with `quorum integration install <harness>` (codex and
opencode; `claude-code` goes through Claude's plugin manager, and the command
prints the exact invocation). `quorum integration list` shows what is bundled
and what is installed; each `integrations/<harness>/README.md` has the details
and the per-project variants.

- **Claude Code** ([integrations/claude-code/](../integrations/claude-code/README.md)):
  `/quorum:adopt <desc>`, plus Stop and SessionEnd hooks.
- **Codex CLI** ([integrations/codex/](../integrations/codex/README.md)):
  `/prompts:quorum-adopt <desc>`, plus SessionStart/Stop/SessionEnd hooks —
  Codex speaks the same hook protocol as Claude Code. Adoption starts id-less
  (Codex prompts cannot see their own session id); the next hook firing learns
  it by directory match.
- **opencode** ([integrations/opencode/](../integrations/opencode/README.md)):
  `/quorum-adopt <desc>`, backed by a plugin that watches idle events and
  injects guidance as a user turn.

Adoption creates an **attached task** (`⚭` in every view): its working
directory is your own checkout, quorum never starts runs for it (`task run`
refuses, by design), and the manager treats it as human-driven — observing its
git state and reports, nudging rather than relaunching, escalating to
`attention` if it looks abandoned. Guidance sent with `task nudge` is
delivered *inside* the session by the adapter the next time the agent stops or
goes idle, as an instruction to continue; the session can also call
`quorum task report` like any harness. If the directory was not a registered
project yet, adoption registers it. When the interactive phase is over,
`quorum task detach <id>` turns it back into an ordinary task the manager may
run headless — a captured session id lets a `resume` template continue the
same conversation.

**herdr (optional).** If you run interactive sessions inside
[herdr](https://herdr.dev) — a terminal multiplexer that detects coding agents
in its panes — tell quorum which pane hosts the session:

```bash
quorum task adopt "port the parser" --herdr-pane w1:p2
```

Two things light up, both fail-soft (herdr stopped or absent changes nothing):
the digest shows the pane's live agent status
(`herdr: state=working|blocked|idle`), and every `task nudge` also rings a
doorbell in the pane telling the session guidance is waiting — which is how
guidance reaches sessions with no quorum adapter installed. The guidance
itself always stays in the task's inbox; the session collects it with
`quorum task inbox <id> --claim`. An optional `[herdr]` table in config.toml
overrides the socket path (`socket = "..."`) or disables the integration
(`enabled = false`).

### Agents

The manager is one instance of a general shape: a prompt, a schedule, and a
harness. You can mint more of them — a standup summarizer, a nightly triage
bot, a docs gardener — without writing Python:

```bash
quorum agent create standup --schedule "every 1d" \
  "Read the board with \`quorum board read\`, then post a short standup
summary with \`quorum board post notes ...\`."
```

The prompt body is the second argument, or `-` to read it from stdin — the
same grammar `task add` uses. This writes two plain files:
`agents/standup.toml` (schedule, type, settings; hand-editable, and the one
config location quorum itself may write) and `prompts/standup.md`, the prompt,
which you can edit at any time. It also pokes a running supervisor, which
schedules the agent within seconds. No restart, and `config.toml` is never
touched.

Each tick a prompt agent renders its prompt and runs your harness over it,
with the same authority and the same rails as the manager: every mutating
`quorum` command it issues is journaled to
`state/agents/<name>/journal.jsonl` and capped per run
(`max_actions_per_run`, default 20). Send it guidance through its own inbox —
it appears in its `{directives}` placeholder. Useful settings in
`agents/<name>.toml`:

```toml
type = "prompt"
schedule = "every 1d"

[settings]
harness = "claude"            # defaults to [tasks].default_harness
prompt = "standup"            # template name, defaults to the agent's name
run_timeout_seconds = 300
max_actions_per_run = 10      # tighten the rail below the default of 20
```

After editing, `quorum agent reload standup` applies the change to a running
supervisor; `quorum agent remove standup` unschedules it (the prompt and state
files stay). There is no wake condition: a scheduled prompt agent spends a
harness run every tick, so give an expensive agent a sparse schedule and put
any "do nothing unless…" logic in the prompt itself.

#### Controlling agents at runtime

While `quorum up` is running you can steer its schedule without editing config
or restarting:

```bash
quorum agent run-now manager      # ask the running supervisor to tick it now
quorum agent pause manager        # stop scheduling it
quorum agent resume manager       # resume (also clears the failure streak)
quorum agent run-once manager     # one tick in *this* shell, supervisor optional
```

`run-now` and `run-once` are two mechanisms, not two spellings: `run-now` is a
message to a running supervisor and returns before the tick does; `run-once`
builds the agent in your shell and runs the tick in front of you, which is
what to reach for with the supervisor stopped or when you want to watch it
fail. Control messages ride the supervisor's own inbox and are applied within
about fifteen seconds.

An agent that fails five ticks in a row is auto-paused and announced on the
`system` topic — unless its config sets `auto_pause = false`, as the manager's
does, in which case it keeps retrying (loud failures, automatic recovery) and
its streak is escalated once to `attention` instead of being paused. Either
lever ends a streak: a successful `run-once` and an `agent resume` both clear
the failure counter and the escalation, so the *next* outage escalates afresh.
A pause survives supervisor restarts.

#### The shipped example: a CI babysitter

`quorum init` seeds one worked example, `prompts/babysitter.md`. It does
nothing until you create an agent over it:

```bash
quorum agent create babysitter --schedule "every 10m" --harness claude
```

No prompt text is needed, because the template already exists;
`quorum agent create ci-cop --prompt babysitter` runs the same prompt under a
different agent name.

Each tick it lists your tasks, asks `gh` about the pull request behind each
one, and — for a red PR whose task is **idle** — reads the failing job's log,
nudges the task with the specific failure, and relaunches it. After two failed
relaunches on the same PR it stops and posts to `attention` for you, because
unrescuable work belongs to a person. It needs `gh` authenticated and a
harness that can run it.

All of that is prompt text, so it is yours to retune: change the two-strike
rule, have it comment on the PR instead of relaunching, restrict it to one
project, or make it queue follow-up work with `quorum task add`. It runs under
the ordinary prompt-agent rails — every action journaled, capped per run.

#### Writing your own agents

Anything you want on a schedule that a prompt cannot express — a SLURM queue
poller, a deadline reminder — is a plugin: a class with a synchronous
`tick()`, dropped into `~/.quorum/plugins/`, no packaging.

A complete, tested example ships in the repo:
[examples/steward.py](../examples/steward.py), a rule-based file organizer
with undo and bounded retries. Copy it into `~/.quorum/plugins/` and add:

```toml
[agents.steward]
type = "steward:Steward"
schedule = "every 1h"
[agents.steward.settings]
watch = ["~/Downloads"]
apply = false                 # propose on the board first; true moves files
rules = [{ match = "*.pdf", dest = "~/papers/inbox" }]
```

A minimal agent from scratch (`~/.quorum/plugins/wordcount.py`):

```python
from pathlib import Path

from quorum.agent import Agent


class WordCount(Agent):
    """Posts a note whenever a watched manuscript grows past a milestone."""

    def tick(self):
        manuscript = Path(self.ctx.settings.get("file", "")).expanduser()
        if not manuscript.is_file():
            return
        words = len(manuscript.read_text(errors="ignore").split())
        state = self.ctx.load_state()
        last = state.get("last_milestone", 0)
        milestone = (words // 1000) * 1000
        if milestone > last:
            self.ctx.bus.post(
                self.name, "writing", "milestone",
                text=f"{manuscript.name} passed {milestone} words ({words} now)",
            )
            state["last_milestone"] = milestone
            self.ctx.save_state(state)
```

Register it with an `[agents.wordcount]` table like the steward's, giving
`type = "wordcount:WordCount"` and a `schedule`, then test it immediately with
`quorum agent run-once wordcount`.

**The contract:**

- `tick()` must be **idempotent** — it can be re-run at any time (missed
  schedules coalesce, `run-once` exists, crashes get retried). Use
  `load_state()` / `save_state()` to remember what you already did.
- **Raising is fine**: the supervisor logs the traceback, marks your heartbeat
  `error`, posts to the `system` topic, and auto-pauses after five consecutive
  failures. You cannot take down other agents.
- Use `self.ctx.now()`, never `datetime.now()` — it makes your agent testable
  with a fake clock.

**What the context gives you:**

| member | purpose |
|---|---|
| `ctx.settings` | your `[agents.<name>.settings]` table, verbatim |
| `ctx.bus.post(sender, topic, type=, text=, payload=)` | post to the board |
| `ctx.bus.send(sender, to, ...)` | send to an inbox (e.g. a task's) |
| `ctx.bus.claim(name)` | consume your own inbox (call `.ack()` per message) |
| `ctx.bus.read_after_cursor(topic, cursor)` | follow a board topic incrementally |
| `ctx.projects.list()` / `.get(slug)` | registered projects, marker-merged |
| `ctx.prompt(name, **placeholders)` | render a template from `prompts/` |
| `ctx.load_state()` / `ctx.save_state(d)` | your private JSON state |
| `ctx.log_action(type, text, **data)` | feed the views' activity log |
| `ctx.now()` | injectable clock |

There is no separate small-completion client: a plugin agent that wants a
model call runs a harness, the same way the manager does. Give the agent a
`harness` setting naming one of your `[harness.*]` tables and call
`quorum.agents.harness_run.run_agent_harness(self.ctx, prompt)`; the run is
synchronous, its output streams to `state/agents/<name>/transcript.jsonl`, and
the per-run action cap applies as it does to any other agent run.

To test one, build an `AgentContext` over a scaffolded home, call `tick()`,
and assert on what it wrote — `tests/test_example_steward.py` is the full
pattern.

### Cleaning up

Quorum accumulates on purpose — a finished task keeps its record, its working
directory and its branch — but a long-lived home fills up with `done` rows. Nothing here deletes a record: a pruned task moves to
`~/.quorum/tasks/.archive/<id>/`, and cleared messages join the same
`messages/archive/YYYY-MM.jsonl.gz` the supervisor's janitor writes. The one
thing that *is* destroyed, and only when you ask for it, is a branch.

```bash
quorum task prune --dry-run              # what would go (always start here)
quorum task prune                        # archive done/blocked/cancelled tasks
quorum task prune --older-than 7d        # ...only those untouched for a week
quorum task prune --status done          # ...only the ones that finished well
quorum task prune --worktrees            # also remove worktrees + merged branches
```

Getting a task back is one move in the other direction:

```bash
mv ~/.quorum/tasks/.archive/01J5R3…  ~/.quorum/tasks/
```

**What prune refuses.** It skips a task, and says why, when a run still holds
its lock, when it is attached to a live session, when another task still lists
it under `--after`, and — the one that catches people — when its working
directory holds stranded work. Archiving the record would be the only thing
that hid that, so commit and push, or pass `--force`. A `--no-worktree` task
is exempt: it ran in your own checkout, and what is uncommitted there is
yours.

**Worktrees and branches.** `--worktrees` runs `git worktree remove` and then
deletes the task branch *only if git agrees it is merged*; an unmerged branch
is kept, with a note naming the `git branch -D` to run. `--force` is never
passed to `git worktree remove` — a worktree holding uncommitted files is left
exactly as it is, and its task stays unarchived. What `--force` does do to git
is run `git branch -D` instead of `-d`, so **an unmerged branch's commits are
lost**. That is the one destructive thing in this section;
`--worktrees --dry-run` names every worktree and branch first.

**The board.** Escalations sit in the banner for seven days because the board
has no read-state. When you have dealt with them:

```bash
quorum board ack 7c1af2                  # just this one
quorum board clear attention             # archive the topic, empty the banner
quorum board clear tasks --before 30d    # or just the old part of one
```

And guidance queued for a task you have changed your mind about:

```bash
quorum task inbox a3f2k9 --clear         # archive what is waiting, undelivered
```

Both take `--dry-run`, and `--clear` only touches unclaimed messages — one a
run is already holding is left alone.

### Exporting a task

To hand one task to someone — a colleague, a bug report, an issue comment —
pack it into one archive:

```bash
quorum task export a3f2k9                        # ./quorum-task-a3f2k9.tar.gz
quorum task export a3f2k9 --out ~/Desktop/run.tgz
quorum task export a3f2k9 --with-worktree-diff   # + a patch of the worktree
quorum task export a3f2k9 --redact               # drop what the tools returned
```

The archive unpacks to `quorum-task-<short-id>/` holding the task record, its
reports, the transcript, the runner log, and the guidance it received: what is
still waiting, what a run is holding, and what was already delivered, read
back out of the message archive. An `export.json` says which task, when, and
which options were on.

**What it never contains** is anything from your project directory. The only
code in an export is `worktree.diff`, and only with `--with-worktree-diff`:
the task's own worktree against the branch it forked from, uncommitted and
untracked files included. A task that ran in your checkout — `--no-worktree`,
or one you adopted — is refused the diff outright rather than exporting your
checkout. Apart from the archive itself, which is refused inside `~/.quorum`
and over an existing file, the command writes nothing.

**`--redact`.** Transcripts carry what the tools *returned* — file contents,
command output, whatever a `cat` of the wrong file showed the model.
`--redact` replaces every tool result with a marker and keeps the rest: the
assistant's text, its reasoning, and each tool call with its arguments, so a
reader can still follow what the run did. The transcript on disk is untouched.
It understands the structured transcripts claude and codex emit; a harness
that prints prose has nothing to redact, and the command tells you how many
plain-text lines it kept verbatim, so read those before you share them.

### Getting notified

Everything above shows you an escalation when you look. The `[notify]` table
is how one reaches you when you are not looking: an argv template quorum runs
once for every new message on the listed board topics — by default just
`attention`, the manager's ask-a-human channel, which is also where the
supervisor reports an agent that keeps failing.

```toml
[notify]
command = ["terminal-notifier", "-title", "quorum", "-message", "{text}"]
topics = ["attention"]        # board topics that fire it (default: attention)
timeout_seconds = 10
```

Some other shapes, one per line of `command`:

```toml
# a phone, via ntfy.sh (or your own ntfy server)
command = ["ntfy", "publish", "--title", "quorum: {from}", "my-quorum-topic", "{text}"]

# a Slack incoming webhook (curl, no shell — every element is one argv)
command = ["curl", "-sf", "-X", "POST", "-H", "Content-Type: application/json",
           "-d", "{\"text\": \"{from}: {text}\"}", "https://hooks.slack.com/services/…"]

# anything at all: a script of yours gets the text as its last argument
command = ["/Users/you/bin/notify-me"]
```

`{text}`, `{from}`, `{topic}`, `{type}` and `{id}` are substituted inside each
argument, exactly like `{prompt}` in a harness template — there is no shell,
so a message containing quotes, spaces or `$` is still one argument. A
template with no `{text}` gets the text appended as the final argument.
Substitution is plain text replacement, though, so if an argument is itself
structured — the JSON body above — a message containing a `"` or a backslash
makes it malformed; prefer a small script of your own when the payload has to
be escaped.

Prove the wiring before an escalation does. `quorum notify test "hello"` runs
the template once, right now, and exits 1 with the reason if it could not
(binary not on PATH, nonzero exit, timeout); `quorum doctor` also reports the
table.

The supervisor delivers every fifteen seconds and once at startup, sending
whatever landed on the listed topics since the last one it delivered, oldest
first, and keeping its place in `state/notify.json`. So an escalation posted
while the supervisor was stopped still goes out when it comes back, and
nothing is ever sent twice: it writes its place down *before* running your
command, so the one thing it will do under a crash or a full disk is skip a
notification, never repeat one. Turning the hook on starts from *now* — it
does not replay old messages the banner already showed. Delivery fails soft: a
command that is missing, exits nonzero or hangs past `timeout_seconds` is one
line in `logs/supervisor.log`, and the next message is still delivered.

What is worth escalating stays in `prompts/manager.md`, because the hook fires
on the *topic*, not on what the message says; list `system` or `tasks` too if
you want the firehose.

### Sandboxing

Quorum pairs with [nono](https://github.com/nolabs-ai/nono), which confines
processes with OS security primitives (Landlock on Linux ≥ 5.13, Seatbelt on
macOS). Durable state is one tree, messaging is pure file I/O, and each task's
writes belong in its worktree, so least-privilege profiles stay short. Three
modes, all fail-closed: if sandboxing was requested and nono-py is missing or
unsupported, the run does not happen unsandboxed — it fails loudly.

**Mode 1 — wrap the world**, zero code:

```bash
nono profile init quorum
nono run --profile quorum -- quorum up
```

A profile granting what a task-running quorum needs:

```json
{
  "fs_write": ["~/.quorum", "~/work/my-api/.git", "~/.claude"],
  "fs_read":  ["~/work"],
  "network":  ["api.anthropic.com"]
}
```

`fs_write` covers the home (worktrees live inside it), each project's `.git`
(a worktree shares the main repo's object store, so commits write there), and
your harness's own state directory; `fs_read` is your project directories; and
`network` is whatever your harness needs. The same wrapping works per command:
`nono run --profile quorum -- quorum task run <id>`.

**Mode 2 — self-sandbox the supervisor**, with the `nono` extra installed
(`uv tool install 'quorum-orchestrator[nono]'`):

```bash
quorum up --self-sandbox
```

Before the scheduler starts, quorum builds a capability set from your resolved
config and applies it via nono-py — irreversibly, children included. This
sandboxes the supervisor and therefore the manager's harness too, and it
**blocks the network** unless a profile file grants it, so run the manager
under mode 2 only with a `profile_file` whose `network` list is non-empty.
Blocking by default is what keeps the mode fail-closed.

**Mode 3 — sandbox each task run**:

```toml
[sandbox]
use_nono = true
task_write = ["~/.claude"]    # your harness's own state dir
task_read  = []               # any extra read-only grants
```

Each run then applies a per-task kernel sandbox before invoking the harness.
Writable: the home, the task's worktree, the project's `.git`, and your
`task_write` extras — nothing else. Readable: the interpreter's tree, the
harness executable resolved through `PATH`, nono's own system-read baseline,
and `task_read`. Network stays open, since a coding harness is assumed to need
its API.

**Bring your own profile.** `[sandbox].profile_file` points quorum at a nono
profile you already maintain, and it serves all three modes — the binary reads
it in mode 1, and modes 2 and 3 merge its grants into the capability set
quorum derives. Grants are *additive*: quorum's derived floor (its home, the
worktree, the interpreter and system read baseline) always stays, so a minimal
profile cannot brick the runner, and an unreadable one stops the run rather
than sandboxing with less than you asked for. Check platform support with
`python -c "import nono_py; print(nono_py.support_info())"`.

### Where everything lives

```
~/.quorum/
  config.toml                       yours; quorum never rewrites it
  supervisor.lock                   pid + start time + version; mtime = liveness
  agents/<name>.toml                agents you made with `agent create`
  projects/<slug>.json              registered projects
  tasks/<id>/task.json              spec, reported status, session, run history
  tasks/<id>/transcript.jsonl       the harness's stdout, line by line
  tasks/<id>/reports.jsonl          what the task reported
  tasks/<id>/notes.jsonl            its notebook
  tasks/<id>/handoff.md             its handoff for dependents
  tasks/<id>/runner.lock            pid of a live run
  tasks/.archive/<id>/              pruned tasks (moved here whole, never deleted)
  worktrees/<id>/                   the task's git worktree
  messages/board/<topic>/*.json     the board (task lifecycle on `tasks`)
  messages/inbox/<name>/new|cur/    guidance and control messages
  messages/archive/YYYY-MM.jsonl.gz compacted history
  prompts/*.md                      prompt templates (incl. manager.md)
  prompts/*.local.md                your overlays: init never touches these
  state/agents/<name>/              heartbeat and private state, plus — for a
                                    harness-driven agent — its journal,
                                    notebook, transcript, usage log and the
                                    digest of each recent tick
  state/manager/                    the same set, for the manager
  state/notify.json                 where the [notify] hook is up to, per topic
  logs/supervisor.log, actions.jsonl
  plugins/                          your plugin agents
```

Two files in a project directory belong to quorum by convention, and it only
ever *reads* either one (task writes happen in the worktree). A `.quorum.toml`
marker (written with `quorum project add --marker`, or by hand) carries
`name`, `deadline`, `tags` and `notes` with the repo across machines, merging
over the registry at read time. `.quorum/task-preamble.local.md` is the
project overlay described under
[Prompts and overlays](#prompts-and-overlays).
