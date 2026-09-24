## Why

E2B lets a program cut or filter a sandbox's outbound network:
`Sandbox.create(allow_internet_access=False)`, `network={"allow_out": [...],
"deny_out": [...]}` with CIDRs, IPs, hostnames, `ALL_TRAFFIC` and selector
callables, a bring-your-own SOCKS5 `egress_proxy`, and
`Sandbox.update_network(...)` on a running sandbox. Rayito 0.2.0 raises
`UnimplementedError` for all of it (`rayito.e2b` raises on
`allow_internet_access=False`; `network`, `update_network` and `egress_proxy`
are not accepted at all), and `SECURITY.md` T8 is open: the only
documented control is a customer-owned VPC connector
(`infra/egress-connector.yaml`), which is validated but was never measured
(Q46).

The platform will not supply a switch:

- **Q44/Q60 (measured)**: a MicroVM launched with `egressNetworkConnectors`
  omitted or set to `[]` inherits the image version's `INTERNET_EGRESS` and
  reaches the internet. The managed ARN `…:aws-network-connector:NO_EGRESS`
  is rejected with `ValidationException`. There is no managed no-egress.
- **§1**: there is no `UpdateMicrovm`, so platform connectors are fixed at
  `run-microvm`. A platform-level `update_network` is impossible.

The guest does have one enforcement point:

- **Q48 (measured)**: on `rayito-base-caps` (`additionalOsCapabilities:
  ["ALL"]`), `ip rule … uidrange 1000-65535` policy routing works. It
  already blackholes IMDS for the sandbox user while root (`rayd`) and the
  platform agent (uids 991–994) keep their paths.

This change delivers E2B's network controls with that mechanism. Where the
mechanism is missing, it fails closed instead of pretending.

## What Changes

Every decision is closed in `design.md`. The summary:

- **ADR-012 "in-guest egress policy on rayito-base-caps; platform connectors
  only through customer VPC"**:
  - Enforcement runs inside the guest, only on images with `CAP_NET_ADMIN`.
  - `rayito-base` (no capability) reports `EGRESS_ENFORCEMENT_NONE`. The SDK
    then terminates the just-launched VM and raises `UnimplementedError`
    naming `rayito-base-caps`, or the customer VPC connector of
    `infra/egress-connector.yaml` as the platform alternative. It does this
    even with `keep_on_failure=True`.
- **Routes mode (CIDR/IP entries only)**:
  - A dedicated routing table (101/102, alternating), selected by
    `ip rule uidrange 1000-65535` at priority 150/151. That is after `local`
    (0) and after the untouched IMDS rule (100), so loopback and
    `get_host(port)` keep working.
  - The table holds `blackhole` routes for `deny_out \ allow_out`, computed
    as a pure CIDR set difference. This keeps E2B's semantics:
    - no `allow_out` means everything except `deny_out` is allowed;
    - allow beats deny;
    - `ALL_TRAFFIC` (`"0.0.0.0/0"`, the E2B value) covers IPv4 and IPv6.
- **Proxy-only mode (any hostname entry, exact or `*.suffix`, or an
  `egress_proxy`)**:
  - Every direct route for uid ≥ 1000 is blackholed.
  - `rayd` serves an HTTP CONNECT + absolute-form HTTP + SOCKS5 forward
    proxy on `127.0.0.1:<ephemeral>`. It is exported to every process, PTY
    and kernel through `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` (upper and
    lower case).
  - The proxy decides on the hostname (ports 80/443, as E2B does) and on
    the resolved IPs.
  - Whatever the rules say, it never dials loopback, link-local (including
    `169.254.169.254` and `fd00:ec2::254`), unspecified, multicast,
    broadcast or the guest's own interface addresses.
  - It chains to the operator's SOCKS5 upstream (RFC 1928/1929) when
    `egress_proxy` is set. Those credentials arrive only in the
    token-authenticated RPC, are zeroized and are never logged or echoed.
  - Clients that ignore the proxy variables fail closed. This is a
    documented divergence from E2B's transparent filtering.
- **Delivery**:
  - The `runHookPayload` gains `"network":{"enforce":true}` (no rules, no
    credentials). With it, `rayd` installs deny-all and starts the local
    proxy **before** answering `/run`.
  - `create()` then sends `NetworkService.UpdateNetwork` with the real
    policy before returning, so no user code runs unprotected.
  - `update_network` is an atomic swap: fill the new table, add the new
    rule, delete the old rule, flush the old table. Any failure after the
    switch falls back to deny-all.
- **Verification**:
  - A route probe (`ip route get <addr> uid 1000`, no packets sent) checks
    the rule, the table size and sample destinations.
  - `rayd` publishes the result as `HealthResponse.egress_enforcement`
    (field 13) and re-verifies it synchronously on `/resume`.
- **Proto (applied by the Contract agent, exact delta in design D2)**:
  - A new `proto/rayito/v1/network.proto` with `NetworkService {UpdateNetwork,
    GetNetwork}`, `EgressEnforcement`, `EgressProxy`, `NetworkPolicy`,
    `UpdateNetworkRequest`, `GetNetworkRequest` and `NetworkState`.
  - `health.proto` imports it and adds `egress_enforcement = 13`.
  - `FAILED_PRECONDITION` without `CAP_NET_ADMIN`; `INVALID_ARGUMENT` for
    malformed entries.
- **SDKs** (Python sync and async identical over `_network_base.py`;
  TypeScript camelCase mirror):
  - `create(network=, allow_internet_access=)`.
  - `sbx.update_network(...)` and the class variant
    `Sandbox.update_network(sandbox_id, ...)`.
  - `sbx.get_network()`, `NetworkPolicy`, `EgressProxy`, `NetworkState`,
    `EgressEnforcement` and `ALL_TRAFFIC`.
  - Selector callables are evaluated SDK-side with `ctx.all_traffic`.
  - A native `UnimplementedError(NotImplementedError)`, shared with the
    other M9 changes.
  - The `rayito.e2b` shim maps `allow_internet_access`, `network`,
    `beta_create(network=)` and `update_network`.
  - `network.https_ports` follows a new measurement: accepted as a no-op if
    the AWS proxy reaches an app port serving TLS, `UnimplementedError`
    otherwise.
- **Docs**:
  - `ARCHITECTURE.md` gains ADR-012 plus a "Política de egress" subsection.
  - `SECURITY.md` updates T8 and adds a new T17: the root proxy as an SSRF
    surface; in-guest layers fall to a guest-kernel exploit or to root; DNS
    under deny-all on caps is a known residual risk (the platform resolvers
    listen inside the guest; ADR-012 addendum, option C).
  - `AWS_API_NOTES.md`:
    - §2 gains `HTTP_INGRESS` as the default ingress, `NO_EGRESS` as
      nonexistent, and `[]` ≡ omitted.
    - §16 gains two measured rows (the guest resolver and route-probe
      behaviour; `https_ports`).
  - A new `docs/site/docs/network.md`, plus updates to `e2b-compat.md`,
    `SPEC.md`, `limits.json` and `MILESTONES.md`.

The deferred `SECURITY_AUDIT.md` §8 rows stay deferred and are not
regressed:

- The IMDS rule (C-05's uidrange family, table 100, priority 100) is
  untouched.
- The proxy's own-address guard keeps `rayd` (root) from becoming a deputy
  towards the hooks, so the future C-01 peer-uid authentication stays
  meaningful.

## Capabilities

### New Capabilities

None. The requirements extend existing capabilities.

### Modified Capabilities

- `egress-control`: ADDED requirements for the entry grammar and E2B
  semantics, the routes mode and atomic swap, the local proxy and its guard,
  the SOCKS5 upstream, `/run` delivery and `NetworkService`, the route
  probe and `Health.egress_enforcement`, the SDK fail-closed gate,
  `update_network`/`get_network`, log hygiene, the platform facts in
  `AWS_API_NOTES.md`, and the real-AWS acceptance.
- `e2b-compat`: MODIFIED "E2B create kwargs map to Rayito or warn"
  (`allow_internet_access=False` and `network=`), MODIFIED "E2B features
  without an AWS primitive raise UnimplementedError" (`beta_create(network=)`
  is no longer on that list), and ADDED "E2B network options and
  update_network map to the guest egress policy".
- `typescript-sdk`: ADDED "Egress policy surface in TypeScript".
- `security-docs`: ADDED "SECURITY.md documents the in-guest egress policy
  (T8 and T17)".
- `architecture-docs`: ADDED "ADR-012 records the in-guest egress policy".

## Impact

- **Proto** (Contract agent): `proto/rayito/v1/network.proto` (new) and
  `proto/rayito/v1/health.proto`. Regenerate:
  - `crates/rayito-proto/build.rs` (`PROTO_FILES` += `"network"`)
  - `clients/python/src/rayito/v1/network_pb2*.py` and `health_pb2*`
  - `clients/typescript/src/gen/rayito/v1/network_pb.ts` and `health_pb.ts`
- **rayd-core**:
  - a new module `network/` (`cidr`, `entry`, `policy`, `route_plan`,
    `swap`, `guard`, `proxy_protocol`, `proxy_env`, `probe`, `state`,
    `error`)
  - `run_payload.rs` (`network.enforce`)
  - `process/env.rs` (egress layer)
  - `health.rs` (`egress_enforcement`)
  - no new dependency
- **rayd**:
  - `adapters/ip_command.rs` (shared `ip` runner, extracted from
    `imds_block.rs`) and `adapters/egress_routes.rs`
  - `network/` (`manager.rs`, `proxy.rs`, `upstream.rs`, `stats.rs`)
  - `grpc/network.rs`, `grpc/health.rs`, `grpc/mod.rs`
  - `hooks/mod.rs` (`/run`, `/resume`), `code/manager.rs` (op envs),
    `process/`, `pty/`, `main.rs`
  - no new dependency: the interface addresses come from `ip -o addr show`
    through the shared runner (the `nix` `net` feature would pull a crate
    absent from `Cargo.lock`)
- **Python**:
  - `_network_base.py` (new), `_models.py`, `_payload.py`,
    `_sandbox_base.py`, `exceptions.py`
  - `sandbox_sync/main.py`, `sandbox_async/main.py`, `__init__.py`
  - `e2b/_compat.py`, `e2b/_sync.py`, `e2b/_async.py`, `e2b/exceptions.py`
  - unit fakes, `_limits.py` (generated)
- **TypeScript**:
  - `sandbox/network.ts` (new), `sandbox/sandbox.ts`, `sandbox/launch.ts`,
    `sandbox/readiness.ts`
  - `payload.ts`, `models.ts`, `errors.ts`, `index.ts`, `limits.ts`
    (generated)
  - `tests/unit/fake/network.ts`
- **Infra**: `.github/workflows/ci.yml` (root netns step on
  `ubuntu-24.04-arm`) and `limits.json`. No image or IAM change: `iproute`
  is already in the image for the IMDS block.
- **Docs**: `ARCHITECTURE.md`, `SECURITY.md`, `AWS_API_NOTES.md`,
  `SPEC.md`, `MILESTONES.md`, `docs/site/docs/network.md` (new),
  `e2b-compat.md`, `security.md` and `mkdocs.yml`.
- **E2E**: `clients/python/tests/e2e/test_m9_egress.py`,
  `clients/typescript/tests/e2e/egress.e2e.test.ts` and
  `crates/rayd/tests/m9_egress.rs`.
