## 1. Domain, errors and constants (first PR)

- [x] 1.1 `limits.json` agent keys; `gen_limits.py` group and Biome-style
  wrapping of long TypeScript constants; `_limits.py` and `limits.ts`
  regenerated; `test_limits.py` and `limits.test.ts` name overrides.
- [x] 1.2 Python `rayito/_agent/_domain.py`: `AgentModel`,
  `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `AgentSpec`,
  `AgentLimits`, `DEFAULT_DENIED_TOOLS`, `RESERVED_CONFIG_KEYS`.
- [x] 1.3 Python `_agent/_events.py`: events, `TokenUsage`, `AgentResult`,
  failure reasons and message table, `truncate_tool_output`.
- [x] 1.4 Python `_agent/_gateways.py`: `bedrock_gateway`,
  `anthropic_gateway`, `openai_compatible_gateway`.
- [x] 1.5 `AgentException` in `rayito.exceptions`; exports in
  `rayito.__all__`.
- [x] 1.6 TypeScript `src/agent/{domain,events,gateways}.ts`, `AgentError`
  in `errors.ts`, exports in `index.ts`.
- [x] 1.7 Shared vectors `testdata/agent/domain-vectors.json`;
  `test_agent_domain.py`, `test_agent_events.py`, `agent-domain.test.ts`.
- [x] 1.8 ADR-025 in `ARCHITECTURE.md`; T29 and T30 in `SECURITY.md`.
- [x] 1.9 CHANGELOG entries (`[Unreleased]`) for both SDKs.

## 2. Runtime port and OpenCode adapter

- [ ] 2.1 `AgentRuntime` port (`_agent/_runtime.py`, `agent/runtime.ts`) and
  the `_runtimes` registry.
- [ ] 2.2 OpenCode adapter: config builder, run script (lock, attach,
  dispose, `--title`, stdin), event mapping, `finish`, `abort_command`.
- [ ] 2.3 Golden files `testdata/agent/opencode-config/*.json` and the
  captured, anonymised `opencode-v1.18.34-events.jsonl` with
  `expected-events.json`.

## 3. Public API

- [ ] 3.1 `sbx.agent.run/stream/prepare` (sync, async, TypeScript), lazy
  property, egress warning, limits and abort.
- [ ] 3.2 Span `rayito.agent.run` and the allowed attributes.
- [ ] 3.3 Cost declaration `agent-run.json`; zero-cost golden trace.

## 4. Acceptance

- [ ] 4.1 Local e2e (`RAYITO_E2E_AGENT=1`) through the gateway.
- [ ] 4.2 AWS acceptance (Q146–Q152) and archive.
