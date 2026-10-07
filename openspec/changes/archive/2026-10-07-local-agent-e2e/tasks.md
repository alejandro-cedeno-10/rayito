## 1. Fixes

- [x] 1.1 `stop_tree_command`/`stopTreeCommand` on abort and on an SDK limit (Python sync and async, TypeScript), with unit tests
- [x] 1.2 Drain the handle after an SDK limit
- [x] 1.3 Timeout end status mapped to `reason="timeout"`
- [x] 1.4 deepagents runner: `LocalShellBackend(..., virtual_mode=False)`; TypeScript asset regenerated

## 2. Tests and harness

- [x] 2.1 Python `tests/local/test_local_agents.py`
- [x] 2.2 TypeScript `tests/local/agents.local.test.ts`
- [x] 2.3 `RAYITO_LOCAL_BEDROCK_KEY_FILE` in the runner, `make local-agent-up`, `make local-bedrock-key`, the runner in the agent guest
- [x] 2.4 Both suites green against real Bedrock (timeout-tool case as strict expected failure)

## 3. Docs

- [x] 3.1 Local testing guide: agents against a real model, cost box
- [x] 3.2 Agent guide: abort and limits stop the process tree; timeout caveat
