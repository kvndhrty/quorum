# The quorum guide

Everything you need to run quorum, in one place: concepts, harness
configuration, driving and guiding tasks, the dashboards, sandboxing, and
writing your own agents. (Internals and design rationale live in
[architecture.md](architecture.md).)

- [Concepts](#concepts)
- [Setup](#setup)
- [Harnesses](#harnesses)
- [Tasks](#tasks)
- [The manager](#the-manager)
- [Guiding tasks](#guiding-tasks)
- [Dashboards](#dashboards)
- [Controlling agents at runtime](#controlling-agents-at-runtime)
- [Sandboxing](#sandboxing)
- [Writing your own agents](#writing-your-own-agents)
- [Where everything lives](#where-everything-lives)

## Concepts

Five words carry the whole system:

- **Home** — one directory (`~/.quorum` by default, `$QUORUM_HOME` or
  `--home` to override) holding every durable byte quorum touches. Plain
  JSON/JSONL/TOML/Markdown; `ls` and `cat` are debuggers; copying the
  directory migrates the whole setup.
- **Project** — a directory you registered with `quorum project add`,
  usually a git repo. Tasks run against projects.
- **Task** — a prompt pointed at a project, executed by a *harness* as a
  sequence of **runs**. Each run is one invocation of the harness in the
  task's working directory (a git worktree by default).
- **Harness** — any coding-agent CLI you already use: `claude`, `codex`,
  `opencode`, or your own script. Quorum never talks to a model API for
  tasks; it invokes your tool and stays out of the way.
- **Manager** — the one built-in agent, and it is *itself* harness-driven.
  Under `quorum up` it periodically reads a digest of everything happening —
  every task's status, output, and liveness, plus its own recent actions and
  your directives — and then *your harness* decides what to do: launch
  queued tasks, poke stuck ones, relaunch dead ones, create follow-up work,
  or escalate to you. Supervision policy is a prompt you can edit
  (`prompts/manager.md`), not code.

Communication is file-based messaging: a public **board** (topics anyone can
read) and per-recipient **inboxes**. Your guidance and the manager's pokes go
into a task's inbox; the harness reports progress back with the `quorum` CLI
itself; and the manager has its own inbox for your directives.

## Setup

```bash
uv tool install "quorum-orchestrator[web,tui]"   # installs the `quorum` command
quorum init                    # scaffolds ~/.quorum and a starter config.toml
```

`config.toml` is yours: quorum reads it and **never writes it back**. The
scaffold contains commented examples of everything below. The `[tasks]`
section sets defaults:

```toml
[tasks]
worktree = true         # run each task in its own git worktree under QUORUM_HOME
default_harness = ""    # harness used by `quorum task add` and the manager
```

## Harnesses

A harness entry tells quorum how to invoke your tool. `{prompt}` and
`{session}` are substituted into the argv; a template with no `{prompt}`
gets the prompt appended as the final argument.

```toml
[harness.claude]
start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]
resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]

[harness.codex]
start = ["codex", "exec", "{prompt}"]

[harness.opencode]
start = ["opencode", "run", "{prompt}"]

[harness.myscript]
start = ["/usr/local/bin/my-agent.sh"]        # prompt appended as last arg
env   = { MY_AGENT_MODE = "headless" }
```

Notes:

- **Sessions.** Quorum scans the harness's stdout for JSON containing a
  `session_id` and stores it on the task. When the harness has a `resume`
  template and a session is known, later runs use it — the agent continues
  its own conversation. Without either, every run starts fresh; that still
  works, because the worktree (and everything the previous run wrote there)
  persists between runs.
- **Autonomy flags.** Runs are unattended: a harness that stops to ask for
  interactive permission stalls silently on its first denied tool call, and
  the manager will eventually poke and resume it to no effect. Grant
  permissions explicitly. **Prefer a scoped allowlist** like the
  `--allowedTools` list above — it covers editing files, git, `gh` for the
  PR step, and quorum's own progress protocol (`Bash(quorum:*)` is what lets
  the harness call `quorum task report` / `quorum task inbox`) — over
  blanket bypasses like `--dangerously-skip-permissions`. Whatever you
  grant, pairing it with the [sandbox](#sandboxing) keeps the blast radius
  to the worktree, not your machine.
- **Environment.** The run inherits your environment plus the harness's
  `env` table, with `QUORUM_HOME` set — that's how `quorum task report`
  inside the run finds the right home.

## Tasks

```bash
quorum project add ~/work/my-api
quorum task add my-api "add rate limiting to the public endpoints, then open a PR"
quorum task add my-api "migrate the test suite to pytest" --harness codex
quorum up                      # the manager launches queued tasks
```

You can also drive runs by hand — no supervisor required:

```bash
quorum task run a3f2k9             # one run, in the foreground
quorum task run a3f2k9 --detach    # same, backgrounded
```

Task ids are short random handles (`quorum task list` shows them); any
unique prefix or suffix of the full id works everywhere.

**Worktrees.** By default each task gets `~/.quorum/worktrees/<id>`, a git
worktree on branch `quorum/<short-id>` of your project. Parallel tasks never
collide, your checkout stays clean, and abandoning a task is
`git worktree remove` plus `git branch -D` — nothing in your repo moved.
Use `--no-worktree` to run directly in the project directory instead.

**The protocol.** Every run's prompt starts with a preamble
(`~/.quorum/prompts/task-preamble.md`, editable) that teaches the harness:

```
quorum task report <id> --status <word> "<note>"     # at every phase change
quorum task inbox <id> --claim                       # check for guidance
quorum task report <id> --status pr --pr-url <url> "<title>"
quorum task report <id> --status done "<summary>"
quorum task report <id> --status blocked "<what you need>"
```

Status is **free-form** — quorum records whatever word the harness reports
and displays it. The conventional flow is `planning → executing → reviewing
→ pr → done`, but nothing is enforced. Only `done`, `blocked`, and
`cancelled` mean anything to quorum itself: they end the manager's
attention.

**Watching.**

```bash
quorum task list                  # every task, one line each
quorum task show a3f2k9           # full record + recent reports
quorum task tail a3f2k9 -f        # live transcript (the harness's stdout)
quorum status                     # tasks alongside agents and projects
```

**Finishing and undoing.** A task that opens a PR reports the URL, which
shows up in every view. `quorum task cancel <id>` stops the manager's
attention (`--kill` also SIGTERMs a live runner). The work itself lives on
the `quorum/<short-id>` branch either way.

## The manager

Supervision in quorum is not a set of thresholds — it's your harness reading
the situation and deciding. On its schedule (default every 5 minutes, only
when there is something to manage), the manager compiles a **digest**:

- every active task — status, whether its runner process is alive, how long
  it has been quiet, its recent reports and the tail of its output;
- the manager's own **recent actions with observed outcomes** ("you nudged
  a3f2k9 at 14:02; status UNCHANGED since") — auto-recorded, so the manager
  never loops on an intervention that isn't working;
- your directives.

It then runs your harness over that digest with `prompts/manager.md` — and
that prompt file *is* the supervision policy. Edit it to change how your
manager behaves: how patient it is, when it escalates, how it words its
pokes. Delete it to restore the default. The manager acts through the same
CLI you use — launching tasks, nudging them, cancelling them, even creating
follow-up tasks with `task add` — and every action lands in an auditable
journal:

```bash
quorum manager tell "prioritize the api task; park the docs work"   # steer it
quorum manager journal                    # audit everything it has done, and why
```

Two things bound a bad run, neither of which second-guesses a decision: a
per-run action cap (`max_actions_per_run`, default 20), and your own eyes on
the journal.

**When the LLM service is down, supervision halts loudly — and heals
itself.** There is no dumbed-down fallback: the manager's tick simply fails
(visible in `quorum status` and on the board), but its schedule keeps firing
(`auto_pause = false`), so the first tick after service returns reads the
state of the world from files and relaunches whatever died in the meantime.
You don't have to do anything.

## Guiding tasks

The steering channel for individual tasks is the task's inbox, and you and
the manager use it identically:

```bash
quorum task nudge a3f2k9 "use the middleware approach, not decorators"
```

The **next run starts with the guidance in its prompt** (a "Guidance
received" section), and a cooperative harness that checks
`quorum task inbox --claim` mid-run sees it sooner. The TUI makes this
fluid: select a task, press `n`, type, enter. The web dashboard has the same
nudge box on each task.

## Dashboards

All views are pure readers of the home directory — they work whether or not
the supervisor is running, including over SSH, and never hold locks.

- `quorum status` — one-shot text: supervisor liveness, agent heartbeats,
  tasks, project deadlines.
- `quorum tui` — live terminal dashboard (`[tui]` extra). Tasks on top;
  select one to see its transcript and reports, `n` to nudge, `esc` back to
  the board, `q` to quit.
- `quorum web` — the same picture at `http://127.0.0.1:8787` (`[web]`
  extra). Localhost only, no exposed ports.
- `quorum board read [topic]` — the raw message stream (`--json` for
  scripting). Task lifecycle lands on the `tasks` topic; manager
  escalations on `attention`.
- `quorum manager journal` — what the manager did and why.

## Controlling agents at runtime

While `quorum up` is running you can steer its schedule without editing
config or restarting:

```bash
quorum agent run-now manager      # tick immediately
quorum agent pause manager        # stop scheduling it
quorum agent resume manager       # resume (also clears the auto-pause counter)
quorum agent run-once manager     # one tick in *this* shell, supervisor optional
```

Commands are delivered through the supervisor's inbox and applied within
~15 seconds. An agent that fails 5 ticks in a row is auto-paused — unless
its config sets `auto_pause = false`, as the manager's does, in which case
it keeps retrying (loud failures, automatic recovery);
`agent resume` is the recovery lever.

## Sandboxing

Quorum pairs with [nono](https://github.com/nolabs-ai/nono), which confines
processes with OS security primitives (Landlock on Linux ≥ 5.13, Seatbelt on
macOS). Quorum is designed to be a well-behaved tenant: durable state in one
tree, messaging is pure file I/O, and each task's writes belong in its
worktree — so least-privilege profiles stay short. Three modes:

### Mode 1 — wrap the world (zero code)

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

- `fs_write`: `QUORUM_HOME` (worktrees live inside it), each project's
  `.git` (a worktree shares the main repo's object store, so commits write
  there), and your harness's own state directory (claude: `~/.claude`).
- `fs_read`: your project directories.
- `network`: whatever your harness needs to reach its API.

The same wrapping works per-command: `nono run --profile quorum -- quorum
task run <id>`.

### Mode 2 — self-sandbox the supervisor

With the `[nono]` extra installed (`uv tool install 'quorum-orchestrator[nono]'`):

```bash
quorum up --self-sandbox
```

Before the scheduler starts, quorum builds a capability set from your
resolved config and applies it via nono-py — irreversibly, children
included. Note this sandboxes the *supervisor* (which only needs
`QUORUM_HOME` and read access); detached task runs are separate processes
and get their own sandbox via Mode 3.

### Mode 3 — sandbox each task run

```toml
[sandbox]
use_nono = true
task_write = ["~/.claude"]    # your harness's own state dir
task_read  = []               # any extra read-only grants
```

### Bring your own profile

If you already maintain a nono profile, point quorum at it and it serves all
three modes — the binary reads it in Mode 1, and Modes 2/3 merge its grants
into the capability set quorum derives:

```toml
[sandbox]
use_nono = true
profile_file = "~/.config/nono/profiles/quorum.json"
```

The file is the same JSON shape as a `nono run` profile
(`{"fs_read": [...], "fs_write": [...], "network": [...]}`). Profile grants
are *additive* — quorum's derived floor (its home, the worktree, the
interpreter/system read baseline) always stays, so a minimal profile can't
brick the runner. A non-empty `network` list also keeps the supervisor's
network open in Mode 2. Fails closed: an unreadable or invalid profile stops
the run rather than sandboxing with less than you asked for.

Each `quorum task run` then applies a per-task kernel sandbox before
invoking the harness. Writable: `QUORUM_HOME`, the task's worktree, the
project's `.git`, and your `task_write` extras — nothing else. Readable:
the interpreter's tree, the harness executable (resolved through `PATH`),
nono's own system-read baseline (loader, system libraries — nothing can exec
without them), and `task_read`. Network stays open, since a coding harness
is assumed to need its API. The same flag also confines plugin agents'
`[llm]` subprocess calls.

**Fail-closed, all modes:** if sandboxing was requested and nono-py is
missing or unsupported, the run does not happen unsandboxed — it fails loud.
Check platform support with
`python -c "import nono_py; print(nono_py.support_info())"`.

## Writing your own agents

The manager is deliberately the only built-in. Anything else you want on a
schedule — a CI watcher, a SLURM queue poller, a deadline reminder — is a
plugin: a class with a synchronous `tick()`, dropped into
`QUORUM_HOME/plugins/`, no packaging.

A complete, tested example ships in the repo:
[examples/steward.py](../examples/steward.py), a rule-based file organizer
with undo, LLM-optional classification, and bounded retries. Copy it into
`~/.quorum/plugins/` and add:

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

```toml
[agents.wordcount]
type = "wordcount:WordCount"
schedule = "every 2h"          # or "cron 0 8 * * *"
[agents.wordcount.settings]
file = "~/work/thesis/main.tex"
```

Test it immediately: `quorum agent run-once wordcount`.

**The contract:**

- `tick()` must be **idempotent** — it can be re-run at any time (missed
  schedules coalesce, `run-once` exists, crashes get retried). Use
  `load_state()`/`save_state()` to remember what you already did.
- **Raising is fine**: the supervisor logs the traceback, marks your
  heartbeat `error`, posts to the `system` topic, and auto-pauses after 5
  consecutive failures. You cannot take down other agents.
- Use `self.ctx.now()`, never `datetime.now()` — it makes your agent
  testable with a fake clock.

**What the context gives you:**

| Member | Purpose |
|---|---|
| `ctx.settings` | your `[agents.<name>.settings]` table, verbatim |
| `ctx.bus.post(sender, topic, type=, text=, payload=)` | broadcast to the board |
| `ctx.bus.send(sender, to, ...)` | direct mail to an inbox (e.g. a task's) |
| `ctx.bus.claim(name)` | consume your own inbox (call `.ack()` per message) |
| `ctx.bus.read_after_cursor(topic, cursor)` | follow a board topic incrementally |
| `ctx.projects.list()` / `.get(slug)` | registered projects, marker-merged |
| `ctx.llm.complete(prompt)` | completion or `None` — always handle `None` |
| `ctx.prompt(name, **placeholders)` | render a template from `prompts/` |
| `ctx.load_state()` / `ctx.save_state(d)` | your private JSON state |
| `ctx.log_action(type, text, **data)` | feed the dashboards' activity log |
| `ctx.now()` | injectable clock |

**Testing** (see `tests/test_example_steward.py` for the full pattern):

```python
from quorum.agent import AgentContext
from wordcount import WordCount

def test_milestone(tmp_path):
    home = tmp_path / "qhome"
    from quorum.home import scaffold; scaffold(home)
    (tmp_path / "ms.txt").write_text("word " * 1500)
    ctx = AgentContext(home=home, name="wc", settings={"file": str(tmp_path / "ms.txt")})
    WordCount(ctx).tick()
    assert ctx.bus.read_topic("writing")
```

## Where everything lives

```
~/.quorum/
  config.toml                       yours; quorum never rewrites it
  supervisor.lock                   pid + start time; mtime = liveness
  projects/<slug>.json              registered projects
  tasks/<id>/task.json              spec, reported status, session, run history
  tasks/<id>/transcript.jsonl       the harness's stdout, line by line
  tasks/<id>/reports.jsonl          what the task reported
  tasks/<id>/runner.lock            pid of a live run
  worktrees/<id>/                   the task's git worktree
  messages/board/<topic>/*.json     public board (task lifecycle on `tasks`)
  messages/inbox/<name>/new|cur/    guidance & control (tasks, supervisor)
  messages/archive/YYYY-MM.jsonl.gz compacted history
  prompts/*.md                      editable prompt templates (incl. manager.md)
  state/agents/<name>/              heartbeats + private agent state
  state/manager/journal.jsonl       the manager's auto-recorded actions
  state/manager/transcript.jsonl    the manager harness's own output
  logs/supervisor.log, actions.jsonl
  plugins/                          your custom agents
```

A `.quorum.toml` marker inside a project directory (written with
`quorum project add --marker`, or by hand) carries `name`/`deadline`/`tags`/
`notes` with the repo across machines; it merges over the registry at read
time and quorum only ever *reads* project directories — task writes happen
in worktrees.
