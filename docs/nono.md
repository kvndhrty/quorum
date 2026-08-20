# Running quorum under the nono sandbox

[nono](https://github.com/nolabs-ai/nono) confines processes with OS
security primitives (Landlock on Linux ≥ 5.13, Seatbelt on macOS). Quorum is
designed to be a well-behaved tenant: all durable state lives in one tree
(`QUORUM_HOME`), project directories are only read, and inter-agent
messaging is pure file I/O — so a least-privilege profile is short.

Quorum runs fine without nono. With it, pick one of three modes.

## Mode 1 — wrap the world (recommended, zero code)

Install the nono binary (`brew install nono` or
`curl -fsSL https://nono.sh/install.sh | sh`), create a profile, and run the
whole supervisor inside it:

```bash
nono profile init quorum
nono run --profile quorum -- quorum up
```

A profile granting exactly what the default agents need:

```json
{
  "fs_write": [
    "~/.quorum",
    "~/Downloads",
    "~/papers/inbox"
  ],
  "fs_read": [
    "~/work",
    "~/research"
  ],
  "network": []
}
```

- `fs_write`: `QUORUM_HOME`, plus any steward `watch` dirs and rule `dest`
  dirs (the steward moves files there).
- `fs_read`: your project directories / workspace roots (tracker and scout
  only read them).
- `network`: empty unless your `[llm]` executable needs to reach an API —
  then allow-list that host, or route it through nono's proxy for credential
  injection.

The same wrapping works per-command: `nono run --profile quorum -- quorum
agent run-once steward`.

## Mode 2 — self-sandbox (nono-py)

With the `[nono]` extra installed (`uv tool install 'quorum[nono]'`):

```bash
quorum up --self-sandbox
```

Before the scheduler starts, quorum builds a `CapabilitySet` from your
resolved config — rw `QUORUM_HOME`, ro each registered project, rw steward
watch/dest dirs, `block_network()` unless `[llm]` is configured — and calls
`nono_py.apply()`. This is **irreversible for the process** and covers all
children, including LLM subprocesses.

## Mode 3 — sandbox only the LLM subprocesses

```toml
[sandbox]
use_nono = true
```

The supervisor stays unsandboxed, but every LLM CLI invocation runs through
`nono_py.sandboxed_exec` with the same derived capability set. Notes:

- `sandboxed_exec` cannot pipe stdin, so with `[llm].input = "stdin"` the
  prompt is staged as a file under `QUORUM_HOME/state/llm/` and redirected
  via `/bin/sh`. `input = "argv"` avoids the shell hop and is recommended
  under nono.
- **Fail-closed**: if `use_nono = true` but nono-py is missing or the
  platform is unsupported, LLM calls return no completion (agents fall back
  to their deterministic behavior) — quorum never silently runs the CLI
  unsandboxed.

## Testing the integration in CI

Two layers, wired up in `.github/workflows/ci.yml`:

- **Glue tests (always run)** — `tests/test_sandbox.py` monkeypatches a fake
  `nono_py` module, so capability derivation, the `sandboxed_exec` mapping,
  and the fail-closed path are pinned down on any runner, with or without
  nono installed.
- **Enforcement tests (`-m nono_integration`)** — `tests/test_nono_integration.py`
  installs the real `nono-py` wheel and asserts the kernel actually enforces
  the derived capability set: writes inside `QUORUM_HOME` succeed,
  out-of-capability writes fail, project dirs are read-only, Mode 3's stdin
  staging round-trips under a real child sandbox, and Mode 2's
  `self_sandbox` is exercised in a subprocess (since `nono_py.apply` is
  irreversible for the calling process). These need Landlock (Linux ≥ 5.13
  with the LSM enabled) or Seatbelt and self-skip elsewhere — check
  `python -c "import nono_py; print(nono_py.support_info())"`. The dedicated
  CI job asserts support before running so the tests can't silently skip.

## Future: managed LLM auth proxy

nono-py ships a filtering proxy with credential injection
(`ProxyConfig` / `start_proxy`). The reserved `[llm].backend = "proxy"` seam
is intended to boot such a proxy from the supervisor so agent subprocesses
get API access without ever holding raw keys. Not implemented in v1.
