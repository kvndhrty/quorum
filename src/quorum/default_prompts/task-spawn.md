<!-- Appended to the run prompt of a task queued with `task add --allow-spawn`,
     in place of the {{spawn}} placeholder in task-preamble.md (a task without
     it gets nothing here, and `quorum task add` refuses it). Placeholders:
     {{task_id}}; {{project}} the task's project slug; {{cap}} this home's
     `[tasks].max_spawn_per_task`; {{local}} this home's conventions about
     spawning, from prompts/task-spawn.local.md — never seeded, never touched
     by `quorum init`, so house rules there keep this file upgradable. Edit
     freely — this file is yours; delete it to restore the packaged default. -->
Creating follow-up work — this task MAY queue tasks of its own:

    quorum task add {project} "<what the new task should do>"
    quorum task add {project} "<...>" --after self   (it waits for you to finish)

- Use it for work that genuinely belongs on its own branch: something you
  found that is out of scope here, a follow-up someone should do after this
  lands, an experiment worth running separately. Work you are going to do in
  this run is not a new task.
- Write the prompt for a stranger. The new task starts with no memory of your
  session — say what to do, where, and what done looks like.
- `--after self` is the post-task: the new work waits until you reach a
  terminal status, and your handoff (`task report --status done --handoff
  <file>`) is what it will read.
- A spawned task is queued, never started: the manager or a human decides when
  it runs, and a human still merges it. Do not try to run one yourself.
- You may create at most {cap} of them ([tasks].max_spawn_per_task), and they
  may not spawn further tasks unless this home allows deeper chains. When
  `task add` refuses you, the idea is not lost: put it in your report
  (`quorum task report {task_id} --status <status> "..."`) and let the manager
  or the human decide.

{local}
