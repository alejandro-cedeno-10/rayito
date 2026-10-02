## Context

`m15-foundations` built `ConfigureSandbox` and the `secret_gateway` stub
slot. This change gives that slot a real adapter: a per-sandbox,
per-upstream reverse proxy that holds a credential the guest can use but
never read, documented as ADR-023 and threat T24 in
`docs/research/2026-10-e2b-out-of-scope.md`.

## Decisions

- **D1 — One listener per route, not one listener multiplexing by host.**
  Simpler isolation (a crash or restart of one route's listener never
  touches another's state) and it matches the architecture's own wording
  ("One loopback HTTP listener per route"). The cost is one bound port per
  route (≤ 8), trivial against the guest's ephemeral port range.
- **D2 — Replace-whole-state on every `Configure`, reconciled by route
  name.** `SecretGatewayConfig`'s own semantics say a present section is
  the complete desired state, and that still decides *which* routes exist.
  But a route whose name is in both the old and the new config keeps its
  listener and port, and only its `RouteState` (vaulted values, allow
  rules, rate) is swapped in place (`SwappableState`, a `RwLock<Arc<_>>`
  held only to clone or replace the `Arc`, never across an `.await`);
  `forward` loads it once per request. Rebinding every route on every call
  (the first version) moved the port on each `refresh()`, so a process
  started with `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` got connection
  refused after a rotation. Worse, since axum 0.8 runs every accepted
  connection in its own detached task, aborting the accept loop left
  keep-alive connections forwarding with the *old* credential. A removed
  route is now shut down gracefully (`axum::serve(..).with_graceful_shutdown`
  plus a per-route `CancellationToken`): it stops accepting, closes idle
  keep-alive connections at once, lets an in-flight request finish, and
  `apply` waits up to `ROUTE_SHUTDOWN_GRACE` (2 s) for it.
- **D2b — Request paths are refused, never normalised.** `AllowRule` is a
  plain prefix compare, and the path is forwarded verbatim, so a
  dot-segment (`/v1/../admin`, `/v1/%2e%2e/admin`, `/v1/..%2fadmin`) would
  pass a `/v1/*` rule and then be normalised to `/admin` by the upstream or
  its CDN (the confused-deputy bypass T24 exists to stop).
  `rayd_core::secret_gateway::route::path_is_safe` refuses (403
  `not_allowed`) any path with a `.`/`..` segment, an encoded `.`, `/` or
  `\`, a backslash or an empty segment, before the allowlist runs.
  Normalising instead would mean the forwarded path differs from what the
  sandbox sent, which is harder to reason about and to test. The edge cases
  live in `testdata/secret-gateway/request-paths.json`.
- **D2c — Header names are validated in all three layers.** An invalid
  token used to surface only as a 502 on every request, and an injected
  `host`/`content-length`/`transfer-encoding` could re-route or desync the
  forwarded request. Python, TypeScript and `rayd` (the trust boundary for
  a client that bypasses the SDK) now reject a non-RFC 9110 token, a
  hop-by-hop or framing name (`header_template::is_reserved_header`), and
  two names equal ignoring case, against the same
  `testdata/secret-gateway/header-names.json` vectors.
- **D3 — Header values arrive pre-resolved, never a secret name.** The
  proto message (`SecretGatewayConfig.routes[].headers`) carries the
  literal value, not a Secrets Manager ARN or name: `rayd` has no AWS
  credentials of its own to resolve one, and ADR-014 already settled that
  the SDK, not the agent, is what talks to AWS. The value crosses exactly
  once, over the already-authenticated `Configure` channel, and `rayd`
  keeps it only in memory (`SecretValue`, `Zeroizing`, never `Debug`).
- **D4 — `GatewaySectionFactory`: the `configure_sections` convention.**
  `_feature_options.plan_features` runs before `create()` resolves the
  `SecretCache` (region, session, `secret_cache=` vs. the shared default),
  so it cannot build a finished `GatewaySection` yet. `FeaturePlan.
  configure_sections` is typed `tuple[Any, ...]` on purpose; this change
  defines what an entry that needs something resolved later looks like: a
  one-argument callable over the resolved cache, invoked once by the
  generic dispatcher this change also adds to `sandbox_{sync,async}/main.py`
  (`_apply_configure_sections`) and `sandbox/sandbox.ts`
  (`#applyConfigureSections`). A future feature that needs nothing resolved
  later can just put a finished `ConfigureSection` in the tuple directly;
  the dispatcher calls `.build(cache)` when the entry exposes it and uses
  the entry as-is otherwise — see `_apply_configure_sections`'s one
  `isinstance`-free call site (TS: a narrow structural cast, since nothing
  else yet implements the shape).

  Dispatcher behavior on a mixed tuple: in Python, every entry a feature
  puts into `configure_sections` is a `GatewaySectionFactory`-shaped
  callable (`Callable[[SecretCache], ConfigureSection]`) by convention;
  the dispatcher always calls it with the cache. A feature that needs
  nothing from the cache still wraps itself trivially: `lambda _cache:
  MySection(...)`. This keeps the dispatcher itself free of per-feature
  branching.
- **D5 — No `LifecycleParticipant`.** `/suspend`'s bounded flush exists for
  state that must reach durable storage before the VM freezes (events,
  telemetry). A gateway holds no such state: an in-flight request is a TCP
  connection like any other, and the sandbox's own HTTP client already
  handles a connection reset by retrying. Giving the gateway a participant
  would add a `/suspend` budget share for nothing to flush.
- **D6 — Streaming, not buffering, both directions.** The research flags
  SSE passthrough (SEC-7) as something the AWS acceptance stage measures.
  Buffering the whole body first would make that measurement meaningless
  (every response would complete only after the whole stream did) and
  would silently cap request/response size. `axum::body::Body` and
  `hyper::body::Incoming` both already stream without extra code once the
  client and the listener pass them through directly, so there is no
  "simpler buffered version first" — the streaming version is not more
  code.
- **D7 — Degrade to `Unsupported`, not panic, if the OS trust store is
  unreadable.** Mirrors `main::transfer_services`'s own fallback for the
  presigned-transfer client: the same missing trust store would already
  break persistence, so a feature-local panic adds nothing but a worse
  failure mode.

## Risks / Trade-offs

- **Per-route listener count is a hard cap (8), not negotiable per
  sandbox.** Acceptable: a sandbox talking to more than 8 distinct
  credentialed upstreams through this feature is not the design point: one
  more could always `fetch` plainly to a public API, or the sandbox can run
  two separate gateway sandboxes.
- **A route's allow list has no path-parameter syntax, only an exact path
  or a `/*` prefix.** Simpler to reason about and to match in both `rayd`
  and the docs; a caller needing finer-grained control writes more, shorter
  allow rules instead.

## Migration

None: `gateways=`/`gateways` is a brand-new, off-by-default option. No
existing behaviour changes for a caller who never sets it.
- **D8 — A generic post-apply hook, not feature names in `create()`.**
  `_configure_base.PostApplySection`/`configure/base.ts`'s
  `PostApplySection` add one optional method, `after_apply(SectionApplied)`
  (`afterApply` in TS). It receives the `ConfigureStatus` taken once after
  the bundled `Configure`, plus a `reapply` callable that re-fills only that
  section, sends `Configure`, raises on any non-`APPLIED` result (the
  same per-section `check_configure_response`/`checkConfigureResponse`
  as `create()`) and
  returns the new `ConfigureStatus`. `main.py`/`sandbox.ts` store what it
  returns under the section's name, and `sbx.gateways` reads it from there.
  The create path names no feature, so s3-mounts, efs-volumes, events and
  telemetry add a section without touching those lines.
- **D9 — Every configure failure after `run-microvm` terminates the VM.**
  Same rule as `_apply_initial_network` (ADR-012): a pre-0.6 image, a
  `false` flag, a missing secret, a failed RPC or a non-`APPLIED` section
  closes the client and calls `TerminateMicrovm` unless
  `keep_on_failure`/`keepOnFailure`. `SandboxPool.take(gateways=)` always
  terminates the taken slot, since slots are launched without
  `keep_on_failure`.
- **D10 — `refresh()` always re-reads.** It invalidates every secret the
  gateway injects in `SecretCache` before re-filling, so a rotation right
  after `SecretStore.update()` never re-sends the cached value (TTL 300 s by
  default). The cost is one `GetSecretValue` per injected secret per
  `refresh()`.
- **D11 — Upstream errors and timeouts.** Only an elapsed
  `CONNECT_TIMEOUT` (10 s) or `RESPONSE_HEAD_TIMEOUT` (600 s, the wait for
  the status line and headers; a streamed SSE body is never bounded) is
  `upstream_timeout` (504). Any other failure after connecting (TLS, reset,
  malformed response) is `upstream_error` (502). `Health.features.
  secret_gateway` comes from the built `FeatureSet` (shared with
  `ConfigureGrpc`), so a slot that degraded to `Unsupported` is never
  advertised.
