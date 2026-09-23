## ADDED Requirements

### Requirement: Egress policy entries follow E2B semantics
`rayd-core` SHALL parse a policy made of `allow_out`, `deny_out` and an optional `egress_proxy`, with no new dependency.

Entry forms:
- An entry SHALL be an IPv4 or IPv6 address, a CIDR (host bits masked, IPv4-mapped IPv6 with prefix ≥ 96 converted to IPv4), the literal `0.0.0.0/0` (`ALL_TRAFFIC`, covering both `0.0.0.0/0` and `::/0`), or a hostname: exact, or `*.suffix` matching subdomains at any depth but never the apex. Hostnames are lowercased, trailing dot stripped, LDH labels of 1–63, at most 253 characters, at least two labels, ASCII only.
- Hostnames SHALL be accepted only in `allow_out`.
- Each list SHALL hold at most 256 entries, with at most 64 hostname entries in total.
- A malformed entry, a hostname in `deny_out` or an exceeded cap SHALL be rejected with a message naming the list and the index, never the entry text.

Evaluation:
- An address SHALL be allowed when any `allow_out` network contains it, otherwise denied when any `deny_out` network contains it, otherwise allowed.
- IPv4-mapped addresses SHALL be evaluated as IPv4.
- When `deny_out` is empty, the allow entries SHALL NOT affect evaluation or mode.

Modes:
- `Unrestricted` when `deny_out` is empty and no `egress_proxy` is set.
- `ProxyOnly` when an `egress_proxy` is set, or when `deny_out` is non-empty and `allow_out` has a hostname.
- `Routes` otherwise.

The Python and TypeScript SDKs SHALL expose `ALL_TRAFFIC == "0.0.0.0/0"`, SHALL evaluate selector callables client-side with a context carrying `all_traffic`, and SHALL treat `allow_internet_access=False` as appending `ALL_TRAFFIC` to `deny_out`.

#### Scenario: allow beats deny even when broader
- **WHEN** the unit test evaluates `allow_out=["10.0.0.0/8"]`, `deny_out=["10.1.0.0/16", "0.0.0.0/0"]`
- **THEN** `10.1.2.3` is allowed, `11.0.0.1` is denied and `::1` evaluates against the `::/0` half of `ALL_TRAFFIC` as denied

#### Scenario: hostname rules
- **WHEN** the unit test parses `allow_out=["*.Example.com."]` and `deny_out=["example.com"]`
- **THEN** the allow entry becomes a subdomain pattern that matches `a.b.example.com` and not `example.com`, and the deny entry is rejected as `deny_out[0]` without quoting `example.com`

#### Scenario: allow_out alone restricts nothing
- **WHEN** a policy has `allow_out=["example.com"]` and no `deny_out` and no `egress_proxy`
- **THEN** its mode is `Unrestricted`, the SDKs send no `network` block and no `UpdateNetwork`, and they log a one-time notice

### Requirement: Routes mode installs a uid-scoped routing table with atomic swaps
On an image whose `CapEff` includes `CAP_NET_ADMIN`, `rayd` SHALL enforce a policy with policy routing scoped by `uidrange 1000-65535`, using fixed slots: table 101 at priority 150, table 102 at priority 151, and table 103 at priority 149 for recovery only. The IMDS rule (table 100, priority 100) SHALL NOT be touched.

Route plan:
- `Routes` mode: `blackhole` routes for the minimal CIDR cover of `deny_out \ allow_out`, per family.
- `ProxyOnly` mode: `blackhole 0.0.0.0/0` and `blackhole ::/0`.
- `Unrestricted` mode: no rule.
- IPv6 steps SHALL be mandatory when `/proc/net/if_inet6` exists and skipped otherwise.
- A plan above 4096 prefixes in one family SHALL be rejected as `INVALID_ARGUMENT`.

Swaps:
- A policy change SHALL fill the inactive table, add its rule, delete the old rule and flush the old table, in that order, per family.
- A failure before the new rule is added SHALL leave the old policy active.
- A failure after it SHALL run a recovery that installs table 103 deny-all at priority 149 first, then leaves deny-all in slot 101/150.
- At no step SHALL the uid range resolve without a complete policy table.

#### Scenario: swap never opens a window
- **WHEN** the domain test simulates the rule set after every step of a swap from slot A to slot B, and of a recovery after a failed rule deletion
- **THEN** after each step the uid range resolves through a complete table (old policy, new policy or deny-all) and never through `main` alone

#### Scenario: Linux adapter in a network namespace
- **WHEN** CI runs `crates/rayd/tests/m9_egress.rs` as root inside `unshare --net` on `ubuntu-24.04-arm` with a dummy interface and the IMDS block installed
- **THEN** under deny-all `ip route get 198.51.100.7 uid 1000` is blocked while `uid 0` is routable and `127.0.0.1 uid 1000` is local; after swapping to `deny_out=["198.51.100.0/24"]`, `allow_out=["198.51.100.7/32"]`, `.7` is routable and `.8` blocked; after swapping to unrestricted no rule remains at 150 or 151; and an injected failure mid-swap leaves deny-all active

### Requirement: Proxy-only mode goes through rayd's local forward proxy
When enforcement is requested on a capable image, `rayd` (root) SHALL serve a forward proxy on `127.0.0.1:<ephemeral>`. The protocol SHALL be chosen by the first byte: `0x05` is SOCKS5, anything else is HTTP/1.x.

HTTP:
- It SHALL support `CONNECT` and absolute-form `http://` requests, the latter rewritten to origin-form with `Proxy-*` headers dropped and `Connection: close` added.
- Answers: `403` for a policy or guard denial, `502` for resolution, dial or upstream failures, `504` for a 10 s connect timeout, `400` for malformed requests, `431` beyond a 16 KiB head, `503` beyond 128 concurrent connections.

SOCKS5:
- It SHALL support `CONNECT` only, with ATYP IPv4, domain and IPv6.
- Replies: `0x02` for a denial, `0x07` for `BIND` and `UDP ASSOCIATE`, `0x08` for an unknown ATYP, `0xFF` when method `0x00` is not offered.

Target decisions:
- A hostname SHALL be allowed by name only on ports 80 and 443 when an `allow_out` hostname pattern matches.
- Under a deny-by-default policy (`deny_out` covering `ALL_TRAFFIC`), any other hostname SHALL be refused without being resolved.
- Otherwise the proxy SHALL resolve, keep only allowed and unguarded addresses, and dial exactly the checked address.
- Policy changes SHALL affect new connections only.

Environment export:
- Every process, PTY and kernel context spawned after the proxy starts SHALL receive `HTTP_PROXY`, `HTTPS_PROXY` (`http://127.0.0.1:<port>`), `ALL_PROXY` (`socks5h://127.0.0.1:<port>`) and `NO_PROXY` (`localhost,127.0.0.1,::1`), in upper and lower case, layered under the payload and request `envs`.
- Kernels SHALL receive them through the envs of every context op. The sidecar process itself SHALL NOT receive them.

#### Scenario: hostname allowlist through the proxy
- **WHEN** the proxy unit suite applies `allow_out=["api.example.test"]`, `deny_out=["0.0.0.0/0"]` with a fake resolver and dialer and sends `CONNECT api.example.test:443`, `CONNECT other.example.test:443` and `CONNECT api.example.test:8443`
- **THEN** the first relays bytes after `200 Connection established`, the second and third get `403`, and the fake resolver was never asked for `other.example.test` or for the `:8443` target

#### Scenario: variables reach new children
- **WHEN** the proxy is running on port P and a process, a PTY and a `CreateContext` op are planned
- **THEN** all three carry the eight proxy variables pointing at `127.0.0.1:P`, a request env overriding `HTTPS_PROXY` wins, and the sidecar spawn spec carries none of them

### Requirement: The local proxy never dials guarded addresses
Regardless of the policy, the local proxy SHALL refuse any target that is a guarded address, and any hostname whose resolved addresses are all guarded. `localhost` and `*.localhost` SHALL be refused before resolution.

Guarded addresses, checked after IPv4-mapped canonicalization:
- loopback;
- unspecified;
- `169.254.0.0/16` (including `169.254.169.254`);
- `fe80::/10` and `fd00:ec2::254/128`;
- multicast and `255.255.255.255`;
- every address assigned to a guest interface, re-read at every policy apply and every `/resume`.

A connection SHALL go only to the exact `SocketAddr` that passed the checks, never re-resolved.

#### Scenario: IMDS and hooks are never reachable through rayd
- **WHEN** the proxy unit suite, with `allow_out=["0.0.0.0/0","example.test"]`, `deny_out=["0.0.0.0/0"]`, receives `CONNECT 169.254.169.254:80`, `CONNECT 127.0.0.1:9000`, `CONNECT [::ffff:169.254.169.254]:80`, a SOCKS5 CONNECT to the guest's own address on port 8080, and a CONNECT to a name the fake resolver maps only to `127.0.0.1`
- **THEN** every one is refused (`403` or SOCKS `0x02`) and the fake dialer is never called

### Requirement: The operator SOCKS5 upstream is chained after filtering
When `egress_proxy` is set, the local proxy SHALL filter first and then tunnel every allowed connection through the operator's SOCKS5 server (RFC 1928, with RFC 1929 username and password when given).
- Name-allowed targets SHALL be sent as ATYP domain without local resolution.
- Every other target SHALL be sent as the checked IP.
- `address` SHALL be `host:port` or `[v6]:port`. Credentials SHALL be 1–255 bytes each. A password without a username SHALL be `INVALID_ARGUMENT`.
- An upstream that resolves only to loopback, unspecified, multicast, `169.254.169.254` or `fd00:ec2::254`, or does not resolve within 5 s, SHALL be `INVALID_ARGUMENT`. Private and own-interface addresses SHALL be allowed.
- An upstream failure SHALL fail the client connection (`502` or SOCKS `0x01`) and SHALL never fall back to a direct connection.
- Credentials SHALL be delivered only by `UpdateNetwork`, held zeroized with a redacted `Debug`, never echoed by `NetworkState` and never logged.

#### Scenario: chained CONNECT carries the name and the credentials
- **WHEN** the unit suite points `egress_proxy` at an in-process recording SOCKS5 server with username `u-marker` and allows `api.example.test`
- **THEN** a client `CONNECT api.example.test:443` makes the recorder see method `0x02`, username `u-marker` and `CONNECT` ATYP `0x03` `api.example.test` port 443, and `GetNetwork` returns `egress_proxy_configured: true` with no address or credential

### Requirement: Policy delivery through the run payload and NetworkService
The SDKs SHALL add `"network":{"enforce":true}` to the `runHookPayload` exactly when the resolved policy needs enforcement (`deny_out` non-empty or `egress_proxy` set). The payload SHALL never carry rules or credentials.

`rayd` at `/run`:
- It SHALL parse `network.enforce` as a JSON bool. Any other type SHALL be a payload error.
- With `enforce` on a capable image, before answering 200, within 1.5 s: start the local proxy, install deny-all (`deny_out=["0.0.0.0/0"]`) and verify it.
- On an image without `CAP_NET_ADMIN`, or on failure, it SHALL leave enforcement `NONE`, log a fixed reason, and still answer 200.

`NetworkService` (`proto/rayito/v1/network.proto`), with both RPCs behind the `x-access-token` layer:
- `UpdateNetwork` SHALL replace the whole policy (omitted fields cleared) and return the `NetworkState`.
- `GetNetwork` SHALL return the `NetworkState`: the lists as sent, `egress_proxy_configured`, `enforcement`, `local_proxy_port` (0 when not running).

Error codes:
- `INVALID_ARGUMENT` for entry, cap and proxy errors.
- `FAILED_PRECONDITION` when the policy needs enforcement and the image lacks `CAP_NET_ADMIN`. A non-enforcing policy on such an image is accepted with enforcement `NONE`.
- `INTERNAL` `egress_update_failed` or `egress_verify_failed` after the recovery to deny-all.

#### Scenario: deny-all precedes user code
- **WHEN** the hooks test sends `/run` with `"network":{"enforce":true}` to a capable `rayd` backed by a fake route executor
- **THEN** the executor has applied and verified the deny-all plan and the proxy is listening before the 200 is written, and `Health.egress_enforcement` is `GUEST_ROUTES`

#### Scenario: default image refuses to enforce
- **WHEN** `UpdateNetwork` with `deny_out=["0.0.0.0/0"]` reaches a `rayd` without `CAP_NET_ADMIN`
- **THEN** it answers `FAILED_PRECONDITION` naming `rayito-base-caps`, while `UpdateNetwork` with an empty policy answers OK with enforcement `NONE`

### Requirement: Enforcement is verified by a route probe and published in Health
`rayd` SHALL publish `HealthResponse.egress_enforcement` (field 13): `NONE` without a policy or without the capability, `GUEST_ROUTES` for a verified `Routes` plan, `GUEST_ROUTES_AND_PROXY` for a verified `ProxyOnly` plan.

A verification SHALL check:
- the rule at the active priority and none at the other;
- the blackhole count of the active table per family;
- sample destinations with `ip route get <addr> uid 1000`, which sends no packet: `127.0.0.1` local, up to three blackholed prefixes blocked, and one allowed public address routable when IPv4 is not fully denied;
- in `ProxyOnly`, a self-connect to the proxy.

An output form the parser does not recognize SHALL fail verification. A failed verification SHALL trigger the recovery to deny-all.

On `/resume`, `rayd` SHALL re-verify synchronously within 3 s before answering 200, reinstalling the stored plan when it is missing. A timeout SHALL set `NONE` and still answer 200.

The policy and the enforcement value SHALL survive suspend and resume.

#### Scenario: classification of probe output
- **WHEN** the domain test classifies a non-zero exit, `blackhole 1.1.1.1 ...`, `local 127.0.0.1 dev lo ...`, `1.1.1.1 via 10.0.0.1 dev eth0 ...` and an empty stdout with exit 0
- **THEN** they are `Blocked`, `Blocked`, `Local`, `Routable` and `Unknown`, and `Unknown` fails the verification

#### Scenario: resume reinstalls a missing table
- **WHEN** the `/resume` test finds the active rule missing on the fake executor
- **THEN** the stored plan is reinstalled and verified before the 200 and `egress_enforcement` keeps its value

### Requirement: The SDK create gate fails closed
When a resolved policy needs enforcement, `Sandbox.create` (Python sync and async) and `Sandbox.create` (TypeScript) SHALL run these steps after readiness:
1. Read `egress_enforcement` from the readiness `Health`. When it is `UNSPECIFIED` or `NONE`, raise `UnimplementedError`. Its `feature` is `allow_internet_access=False` or `network`. Its reason names `rayito-base-caps` and the customer VPC connector of `infra/egress-connector.yaml` as the platform alternative.
2. Otherwise send `UpdateNetwork` with the resolved policy and require a returned enforcement other than `NONE`/`UNSPECIFIED` before returning.

Failure handling:
- On any failure in these steps the SDK SHALL close its client and terminate the MicroVM even when `keep_on_failure` / `keepOnFailure` is true, then raise.
- `INVALID_ARGUMENT` SHALL map to `InvalidArgumentException` / `InvalidArgumentError`.
- `FAILED_PRECONDITION` and `UNIMPLEMENTED` SHALL map to `UnimplementedError`.

Other rules:
- `create(pool=..., network=...)`, and `pool` with `allow_internet_access=False`, SHALL be rejected.
- `reincarnate()` SHALL re-apply the stored policy.
- The native `UnimplementedError` SHALL subclass `NotImplementedError` in Python and `Error` in TypeScript, and carry `feature` and `reason`.

#### Scenario: older or default image terminates
- **WHEN** the unit test creates a sandbox with `allow_internet_access=False` and `keep_on_failure=True` against a fake `rayd` whose `Health` has `egress_enforcement` `NONE`, and again with the field unset
- **THEN** both raise `UnimplementedError` naming `rayito-base-caps`, the stub control plane recorded `terminate-microvm` for each id, and no `UpdateNetwork` was sent

#### Scenario: capable image receives the policy before create returns
- **WHEN** the fake reports `GUEST_ROUTES` and the caller passes `network={"allow_out": ["1.2.3.4/32"], "deny_out": lambda ctx: [ctx.all_traffic]}`
- **THEN** the payload carries `"network":{"enforce":true}`, `UpdateNetwork` carries `allow_out=["1.2.3.4/32"]` and `deny_out=["0.0.0.0/0"]` before `create()` returns, and the sandbox is returned

### Requirement: update_network and get_network in the Python SDK
`rayito.Sandbox` and `rayito.AsyncSandbox` SHALL expose identical surfaces over the shared pure helpers of `_network_base.py`:
- `update_network(network=None, *, allow_internet_access=None, request_timeout=None) -> NetworkState`: replaces the whole policy, where `None` or `{}` means unrestricted.
- A class variant `Sandbox.update_network(sandbox_id, network=None, *, allow_internet_access=None, access_token=None, region=None, session=None, control_plane=None, transport=None, request_timeout=...)`: connects with the access token (or `RAYITO_ACCESS_TOKEN`), updates and closes the client without killing the sandbox.
- `get_network(*, request_timeout=None) -> NetworkState`.

`rayito` SHALL export `ALL_TRAFFIC`, `EgressProxy` (whose `repr` hides the password), `EgressEnforcement`, `NetworkOptions`, `NetworkPolicy`, `NetworkSelectorContext`, `NetworkState` and `UnimplementedError`. `SandboxHealth` SHALL carry `egress_enforcement`.

#### Scenario: class variant never kills
- **WHEN** the unit test calls `Sandbox.update_network(<id>, {"deny_out": [ALL_TRAFFIC]}, access_token=<token>)` against the fakes
- **THEN** `get-microvm`, a token mint and one `UpdateNetwork` happen, the client is closed, and no `terminate-microvm` is recorded

### Requirement: Egress logs carry decision counts only
`rayd` SHALL log only these egress events, with fields from a fixed set and never a target hostname, a target IP or port, the upstream address, a credential, `ip` output or payload text:
- `egress_policy_applied`: mode, entry counts, route counts, `proxy_configured`, `duration_ms`.
- `egress_verify`.
- The fixed-reason failure events.
- `egress_proxy_started`: port.
- `egress_proxy_stats` every 60 s when changed: `allowed`, `denied_policy`, `denied_guard`, `denied_invalid`, `upstream_failed`, `dial_failed`, `rejected_busy`, `active`.

The SDKs SHALL never log policy lists, proxy addresses or credentials.

#### Scenario: captured tracing stays clean
- **WHEN** the `rayd` unit test captures tracing output while the proxy denies `CONNECT secret-host.example:443` and while an `UpdateNetwork` with `egress_proxy {address: "proxy.example:1080", username: "u-marker", password: "p-marker"}` is applied
- **THEN** none of `secret-host`, `proxy.example`, `u-marker` or `p-marker` appears in the captured output

### Requirement: Platform egress facts are recorded before code
`AWS_API_NOTES.md` §2 SHALL list `HTTP_INGRESS` as the managed ingress the platform attaches by default, state that `…:aws-network-connector:NO_EGRESS` does not exist (`ValidationException`), and state that `egressNetworkConnectors: []` behaves like the field omitted (it inherits the image version's `INTERNET_EGRESS`), citing Q60.

§16 SHALL gain two measured rows with placeholders only:
- QE1: the guest resolver and its address class, `getaddrinfo` as uid 1000 under deny-all, the `ip route get` output forms, IPv6 presence, the interface address scopes, and the coexistence of the IMDS rule with the policy rule.
- QE2: whether the AWS proxy reaches an app port serving TLS through `get_host(port)`, with and without `x-aws-proxy-force-h2`.

When QE1 shows a loopback resolver, implementation SHALL stop and an ADR-012 addendum SHALL be proposed in writing. QE1 showed in-guest resolvers, and the addendum "Adenda: DNS bajo deny-all en caps" (option C) records that DNS may resolve under deny-all on caps while every connection outside the VM fails.

#### Scenario: rows present before the adapter lands
- **WHEN** a reviewer reads `AWS_API_NOTES.md` at the commit that adds `adapters/egress_routes.rs`
- **THEN** §2 carries the three facts with their Q60 citation and §16 carries the QE1 and QE2 rows (measured values filled before the change is accepted)

### Requirement: Real-AWS acceptance of the egress policy
The change SHALL be accepted only after `clients/python/tests/e2e/test_m9_egress.py` and `clients/typescript/tests/e2e/egress.e2e.test.ts` pass against real AWS with an M9 `rayito-base-caps` and an M9 `rayito-base`.

Required outcomes:
- **Internet off**: `allow_internet_access=False` on caps makes a uid-1000 `urllib` to `https://aws.amazon.com` fail within 5 s. DNS may resolve through the in-guest platform resolvers (ADR-012 addendum, QE1); whether or not it resolves, a uid-1000 TCP connect to every resolved address fails. Meanwhile a loopback server reached through `get_host(port)` answers 200. `Health.egress_enforcement == GUEST_ROUTES`, and a `files.download_url` export (root traffic) still succeeds.
- **Default image**: `allow_internet_access=False` raises `UnimplementedError` naming `rayito-base-caps` and the MicroVM reaches `TERMINATED`, also with `keep_on_failure=True`.
- **IP rules**: a `/32` deny blocks that IP while another host is reachable, and an overlapping allow wins.
- **Hostname allowlist**: `allow_out=["aws.amazon.com"]` with `deny_out=[ALL_TRAFFIC]` gives curl 200 through the proxy variables, a proxy 403 for `https://example.com`, and failures for a raw socket to `1.1.1.1:443` and for `curl --noproxy '*'`.
- **Proxy guard**: with `allow_out` containing `ALL_TRAFFIC` in proxy mode, CONNECT to `169.254.169.254:80` and to `127.0.0.1:9000` through the local proxy is refused.
- **Upstream**: an `egress_proxy` pointing at a recording SOCKS5 server on the VM's non-loopback address receives the CONNECT for the allowed host, while a direct connection fails.
- **update_network**: allow-all → deny-all → allow-all, with new connections following each policy within 1 s, and `FAILED_PRECONDITION` (surfaced as `UnimplementedError`) on the default image.
- **Suspend/resume**: the policy and `egress_enforcement` survive `pause()` and `resume()`.
- **Measurements**: QE1 and QE2 are recorded.
- **Shim**: the E2B shim runs in sync and async.

#### Scenario: acceptance run recorded
- **WHEN** the acceptance agent runs both e2e files with real credentials
- **THEN** every listed outcome passes, the timings (refusal ≤ 5 s, policy switch ≤ 1 s) and the CloudWatch log check (no upstream address or username in the `rayd` stream) are recorded in the task note, and only then is the change archived
