## Why

`ai-agent-core` runs an AI agent inside a sandbox, but the runtime has to be
in the image first and the first run pays a cold page cache: the spike
measured the OpenCode binary (185 MB) being read from a lazily restored
snapshot on the first exec. There is no SDK way to build the image the spike
built by hand, and a `SandboxPool` cannot prepare a slot for the agent
before it parks it, nor close its egress.

## What Changes

- **`AgentTemplate` / `AsyncAgentTemplate`** (Python) and **`AgentTemplate`**
  (TypeScript): compose the existing `Template` DSL into the spike's recipe
  (OpenCode and ripgrep pinned by version and sha256, a hash-locked
  deepagents venv, everything root-owned and 0755), bake the
  `rayito.agent-template/1` manifest and, with `prefetch` (on by default),
  a prefetch daemon as `start_cmd`. `memory_mib` below 2048 is rejected.
- **`rayito agent template build`**: the CLI over `AgentTemplate`, with the
  `rayito template build` flags plus `--no-deepagents` and `--no-prefetch`.
- **Prefetch daemon** (option A): package data
  `rayito/_agent/_assets/rayito-agent-prefetch`; a wall-clock jump above
  `AGENT_PREFETCH_RESTORE_JUMP_SECONDS` means a snapshot restore, after
  which it reads the manifest's `prefetch_paths` with `nice -n 19`.
- **The deepagents requirements move to package data**
  (`rayito/_agent/_assets/requirements-deepagents.txt`, sha256 in
  `limits.json`); `dev/local/agent/Dockerfile` copies the same file through
  a compose additional context. `scripts/gen_agent_template_assets.py`
  renders both assets into `src/agent/assets/template-assets.gen.ts`; CI
  checks it for drift.
- **Pool warm-up** (option C): `PoolConfig.warmup` (`Sequence[WarmupStep]`,
  empty by default) runs after the slot settles and before `pause()`; a
  failing foreground step is a failed warm-up. `PoolConfig` also gains
  `allow_internet_access` and `network`, forwarded to `create()`.
- **Serve-in-pool** (option D): `agent_pool_warmup(runtime, serve=True)` /
  `agentPoolWarmup` adds a placeholder config, the runtime's resident
  `opencode serve` (password generated inside each VM), a health poll and
  a warmed instance; `agent.run` attaches after `take()` and the per-directory
  config load (F1) picks up the gateway port.
- `agent.prepare()` starts background warm-up steps without a timeout (a
  timeout would kill the resident server).
- Cost declarations `agent-template.json` and `pool-warmup.json`.

## Capabilities

### New Capabilities

- `ai-agent-template`: the agent image recipe, its manifest, the prefetch
  daemon, the CLI and the pool warm-up helper.

### Modified Capabilities

- `sandbox-pool`: `PoolConfig` gains `warmup`, `allow_internet_access` and
  `network`; the warm-up sequence runs the steps between settle and pause.

## Impact

- Python: `rayito/_agent/{_template,_warmup}.py`, `_agent/_assets/`,
  `cli/agent.py`, `_pool_base.py`, `sandbox_{sync,async}/pool.py`, exports.
- TypeScript: `src/agent/{template,warmup}.ts`,
  `src/agent/assets/template-assets.gen.ts`, `src/pool/{config,pool}.ts`,
  exports.
- `limits.json`: `agentPrefetchRestoreJumpSeconds`,
  `agentPrefetchIntervalSeconds`, `agentDeepagentsRequirementsSha256`.
- No `SlotRecord` / `rayito.pool/1` change; no `rayd` change; nothing runs
  or is billed unless a caller builds the template or passes `warmup`.
