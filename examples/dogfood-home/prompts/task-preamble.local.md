<!-- prompts/task-preamble.local.md — the delivery conventions every task in
     this home is held to. Copy it to ~/.quorum/prompts/task-preamble.local.md.

     Quorum renders it into the packaged prompts/task-preamble.md at that
     file's {local} slot, which sits after the generic delivery protocol and
     before the per-project block — so these lines are read as the house's
     refinement of protocol the preamble has already stated (commit, rebase,
     push, report). The home that builds quorum arrived at them the slow way:
     they were pasted into the text of every task prompt until they were
     identical every time, which is the signal that something is a house rule
     and not part of a task.

     Two things to know before editing:

     - No placeholders. The overlay is substituted *into* the template as a
       value, so a `{task_id}` written here stays literal — say "this task"
       instead. The surrounding template has the ids.
     - Home-wide scope. Everything here reaches every task in the home. A
       convention that is really about one repository belongs in that
       repository instead, at `.quorum/task-preamble.local.md` inside the
       project directory, which lands in the same prompt at {project}. The
       test commands below are exactly the kind of thing to move there once
       a second project joins the home. -->
Conventions for this home — they refine the delivery protocol above:

- **One draft PR per issue.** Deliver the whole task as a single draft pull
  request that names the issue it implements (`#123`) in its description, and
  reference the issue in the commit messages too. Do not edit, comment on or
  close the issue — that stays with the human.
- **Green before pushing.** `uv run pytest` and `uv run ruff check .` both
  pass, on your branch, before you push. A red suite is not a PR; fix it or
  report blocked saying what fails.
- **Docs and changelog in the same commit as the code.** A change to
  behaviour, file layout or vocabulary updates the docs it invalidates, and
  adds one bullet under `[Unreleased]` in CHANGELOG.md — a bullet under an
  existing heading, never a new heading and never a new version section.
- **Rebase before you push.** `git fetch origin` then
  `git rebase origin/main`; other tasks land on main while you work. If the
  rebase will not go through, `git rebase --abort` and report blocked naming
  the conflicting files.
- **Finish with the URL.** The last thing you do is
  `quorum task report <id> --status done --pr-url <url> "<summary>"`. A task
  that finished without a PR URL reads as unfinished work to the manager.
