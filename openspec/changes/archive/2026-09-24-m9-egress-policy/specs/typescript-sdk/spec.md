## ADDED Requirements

### Requirement: Egress policy surface in TypeScript
The TypeScript SDK SHALL mirror the Python egress surface in camelCase over pure helpers in `src/sandbox/network.ts`.

Exports:
- `ALL_TRAFFIC = "0.0.0.0/0"`.
- `NetworkSelectorContext { allTraffic, rules }`, where `rules` is always an empty `ReadonlyMap`.
- `NetworkSelector`.
- `EgressProxyInput { address, username?, password? }`.
- `NetworkPolicyInput { allowOut?, denyOut?, egressProxy? }`.
- `EgressEnforcement` (`"unspecified" | "none" | "guest_routes" | "guest_routes_and_proxy"`).
- `NetworkState { allowOut, denyOut, egressProxyConfigured, enforcement, localProxyPort }`.
- `UnimplementedError extends Error` with `feature` and `reason`.
- `SandboxHealth.egressEnforcement`.

Creation:
- `SandboxCreateOptions` SHALL accept `network` and `allowInternetAccess`.
- `Sandbox.create` SHALL apply the same fail-closed gate as Python, terminating the MicroVM even with `keepOnFailure: true`.
- `pool` combined with `network` or `allowInternetAccess: false` SHALL be `InvalidArgumentError`.

Methods:
- `sandbox.updateNetwork(network?, { allowInternetAccess?, requestTimeoutMs? })` SHALL resolve to the `NetworkState`.
- `Sandbox.updateNetwork(sandboxId, network, opts)` SHALL connect with the access token, update and close without killing.
- `sandbox.getNetwork(opts?)` SHALL resolve to the `NetworkState`.

Errors:
- Connect `FailedPrecondition` and `Unimplemented` from `NetworkService` SHALL become `UnimplementedError`.
- `InvalidArgument` SHALL become `InvalidArgumentError`.

#### Scenario: fail-closed create in TypeScript
- **WHEN** the unit test calls `Sandbox.create({ allowInternetAccess: false, keepOnFailure: true })` against the fake server reporting `egressEnforcement` `none`
- **THEN** the promise rejects with `UnimplementedError` whose reason names `rayito-base-caps` and the fake control plane recorded `TerminateMicrovm` for that id

#### Scenario: updateNetwork cycle against the fake
- **WHEN** the unit test calls `sandbox.updateNetwork({ denyOut: ({ allTraffic }) => [allTraffic] })` and then `Sandbox.updateNetwork(sandbox.sandboxId, undefined, { accessToken })`
- **THEN** the fake receives `denyOut ["0.0.0.0/0"]` and then an empty policy, both calls resolve to a `NetworkState`, and the static call closed its client without `TerminateMicrovm`

#### Scenario: real-AWS mirror
- **WHEN** `clients/typescript/tests/e2e/egress.e2e.test.ts` runs against the M9 caps and default images
- **THEN** `allowInternetAccess: false` blocks a uid-1000 fetch while a loopback server stays reachable through `getHost`, the hostname allowlist returns 200 through the proxy variables and 403 for another host, the `updateNetwork` cycle follows each policy within 1 s, and the default image rejects with `UnimplementedError` after terminating the MicroVM
