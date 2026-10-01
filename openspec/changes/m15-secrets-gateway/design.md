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
- **D2 — Replace-whole-state on every `Configure`, never a diff.**
  `SecretGatewayConfig`'s own semantics already say a present section is
  the complete desired state. Diffing routes (keep unchanged ones running,
  only restart changed ones) would save a reconnect per unrelated route on
  every rotation, but doubles the state machine for a feature whose
  `Configure` calls are expected to be rare (initial setup, then only
  rotation). Revisit if acceptance testing shows rotation is frequent
  enough to matter.
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
