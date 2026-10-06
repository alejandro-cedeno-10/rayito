## 1. Adapter and runner

- [x] 1.1 Runner `rayito/_agent/_runner/deepagents_runner.py`: protocol on a
  private fd, request on stdin, model through the gateway, permission
  middleware, prompt caching switch, entry point with `RunnerContext`,
  sessions.
- [x] 1.2 Python adapter `_agent/_deepagents.py` (`DeepAgents`), registry
  entry and exports.
- [x] 1.3 TypeScript adapter `src/agent/deepagents.ts`, registry entry and
  export; `scripts/gen_agent_assets.py` and the generated
  `src/agent/assets/deepagents-runner.ts`.
- [x] 1.4 `langchain-openai` in `requirements-deepagents.txt` without moving
  existing pins.

## 2. Tests and gates

- [x] 2.1 Golden files `testdata/agent/deepagents-config.json`,
  `deepagents-run-commands.json`, `rayito-protocol-v1.jsonl`,
  `rayito-protocol-v1-expected.json`; `test_agent_deepagents.py` and
  `agent-deepagents.test.ts`.
- [x] 2.2 Runner tests with stubs (`test_agent_deepagents_runner.py`).
- [x] 2.3 `make agent-runner-test` (real pinned venv, Linux arm64, fake
  model) and its step in the CI `arm` job; `gen_agent_assets.py --check`
  in `make lint` and CI.
- [x] 2.4 CHANGELOG entries for both SDKs.

## 3. Later stages

- [ ] 3.1 Template installs the runner and venv (`ai-agent-fast-start`).
- [ ] 3.2 Docs page and cost box (`ai-agent-docs-pricing`).
- [ ] 3.3 Local e2e with real Bedrock and AWS acceptance Q150.
