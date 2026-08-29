// Drives the opencode quorum plugin outside opencode, with the PluginInput
// stubbed, so tests exercise the real plugin logic against a real quorum
// home (via QUORUM_BIN/QUORUM_HOME). One lifecycle step per invocation —
// the plugin is re-instantiated each time, as opencode itself would across
// restarts. Prints one JSON object on stdout.
//
// usage: node opencode_plugin_driver.mjs <plugin.js> <adopt|idle|dispose> <dir> <sessionID> [args]

import { pathToFileURL } from "node:url"

const [pluginPath, mode, directory, sessionID, cmdArgs] = process.argv.slice(2)
const { QuorumPlugin } = await import(pathToFileURL(pluginPath))

const injected = []
const client = {
  session: {
    prompt: async (call) => {
      injected.push(call)
    },
  },
}
const hooks = await QuorumPlugin({ client, directory })

if (mode === "adopt") {
  const output = { parts: [{ type: "text", text: "original command body" }] }
  await hooks["command.execute.before"](
    { command: "quorum-adopt", sessionID, arguments: cmdArgs || "" },
    output,
  )
  console.log(JSON.stringify({ parts: output.parts }))
} else if (mode === "idle") {
  await hooks.event({
    event: {
      type: "session.status",
      properties: { sessionID, status: { type: "idle" } },
    },
  })
  console.log(JSON.stringify({ injected }))
} else if (mode === "dispose") {
  await hooks.dispose()
  console.log(JSON.stringify({ disposed: true }))
} else {
  console.error(`unknown mode: ${mode}`)
  process.exit(2)
}
