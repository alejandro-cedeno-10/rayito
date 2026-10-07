## Why

`Sandbox.connect()` (sync, `AsyncSandbox` and TypeScript) left
`sbx.gateways` empty: only the handle that applied `gateways=` in
`create()`/`take()` knew each route's port. A second process could not point
`sbx.agent` at the model's gateway without recreating the sandbox.

## What Changes

- `connect()` (static and instance, both SDKs) reads `ConfigureStatus` once,
  after readiness, when the agent advertises `configure` and
  `secret_gateway` and the handle has no gateway handle of its own, and
  rebuilds `sbx.gateways` from `SecretGatewayStatus` (name, port, last error
  class). `rayd` already serves that status; no proto or `rayd` change.
- No secret material is read or returned: `ConfigureStatus` never carries
  the upstream, header names or values.
- A recovered handle's `refresh()`/`arefresh()` only re-reads
  `ConfigureStatus`; rotating values stays with the process that holds the
  `SecretGateway` definitions.
- An agent without the feature, or a sandbox without routes, keeps
  `sbx.gateways` empty and makes no extra call (or one read, respectively).

## Capabilities

### Modified Capabilities

- `secret-gateway`: adds recovery of `sbx.gateways` on `connect()`.

## Impact

Python `_secret_gateway/_section.py`, `sandbox_sync/main.py`,
`sandbox_async/main.py`; TypeScript `secret-gateway/section.ts`,
`sandbox/sandbox.ts`; agent guide. No AWS cost: one extra unary gRPC to
`rayd` per `connect()` on a gateway-capable agent.
