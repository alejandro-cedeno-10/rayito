## Context

0.8.0 shipped four fast-start options for `sbx.agent`: A (prefetch daemon
in `AgentTemplate`), B (`pause()` between turns), C (`agent_pool_warmup`
loads the runtime in each slot) and D (C plus a resident `opencode serve`
that `agent.run` attaches to). D was measured as not worth recommending
(Q154) and is the only reason for a server password, an HTTP control path
from the run script and the `abort_command` hook.

## Goals / Non-Goals

**Goals:** remove D and everything that only D needed, in both SDKs, with
the same shared vectors; keep A, B, C and plain `create()` byte-identical
in behaviour.

**Non-Goals:** changing `WarmupStep`, `PoolConfig.warmup` or the pool;
changing `rayd`; deprecating first (the maintainer chose an outright
removal, announced as a breaking change).

## Decisions

- **D1 Remove, do not deprecate.** `serve` and `attach` disappear from the
  signatures, so an old call fails with `TypeError` (Python) or a type
  error (TypeScript). The CHANGELOG lists it under breaking changes with
  the migration: `agent_pool_warmup(runtime)` (C) or the normal start.
- **D2 Remove `abort_command` from the port.** Without an attached server
  both runtimes returned `None`/`undefined`; a hook with no caller is
  speculative. `abort()` keeps `stop_tree_command(pid)` and `kill()`.
  `resolve_runtime` checks `warmup_steps` instead, which every runtime
  still implements.
- **D3 `warmup_steps()` takes no argument.** `serve` was its only
  parameter.
- **D4 The run script `exec`s OpenCode directly.** After the lock and the
  binary check it runs `exec 'opencode' 'run' …`; the exit code now always
  reflects the process, and `finish()` keeps its rule (`Done` only with exit
  0, no `error` and a known session).
- **D5 Keep the measurements.** `AWS_API_NOTES.md` Q148 and Q154 stay, as
  historical rows; the docs drop D from every table and decision list.
- **D6 Golden data.** `opencode-run-commands.json` keeps two cases
  (`default`, `resume`); `pool-warmup.json` lists one case per runtime
  (`opencode`, `deepagents`); `opencode-attach/` is deleted; the captured
  events lose their `rayito.attached` line.

## Risks / Trade-offs

- A caller still passing `serve=True` or `attach=` breaks on upgrade. This
  is intended (D1) and documented with the migration.
