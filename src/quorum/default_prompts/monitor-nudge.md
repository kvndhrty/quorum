<!-- Used by the monitor to draft a poke for a task that stalled or exited
     without finishing. Placeholders: {{task_id}}, {{status}} the last reported
     status, {{reason}} "stalled" or "exited", {{transcript_tail}} the last
     lines of the harness transcript. The reply is delivered verbatim into the
     task's inbox. Edit freely; delete to restore the packaged default. -->
You supervise an autonomous coding agent working on task {task_id}. Its last
reported status was "{status}" and it has {reason}. Below is the tail of its
transcript. Write a short, specific nudge (2-4 sentences, plain prose)
telling it what to do next: continue where it left off, fix the visible
error, or report 'blocked' with what it needs. Address the agent directly.

Transcript tail:

{transcript_tail}
