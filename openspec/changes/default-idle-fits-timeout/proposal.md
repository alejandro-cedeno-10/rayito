## Why

Since 0.3.0, `Sandbox.create(timeout=120)` (any `timeout` at or below the
default idle window of 300 s) raised `InvalidArgumentException` /
`InvalidArgumentError` even though the caller never passed `idle`: the SDK
validated its own default `IdlePolicy` against the timeout. The same
happened with `on_timeout` and a `max_lifetime` of 300 s or less. A
sandbox that ends before 300 s can never be auto-suspended, so the error
only blocked a valid E2B-style launch.

## What Changes

- Without an explicit `idle`, the default window adapts to the bound it is
  checked against (`timeout`, or `max_lifetime` with `on_timeout`):
  - no lifecycle block and `on_timeout="kill"`: when 300 s does not fit,
    the default policy is dropped (no `idlePolicy` in `run-microvm`), so
    the sandbox ends at its timeout as in E2B;
  - `on_timeout="pause"`: the idle policy is what suspends without a
    client, so it is kept and lowered to the API minimum (60 s).
- An explicit `idle` (any `IdlePolicy(...)` / any object in TypeScript)
  is validated exactly as before and still raises when it does not fit.
- Python (sync and async share `_lifecycle_base`) and TypeScript
  (`sandbox/lifecycle.ts`) with named constants
  (`PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS`); the E2B shims already pass an
  explicit idle and are covered by tests.
- Docs: pausar-reanudar, concepts, lifecycle, limits, ciclo-de-vida.
- E2E docs: the `s3-mounts` and `rayd-otlp` e2e headers, `CONTRIBUTING.md`
  and the gates reference state that the managed policy of each stack must
  be attached to the execution role.

## Capabilities

### Modified Capabilities

- `sandbox-timeout`: the default idle policy adapts to a short timeout.

## Impact

`clients/python/src/rayito/_lifecycle_base.py`, `_sandbox_base.py`,
`clients/typescript/src/sandbox/lifecycle.ts`, unit tests in both SDKs,
docs site, both CHANGELOGs. No `rayd` or wire change. Behaviour only
changes for launches that used to raise.
