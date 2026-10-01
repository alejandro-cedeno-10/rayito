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
- **D2 — `signatureVersion: v4`, not SigV4A.** The M15 architecture's
  research assumed KeyValueStore writes need SigV4A
  (`@aws-sdk/signature-v4a`/`awscrt`). Checked offline against the
  `cloudfront-keyvaluestore` botocore model (1.43.103) before writing any
  code: `metadata.signatureVersion == "v4"`. Plain SigV4 is correct and
  removes a whole dependency axis (no CRT native binary, no extra Python
  extra). Recorded as a correction in `AWS_API_NOTES.md` §29.
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
is the mechanism, its timing is unverified against a real distribution) and
DOM-8 (auto-resume through the domain, which depends on the `Sandbox`
wiring in D6). `docs/site/docs/funciones-opcionales/dominio-propio.md`
marks the feature experimental for exactly these reasons.

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
