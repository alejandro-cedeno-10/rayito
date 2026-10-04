## Context

M15 (Rayito 0.6) is ten OpenSpec changes: this one first, eight feature
changes building in parallel from the `main` this change merges into, and
`m15-docs-integration` last, applying every feature's `docs-delta.md` to
the shared markdown tables in one PR. The full architecture (module map,
proto design, shared-file protocol, per-feature API) is the "M15
foundations" plan handed to this change's agent; this `design.md` records
only the decisions internal to this change plus the four the maintainer
still has to make (D1-D4), drafted here but **not applied**.

## Decisions (this change)

### E1. `ConfigureGrpc` builds its own `FeatureSet` inside `router_with_transfers`, not via a new `Services` field

Threading `Arc<FeatureSet>` through `grpc::Services` (constructed in
`main.rs` and in a dozen integration test files) would touch every
integration test in `crates/rayd/tests/` for a value that is always the
same stateless, all-`Unsupported` set in this change. `ConfigureGrpc`
builds `FeatureSet` fresh inside `router_with_transfers` instead. The
first feature that needs real shared context (a credential broker handle,
a bucket name) promotes this to a `Services` field in its own change —
a one-time, well-motivated touch of that shared struct instead of a
speculative one now.

### E2. Zombie reaping ships as a correct, tested, dormant building block

`rayd_core::orphans::orphans_to_reap` and the `ChildRegistry`/
`OrphanReaper` adapter are complete and unit-tested, but `OrphanReaper` is
not added to `main.rs`'s `reapers` vec and `ChildRegistry` is not wired
into `process_spawner`/`pty_backend`/`sidecar_process` in this change.
Activating the reaper without that registration would be actively unsafe:
every zombie whose `ppid` is `rayd`'s own pid — including `rayd`'s own
pending direct children, which tokio's internal reaper has not yet
`waitpid`'d — would look "not owned" and get reaped out from under tokio,
hanging that `Child::wait` forever. Wiring the registration is a
three-file, carefully-reviewed change on its own; shipping the unsafe
half now to claim the feature "works" would violate hard rule 8 (stop
when something doesn't fit, don't improvise). Tracked as a non-blocking
follow-up in `proposal.md`.

### E3. `template_start`'s slot does not fit the five-section `ConfigureSandbox` shape

`configure.proto`'s `ConfigureRequest` only has fields for the five
features with a real `ConfigSection` (events, telemetry, gateway,
s3_mounts, efs_volumes); `template_start` configures itself by reading
`/etc/rayito/template.json` at boot, never through `ConfigureSandbox`.
`FeatureSet.template_start` is still declared (`ConfigurableFeature<(),
()>`) so `supported()`/`participant()` exist uniformly, but
`ConfigureGrpc` never dispatches to it and `m15-templates` may narrow or
reshape this one slot's type in its own change without touching the other
five.

### E4. The TS `BuildError`/`TemplateError` collision is deferred, not silently resolved

`src/e2b/errors.ts` already declares `TemplateError`/`BuildError`
stand-ins with a *different* shape than this change's new top-level
classes (the shim's `BuildError` is a plain `Error`, asserted by
`tests/unit/e2b-exports.test.ts` to **not** be a `SandboxError`; the new
one is a `SandboxError` subclass with `reason`/`step`/`command`/
`exitCode`/`logTail`). Exporting the new classes from `index.ts` under the
same names would either break that existing, intentional assertion or
require rewriting it speculatively for a feature not yet built. The new
classes are defined (the seam the architecture asks foundations to
pre-create) but not exported from `index.ts`; `m15-templates` makes the
real call once it knows which shape it actually needs.

### E5. The `OptionalStacks` cost block is data, not a TSDoc comment

Every other ADR-014 opt-in (`SecretStore`, `index=`, `tracer_provider=`)
documents its "Coste y activación" as a docstring/TSDoc block that
`check-dts-cost-blocks.mjs`/`test_optional_features_docs.py` can grep.
`OptionalStacks` is a single generic mechanism serving nine components
with nine different cost profiles; a static comment on the class cannot
describe them all correctly. Each component's `CostStatement` carries its
own structured cost data instead, and `rayito stack deploy`/`OptionalStacks.deploy`
prints it before acting. `COST_DECLARATIONS`/`FUNCTION_ANCHORS` are left
untouched (E6) since no row names `OptionalStacks` itself.

### E6. Drop-in registries only converted where a feature already exists to prove them

The architecture names three "drop-in registry" conversions. This change
does the one with an existing concrete need
(`crates/rayito-proto/build.rs`, which must glob five new per-feature
`.proto` files this change itself adds). The other two
(`check-dts-cost-blocks.mjs`'s `COST_DECLARATIONS`,
`test_optional_features_docs.py`'s `FUNCTION_ANCHORS`) have no new entry
to add yet in this change (no feature here gets a "disponible" row), so
converting them now would be an untested refactor of a passing, unrelated
check. Left as a non-blocking follow-up for whichever change first adds a
row.

## Needs the maintainer (drafted, not decided)

### D1 — Non-goals (`SPEC.md` §4, `openspec/project.md` hard rule 3)

Proposed amendments, to apply once the maintainer confirms:

- Remove "Templates declarativos con CLI propia" as a non-goal (ADR-022
  `m15-templates` delivers exactly this).
- Replace "No hay, ni se promete, un resolvedor de tamaño por sandbox" with
  language reflecting `size=`/`size` resolving to a published image
  variant (ADR-019 `m15-sizes-catalog`).
- Change the volumes line ("Volúmenes: aprobados sobre S3 ... fuera de
  M11-M14") to "S3 mounts + EFS experimental" (ADR-017/ADR-018).

### D2 — ADR-014 rule 2 wording

Today: "the user deploys templates explicitly with the aws CLI." Proposed
(ADR-016): "or via an explicit SDK/CLI call (`OptionalStack`)." The rule
stays explicit and never automatic either way; this is a wording update to
match the new convention, not a behavior change.

### D3 — custom-domain acceptance

`m15-custom-domain`'s AWS acceptance needs a domain the maintainer owns and
an ACM certificate in `us-east-1`. No feature agent can create either;
acceptance for that one change blocks on the maintainer providing them.

### D4 — efs-volumes measurement budget

Up to 1 h of NAT gateway time (≈ $0.10), only if the EFS-4 measurement
shows a VPC connector cannot coexist with `INTERNET_EGRESS`. Within the
$8.00 total M15 acceptance ceiling (`AWS acceptance budget` table); flagged
here because it is the one line item contingent on a result not yet
measured.
