// quorum × opencode: adoption plugin.
//
// opencode has no stdin/stdout hook commands or Stop-hook block protocol;
// its extension surface is this in-process plugin. The plugin stays a dumb
// pipe: every decision lives in the quorum CLI, which it shells out to with
// the same JSON payloads the Claude Code / Codex hooks send. Guidance is
// delivered by injecting a user turn via the SDK client — the moral
// equivalent of the Stop-hook `{"decision": "block"}` continuation.
//
// Fail-soft by design (the herdr.py stance): a missing or broken quorum CLI
// must never break the user's session, so every shell-out swallows errors.

import { spawn } from "node:child_process"

const QUORUM_BIN = () => process.env.QUORUM_BIN || "quorum"

function quorum(args, payload) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(QUORUM_BIN(), args, { stdio: ["pipe", "pipe", "ignore"] })
    } catch {
      return resolve(null)
    }
    let out = ""
    child.stdout.on("data", (d) => (out += d))
    child.on("error", () => resolve(null))
    child.on("close", (code) => resolve(code === 0 ? out : null))
    try {
      if (payload !== undefined) child.stdin.write(JSON.stringify(payload))
      child.stdin.end()
    } catch {
      resolve(null)
    }
  })
}

export const QuorumPlugin = async ({ client, directory }) => {
  const inflight = new Set() // serialize per-session idle handling

  async function onIdle(sessionID) {
    if (!sessionID || inflight.has(sessionID)) return
    inflight.add(sessionID)
    try {
      const out = await quorum(["task", "hook-stop", "--format", "text"], {
        session_id: sessionID,
        cwd: directory,
      })
      if (out && out.trim()) {
        await client.session.prompt({
          path: { id: sessionID },
          body: { parts: [{ type: "text", text: out.trim() }] },
        })
      }
    } catch {
      // never break the session over supervision
    } finally {
      inflight.delete(sessionID)
    }
  }

  return {
    "command.execute.before": async (input, output) => {
      if (input.command !== "quorum-adopt") return
      const out = await quorum([
        "task", "adopt", input.arguments || "", "--json",
        "--session", input.sessionID, "--dir", directory,
      ])
      const report = out
        ? `The quorum adoption command succeeded with output:\n${out.trim()}`
        : "The quorum adoption command failed (is the quorum CLI on PATH, " +
          "and has `quorum init` been run with the right QUORUM_HOME?)."
      output.parts = [
        {
          type: "text",
          text:
            `${report}\n\nReport the outcome to the user in one or two ` +
            "sentences: the attached task's short id (from the JSON above), " +
            "and that quorum's manager will now observe this session and may " +
            "inject guidance here whenever you go idle — follow such " +
            "guidance like a user message.",
        },
      ]
    },
    event: async ({ event }) => {
      // session.status{idle} is current; session.idle is its deprecated
      // predecessor — handling both double-calls hook-stop, which is safe
      // (guidance is consumed by whichever claim wins; the loser is silent).
      if (event.type === "session.status" && event.properties?.status?.type === "idle") {
        await onIdle(event.properties.sessionID)
      } else if (event.type === "session.idle") {
        await onIdle(event.properties?.sessionID)
      }
    },
    dispose: async () => {
      await quorum(["task", "hook-session-end"], { cwd: directory })
    },
  }
}
