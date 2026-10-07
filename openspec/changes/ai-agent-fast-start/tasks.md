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
- [x] 3.4 AWS acceptance (Q146–Q149): G1 met (prefetch cuts the post-create
  first token 19.6 s → 4.7 s); G2 not met — `opencode run --attach` loses
  events on AWS, so D is not recommended. The acceptance also found that
  the template did not install the deepagents runner; fixed here.
- [ ] 3.5 Fix or drop serve-in-pool (D) before archiving (Q148).
  - [x] 3.5.1 Root cause in `run.ts` (attach `finish()` does not await the
    event loop; same in 1.18.35) and local reproduction.
  - [x] 3.5.2 The run script creates the session and re-reads the turn;
    the adapter dedups by part id (D9), Python and TypeScript, with the
    captured fixtures `testdata/agent/opencode-attach/`.
  - [ ] 3.5.3 AWS re-measure of D (n=5): take → first token and end.
- [ ] 3.6 Prefetch must not delay `create()` (Q146).
  - [x] 3.6.1 The daemon waits for a quiet guest (no I/O in flight for 1 s)
    after the jump; unit test with a fake `diskstats`.
  - [ ] 3.6.2 AWS re-measure (n=5): `create()` and `create()` → first token;
    if `create()` still grows, `prefetch` becomes opt-in.
