## Why

Fast-start option D (a resident `opencode serve` in every pool slot, with
`opencode run --attach`) only beats option C by a few tenths of a second
(AWS_API_NOTES Q154: 4.8 s against 5.1 s to the first token), costs more
memory and more per idle slot (≈ $0.82 against ≈ $0.64 a month), keeps a
server password in every parked snapshot and needs a run script that
re-reads each turn from the server because `opencode run --attach` exits
before writing its own events (Q148). The maintainer decided to remove it
outright in 0.9.0, with no deprecation period, as a breaking change.

## What Changes

- **BREAKING** `agent_pool_warmup(runtime)` / `agentPoolWarmup(runtime)`
  lose `serve`; they return the runtime's warm-up steps only (option C).
- **BREAKING** `sbx.agent.prepare()` loses `serve`; `sbx.agent.run()` and
  `stream()` lose `attach`; `RunRequest.attach` is gone.
- **BREAKING** The `AgentRuntime` port loses `abort_command`/`abortCommand`
  (dead for both runtimes once nothing attaches), and
  `warmup_steps`/`warmupSteps` takes no argument.
- The OpenCode run script no longer probes a resident server, creates or
  re-reads sessions over HTTP or prints `rayito.attached`/`rayito.message`:
  it takes the run lock, checks the binary and `exec`s `opencode run`.
- The `rayito.agent.attached` span attribute and `opencodeServePort`
  (`OPENCODE_SERVE_PORT`) are removed.
- `testdata/agent/opencode-attach/` is deleted; `opencode-run-commands.json`
  and `pool-warmup.json` are regenerated.
- Options A (prefetch), B (pause/resume), C (pool warm-up) and the plain
  `create()` path do not change. `WarmupStep.background`/`tag` stay: they
  are general and also used by caller-supplied steps.

## Capabilities

### Modified Capabilities

- `ai-agent-runtime`: the port without `abort_command`, abort without a
  runtime abort command, and the OpenCode adapter without attach.
- `ai-agent-template`: `agent_pool_warmup` without `serve`, and
  `agent.prepare()` without `serve`.

## Impact

- Python: `rayito/_agent/` (`_opencode.py`, `_deepagents.py`,
  `_runtime.py`, `_runtimes.py`, `_warmup.py`, `_telemetry.py`,
  `_stream_base.py`), `sandbox_sync/agent.py`, `sandbox_async/agent.py`,
  `_otel.py`, `_limits.py` (generated).
- TypeScript: `src/agent/` (`opencode.ts`, `deepagents.ts`, `runtime.ts`,
  `runtimes.ts`, `warmup.ts`, `telemetry.ts`, `stream.ts`),
  `src/sandbox/agent.ts`, `src/otel.ts`, `src/limits.ts` (generated).
- `limits.json`, `scripts/gen_limits.py`, `testdata/agent/`.
- Docs (agent guide, pool, cost, optional features, security, references,
  release notes), `SECURITY.md`, `AWS_API_NOTES.md` (Q148 and Q154 marked
  historical), CHANGELOG of both SDKs.
- No `rayd` or infrastructure change.
