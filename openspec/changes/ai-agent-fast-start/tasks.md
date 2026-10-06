## 1. Template and prefetch

- [x] 1.1 `limits.json`: `agentPrefetchRestoreJumpSeconds`,
  `agentPrefetchIntervalSeconds`, `agentDeepagentsRequirementsSha256`;
  regenerate `_limits.py` and `limits.ts`.
- [x] 1.2 Move `requirements-deepagents.txt` to Python package data; the
  local Dockerfile copies it through a compose additional context.
- [x] 1.3 `rayito-agent-prefetch` daemon (package data) and a unit test
  with a fake `date`.
- [x] 1.4 Python `AgentTemplate` / `AsyncAgentTemplate`
  (`_agent/_template.py`) and exports.
- [x] 1.5 `scripts/gen_agent_template_assets.py` and the generated
  TypeScript module; CI drift check.
- [x] 1.6 TypeScript `AgentTemplate` (`src/agent/template.ts`) and exports.
- [x] 1.7 Shared vectors `testdata/agent/agent-template/cases.json`.
- [x] 1.8 `rayito agent template build` (`cli/agent.py`).

## 2. Pool warm-up and serve

- [x] 2.1 `PoolConfig.warmup`, `allow_internet_access`, `network` (Python
  and TypeScript) with validation and launch kwargs only when set.
- [x] 2.2 Run the steps between settle and pause in `SandboxPool`,
  `AsyncSandboxPool` and the TypeScript pool.
- [x] 2.3 `agent_pool_warmup` / `agentPoolWarmup` with the serve steps;
  shared vectors `testdata/agent/pool-warmup.json`.
- [x] 2.4 `agent.prepare()` background steps without a timeout.

## 3. Declarations and gates

- [x] 3.1 "Coste y activación" blocks; cost declarations
  `agent-template.json` and `pool-warmup.json`.
- [x] 3.2 CHANGELOG entries for both SDKs.
- [x] 3.3 Local gates (Python, TypeScript, OpenSpec, docs, hygiene).
- [ ] 3.4 AWS acceptance (Q146–Q148, gates G1/G2) in the acceptance stage,
  then archive.
