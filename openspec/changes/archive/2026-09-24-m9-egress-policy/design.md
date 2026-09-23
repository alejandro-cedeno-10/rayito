## Context

**What E2B offers** (E2B SDK 2.51, `e2b/sandbox/sandbox_api.py` and
<https://docs.e2b.dev/network/internet-access.md>, read for this change):

- `allow_internet_access=False` behaves like `deny_out=["0.0.0.0/0"]`.
- `network.allow_out` accepts CIDRs, bare IPs and domain names (`example.com`,
  `*.example.com`). `network.deny_out` accepts CIDRs and IPs only ("Domain
  names are not supported for deny rules").
- "Allowed entries always take precedence over denied entries." When
  `allow_out` is not given, everything is allowed.
- `ALL_TRAFFIC == "0.0.0.0/0"`. Selectors may be callables that receive
  `ctx.all_traffic` (and `ctx.rules`).
- Domain rules apply only to HTTP on port 80 (Host) and TLS on port 443
  (SNI). A wildcard `*.example.com` matches subdomains at any depth but not
  the apex. UDP/QUIC is not domain-filtered.
- `egress_proxy = {address: "host:port", username?, password?}` is a SOCKS5
  upstream (RFC 1929 credentials, 255 bytes max). Traffic is tunneled
  **after** allow/deny filtering. Domain-matched flows use remote DNS
  (ATYP=domain), and the proxy fails closed.
- `update_network(network)` replaces the whole egress configuration: omitted
  fields are cleared. Instance, static `(sandbox_id, network)` and JS
  `updateNetwork` forms exist.
- `network.https_ports`: sandbox ports that serve TLS; the proxy
  re-encrypts to them without verifying the certificate.

**What the platform offers** (`AWS_API_NOTES.md`):

- There is no managed no-egress. §2 lists `INTERNET_EGRESS` as the only
  managed egress connector. Q44 and Q60 measured that omitting
  `egressNetworkConnectors` and passing `[]` both inherit `INTERNET_EGRESS`,
  and that `…:aws-network-connector:NO_EGRESS` answers `ValidationException`.
- There is no `UpdateMicrovm` (§1), so platform connectors are immutable.
- The platform alternative is a customer VPC connector
  (`infra/egress-connector.yaml`). It is validated but not measured (Q46,
  `SECURITY.md` T8).
- Q47/Q48: on `rayito-base-caps` root has the full `CapEff`, while uid 1000
  has `CapEff 0` and `NoNewPrivs 1`. The guest kernel has no `xt_owner`,
  but `ip rule … uidrange` works. The platform agent owns sockets as uids
  991–994 and keeps its own IMDS connection, so any per-uid rule must start
  at 1000. `rayd` already installs `ip -4 rule add uidrange 1000-65535
  lookup 100 priority 100` + `blackhole 169.254.169.254/32 table 100`, and
  the IPv6 pair best effort (`crates/rayd/src/adapters/imds_block.rs`).
- §15: all outbound non-local connections are killed on run and resume.
  Loopback TCP state survives.
- §9: `CLOCK_MONOTONIC` advances during suspend; routes and listeners live
  in the memory snapshot.

**What Rayito has today**:

- `rayito.e2b` raises `UnimplementedError` for `allow_internet_access=False`
  (`e2b/_compat.py` `NO_EGRESS_REASON`).
- `beta_create(network=)` raises too. `network=` and `update_network` do
  not exist in either SDK.
- The native `create(egress=[...])` passes platform connectors through.
- Every child environment is built from scratch
  (`rayd-core/src/process/env.rs` `build_child_env`, used by processes,
  PTYs and the sidecar).
- Kernels inherit the sidecar environment plus each context's op `envs`
  (`kernel-sidecar/.../kernels.py` `kernel_environment`).

**Coordination with the sibling M9 changes** (Scope phase):

- **Implementation order**: `m9-deno-kernels` ∥ `m9-file-transfer` →
  `m9-server-timeout` → `m9-sandbox-observability` → **`m9-egress-policy`**
  → `m9-e2b-v2-surface`.
- **Numbering**: ADR-010 and T16 belong to `m9-file-transfer`, ADR-011 to
  `m9-server-timeout`. This change takes ADR-012 and T17. `HealthResponse`
  field 12 is `lifecycle`, 13 is `egress_enforcement` (this change), and 14
  and 15 go to observability. The next free §16 rows at implementation
  time are named **QE1** (guest network facts) and **QE2** (`https_ports`)
  here. Task 0.1 maps them to real numbers.
- **Native `UnimplementedError`**: `m9-file-transfer` also needs a native
  `UnimplementedError`. Whichever change lands first adds
  `rayito.exceptions.UnimplementedError` (and the TS `UnimplementedError`).
  This change reuses it if present and adds it (D13/D15) if not.
- **Shim naming and exports**: `m9-e2b-v2-surface` owns the shim's 2.x
  `__all__`, `SandboxInfo.network`/`allow_internet_access` and the
  explicit rows for `network.rules`, `mask_request_host` and
  `allow_public_traffic`. This change raises minimal `UnimplementedError`s
  for those keys so nothing is ignored silently (D14). It exports
  `ALL_TRAFFIC` from `rayito` and `rayito.e2b` as importable names, without
  touching `rayito.e2b.__all__`.
- **Acceptance dependency**: the acceptance's "root traffic still works"
  check uses `files.download_url` from `m9-file-transfer`, which lands
  earlier.

## Goals / Non-Goals

**Goals**

- E2B's `allow_internet_access`, `allow_out`/`deny_out` (CIDR, IP,
  `ALL_TRAFFIC`, hostnames, selector callables), `egress_proxy` and
  `update_network` in both SDKs and in the Python shim, with E2B semantics
  wherever the guest can enforce them.
- Fail closed everywhere enforcement is impossible: the default image, an
  older agent, a failed install or a failed verification.
- A verifiable enforcement state (`Health.egress_enforcement`), a policy
  that survives suspend/resume, and log hygiene (decision counts only).
- `network.https_ports` decided by a measurement.

**Non-Goals**

- A platform-level no-egress. None exists (Q60); the VPC connector recipe
  stays the platform route, unchanged.
- Transparent (proxy-unaware) hostname filtering. It would need SNI/Host
  interception inside a separate network namespace, which is a large
  project; see the divergence note.
- `network.rules` transforms and credential injection, `mask_request_host`
  and `allow_public_traffic=True`. These are impossible on the platform
  (ledger) and owned by `m9-e2b-v2-surface`.
- UDP through the proxy (SOCKS5 `UDP ASSOCIATE`) and SOCKS5 `BIND`. They
  answer "command not supported".
- Port-scoped rules (E2B has none).
- Filtering root traffic (`rayd`, S3 checkpoint, presigned transfers) or
  processes started as root with `RAYITO_ALLOW_ROOT=1`.
- A pool-level network policy (`SandboxPool`); `create(pool=…, network=…)`
  is refused.
- The deferred `SECURITY_AUDIT.md` §8 rows (C-01, C-05 uidrange rules,
  C-07, …). They are not touched and not regressed (D9, D20).

## Decisions

### D1. ADR-012 and the shape of the mechanism

**ADR-012 "Política de egress en el guest sobre rayito-base-caps; conectores
de plataforma sólo por VPC del cliente"**:

- **Context**: Q44, Q60, Q48 and §1, as above.
- **Decision**: the guest enforces the policy through two layers that share
  one policy object in `rayd`.
  1. **Routes (kernel)**: a uid-scoped routing table decides every packet
     of uid 1000–65535.
  2. **Local forward proxy (`rayd`, root)**: it decides hostname rules and
     chains to the operator's SOCKS5 upstream.
- **Modes** (D4):
  - `Unrestricted`: no rule.
  - `Routes`: blackholes for `deny_out \ allow_out`.
  - `ProxyOnly`: blackhole of everything direct; only the proxy leaves.
- **Where it runs**: only where `CapEff` has `CAP_NET_ADMIN`. Elsewhere the
  SDK terminates the VM and raises `UnimplementedError`.
- **Consequences**:
  - In-guest layers fall to a guest-kernel exploit or to guest root (T17).
  - Proxy-unaware clients fail closed in proxy mode.
  - DNS for uid ≥ 1000 is not blocked on caps: QE1 measured the platform
    resolvers listening inside the guest, so names may resolve under
    deny-all while every connection outside the VM fails (D17, ADR-012
    addendum).
  - The platform alternative (customer VPC connector with a deny-all
    security group) stays the only out-of-guest control. Its DNS caveat
    applies: security groups never filter Amazon DNS.

### D2. Proto delta (the Contract agent applies it verbatim)

> **Status: applied by the Contract step (2026-09-22), verbatim.** Final numbers: new `network.proto` with `NetworkService.UpdateNetwork` and `GetNetwork`, `EgressEnforcement` (0-3), `EgressProxy` (1-3), `NetworkPolicy` (1-3), `UpdateNetworkRequest` (1), `GetNetworkRequest`, `NetworkState` (1-5); `HealthResponse.egress_enforcement = 13`. Compile stubs to replace: `NetworkGrpc` (unit struct in `crates/rayd/src/grpc/network.rs`, already registered in `grpc/mod.rs` behind the access-token layer) answers `UNIMPLEMENTED`, and `grpc/health.rs` reports `EGRESS_ENFORCEMENT_UNSPECIFIED`. Regenerated with `buf generate` (Python and TypeScript, byte-identical to the committed gencode for untouched protos) and `crates/rayito-proto/build.rs` (`PROTO_FILES` now lists `common`, `lifecycle`, `network`, `health`, `process`, `filesystem`, `pty`, `code`). `buf lint` passes with the unchanged `buf.yaml`, and `buf breaking --against` a copy of the pre-M9 `proto/` (FILE) reports nothing. Do not regenerate Python with `python scripts/gen_python.py`: grpcio-tools 1.84.0 emits protobuf 7.35.1 gencode plus a gRPC version-check preamble, which differs from the committed 7.36.1 output of `buf generate`.

New file `proto/rayito/v1/network.proto`:

```proto
syntax = "proto3";

package rayito.v1;

// Política de egress dentro del guest (ADR-012). Sólo se aplica en imágenes
// con CAP_NET_ADMIN (rayito-base-caps); nunca filtra el tráfico del propio
// rayd (root). Los dos RPCs exigen x-access-token.
service NetworkService {
  // Sustituye la política entera de forma atómica (semántica de
  // update_network de E2B: lo omitido se borra) y afecta a las conexiones
  // nuevas. INVALID_ARGUMENT si una entrada o el proxy están mal formados;
  // FAILED_PRECONDITION si la política exige restringir y la imagen no tiene
  // CAP_NET_ADMIN; INTERNAL si la instalación o la verificación fallan (rayd
  // deja deny-all instalado).
  rpc UpdateNetwork(UpdateNetworkRequest) returns (NetworkState);
  // Estado actual; nunca devuelve la dirección ni las credenciales del proxy.
  rpc GetNetwork(GetNetworkRequest) returns (NetworkState);
}

enum EgressEnforcement {
  // Agente anterior a M9: el SDK lo trata como NONE.
  EGRESS_ENFORCEMENT_UNSPECIFIED = 0;
  // Sin política en el guest: el egress es el del conector de la plataforma.
  EGRESS_ENFORCEMENT_NONE = 1;
  // Rutas de política para uid 1000-65535 instaladas y verificadas.
  EGRESS_ENFORCEMENT_GUEST_ROUTES = 2;
  // Todo el egress directo bloqueado; sólo sale lo que acepta el proxy local.
  EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY = 3;
}

// Proxy SOCKS5 del operador ("bring your own proxy"). Las credenciales sólo
// viajan en UpdateNetworkRequest; rayd las borra de memoria al sustituirlas y
// nunca las registra ni las devuelve.
message EgressProxy {
  // host:puerto o [IPv6]:puerto.
  string address = 1;
  // RFC 1929, hasta 255 bytes.
  optional string username = 2;
  // RFC 1929, hasta 255 bytes; sólo junto a username.
  optional string password = 3;
}

message NetworkPolicy {
  // CIDR, IP, "0.0.0.0/0" (ALL_TRAFFIC: IPv4 e IPv6) o nombre de host
  // exacto o "*.sufijo" (sólo puertos 80 y 443, a través del proxy local).
  // Una entrada permitida gana siempre a una denegada.
  repeated string allow_out = 1;
  // CIDR o IP; los nombres de host no se admiten.
  repeated string deny_out = 2;
  optional EgressProxy egress_proxy = 3;
}

message UpdateNetworkRequest {
  NetworkPolicy policy = 1;
}

message GetNetworkRequest {}

// La política tal como se envió (listas en el orden recibido) y cómo se
// aplica. Nunca incluye la dirección ni las credenciales del proxy.
message NetworkState {
  repeated string allow_out = 1;
  repeated string deny_out = 2;
  bool egress_proxy_configured = 3;
  EgressEnforcement enforcement = 4;
  // Puerto del proxy local en 127.0.0.1; 0 si no está corriendo.
  uint32 local_proxy_port = 5;
}
```

`proto/rayito/v1/health.proto` gains `import "rayito/v1/network.proto";`
next to the `lifecycle.proto` import of `m9-server-timeout`, and this field
in `HealthResponse`:

```proto
  // Egress en el guest verificado por la sonda de rutas de rayd (ADR-012).
  // UNSPECIFIED en agentes anteriores a M9 (el SDK lo trata como NONE).
  EgressEnforcement egress_enforcement = 13;
```

**Contract checks and regeneration**:

- `buf lint` stays clean with the existing `buf.yaml`: the RPC
  request/response naming exceptions already cover `NetworkState` as a
  shared response.
- `buf breaking --against` the pre-change tree stays clean (FILE rules;
  everything is additive).
- `crates/rayito-proto/build.rs` `PROTO_FILES` gains `"network"` (the array
  grows to include `lifecycle` and `network`).
- `python scripts/gen_python.py` and `buf generate` regenerate the Python
  and TS stubs. Nobody hand-writes a request or response type.

**Not proto**: the `runHookPayload` gains an optional
`"network":{"enforce":true}` (hand-written JSON contract in
`run_payload.rs`, D7). It never carries rules or credentials.

### D3. Entry grammar and evaluation semantics (`rayd-core/src/network/`)

All of this is pure, `thiserror` only, with no new dependency.

**`cidr.rs`**:

- `Cidr { network: IpAddr, prefix: u8 }`, normalized so that host bits are
  masked. `Cidr::parse` accepts:
  - `a.b.c.d` → /32;
  - `a.b.c.d/n` with `0 ≤ n ≤ 32`;
  - an IPv6 literal → /128;
  - an IPv6 literal `/n` with `0 ≤ n ≤ 128`;
  - brackets are not accepted.
- An IPv4-mapped IPv6 (`::ffff:a.b.c.d[/n]`) with `n ≥ 96` is converted to
  the IPv4 CIDR. Any other IPv4-mapped prefix is `InvalidEntry`.
- Methods: `contains(IpAddr) -> bool` (canonicalizes IPv4-mapped
  addresses and, since the independent review of 2026-09-24, the
  deprecated IPv4-compatible `::a.b.c.d` too, via `Ipv6Addr::to_ipv4` as
  the transfer URL policy does; `::` and `::1` stay IPv6; `covers` between
  two prefixes never folds, so an IPv6 entry written inside `::/96` keeps
  its place in the IPv6 arithmetic), `family()`, and `subtract(minuend: &[Cidr], subtrahend:
  &[Cidr]) -> Vec<Cidr>`. `subtract` works per family, returns a minimal,
  sorted, non-overlapping cover of `minuend \ subtrahend`, and splits a
  prefix into its two halves recursively only where a subtrahend
  intersects it.

**`entry.rs`**:

- `EgressEntry::Networks(Vec<Cidr>)`: the literal `0.0.0.0/0`
  (`ALL_TRAFFIC`) becomes `[0.0.0.0/0, ::/0]`; `::/0` is IPv6 only; any
  other CIDR or IP is itself.
- `EgressEntry::Host(HostPattern)`, with `HostPattern::Exact(String)` or
  `HostPattern::Subdomains(String)` (from `*.suffix`):
  - The name is lowercased and a trailing `.` stripped.
  - LDH labels of 1–63 characters, 253 characters in total, at least two
    labels.
  - `*` only as the whole first label, and never a bare `*`.
  - Non-ASCII names are `InvalidEntry` (no IDNA).
  - `matches(name)`: `Exact` compares equal. `Subdomains(s)` matches
    `x.s` at any depth and never `s` itself (E2B apex rule).
- An entry that is none of these is `NetworkError::InvalidEntry { list,
  index }`. A hostname in `deny_out` is
  `NetworkError::HostnameInDenyOut { index }`. Messages carry the list name
  and the index, never the entry text.

**`policy.rs`**: `EgressPolicy::parse(PolicyInput) -> Result<EgressPolicy,
NetworkError>`.

- `PolicyInput { allow_out: Vec<String>, deny_out: Vec<String>, upstream:
  Option<UpstreamInput> }` is the tonic-free mirror filled by the gRPC
  adapter.
- Caps (D16): at most 256 entries per list and 64 hostname entries.
- The policy keeps:
  - `raw_allow` / `raw_deny`, echoed verbatim in `NetworkState`;
  - `allow_nets`, `deny_nets`, `allow_hosts`;
  - `upstream: Option<UpstreamProxy>` (D10).

**Evaluation rules** (E2B semantics):

1. **Normalization**: when `deny_out` is empty, the allow sets are ignored
   for evaluation, because under "default allow" they cannot change a
   verdict. They are still echoed. So `allow_out` alone never triggers
   enforcement or proxy mode.
2. **`ip_verdict(ip)`**: `Allow` if any `allow_nets` contains `ip`, else
   `Deny` if any `deny_nets` contains `ip`, else `Allow`. The IP is
   canonicalized first, so IPv4-mapped addresses follow IPv4 rules.
3. **`deny_by_default()`**: `deny_nets` contains both `0.0.0.0/0` and
   `::/0`, which is what `ALL_TRAFFIC` yields.
4. **`mode()`**:
   - `Unrestricted` when `deny_nets` is empty and `upstream` is `None`;
   - `ProxyOnly` when `upstream` is set, or when `deny_nets` is non-empty
     and `allow_hosts` is non-empty;
   - `Routes` otherwise.
5. **`requires_enforcement()`**: `mode() != Unrestricted`. The SDKs mirror
   this as "`deny_out` non-empty or `egress_proxy` set" (D13).

### D4. Routes: tables, priorities, plan (`route_plan.rs`)

**Fixed constants** (`network/route_plan.rs`):

| Slot | Table | Rule priority | Use |
|---|---|---|---|
| `A` | `101` | `150` | active or next policy |
| `B` | `102` | `151` | active or next policy |
| `Emergency` | `103` | `149` | transient deny-all during recovery (D5) |

- The selector is always `uidrange 1000-65535` (`SANDBOX_UID_RANGE`,
  shared with `imds_block.rs`).
- Priorities 149–151 sit after `local` (0) and after the IMDS rule (100,
  table 100), and before `main` (32766).
- A lookup that finds no route in our table falls through to `main`. A
  lookup in table 100 for a non-IMDS destination also finds nothing and
  continues. So the IMDS block and our table compose without touching each
  other. The IMDS rule and table 100 are **never** modified by this change.

`RoutePlan { v4: Vec<Cidr>, v6: Vec<Cidr> }` holds the blackhole prefixes.
`RoutePlan::for_policy(&EgressPolicy, ipv6_present: bool) ->
Result<Option<RoutePlan>, NetworkError>`:

- `Unrestricted` → `None`: no rule, no table.
- `ProxyOnly` → `v4 = [0.0.0.0/0]`, `v6 = [::/0]`.
- `Routes` → `v4 = subtract(deny_v4, allow_v4)`, `v6 = subtract(deny_v6,
  allow_v6)`. Only blackholes are needed: after the subtraction, anything
  unmatched falls through to `main` (allowed), and allow-over-deny holds
  even when the allow prefix is broader than the deny prefix, which
  longest-prefix-match `throw` routes would get wrong.
- More than 4096 prefixes in one family is `NetworkError::PolicyTooComplex`
  (`INVALID_ARGUMENT`).
- Without IPv6 (`ipv6_present == false`), `v6` is dropped. `ipv6_present`
  is decided by the adapter as "`/proc/net/if_inet6` exists". When IPv6 is
  present, IPv6 steps are mandatory: a failure is a failure, never a best
  effort.

### D5. Atomic swap, rollback and deny-all recovery (`swap.rs` + adapter)

The swap sequence is a pure planner producing `Vec<RouteStep>`; the adapter
executes it.

- `RouteStep` is one of `FillTable { table, family, prefixes }` (flush and
  refill), `FlushTable { table, family }`, `AddRule { priority, table,
  family }` or `DelRule { priority, table, family }`.
- `plan_swap(active: Option<Slot>, next: Option<&RoutePlan>) ->
  SwapPlan { steps, next_slot }`. `next_slot` is the slot that is not
  active (`A` when none is).
- Step order, for each present family in the order v4 then v6:
  1. `FillTable(next)`
  2. `AddRule(next)`
  3. `DelRule(active)`
  4. `FlushTable(active)`
- To `Unrestricted`, only `DelRule(active)` and `FlushTable(active)` run.
- At every instant after step 2, the uid range resolves through a complete
  table, the old one or the new one. The kernel matches the lowest
  priority first, and `A`/`B` alternate 150/151, so "old still wins until
  deleted" or "new wins as soon as added". Never a window without a rule.

**Failure handling**:

- A failure in step 1 is rolled back with `FlushTable(next)`. The old
  policy stays active and the RPC answers `INTERNAL`
  `egress_update_failed` with the step name.
- A failure at or after step 2 runs `plan_recovery()`:
  1. `FillTable(103, deny-all)`, then `AddRule(149, 103)`. Immediate
     deny-all for uid ≥ 1000.
  2. `DelRule` and `FlushTable` on both slots. Errors from absent rules
     are ignored.
  3. `FillTable(101, deny-all)` + `AddRule(150, 101)`.
  4. `DelRule(149)` + `FlushTable(103)`.
  After recovery the state is deny-all in slot `A`
  (`deny_out = ["0.0.0.0/0"]`, enforcement `GUEST_ROUTES` once verified)
  and the RPC answers `INTERNAL` `egress_update_failed`. If recovery itself
  fails, `rayd` logs `egress_recovery_failed` and enforcement becomes
  `NONE`. The SDK treats that as fatal at `create()` (D13).

**Adapter execution**:

- The adapter is `crates/rayd/src/adapters/egress_routes.rs`, over a shared
  runner extracted from `imds_block.rs` into `adapters/ip_command.rs`
  (`ip <family> <args>` with the `IP_BINARIES` fallback, stdout and stderr
  captured, never logged). stderr is read only to recognise the fixed
  "FIB table does not exist" answer of `ip route flush|show table N` on a
  table that was never created, which the adapter treats as an empty table
  (`rayd_core::network::probe::table_missing`, tasks 3.9). `imds_block.rs` switches to the shared
  runner with no behaviour change; its tests stay green.
- `FillTable` runs `ip -4|-6 route flush table N` and then `ip -4|-6 -batch
  -` with one `route add blackhole <prefix> table N` line per prefix. The
  batch fails on its first error.
- Rules are added with `ip -4|-6 rule add uidrange 1000-65535 lookup N
  priority P`, and deleted with the same arguments.

**Concurrency**: one `tokio::sync::Mutex` in `NetworkManager` serializes
`/run` enforcement, `UpdateNetwork`, the `/resume` re-verification and
recovery.

### D6. Route probe, enforcement state, Health and resume (`probe.rs`)

A verification passes only when all checks hold:

- **Rule check**: `ip -4 rule show` (and `-6` when present) contains
  `<P>:` + `from all uidrange 1000-65535 lookup <T>` for the active slot,
  and no rule of ours at the other priority. The pure parser is
  `probe::rule_present(stdout, slot)`.
- **Table check**: `ip -4|-6 route show table <T>` has exactly
  `plan.v4.len()` / `plan.v6.len()` lines starting with `blackhole`.
- **Samples**: `probe::samples(&policy, &plan) -> Vec<(IpAddr,
  RouteExpect)>`, each checked with `ip route get <addr> uid 1000`. No
  packet is sent.
  - `127.0.0.1` → `Local`.
  - Up to three blackholed prefixes (the first ones of `v4`, then `v6`):
    the network address itself for /32 and /128, otherwise network + 1 →
    `Blocked`.
  - When the plan does not deny all of IPv4, the first of `1.1.1.1`,
    `8.8.8.8`, `9.9.9.9` and `208.67.222.222` whose `ip_verdict` is
    `Allow` and that no plan prefix covers → `Routable`.
  - In `ProxyOnly` the samples are `127.0.0.1` → `Local` and `1.1.1.1` →
    `Blocked`, plus `2606:4700:4700::1111` → `Blocked` when IPv6 is
    present. A TCP self-connect to `127.0.0.1:<local_proxy_port>` must
    also succeed.
- **Parsing `ip route get`**: the pure `probe::classify_route_get(exit_code,
  stdout) -> RouteVerdict`.
  - `Blocked` when the exit code is non-zero, or when stdout starts with
    `blackhole`, `unreachable` or `prohibit`.
  - `Local` when stdout starts with `local `.
  - `Routable` when stdout contains ` dev ` and none of the above.
  - `Unknown` otherwise, which fails verification (fail closed).
  - QE1 records the real output forms on the guest.

**Enforcement state** (`network/state.rs`):

- `EgressEnforcement { Unspecified, None, GuestRoutes,
  GuestRoutesAndProxy }` mirrors the proto.
- `NetworkManager` keeps it in an atomic that `grpc/health.rs` reads into
  `HealthSnapshot.egress_enforcement` (a new field in `rayd-core/src/health.rs`).
- Values by mode after a passing verification: `Unrestricted` → `None`,
  `Routes` → `GuestRoutes`, `ProxyOnly` → `GuestRoutesAndProxy`.
- A failed verification runs recovery (D5) and answers `INTERNAL`
  `egress_verify_failed`.
- Before any policy exists, or on an image without `CAP_NET_ADMIN`, the
  value is `None`.

**`/resume`**:

- The hook handler runs `NetworkManager::reverify_after_resume()`
  **synchronously**, before answering 200, within a 3 s sub-budget of the
  `/resume` budget. It first refreshes the guest's own addresses (D9),
  then verifies. On failure it reinstalls the stored plan with `plan_swap`,
  and falls back to recovery if that fails.
- Timing out the sub-budget sets enforcement `None`, logs
  `egress_resume_verify_timeout`, and still answers 200: a non-200 hook
  answer is never used. The SDK then sees `None` in `Health` (D13).
- Routes and the proxy listener live in the memory snapshot, so the
  expected path is "verified, no change". Tunnels through the proxy die
  with every other outbound connection on resume (§15).

### D7. Delivery at `/run`, proxy start and environment export

**`run_payload.rs`**:

- `RunPayloadWire` gains `network: Option<NetworkWire { enforce:
  Option<serde_json::Value> }>`.
- `enforce` absent → `false`; a JSON bool → that value; anything else →
  `RunPayloadError::InvalidNetwork`. That error puts `rayd` in "sin token"
  mode like every other payload error: a malformed payload must not be
  half-honoured.
- Unknown keys inside `network` are ignored. `RunPayload` gains
  `network_enforce: bool`, and `v` stays 1.
- An older agent ignores the key, which is why the SDK gate reads `Health`
  and never trusts the payload.

**`/run`** (`crates/rayd/src/hooks/mod.rs`), after the payload is parsed and
installed and **before** the 200, when `network_enforce` is set:

1. Without `CAP_NET_ADMIN`: log `egress_enforce_unavailable reason="no
   CAP_NET_ADMIN"`, leave enforcement `None`, answer 200.
2. Otherwise `NetworkManager::enforce_deny_all_at_run()`, under a 1.5 s
   sub-budget:
   - start the local proxy (bind `127.0.0.1:0`, D8);
   - install the deny-all policy (`deny_out = ["0.0.0.0/0"]`, `Routes`
     mode, slot `A`);
   - verify (D6).
   Any failure or timeout runs recovery once. If the result is still
   unverified, enforcement is `None` and `rayd` logs
   `egress_enforce_failed step=<fixed step name>`. In every case the hook
   answers 200 (a 4xx would kill the VM without a diagnosis,
   ARCHITECTURE "Lifecycle hooks").
3. Only then 200. The kernel rotation that follows the 200 already sees the
   proxy variables.
4. Measured on AWS (QE1 row): the platform lets `Health` through while
   `/run` is still running, and the first `ip` after the snapshot restore
   took 2.76 s, past the sub-budget. So accepting a payload with
   `network.enforce` marks the session as settling, and `Health` reports
   `agent_ready = false` until the deny-all task finishes (verified or
   not), or at once without `CAP_NET_ADMIN`. The SDK readiness therefore
   never reads the `None` of an unfinished install.
5. Independent review (2026-09-24): the flag is cleared by a drop guard
   owned by the spawned task, so a task that panics (or is dropped with
   the runtime) still ends the settling, with `None` published; and every
   `ip` invocation of the shared runner is bounded by
   `IP_COMMAND_TIMEOUT` = 5 s (above the 2.76 s measured cold `ip` and the
   3 s `/resume` sub-budget), after which the child is killed and reaped
   and the call fails as `timed out`. A hung `ip` therefore can no longer
   hold the manager's lock, and `Health` not ready, indefinitely.

**Local proxy lifecycle**:

- The proxy starts at the first of: `/run` with `enforce`, or the first
  `UpdateNetwork` that `requires_enforcement()` on a capable image. It
  never stops for the rest of the boot.
- `UpdateNetwork` to `Unrestricted` leaves it running with a policy that
  only applies the guard. Processes that already hold the variables keep
  working.
- Without `CAP_NET_ADMIN` the proxy never starts and nothing is exported.

**Environment export**: `rayd-core/src/network/proxy_env.rs`
`egress_proxy_env(port) -> BTreeMap<String, String>` returns:

- `HTTP_PROXY` = `HTTPS_PROXY` = `http://127.0.0.1:<port>`;
- `ALL_PROXY` = `socks5h://127.0.0.1:<port>`;
- `NO_PROXY` = `localhost,127.0.0.1,::1`;
- the same four keys in lower case.

Once the proxy is up, `NetworkManager` holds the map in a `RwLock`
(`EgressEnv`). It reaches children as follows:

- **Processes and PTYs**: `build_child_env(identity, egress_env,
  sandbox_envs, request_envs)` gains the egress layer between the identity
  variables and the payload `envs`, so payload and request `envs` can still
  override it. Overriding is harmless: a direct connection is still decided
  by the routes. All six call sites are updated. The sidecar spec passes an
  empty map.
- **Kernels**: the code manager (`crates/rayd/src/code/manager.rs`) merges
  the current egress map **under** the context `envs` of every
  `CreateContext`, `RestartContext`, `/run` rotation and post-resume
  restart op it sends, for every language. The Python-only rule of
  `require_python_for_envs` keeps applying to user-supplied envs only. The
  sidecar's `kernel_environment` already layers op envs over its own
  environment, so no sidecar code changes (a pin test covers it, tasks
  §3).
- **Sidecar**: the sidecar itself never receives the variables. It starts
  at boot, before `/run`, and it never dials out (ADR-002).
- **Existing children**: a process, PTY or kernel spawned **before** the
  proxy started (only possible when the first enforcing policy arrives by a
  later `update_network`) keeps its environment. The routes still apply to
  it; hostname rules do not reach it unless it sets the variables itself.
  This is documented in `network.md`: to cover the default kernel, pass
  `network=` at `create()`.

### D8. The local forward proxy (`crates/rayd/src/network/proxy.rs`)

**Listener**:

- One `tokio::net::TcpListener` on `127.0.0.1:0`. The port is reported as
  `NetworkState.local_proxy_port` and used in the environment.
- A `tokio::sync::Semaphore` of 128 concurrent client connections (rayd's
  `RLIMIT_NOFILE` is 1024, §9). Beyond that, an HTTP client gets
  `HTTP/1.1 503 Service Unavailable` and a SOCKS client gets reply `0x01`,
  then the connection is closed.

**Protocol sniffing**: the proxy reads the first byte within 10 s. `0x05`
means SOCKS5; anything else means HTTP/1.x. The pure parsers live in
`rayd-core/src/network/proxy_protocol.rs`.

**HTTP**:

- The head is read until `\r\n\r\n`, 16 KiB max (`431` beyond), with a 10 s
  total head timeout (`408`).
- `parse_http_request(head) -> Result<HttpProxyRequest, ProtocolError>`
  returns one of:
  - `Connect { target }` for `CONNECT host:port HTTP/1.1` (authority-form,
    `[v6]:port` accepted);
  - `Forward { target, head }` for absolute-form `http://host[:port]/path`
    with any method. The head is rewritten to origin-form. `Proxy-*`
    headers and any existing `Connection` header are dropped, and
    `Connection: close` is added, so one proxied request is handled per
    client connection.
  - `Host` (independent review, 2026-09-24): every client `Host` line is
    dropped and `Host: <authority of the request-target>` is written
    first, the authority D9 decided on (RFC 9112 §3.2.2 requires a proxy
    to ignore the received `Host` for an absolute-form target). Keeping
    the client's value let an allowed name front another virtual host on
    the same address. Overwriting was chosen over a `403` on mismatch:
    it is what the RFC prescribes, it needs no port or case normalisation
    to compare (`example.com` vs `example.com:80`), and conforming clients
    already send the same value, so none breaks. Obs-fold continuation
    lines (leading SP/HTAB) are refused with `400`, so none can re-attach
    to a dropped line.
  - `CONNECT` and SOCKS5 tunnels carry TLS the proxy does not inspect: the
    SNI and the inner `Host` are the client's, so a name allowed on 443
    can front another name served from the same address (shared CDN
    front ends). This residual is stated in ARCHITECTURE.md (egress) and
    SECURITY.md T17.
- Absolute-form `https://`, origin-form and malformed requests answer
  `400`.
- Bytes already read past the head are forwarded after the connect.
- Responses: `403 Forbidden` (policy or guard deny, body
  `rayito: destino bloqueado por la política de egress\n`), `502 Bad
  Gateway` (resolution, dial or upstream failure), `504 Gateway Timeout`
  (10 s connect timeout), `200 Connection established` (CONNECT success).
  These are fixed byte strings from `proxy_protocol::http_response`.

**SOCKS5** (RFC 1928):

- The greeting must offer method `0x00`; otherwise the proxy answers
  `0x05 0xFF` and closes.
- Only `CONNECT` (`0x01`) is supported; `BIND` and `UDP ASSOCIATE` answer
  `0x07`. ATYP IPv4, domain (1–255 bytes) and IPv6 are accepted; anything
  else answers `0x08`.
- Replies: `0x00` success (bound address `0.0.0.0:0`), `0x02` not allowed
  by ruleset (policy or guard), `0x04` host unreachable (resolution
  failed), `0x05` connection refused, `0x06` TTL expired (connect
  timeout), `0x01` general failure (upstream error).

**After a successful dial**: `tokio::io::copy_bidirectional` until either
side closes. There is no idle timeout on established tunnels, so long
downloads keep working.

**Test seams**: `Resolve` (default `tokio::net::lookup_host`) and `Dial`
(default `TcpStream::connect` with a 10 s timeout) are adapter-internal
traits, not `rayd-core` ports. Unit tests inject a resolver that returns
documentation addresses (`203.0.113.x`) and a dialer that maps them to
local listeners, so the guard runs on realistic addresses.

**Policy updates**: the policy object is shared as `RwLock<Arc<ProxyPolicy>>`
and read once per connection. An `UpdateNetwork` affects new connections
only; existing tunnels continue.

### D9. Decision algorithm and target guard (`policy.rs`, `guard.rs`)

**`TargetGuard::new(local_addresses)`**: `blocks(ip)` is true, after
IPv4-mapped and IPv4-compatible canonicalization (D3), for:

- loopback (`127.0.0.0/8`, `::1`);
- unspecified (`0.0.0.0/8`, `::`);
- IPv4 link-local `169.254.0.0/16`, which includes IMDS `169.254.169.254`;
- IPv6 link-local `fe80::/10` and `fd00:ec2::254/128` (IMDS);
- multicast (`224.0.0.0/4`, `ff00::/8`) and the broadcast address
  `255.255.255.255`;
- every address currently assigned to a guest interface.

The adapter reads the interface addresses from `ip -o addr show` through the
shared `ip` runner, parsed by the pure
`rayd_core::network::probe::interface_addresses`, at every policy apply and
every `/resume`; a failed read fails the apply (`egress_update_failed:
local_addresses`), so the guard never runs without its list. (The first draft
used `nix::ifaddrs::getifaddrs`; the `nix` `net` feature enables `socket`,
which pulls `memoffset`, a crate that is not in `Cargo.lock`, so the runner
was used instead and no manifest changed, tasks 3.2.) The own-address rule keeps the proxy, which runs as root, from
dialing the guest's `0.0.0.0` listeners (`:8080`, `:9000`) on behalf of a
uid-1000 client. That preserves the premise of the deferred C-01 peer-uid
authentication. Names `localhost` and `*.localhost` are denied before
resolution.

**Decision** (target = host + port):

1. **IP-literal target**: guard blocks → `Deny(Guard)`. Otherwise
   `ip_verdict`: `Deny` → `Deny(Policy)`, else `ConnectIp`. With an
   upstream, the connection goes to the upstream as ATYP IPv4/IPv6.
2. **Hostname target**, normalized as in D3 (invalid → `Deny(Invalid)`,
   i.e. 400 / SOCKS `0x02`):
   1. When the port is 80 or 443 and an `allow_hosts` pattern matches:
      - with an upstream → `ForwardByName`: ATYP domain, no local
        resolution (E2B "remote DNS");
      - without one → `ResolveByName`: resolve, drop guarded addresses,
        no `ip_verdict` (the name is allowed, and allow beats deny).
   2. Otherwise, when `deny_by_default()` → `Deny(Policy)` **without
      resolving**. The proxy never turns a denied name into a DNS query,
      which closes the DNS-exfiltration path through `rayd`'s resolver
      (T17).
   3. Otherwise `ResolveChecked`: resolve, drop guarded addresses, keep the
      ones whose `ip_verdict` is `Allow`. With an upstream, the chosen IP
      goes to it as ATYP IPv4/IPv6, so the upstream cannot resolve to a
      denied address.
3. **After resolution** (`select_addresses`): if no address is left, the
   result is `Deny(Guard)` when every address was guarded, else
   `Deny(Policy)`. Otherwise the addresses are tried in resolver order;
   the first successful dial wins. The dial goes to exactly the checked
   `SocketAddr`, never re-resolved, so there is no DNS-rebinding window.

**Counters**: every decision increments `DecisionCounters` (D12). No target
is ever logged.

### D10. Operator SOCKS5 upstream (`crates/rayd/src/network/upstream.rs`)

**Parsing** (`policy.rs`): `UpstreamProxy::parse(UpstreamInput)`.

- `address` is `host:port` or `[v6]:port`, port 1–65535. The host is an IP
  literal or a hostname with the D3 rules.
- `username` and `password` are 1–255 bytes each. A password without a
  username is `InvalidProxyCredentials`.
- Credentials are held as `ProxyCredentials { username: Zeroizing<String>,
  password: Zeroizing<String> }`, with a manual `Debug` that prints
  `ProxyCredentials(<redacted>)`. They are dropped (and zeroized) when the
  policy is replaced.

**`UpstreamGuard`** blocks loopback, unspecified, multicast,
`169.254.169.254/32` and `fd00:ec2::254/128` (after IPv4-mapped
canonicalization). Other link-local, private and own-interface addresses
are **allowed**: the upstream is operator configuration delivered only by
the token-authenticated RPC, and the acceptance runs its recording server
on the VM's own private address.

- At `UpdateNetwork`, an IP-literal upstream that the guard blocks is
  `ProxyForbiddenAddress`.
- A hostname upstream is resolved once within 5 s: unresolvable →
  `ProxyUnresolvable`; every address guarded → `ProxyForbiddenAddress`.
- At dial time the hostname is re-resolved and the first unguarded address
  is used (pinned for that connection, as E2B does).

**Handshake**:

- Greeting: `05 01 00`, or `05 02 00 02` with credentials. The upstream's
  method must be `00`, or `02` with credentials.
- RFC 1929 `01 ULEN USER PLEN PASS`; the status must be `00`.
- `CONNECT` with ATYP domain or IP (D9); the reply must be `00`.
- The whole handshake is bounded to 10 s.
- The proxy never retries directly: any failure answers `502` or SOCKS
  `0x01` to the client, and E2B's fail-closed behaviour holds.
- The byte encoders and decoders are pure functions in `proxy_protocol.rs`.

### D11. `NetworkService` RPCs and error mapping

`crates/rayd/src/grpc/network.rs`:

- It sits behind the same `AccessTokenLayer` and `ClientAbortLayer` as
  every non-Health service (`grpc/mod.rs` gains `add_service(
  NetworkServiceServer::new(...))`), and behind the same phase gate as the
  other unary RPCs.
- `UpdateNetwork` converts the proto into `PolicyInput`, then calls
  `NetworkManager::update(input)`: parse → capability check → plan → swap →
  verify.
- `GetNetwork` returns the current snapshot.
- Unary errors use standard gRPC codes, and messages never carry an entry,
  an address or a credential:

| Domain error | gRPC | Message |
|---|---|---|
| `InvalidEntry{list,index}` | `INVALID_ARGUMENT` | `allow_out[3]: no es un CIDR, una IP ni un nombre de host válido` |
| `HostnameInDenyOut{index}` | `INVALID_ARGUMENT` | `deny_out[1]: los nombres de host no se admiten en deny_out` |
| `TooManyEntries{list}` / `TooManyHostnames` / `PolicyTooComplex` | `INVALID_ARGUMENT` | fixed text with the limit |
| `InvalidProxyAddress` / `InvalidProxyCredentials` / `ProxyForbiddenAddress` / `ProxyUnresolvable` | `INVALID_ARGUMENT` | fixed text naming `egress_proxy` |
| `NoNetAdmin` (only when `requires_enforcement()`) | `FAILED_PRECONDITION` | `la imagen no tiene CAP_NET_ADMIN: la política de egress exige rayito-base-caps` |
| `InstallFailed{step}` | `INTERNAL` | `egress_update_failed: <step>` (fixed step names) |
| `VerifyFailed` | `INTERNAL` | `egress_verify_failed` |

- A non-enforcing policy (`Unrestricted`) on an image without
  `CAP_NET_ADMIN` is accepted and returns `enforcement NONE`, because there
  is nothing to enforce.
- `GetNetwork` never fails for lack of capability.

### D12. Logging hygiene and statistics

**Allowed log events and fields**:

- `egress_policy_applied`: `mode`, `allow_count`, `deny_count`,
  `hostname_count`, `routes_v4`, `routes_v6`, `proxy_configured`,
  `duration_ms`.
- `egress_verify`: `ok`, `samples`, `failed_check` (fixed name).
- `egress_enforce_unavailable`, `egress_enforce_failed`,
  `egress_update_failed`, `egress_recovery_failed`,
  `egress_resume_verify_timeout`: with a `step`/`reason` from a fixed set.
- `egress_proxy_started` with `port`.
- `egress_proxy_stats`, every 60 s and only when a counter changed:
  `allowed`, `denied_policy`, `denied_guard`, `denied_invalid`,
  `upstream_failed`, `dial_failed`, `rejected_busy`, `active`.

**Never logged**: target hostnames, IPs, ports of targets, the upstream
address, credentials, `ip` output or payload text.

A `rayd` unit test captures tracing output while the proxy handles
`CONNECT secret-host.example:443` with a policy that denies it, and while an
`UpdateNetwork` with `egress_proxy {address: "proxy.example:1080",
username: "u-marker", password: "p-marker"}` is applied. It asserts that
`secret-host`, `proxy.example`, `u-marker` and `p-marker` never appear. The
SDKs never log the policy lists, the proxy address or the credentials, and
the Python `EgressProxy.__repr__` hides the password.

### D13. Python SDK surface (sync and async identical)

**`rayito/_models.py`** (exported from `rayito`):

```python
ALL_TRAFFIC: Final = "0.0.0.0/0"

@dataclass(frozen=True)
class EgressProxy:
    address: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)

@dataclass(frozen=True)
class NetworkSelectorContext:
    all_traffic: str = ALL_TRAFFIC
    rules: Mapping[str, tuple[object, ...]] = field(default_factory=empty_rules)  # always empty

NetworkSelector = Sequence[str] | Callable[[NetworkSelectorContext], Sequence[str]]

class NetworkOptions(TypedDict, total=False):
    allow_out: NetworkSelector
    deny_out: NetworkSelector
    egress_proxy: EgressProxy | Mapping[str, str]

@dataclass(frozen=True)
class NetworkPolicy:
    allow_out: tuple[str, ...] = ()
    deny_out: tuple[str, ...] = ()
    egress_proxy: EgressProxy | None = None

class EgressEnforcement(StrEnum):
    UNSPECIFIED = "unspecified"
    NONE = "none"
    GUEST_ROUTES = "guest_routes"
    GUEST_ROUTES_AND_PROXY = "guest_routes_and_proxy"

@dataclass(frozen=True)
class NetworkState:
    allow_out: tuple[str, ...]
    deny_out: tuple[str, ...]
    egress_proxy_configured: bool
    enforcement: EgressEnforcement
    local_proxy_port: int | None   # None when 0
```

`SandboxHealth` gains `egress_enforcement: EgressEnforcement =
EgressEnforcement.UNSPECIFIED`, and `health_from_proto` fills it.

**`rayito/exceptions.py`** gains `UnimplementedError(NotImplementedError)`
with `feature: str` and `reason: str`, and the message `f"{feature} no está
disponible: {reason}"`. This applies unless `m9-file-transfer` already
added it. `rayito.e2b.exceptions.UnimplementedError` becomes a subclass of
it that keeps its E2B message, so `except rayito.UnimplementedError`
catches both.

**`rayito/_network_base.py`** (new, pure, shared by both trees):

- `resolve_selector(selector, ctx) -> tuple[str, ...]`: a callable is
  called with `ctx`. A result that is not a sequence of `str` is
  `InvalidArgumentException`.
- `resolve_network(network: NetworkPolicy | NetworkOptions | None, *,
  allow_internet_access: bool) -> NetworkPolicy`:
  - A mapping accepts only the three keys; any other key is
    `InvalidArgumentException` naming it.
  - `egress_proxy` as a mapping accepts only `address`, `username` and
    `password`.
  - `allow_internet_access=False` appends `ALL_TRAFFIC` to `deny_out` when
    absent (E2B equivalence).
- `requires_enforcement(policy) -> bool`: `deny_out` non-empty or
  `egress_proxy` is not `None`.
- `validate_policy_shape(policy)`: early client-side checks with the
  `_limits.py` constants: list sizes, non-empty strings, a hostname-shaped
  entry in `deny_out` (anything `ipaddress.ip_network(entry, strict=False)`
  rejects), credential byte lengths, a password without a username. `rayd`
  stays authoritative.
- `policy_to_proto(policy) -> network_pb2.NetworkPolicy` and
  `state_from_proto(resp) -> NetworkState`.
- `egress_gate_error(sandbox_id, enforcement, feature) -> UnimplementedError
  | None`: returns an error for `UNSPECIFIED` and `NONE`.
  - `feature` is `"allow_internet_access=False"` when the policy came only
    from that flag, else `"network"`.
  - The reason is the constant `EGRESS_UNAVAILABLE_REASON`: "la imagen no
    aplica política de egress en el guest (Health.egress_enforcement=NONE):
    usa una imagen M9 de rayito-base-caps (additionalOsCapabilities ALL) o,
    a nivel de plataforma, rayito.Sandbox.create(egress=[<ConnectorArn de
    infra/egress-connector.yaml>]); el sandbox se ha terminado".
- `network_rpc_error(exc) -> Exception`:
  - `FAILED_PRECONDITION` → `UnimplementedError("update_network",
    CAPS_REASON)`;
  - `UNIMPLEMENTED` → `UnimplementedError("update_network",
    OLD_AGENT_REASON)`, whose reason names "una imagen M9 de
    rayito-base-caps";
  - anything else → the existing unary mapping of `_transport.py`
    (`INVALID_ARGUMENT` → `InvalidArgumentException`, `INTERNAL` →
    `SandboxException`).
- `log_allow_only_notice()`: a one-time `logger.info` when `allow_out` is
  given without `deny_out` ("allow_out sin deny_out no restringe nada").

**`_payload.py`**: `build_run_hook_payload(..., network_enforce: bool =
False)` adds `"network": {"enforce": true}` only when true.
`_sandbox_base.build_launch_plan(..., network_enforce: bool)` passes it.

**`Sandbox.create`** (sync `sandbox_sync/main.py`; async
`sandbox_async/main.py` with `async`/`await`) gains `network:
NetworkPolicy | NetworkOptions | None = None` and `allow_internet_access:
bool = True`:

1. `policy = resolve_network(network, allow_internet_access=...)`, then
   `validate_policy_shape(policy)`, before any AWS call.
2. `pool=` together with a non-empty `network` or
   `allow_internet_access=False` is `InvalidArgumentException`. The two
   kwargs join `reject_launch_kwargs_with_pool`.
3. The payload carries `network_enforce=requires_enforcement(policy)`.
4. `run-microvm`, then `_open` (readiness). `_wait_until_ready` keeps the
   `SandboxHealth` that satisfied readiness in `self._readiness_health`.
5. When enforcement is required, `sandbox._apply_initial_network(policy,
   feature)`:
   - `egress_gate_error(...)` on `_readiness_health.egress_enforcement`;
   - otherwise `UpdateNetwork(policy)`, then a check that the returned
     `enforcement` is neither `NONE` nor `UNSPECIFIED`.
   On **any** exception in this step (gate, RPC error or Ctrl-C), the SDK
   closes the client, calls `terminate_quietly(plane, sandbox_id)` **even
   when `keep_on_failure=True`** (a VM whose requested policy is not
   enforced is never left running), and re-raises. RPC errors go through
   `network_rpc_error`.
6. `persist=` auto-restore runs after step 5 (root traffic is not
   filtered).

`LaunchOptions` gains `network: NetworkPolicy | None`, and `reincarnate()`
re-applies it through `create(network=launch.network)`.

**New methods**:

- `sbx.update_network(network: NetworkPolicy | NetworkOptions | None =
  None, *, allow_internet_access: bool | None = None, request_timeout:
  float | None = None) -> NetworkState`:
  - replaces the whole policy (`None` or `{}` → unrestricted);
  - `allow_internet_access=False` appends `ALL_TRAFFIC` to `deny_out`;
  - `True` or `None` leaves the lists as given;
  - errors through `network_rpc_error`;
  - the SDK logs nothing about the lists.
- The class variant through `@class_method_variant("_class_update_network")`:
  `Sandbox.update_network(sandbox_id, network=None, *,
  allow_internet_access=None, access_token=None, region=None,
  session=None, control_plane=None, transport=None, request_timeout=...)`.
  It is `connect(...)` (the access token comes from `require_access_token`,
  so `access_token=` or `RAYITO_ACCESS_TOKEN`), then `update_network`, then
  `close()`, never `kill()`.
- `sbx.get_network(*, request_timeout=None) -> NetworkState`.

**Exports**: `rayito.__all__` gains `ALL_TRAFFIC`, `EgressProxy`,
`EgressEnforcement`, `NetworkOptions`, `NetworkPolicy`,
`NetworkSelectorContext`, `NetworkState` and `UnimplementedError`.

### D14. `rayito.e2b` shim mapping

**`e2b/_compat.py`**:

- `map_create_kwargs(..., allow_internet_access: bool = True, network:
  Mapping[str, Any] | None = None)` stops raising for
  `allow_internet_access=False`. It forwards `allow_internet_access` and
  `network=map_network(network)` to the native `create()`.
- The shim keeps `egress=["INTERNET_EGRESS"]`: the platform connector stays
  and the guest policy enforces.
- `NO_EGRESS_REASON` is removed.

**`map_network(network) -> NetworkOptions | None`** (pure):

- Accepted keys:
  - `allow_out` and `deny_out`: passed through. Callables are wrapped so
    that they receive a `NetworkSelectorContext(all_traffic=ALL_TRAFFIC,
    rules={})`, which exposes E2B's `ctx.all_traffic` and `ctx.rules`
    attribute names.
  - `egress_proxy`: a dict with `address` and optional `username` and
    `password`.
  - `https_ports`, per QE2 (below).
  - `allow_public_traffic=False`: accepted as a no-op, because it is
    Rayito's permanent behaviour.
- Raising `UnimplementedError`, before any AWS call:
  - `rules` (feature `network.rules`);
  - `mask_request_host` (feature `network.mask_request_host`);
  - `allow_public_traffic=True` (feature `network.allow_public_traffic`).
  The minimal reasons say "sin primitiva en Lambda MicroVMs";
  `m9-e2b-v2-surface` refines the texts.
- Any other key → `TypeError("network: clave desconocida '<k>'")`, the shim's rule for unmapped E2B surface (and the wording `m9-e2b-v2-surface` uses). The native `resolve_network` keeps `InvalidArgumentException` for unknown keys.
- **`https_ports`**: a module constant `HTTPS_PORTS_SUPPORTED: Final[bool]`,
  set by task 7.2 from QE2.
  - `True`: a list of ints 1–65535 is accepted and ignored. `get_host(p)`
    already reaches the port; E2B's re-encryption is the AWS proxy's
    behaviour, as QE2 measured.
  - `False`: a non-empty list raises `UnimplementedError(
    "network.https_ports", <reason citing the QE2 row>)`.
  - An empty list is always accepted.

**`e2b/_sync.py` and `_async.py`**:

- `Sandbox.__init__` / `create` gain the keyword-only `network=`.
- `beta_create(network=)` maps like `create(network=)`. `auto_pause=` and
  `mcp=` keep raising until their own changes land.
- The native `UnimplementedError` from the gate is re-raised as the shim's
  `UnimplementedError(feature, reason)` with the same feature and reason.
- `sbx.update_network(network: Mapping[str, Any], **opts) -> None` accepts
  E2B's update keys `allow_out`, `deny_out`, `egress_proxy`, `rules` (→
  `UnimplementedError`) and `allow_internet_access`, and calls the native
  method (dropping its result, as E2B returns `None`).
- The class variant `Sandbox.update_network(sandbox_id, network, *,
  access_token=None, region=None, session=None, request_timeout=None) ->
  None` uses the native class variant.
- `ALL_TRAFFIC` is importable from `rayito.e2b` (module attribute re-exported
  from `rayito`). `rayito.e2b.__all__` is **not** changed here
  (`m9-e2b-v2-surface` owns it).

### D15. TypeScript surface (camelCase mirror)

- **`src/models.ts`**:
  - `export const ALL_TRAFFIC = "0.0.0.0/0";`
  - `NetworkSelectorContext { readonly allTraffic: string; readonly rules:
    ReadonlyMap<string, readonly unknown[]> }` (always empty);
  - `type NetworkSelector = readonly string[] | ((ctx:
    NetworkSelectorContext) => readonly string[])`;
  - `EgressProxyInput { readonly address: string; readonly username?:
    string; readonly password?: string }`;
  - `NetworkPolicyInput { readonly allowOut?: NetworkSelector; readonly
    denyOut?: NetworkSelector; readonly egressProxy?: EgressProxyInput }`;
  - `type EgressEnforcement = "unspecified" | "none" | "guest_routes" |
    "guest_routes_and_proxy"`;
  - `NetworkState { readonly allowOut: readonly string[]; readonly
    denyOut: readonly string[]; readonly egressProxyConfigured: boolean;
    readonly enforcement: EgressEnforcement; readonly localProxyPort:
    number | undefined }`;
  - `SandboxHealth` gains `egressEnforcement`.
- **`src/errors.ts`**: `UnimplementedError extends Error` with `feature`
  and `reason` (unless `m9-file-transfer` added it).
- **`src/sandbox/network.ts`** (new, pure helpers mirroring
  `_network_base.py`): `resolveNetwork`, `requiresEnforcement`,
  `validatePolicyShape`, `policyToProto`, `stateFromProto`,
  `egressGateError`, `networkRpcError` (Connect `FailedPrecondition` /
  `Unimplemented` → `UnimplementedError`, others through the existing
  error mapping).
- **`src/payload.ts`** and **`src/sandbox/launch.ts`**: `networkEnforce`
  produces `"network":{"enforce":true}`.
- **`src/sandbox/sandbox.ts`**:
  - `SandboxCreateOptions` gains `network?: NetworkPolicyInput` and
    `allowInternetAccess?: boolean`.
  - The create flow is the same as D13 steps 1–6, including termination
    with `keepOnFailure`.
  - Instance methods: `updateNetwork(network?: NetworkPolicyInput, opts?:
    { allowInternetAccess?: boolean; requestTimeoutMs?: number }):
    Promise<NetworkState>` and `getNetwork(opts?: { requestTimeoutMs?:
    number }): Promise<NetworkState>`.
  - Static: `Sandbox.updateNetwork(sandboxId: string, network:
    NetworkPolicyInput | undefined, opts?: StaticUpdateNetworkOptions):
    Promise<NetworkState>`, where `StaticUpdateNetworkOptions extends
    SandboxConnectOptions` plus `allowInternetAccess` and
    `requestTimeoutMs`. It connects, updates and closes, never kills.
  - `pool` together with `network` or `allowInternetAccess: false` →
    `InvalidArgumentError`.
- **`src/index.ts`** exports all of the above.

### D16. Limits (`limits.json` → `_limits.py`, `limits.ts`; `rayd-core` constants)

| Key | Value | Enforced by |
|---|---|---|
| `egressMaxEntriesPerList` | 256 | SDKs (early) and `rayd` |
| `egressMaxHostnameEntries` | 64 | SDKs and `rayd` |
| `egressHostnameMaxChars` | 253 | SDKs and `rayd` |
| `egressProxyCredentialMaxBytes` | 255 | SDKs and `rayd` |

`rayd` also has fixed constants that are not in `limits.json`:

- `EGRESS_MAX_ROUTES_PER_FAMILY` 4096;
- `LOCAL_PROXY_MAX_CONNECTIONS` 128;
- `PROXY_HEAD_MAX_BYTES` 16384;
- `PROXY_HEAD_TIMEOUT` 10 s;
- `PROXY_CONNECT_TIMEOUT` 10 s;
- `UPSTREAM_RESOLVE_TIMEOUT` 5 s;
- `RUN_ENFORCE_BUDGET` 1.5 s;
- `RESUME_VERIFY_BUDGET` 3 s.

A `rayd-core` unit test reads `../../limits.json` with `include_str!` and
checks that the four shared values agree with its constants.

### D17. Measurements and stop rules (before code, hard rule 1)

**QE1: guest network facts under the uidrange blackhole** (M9 caps image,
real AWS, placeholders only in the row):

- (a) the `nameserver` lines of `/etc/resolv.conf`, and whether each one is
  loopback, link-local or other;
- (b) whether `getaddrinfo("aws.amazon.com")` as uid 1000 fails under
  deny-all;
- (c) the exact output forms of `ip route get 1.1.1.1 uid 1000`
  (blackholed), `ip route get 127.0.0.1 uid 1000` and `ip route get
  1.1.1.1 uid 0`, including the exit codes;
- (d) whether IPv6 is present (`/proc/net/if_inet6`, a default v6 route);
- (e) the scope and prefix length of the guest's interface addresses (not
  the literal addresses);
- (f) whether the IMDS rule (priority 100) and the policy rule (150)
  coexist, with root still reaching IMDS and uid 1000 not.

**QE1 stop rules**:

- If (a) shows a loopback resolver (for example a `127.0.0.53` stub), the
  routes cannot block DNS for uid 1000, which breaks the acceptance "DNS
  resolution fails". The implementer stops and proposes an ADR-012
  addendum in writing instead of improvising (constitution rule 8).
- If (c) shows a form that `classify_route_get` maps to `Unknown`, the
  parser is extended to the measured form and the change continues.

**QE1 outcome (2026-09-23): the stop rule fired.** Row Q66: (a) lists two
resolvers, one loopback and one link-local, both listening on UDP 53
**inside** the guest; (b) `getaddrinfo` as uid 1000 succeeds under
deny-all, while every connection outside the VM fails; (c) all three forms
are known, so `classify_route_get` is unchanged. The owner chose **option
C**, written as the ADR-012 addendum "Adenda: DNS bajo deny-all en caps"
in `ARCHITECTURE.md`:

- No enforcement change in M9. Under deny-all on caps, names may resolve
  through the in-guest platform resolvers; every connection outside the VM
  stays blocked. Hostname rules and proxy mode are unaffected.
- The DNS acceptance becomes "resolves or not, connection fails": the e2e
  resolves the name and asserts that a TCP connect to every resolved
  address fails (D19 test 2, the TS mirror).
- The residual DNS-exfiltration channel is documented in `SECURITY.md` T17
  and on the "Red saliente" page.
- Option A (an `ip rule` with `uidrange 1000-65535`, `ipproto udp`/`tcp`,
  `dport 53` and `prohibit`, placed before the priority-0 `local` rule,
  which must move) is deferred to the next cycle as hardening. Option B (a
  per-process `resolv.conf` through a mount namespace) is rejected for M9.

**QE2: `https_ports`**:

1. The test host generates a throwaway self-signed certificate with
   `openssl req -x509` (the e2e fails with an explicit message when
   `openssl` is missing).
2. It writes the certificate and key into the sandbox, and starts
   `python3` as uid 1000 serving HTTPS on port 8443 with ALPN
   `["http/1.1"]`.
3. It sends `GET /` through `get_host(8443)` with the proxy headers, once
   without and once with `x-aws-proxy-force-h2: true`.

The row records the status codes and whether the body arrived.

- A 200 with the body on either request means supported: the shim sets
  `HTTPS_PORTS_SUPPORTED = True`.
- Otherwise `False`, and the reason text cites the row.

Only then is `HTTPS_PORTS_SUPPORTED` set (task 7.2).

### D18. Tests that fail without the change

**`rayd-core`** (Windows and Linux):

- `network::cidr`: parse table (v4/v6/mapped/brackets rejected/prefix
  bounds, host bits masked), `contains`, `subtract` (disjoint, nested both
  ways, allow broader than deny → empty, several holes, minimality, v6).
- `network::entry`: `ALL_TRAFFIC` → both families, `::/0` v6 only,
  hostnames (case, trailing dot, apex vs wildcard, bare `*`, `a.*.b`,
  single label, 254 chars, non-ASCII), hostname in `deny_out` rejected,
  error messages without entry text.
- `network::policy`: allow-over-deny, default allow, `allow_out` without
  `deny_out` → `Unrestricted`, mode selection table, `deny_by_default`,
  IPv4-mapped verdicts, caps (257 entries, 65 hostnames).
- `network::route_plan`: plans for the three modes, IPv6 dropped when
  absent, `PolicyTooComplex` at 4097.
- `network::swap`: step order from none/A/B to plan/unrestricted, rollback
  before `AddRule`, recovery sequence keeping deny-all at every step (the
  test simulates the rule set after each step and asserts that the uid
  range always has a complete deny-or-policy table).
- `network::probe`: `samples` per mode, `classify_route_get` table
  (including the QE1 forms), `rule_present` parser.
- `network::guard`: loopback, IMDS v4/v6, link-local, `::ffff:169.254.169.254`,
  unspecified, multicast, broadcast, own addresses; `UpstreamGuard` allows
  private and own addresses and refuses IMDS and loopback.
- `network::policy` decisions: IP literal, name allowed on 443 and denied
  on 8443 under deny-by-default without resolution, the `ForwardByName` /
  `ResolveByName` / `ResolveChecked` branches, `select_addresses`
  guard-vs-policy reasons, `localhost` names.
- `network::proxy_protocol`: CONNECT authority forms, absolute-form rewrite
  (`Proxy-*` dropped, `Connection: close`), https absolute-form 400, 16 KiB
  cap, SOCKS greeting without `00` → `FF`, request ATYP 1/3/4 and invalid,
  `BIND`/`UDP` → `07`, the reply encoders, the upstream client
  encoders/decoders including RFC 1929.
- `network::proxy_env`: the eight keys and values.
- `process::env`: the egress layer sits under the payload and request
  envs; an empty egress map changes nothing.
- `run_payload`: `network.enforce` true/false/absent/`"yes"` → error,
  unknown inner keys ignored.
- `health`: builder sets `egress_enforcement`.
- The `limits.json` agreement test.

**`rayd`**:

- `grpc/network.rs` through tonic's in-process channel with a fake
  `EgressRoutes` executor: the error table of D11, `FAILED_PRECONDITION`
  without the capability only for enforcing policies, `GetNetwork` never
  echoing proxy data, the Health field.
- `hooks` `/run` with `enforce` on a fake executor: deny-all is applied and
  verified before the 200. A failing executor gives `None` and still 200.
- `/resume` re-verification reinstalls a missing table.
- `network/proxy.rs` (tokio, any OS, fake resolver and dialer): CONNECT
  allowed → bytes relayed; denied → 403; guarded IMDS/loopback/own address
  → 403; SOCKS5 flows and reply codes; absolute-form forward; 129th
  connection → 503; upstream chaining against an in-process recording
  SOCKS5 server (ATYP domain for a name-allowed target, IP for a checked
  one, credentials sent); log-hygiene capture (D12).
- `crates/rayd/tests/m9_egress.rs` (Linux, root, self-skips when not root,
  run in a fresh network namespace by CI, D19): `lo` up, dummy `rtest0`
  with `192.0.2.1/24`, `default dev rtest0`. Then:
  - IMDS block installed plus deny-all: `ip route get 198.51.100.7 uid
    1000` blocked, `uid 0` routable, `127.0.0.1 uid 1000` local,
    `169.254.169.254 uid 1000` blocked by table 100.
  - Swap to `deny 198.51.100.0/24, allow 198.51.100.7/32`: `.7` routable,
    `.8` blocked.
  - Swap to unrestricted: no rule at 150/151.
  - A failing `FillTable` injected at step 2 of a swap leaves deny-all
    active.

**Python**:

- `tests/unit/test_network_base.py`: selector resolution (list, callable,
  bad return), `allow_internet_access` merge, unknown keys,
  `requires_enforcement`, shape validation, proto round trip, gate errors
  for `UNSPECIFIED`/`NONE`, the RPC error mapping.
- `tests/unit/test_payload.py`: the `network` block present only when
  enforcing.
- `tests/unit/test_network_sync.py` / `test_network_async.py`, against the
  fake `rayd` with a new `fake_network.py` servicer and the stub control
  plane:
  - `NONE`/`UNSPECIFIED` → `UnimplementedError`, and `terminate` is
    recorded even with `keep_on_failure=True`;
  - `GUEST_ROUTES` → `UpdateNetwork` carries the resolved policy and create
    returns;
  - `UpdateNetwork` `INVALID_ARGUMENT` → terminate plus
    `InvalidArgumentException`;
  - `allow_out`-only sends no `network` block and no `UpdateNetwork`;
  - `update_network`/`get_network` instance and class variant (close, not
    kill);
  - `pool=` with `network` refused;
  - `reincarnate` re-sends the policy.
- `tests/unit/test_models.py`: `EgressProxy` repr hides the password.
  `tests/unit/test_sandbox_base.py`: `health_from_proto` fills
  `egress_enforcement`.
- `tests/unit/test_e2b_compat_base.py`, `_sync.py` and `_async.py`:
  - `allow_internet_access=False` forwards the flag and keeps
    `INTERNET_EGRESS`;
  - `network` mapping including the E2B-style `lambda ctx:
    [ctx.all_traffic]`;
  - `rules`/`mask_request_host`/`allow_public_traffic=True` →
    `UnimplementedError`;
  - `https_ports` follows the constant (both branches, with the constant
    patched);
  - `beta_create(network=)` maps;
  - `update_network` instance and class forms return `None`;
  - the gate error surfaces as the shim's `UnimplementedError`.
- `tests/unit/test_exceptions.py`: the shim's `UnimplementedError`
  subclasses the native one.

**TypeScript**: `tests/unit/network.test.ts` (the pure helpers),
`tests/unit/sandbox.test.ts` (gate, termination with `keepOnFailure`,
`UpdateNetwork` payload, static `updateNetwork` closes, `pool` refused), and
`tests/unit/payload.test.ts`, against `tests/unit/fake/network.ts` added to
the fake server.

**Sidecar**: `kernel-sidecar/tests/test_kernels.py::
test_op_envs_proxy_variables_reach_the_kernel_environment` pins that
`kernel_environment` puts `HTTPS_PROXY` from the op envs into the kernel
environment. It is a pin test (no sidecar code change); the behaviour that
needs the change is covered on the `rayd` side (op envs merge).

### D19. Real-AWS acceptance and CI

**Images**:

- An M9 `rayito-base-caps` image (`make image-publish-caps` equivalent,
  run by hand because `make` is not installed on the dev host).
- An M9 `rayito-base` image, published from the same tree.
- Templates come from `RAYITO_TEMPLATE_CAPS` and `RAYITO_TEMPLATE`, the
  transfer bucket from `RAYITO_E2E_TRANSFER_BUCKET` /
  `RAYITO_E2E_TRANSFER_PREFIX`.
- Tracked files use placeholders only.

**`clients/python/tests/e2e/test_m9_egress.py`**:

1. **`test_guest_network_facts`**: records QE1 and prints the row values.
   It asserts only that the probe ran.
2. **`test_internet_off`** (caps): `create(template=caps,
   allow_internet_access=False, allowed_ports=[8080, 8000])`.
   - As uid 1000, `urllib` to `https://aws.amazon.com` exits non-zero and
     the command's wall time is < 5 s + 1 s of slack.
   - Resolve-then-connect as uid 1000: `socket.getaddrinfo("aws.amazon.com",
     443)` may succeed or fail (D17 outcome); when it resolves, a TCP connect
     to every resolved address fails.
   - A background `python3 -m http.server 8000 --bind 127.0.0.1` is
     reached from the test host through `get_host(8000)` with its headers
     → 200.
   - `get_health().egress_enforcement == GUEST_ROUTES`.
   - `get_network().deny_out == ("0.0.0.0/0",)`.
   - `files.download_url` of a 1 MiB file fetched with `urllib` from the
     test host → sha256 equal (root traffic unfiltered).
3. **`test_default_image_fails_closed`**: `create(template=default,
   allow_internet_access=False, control_plane=<recording plane>)` raises
   `UnimplementedError` whose reason names `rayito-base-caps`. The
   recorded `sandbox_id` reaches `TERMINATED` within 60 s (poll
   `get-microvm`). It also runs with `keep_on_failure=True`, with the same
   outcome.
4. **`test_deny_ip_and_allow_wins`** (caps): the test host resolves
   `example.com` → A and `aws.amazon.com` → B (first IPv4 of each).
   - `create(network={"deny_out": [f"{A}/32"]})`: a raw TCP connect as uid
     1000 to `(A, 443)` fails and to `(B, 443)` succeeds.
   - `update_network({"deny_out": [f"{A}/24"], "allow_out": [f"{A}/32"]})`:
     A succeeds.
   - `update_network({"deny_out": [ALL_TRAFFIC], "allow_out": [f"{A}/32"]})`:
     A succeeds and B fails.
5. **`test_hostname_allowlist`** (caps): `create(network={"allow_out":
   ["aws.amazon.com"], "deny_out": [ALL_TRAFFIC]})`.
   - Health is `GUEST_ROUTES_AND_PROXY`.
   - `curl -sS -o /dev/null -w '%{http_code}' https://aws.amazon.com` →
     `200`.
   - `curl https://example.com` exits non-zero with `403` in stderr.
   - `curl --noproxy '*' https://aws.amazon.com` exits non-zero.
   - A raw `python3` socket to `1.1.1.1:443` fails.
   - `python3 urllib.request.urlopen("https://aws.amazon.com")` → 200 (it
     honours the variables).
6. **`test_proxy_guard`** (caps): `create(network={"allow_out":
   [ALL_TRAFFIC, "example.com"], "deny_out": [ALL_TRAFFIC]})`, which is
   proxy mode with everything allowed.
   - `curl -x http://127.0.0.1:$P http://169.254.169.254/latest/meta-data/`
     → `403`.
   - `curl -p -x http://127.0.0.1:$P http://127.0.0.1:9000/` → the CONNECT
     is refused with `403`.
   - `curl --socks5-hostname 127.0.0.1:$P http://169.254.169.254/` → SOCKS
     failure.
   - `$P` is `get_network().local_proxy_port`.
   - The same IMDS check is repeated on a routes-mode sandbox
     (`allow_internet_access=False`).
7. **`test_egress_proxy_chain`** (caps):
   - `create()` with no network (so enforcement starts lazily, D7).
   - Read the VM's address X with `ip -4 -o addr show scope global`,
     falling back to `scope link` excluding `169.254.169.254`, as uid 1000.
   - Start a recording SOCKS5 server as uid 1000 on `X:1080` (a script
     written with `files.write`). It logs the greeting, the RFC 1929
     username and the CONNECT ATYP, host and port to a file, and answers
     `0x05`.
   - `update_network({"allow_out": ["aws.amazon.com"], "deny_out":
     [ALL_TRAFFIC], "egress_proxy": {"address": f"{X}:1080", "username":
     "rayito-e2e", "password": <random>}})`.
   - `curl https://aws.amazon.com` from a new command: the log shows method
     `02`, username `rayito-e2e` and `CONNECT` ATYP `03` `aws.amazon.com`
     `443`. The password is compared inside the script and never printed.
   - `curl --noproxy '*' https://aws.amazon.com` fails.
   - The rayd log check: no `rayito-e2e` and no X in the CloudWatch stream
     when `logging=cloudwatch` is enabled for this test.
8. **`test_update_network_cycle`** (caps): allow-all → `update_network(
   allow_internet_access=False)` → `update_network(None)`. After each
   call, a loop of TCP connects to `(B, 443)` as uid 1000 matches the new
   policy within 1 s (measured and printed). On the default image,
   `update_network({"deny_out": [ALL_TRAFFIC]})` raises `UnimplementedError`
   (`FAILED_PRECONDITION`).
9. **`test_policy_survives_pause_resume`** (caps): `deny_out=[ALL_TRAFFIC]`,
   `allow_out=[f"{B}/32"]`, then `pause()` and `resume()`. Health is still
   `GUEST_ROUTES`, `get_network()` is unchanged, B is reachable, A is not.
10. **`test_https_ports_measurement`**: runs QE2 and asserts that the
    shim's behaviour matches `HTTPS_PORTS_SUPPORTED`.
11. **`test_e2b_shim_sync`** / **`test_e2b_shim_async`**:
    - `from rayito.e2b import Sandbox, ALL_TRAFFIC`.
    - `Sandbox.create(template=caps, allow_internet_access=False)` blocks.
    - `Sandbox.create(template=caps, network={"allow_out":
      ["aws.amazon.com"], "deny_out": lambda ctx: [ctx.all_traffic]})`:
      curl 200/403.
    - `sbx.update_network({})` opens again.
    - `Sandbox.update_network(sbx.sandbox_id, {"deny_out": [ALL_TRAFFIC]},
      access_token=sbx.native.access_token)` closes.

**`clients/typescript/tests/e2e/egress.e2e.test.ts`**: the mirror of
`allowInternetAccess: false` (blocked, localhost ok, `guest_routes`), the
hostname allowlist, the `updateNetwork` cycle (instance and static), and the
fail-closed default image.

**CI** (`.github/workflows/ci.yml`, job `arm`): a new step after `cargo
test` runs:

```
cargo test -p rayd --test m9_egress --locked --no-run
sudo unshare --net -- "$(ls target/debug/deps/m9_egress-* | grep -v '\.d$' | head -1)" --test-threads=1
```

The regular `cargo test --workspace` run self-skips that suite as non-root.

### D20. Documentation

- **`ARCHITECTURE.md`**:
  - ADR-012 (D1) after ADR-011.
  - "Capa 2 — Agente rayd" gains the `NetworkService` row in the services
    table and a subsection "Política de egress (M9)" with the table and
    priority map of D4, the modes, the proxy and its guard, and the
    environment export.
  - The hexagonal tables list `network` in `rayd-core` and the adapters.
  - "Variantes de imagen" states that caps also enforces the egress
    policy.
- **`SECURITY.md`**:
  - T8 gains the in-guest layer on caps, the fail-closed default image,
    and that the VPC connector remains the platform control with its DNS
    caveat.
  - New **T17** "Política de egress en el guest y proxy local de rayd":
    - the root proxy as an SSRF surface, and its guard (D9);
    - in-guest layers fall to a guest-kernel exploit, to root in the guest
      (`RAYITO_ALLOW_ROOT`) and to the exempt platform agent uids;
    - DNS for uid ≥ 1000 under deny-all on caps is a known residual risk
      (in-guest platform resolvers, QE1, D17 outcome): names may resolve,
      connections outside the VM fail; in-guest enforcement is best-effort
      and the VPC connector remains the hard control. The proxy never
      resolves a denied name under deny-by-default;
    - proxy-unaware clients fail closed;
    - upstream credentials: RPC only, zeroized, never logged or echoed,
      never in the `runHookPayload`;
    - any uid can use the proxy, but only within the policy;
    - C-05's IMDS rule and the deferred C-01 are not regressed.
- **`AWS_API_NOTES.md`**:
  - §2: `HTTP_INGRESS` joins the managed connectors as the default ingress
    (Q60), `NO_EGRESS` does not exist (`ValidationException`, Q60), and
    `egressNetworkConnectors: []` ≡ omitted.
  - §16: rows QE1 and QE2 with the values, placeholders only.
- **`SPEC.md`**: the §3 shim row no longer lists `allow_internet_access=False`
  as unimplemented; it says "en rayito-base-caps; falla cerrado en otras
  imágenes".
- **`MILESTONES.md`**: under `## M9 — Paridad con E2B` (created by the
  first M9 change to land if absent), the egress acceptance line.
- **`docs/site/docs/network.md`** (new, nav "Red saliente" after
  "Persistencia" in `mkdocs.yml`):
  - modes, semantics, examples in Python and TS, `update_network`,
    `egress_proxy`;
  - the divergences from E2B: proxy-unaware clients fail closed; hostname
    rules only on 80/443 through the proxy; names may still resolve under
    deny-all through the in-guest platform resolvers (D17 outcome); UDP/QUIC not proxied; policy
    changes affect new connections; processes spawned before a lazily
    started proxy lack the variables; root is not filtered; caps only;
  - the platform alternative.
- **`docs/site/docs/e2b-compat.md`**:
  - `allow_internet_access=False`, `network` and `update_network` move from
    "Lanza UnimplementedError" to "Se mapea, con una nota";
  - `https_ports` per QE2;
  - `rules`, `mask_request_host` and `allow_public_traffic=True` stay
    listed as unimplemented.
- **`docs/site/docs/security.md`**: the T17 summary line.

### D21. Gates

- `cargo fmt --all --check`
- `cargo clippy --workspace --all-targets -- -D warnings` (pedantic,
  `unwrap`/`expect`/`panic` denied outside tests)
- `cargo test --workspace --locked`
- `cargo deny check` (no manifest changes: no new crate)
- the auditable build + `scripts/check_auditable.py`
- `cd clients/python && uv run pytest tests/unit && uv run ruff check . &&
  uv run ruff format --check . && uv run mypy src tests`
- `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test &&
  pnpm build`
- `buf lint`
- `python scripts/check_pins.py`, `python scripts/check_license.py`,
  `python scripts/check_hygiene.py`
- `python scripts/gen_limits.py --check`

The change closes only after the real-AWS e2e of D19 is green.

## Risks / Trade-offs

- **The guest resolver may be loopback.** DNS would then survive the
  routes. Mitigation: QE1 runs first, with a written stop rule (D17).
  Realised: QE1 found in-guest resolvers; accepted as a documented residual
  risk (ADR-012 addendum, option C), with option A in the next cycle.
- **Proxy mode breaks proxy-unaware software** (raw sockets, some language
  runtimes, `git://`, `ssh`). This is accepted and documented as fail
  closed, not approximated; E2B filters transparently.
- **In-guest enforcement is not a hard boundary** against a guest-kernel
  exploit or guest root. T17 states it, and the VPC connector remains the
  platform control.
- **The proxy runs in `rayd` (root).** A bug in the parser or the guard is
  an SSRF. Mitigations: pure parsers with table tests, a guard applied
  after resolution, and dials only to the checked `SocketAddr`.
- **`rayd` fd budget** (1024 hard): capped at 128 proxy connections.
- **Existing connections are not guaranteed to be cut** by
  `update_network`. The contract is "new connections", as documented.
- **Policy changes are CPU-bound for large plans.** The 4096-prefix cap per
  family bounds `ip -batch`.

## Migration Plan

Everything is additive:

- An older SDK against an M9 agent: no `network` block, no change.
- An M9 SDK against an older agent: `egress_enforcement` is `UNSPECIFIED`,
  so the gate terminates the VM and raises `UnimplementedError` naming an
  M9 caps image. `create()` without `network` is unaffected.
- The shim's `allow_internet_access=False` changes from "always
  `UnimplementedError`" to "works on caps, `UnimplementedError` plus
  termination elsewhere". Programs that caught the old error keep
  catching it on the default image.

Rollback is `git revert` of the implementation commits; no persisted state
exists.

## Open Questions

None. Every choice is closed above. The only branch points are
measurement-driven with written outcomes: QE1 (stop rule or parser
extension) and QE2 (`HTTPS_PORTS_SUPPORTED`).

**Rebase rule for the MODIFIED deltas**: this change's two MODIFIED
requirements in `e2b-compat` are written against the current
`openspec/specs` text. If a sibling change (for example `m9-server-timeout`
or `m9-file-transfer`) archives a MODIFIED version of the same requirement
first, the archiver of this change rebases these blocks onto the archived
text and changes only the clauses named in this change: the
`allow_internet_access` and `network` mapping, the "internet access off"
scenario, and the removal of `network` from the `beta_create` clause.
