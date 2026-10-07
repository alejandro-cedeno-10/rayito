## 1. Python

- [x] 1.1 `_opencode.py`: run script without attach, no `rayito.attached`/`rayito.message`, no serve script, no `abort_command`; `warmup_steps()` without `serve`
- [x] 1.2 `_runtime.py`/`_runtimes.py`/`_deepagents.py`: port without `abort_command`, `RunRequest` without `attach`
- [x] 1.3 `_warmup.py`: `agent_pool_warmup(runtime)` returns the runtime's steps
- [x] 1.4 `sandbox_sync`/`sandbox_async` `agent.py`, `_stream_base.py`, `_telemetry.py`, `_otel.py`: no `attach`, no `serve`, no `rayito.agent.attached`
- [x] 1.5 Unit tests updated (sync and async)

## 2. TypeScript

- [x] 2.1 `agent/opencode.ts`, `deepagents.ts`, `runtime.ts`, `runtimes.ts`, `warmup.ts`, `telemetry.ts`, `stream.ts`, `sandbox/agent.ts`, `otel.ts`: same removals
- [x] 2.2 Unit tests updated

## 3. Shared

- [x] 3.1 `limits.json` without `opencodeServePort`; `gen_limits.py` regenerated
- [x] 3.2 `testdata/agent/`: delete `opencode-attach/`, regenerate `opencode-run-commands.json` and `pool-warmup.json`, drop the `rayito.attached` line

## 4. Docs

- [x] 4.1 Agent guide decision table A/B/C and "sin pool"; pool, cost, optional features, security, references and release notes without D
- [x] 4.2 `AWS_API_NOTES.md` Q148 and Q154 marked historical
- [x] 4.3 CHANGELOG of both SDKs under breaking changes, with the migration
