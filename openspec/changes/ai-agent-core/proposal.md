## Why

The agent spike (`docs/research/2026-10-agent-spike.md`) showed that a coding
agent (OpenCode 1.18.34, or a deepagents graph) runs inside one Rayito
sandbox with its model credential kept out of the guest: the secret gateway
(ADR-023) injects the real `authorization`/`x-api-key` header and the agent
only ever sees a placeholder. Today a caller has to rebuild that recipe by
hand: install the runtime, write its config, pick the gateway paths, start
the process, parse its JSONL, enforce a budget and map failures. Each of
those steps has a sharp edge the spike measured (OpenCode retries a 5xx
forever, an omitted `--title` makes a hidden model call to a different
model, the exit code under `--attach` hides `session.error`).

`ai-agent-core` adds `sbx.agent`: one call that runs an AI agent inside the
sandbox with a closed set of events and failure reasons, hard step, token,
output and time limits, and credentials that only ever travel through a
gateway. It is the first of four changes (`ai-agent-core`, then
`ai-agent-fast-start`, `ai-agent-deepagents` and `ai-agent-docs-pricing`)
and lands first because the other three build on its domain types.

## What Changes

- **Pure domain** (`rayito/_agent/_domain.py`, `_events.py`;
  `src/agent/domain.ts`, `events.ts`): `AgentModel`, `AgentPermissions`,
  `SubAgent`, `McpLocal`, `McpRemote`, `AgentSpec`, `AgentLimits`; the event
  union (`TextDelta`, `Text`, `Reasoning`, `ToolCall`, `StepStarted`,
  `StepFinished`, `AgentFailed`, `Done`), `TokenUsage` and `AgentResult`; a
  closed list of failure reasons with a fixed Spanish message table. Shared
  vectors in `testdata/agent/domain-vectors.json`.
- **Errors**: `AgentException(SandboxException)` / `AgentError extends
  SandboxError` with `reason`, `session_id`/`sessionId`, `usage`,
  `exit_code`/`exitCode` and `detail_code`/`detailCode`.
- **Gateway presets** (`_agent/_gateways.py`, `agent/gateways.ts`):
  `bedrock_gateway`, `anthropic_gateway`, `openai_compatible_gateway` return
  a `SecretGateway` whose `allow` covers only the chosen models' paths.
- **Shared constants** in `limits.json` (run defaults, in-VM paths, the
  agent protocol version, the template manifest path and schema, the
  minimum memory and the OpenCode/ripgrep pins), rendered by
  `scripts/gen_limits.py`.
- **Runtime port and OpenCode adapter** (`AgentRuntime`, `_opencode.py`,
  `opencode.ts`): config, run script, event mapping, finish and abort.
- **Public API**: `sbx.agent.run/stream/prepare` (sync, async, TypeScript),
  telemetry span `rayito.agent.run` behind the existing opt-in tracer, and
  the `agent-run` cost declaration.
- **Docs of record**: ADR-025 in `ARCHITECTURE.md`; T29 (the agent inside
  the sandbox) and T30 (runtime supply chain) in `SECURITY.md`.

The domain, errors, presets, constants and docs of record land in the first
PR of this change; the runtime port, the OpenCode adapter and the public
`sbx.agent` API land in the next ones. No AWS call is added by the first PR.

## Impact

- New capability `ai-agent-runtime`.
- Python: new private package `rayito._agent`; new public names in
  `rayito.__all__`; new `AgentException` in `rayito.exceptions`.
- TypeScript: new `src/agent/`; new exports in `src/index.ts`; new
  `AgentError` in `src/errors.ts`.
- `limits.json` gains the agent keys; `_limits.py` and `limits.ts` are
  regenerated; `gen_limits.py` wraps a TypeScript constant longer than 100
  columns the way Biome does.
- No `rayd` change, no `.proto` change, no new AWS call. The E2B shims are
  unchanged.
