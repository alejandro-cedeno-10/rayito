## Context

ADR-016 (`OptionalStack`) and ADR-015 (`ConfigureSandbox`) are in place from
`v06-foundations`. `custom-domain` is unlike the other seven M15 features in
one way: it has **no `rayd`/agent-side component at all** (the module map in
the M15 architecture marks its rayd column "none") — everything lives in the
CloudFront distribution and the SDK. That makes the off-by-default surface
different too: there is no `ConfigSection`/`ConfigureSandbox` wiring to
build, only a stack to deploy and a KeyValueStore to read/write.

## Decisions

- **D1 — Two KVS keys per route, not one.** A `create-microvm-auth-token`
  JWE measures 823 B (DOM-1, re-derived here from the token the SDK already
  mints for `HostAccess`, not a new measurement). CloudFront KeyValueStore
  caps a value at 1 KiB. Metadata (endpoint, the `traffic_token` hash, the
  expiry) does not fit alongside the JWE in one value, so `j:<label>` and
  `m:<label>` are separate keys, written as two chained `PutKey` calls
  (architecture §7.8). The refresher only ever needs to touch `j:`.
- **D2 — SigV4A, not plain SigV4 despite `signatureVersion: v4`.** An
  earlier pass of this change read `metadata.signatureVersion == "v4"` in
  the `cloudfront-keyvaluestore` botocore model (1.43.103) and concluded
  SigV4A was unnecessary — the wrong field. The model's
  `endpoint-rule-set-1.json` sets `authSchemes: [{"name": "sigv4a", ...}]`,
  and endpoint resolution picks the signer, not `metadata.signatureVersion`.
  Checked offline with dummy credentials and a `before-send` hook: `boto3`
  `describe_key_value_store` signs with `AWS4-ECDSA-P256-SHA256` when
  `awscrt` is installed and raises `MissingDependencyException` ("Use pip
  install botocore[crt]") when it is not; the JS v3 SDK fails the same way
  without a SigV4A signer. So this service's data plane genuinely needs
  SigV4A: Python gets `awscrt` through the new `rayito[custom-domain]`
  extra, TypeScript declares `@aws-sdk/signature-v4a` as an additional
  optional peer. Both are loaded lazily and mapped to a clear
  `CustomDomainException`/`CustomDomainError` naming the missing package.
  Recorded as a correction (of the earlier correction) in
  `AWS_API_NOTES.md` §29.
- **D3 — The placeholder origin is never meant to serve traffic.** CloudFront
  requires a `DefaultCacheBehavior`/`TargetOriginId` pointing at a real
  `Origins` entry at template-create time, but the real target is chosen per
  request by `RouterFunction` via `cf.updateRequestOrigin({domainName:
  meta.e, ...})`, which does not need the placeholder's domain to resolve to
  anything (`updateRequestOrigin`'s own docs: "doesn't need to be an
  existing origin within your CloudFront distribution"). A request for a
  label with no KVS route gets a 404 straight from the Function and never
  reaches the placeholder.
- **D4 — The router strips `x-aws-proxy-*` before trusting anything.**
  Mirrors the existing MicroVM proxy's own header contract
  (`PROXY_AUTH_HEADER`/`PROXY_PORT_HEADER` in `_transport.py`): if a viewer
  could set those headers themselves, they could redirect any route's
  traffic to an arbitrary port on an arbitrary sandbox. `custom_domain_router.js`
  deletes every `x-aws-proxy-*` the viewer sent before computing its own.
- **D5 — `register`/`unregister`/`refresh` take the JWE and endpoint as
  plain arguments, not a `Sandbox`.** Keeps `_custom_domain` free of any
  dependency on `sandbox_{sync,async}` (hexagonal: a feature module never
  imports the thing that will eventually import it) and is exactly the
  shape a future `Sandbox` integration would call internally. It also makes
  the class fully usable today, standalone.
- **D6 — Why `Sandbox.create(domain=)`/`get_host()` wiring is a follow-up,
  not part of this change.** The M15 architecture (§1(g)) expected
  foundations to pre-add a `HostResolver` seam and delegating `expose()`/
  `unexpose()` members on `Sandbox` for exactly this purpose. `v06-foundations`
  shipped a leaner version: the `domain=` kwarg exists and is rejected, but
  no `HostResolver`/`expose()`/`unexpose()` seam was added, and
  `sandbox_{sync,async}/main.py` is foundations-only for "kwargs,
  delegations and exports" (M15 architecture §5). Two honest options at
  that point: (a) add the seam here, in a file this change does not own,
  increasing merge-conflict surface across the seven sibling feature
  branches that touch the same `create()` body; or (b) ship the
  stack+KVS+route layer as a complete, independently useful, fully tested
  unit, and leave the `Sandbox` wiring as a named follow-up. `v06-foundations`
  made the same call for the PID-1 zombie reaper ("documented as a reasoned
  non-blocking follow-up rather than shipped half-safe") rather than wire a
  shared adapter path under time pressure; this change follows the same
  precedent. The follow-up is small and precisely scoped: a `HostResolver`
  Protocol/interface, `Sandbox.get_host()` consulting it when a `domain=`
  is attached, and `expose()`/`unexpose()` delegating to
  `CustomDomain.register()`/`unregister()` with the port's already-minted
  JWE. Until then, `domain=` keeps raising `UnimplementedError` (unchanged
  from `v06-foundations`) rather than silently accepting a value it cannot
  yet act on.

## What is explicitly unmeasured here (needs D3 + AWS acceptance)

DOM-2 (HTTP/1.1 through `updateRequestOrigin`, header budget under 1,783
characters), DOM-3 (WebSocket upgrade), DOM-5 (KVS put/delete propagation
latency to edge), DOM-7 (keep-alive past JWE expiry — `CustomDomain.refresh()`
is the mechanism, its timing is unverified against a real distribution),
DOM-8 (auto-resume through the domain, which depends on the `Sandbox`
wiring in D6) and **DOM-14** (new: whether the optional refresher Lambda
that §7.8 of the M15 architecture describes — periodic, off by an
`EnableRefresher` parameter default `false`, re-`PutKey`-ing `j:`/`m:`
before a route's TTL runs out — works against the bundled
`lambda-microvms`-style packaging this repo already uses for other
components' Lambdas). `docs/site/docs/funciones-opcionales/dominio-propio.md`
marks the feature experimental for exactly these reasons.

**DOM-14 is deferred, not measured, same as the `Sandbox` wiring in D6.**
`_stacks/components/custom_domain.py`'s module docstring already explains
why: packaging and uploading a conditional Lambda artifact on every
`deploy()` is not something `OptionalStacks`'s generic mechanism supports
today, and a refresher Lambda cannot be exercised against real AWS in this
change anyway (no D3). `CustomDomain.refresh()` on the SDK side covers the
same need in the meantime (the caller invokes it before a route's JWE
expires, DOM-7). Recorded here, in `tasks.md`, `MILESTONES.md`'s M15
section and `docs-delta.md` so the acceptance stage marks DOM-14
"pending", not "failed" — a review finding on PR #74 noted it was only in
a module docstring before this note existed.

## Risks / trade-offs

- Shipping the stack without the `Sandbox` wiring means `domain=` is not
  yet a one-line opt-in; a caller who wants a custom-domain sandbox today
  has to call `CustomDomain.register()` by hand with the sandbox's
  `endpoint` and a minted JWE. Documented plainly, not hidden.
- `RouterFunction`'s JS has not run inside a real CloudFront edge location
  (D3 blocks that). Its pure helpers are unit-tested; the `cf`/KVS
  integration is only as correct as the AWS documentation for
  `cf.kvs()`/`cf.updateRequestOrigin()` consulted while writing it — DOM-2/3
  are the real check.
