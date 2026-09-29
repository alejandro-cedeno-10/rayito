## Context

Four TypeScript findings from the 2026-09-24 hexagonal/DDD review
(`docs/research/2026-09-m9-architecture-review.md`), all inside
`clients/typescript`, none requiring a `.proto`/`rayd` change or a
published-API change. Two more findings from the same review
(`Sandbox.probedInfo`, the integration-name regex mismatch) are read and
reported, not implemented, because their only clean fixes are themselves
behaviour changes this wave does not ask for.

## Decision 1 — share the `TransferClient`, not the `Filesystem`

The review's own suggested fix was composition: make the shim `Filesystem`
wrap `native.files` like `e2b/pty.ts` wraps the native `Pty`. That would
work, but it changes the shim's published shape: `sbx.files instanceof
rayito.Filesystem` would become `false` (a `Proxy` or hand-written
delegate is not the native class), and the `.d.ts` for `rayito/e2b` would
have to declare a new, separate `Filesystem` type instead of reusing
`rayito`'s. That is a bigger surface change than the finding calls for —
the actual defect is that two `TransferClient`s (and two `#supported`
probe caches, two S3 clients) exist per sandbox, not that the shim
`Filesystem` happens to be a subclass.

The narrower fix: let the native `Filesystem` constructor accept the
sibling it should share a `TransferClient` with.

```ts
constructor(core: SandboxCore, sharedWith?: Filesystem) {
  this.core = core;
  this.#transfers =
    sharedWith === undefined ? new TransferClient(core, entryInfoFromProto) : sharedWith.#transfers;
}
```

(`sharedWith?.#transfers` — optional chaining into a private field of
another instance — is not legal TypeScript/JS syntax, `TS18030`; the
`sharedWith === undefined ? … : sharedWith.#transfers` form is, and ES
private fields are accessible on any instance of the same class from
inside the class body, so no accessor is needed.)

`e2b/filesystem.ts`'s class shape does not change at all: it still
`extends NativeFilesystem` and still overrides only `watchDir`; the new
constructor parameter is inherited. `e2b/sandbox.ts:126` becomes `new
Filesystem(native.files.core, native.files)`, so the shim's `Filesystem`
and the native `Filesystem` it wraps share one `TransferClient`, hence one
probe cache and one set of S3 clients — while `sbx.files instanceof
rayito.Filesystem` still holds, `.core` still works, and the only `.d.ts`
diff is the new optional parameter (confirmed by diffing
`dist/e2b.d.mts`/`dist/index.d.mts` and their `sandbox-*.d.mts` chunk
before and after this change: the constructor line and the new
`ControlPlane` field from Decision 2 are the only two changes in ~5600
lines).

A bare `new Filesystem(core)` (no second argument) is unaffected: it still
builds and owns its own `TransferClient`, and probes on its own — pinned by
a new test in `filesystem.test.ts`.

## Decision 2 — `awsClientSettings` becomes a port field, not a downcast

`awsClientSettingsOf` used to read `(plane as
Partial<AwsClientSettingsSource>).awsClientSettings`: a structural
downcast that works today only because every real `ControlPlane`
implementation (`LambdaMicrovmsControlPlane`, `LaunchObserver`) happens to
carry that property. Nothing in the `ControlPlane` interface required it,
so a new decorator or a hand-written fake could omit it and silently get
the default AWS credential chain and no proxy for its S3 traffic — not an
error, just wrong.

The fix makes the dependency explicit at the type level:
`ControlPlane.awsClientSettings?: AwsClientSettings | undefined`, with a
JSDoc telling any decorator it must forward the field. `awsClientSettingsOf`
becomes a one-line projection, `plane.awsClientSettings ?? {}`, with the
same exported name so `pool/pool.ts` and `sandbox/transfer.ts`'s call
sites need no change. The redundant `AwsClientSettingsSource` interface is
deleted (confirmed nothing else imports it, `grep -rn
AwsClientSettingsSource src tests`).

The review's "better" alternative — resolve S3 overrides once at
`Sandbox.create`/`connect` and inject them into `SandboxCore`, decoupling
the transfer adapter from the control-plane adapter entirely — is a larger
composition-root change explicitly out of scope for this wave (see
proposal's Non-goals); the port field alone already closes the finding's
actual defect (an implicit, structurally-typed contract with no compiler
enforcement).

New test coverage goes one step further than the existing
credential/proxy-forwarding tests: a bare object literal that implements
`ControlPlane` (not a `LambdaMicrovmsControlPlane`, not `LaunchObserver`,
not `FakeControlPlane`) with `awsClientSettings: { credentials }` is
honoured by `s3ClientOverridesFor`, and the same literal without the field
yields `{}` — proving the port field, not a coincidence of which classes
happen to expose the property.

## Decision 3 — break the cycle with a new leaf module, not a merge

`core.ts` (transports, clients, the M5 reconnection contract) value-imports
`STREAM_PROBE_TIMEOUT_MS`/`streamFailureError` from `commands.ts` (command
streams, `CommandHandle`), which in turn value-imports `withTimeout` from
`core.ts`. Two ways to break this: merge the two constants into `core.ts`
directly, or extract them into a third module both import. Merging into
`core.ts` would make the lower-level module define a command-stream-shaped
error type (`streamFailureError`'s `state`/`healthOk` options exist for
`core.ts`'s own stream-reconnection callers, but the function's name and
shape are generic to any stream, not commands-specific) — workable, but it
keeps `core.ts` as the place new stream-error logic would go, drifting it
further from "transports and clients" toward "everything".

The extraction is cleaner and mechanical: `sandbox/stream-errors.ts` is a
new leaf with no imports from `core.ts` or `commands.ts` (confirmed by
`grep -n "core.js\|commands.js" src/sandbox/stream-errors.ts` — no matches),
holding exactly the two moved symbols verbatim, with the imports they need
(`ConnectError` type, `isStreamReset`/`translateRpcError` from
`transport/errors.ts`, the three `Sandbox*Error` classes, `TERMINAL_STATES`/
`SUSPENDED_STATES` from `limits.ts`). `core.ts` imports from
`./stream-errors.js` instead of `./commands.js`. `commands.ts` re-exports
both names (`export { STREAM_PROBE_TIMEOUT_MS, streamFailureError } from
"./stream-errors.js"`), so every existing `import … from
"./sandbox/commands.js"` (production and test code alike) keeps working
unchanged — verified by the full test suite passing with zero import-path
edits outside `core.ts` and `commands.ts` themselves.

No `madge` (or any new dependency) was added per the plan's constraint;
the "no remaining cycle" claim rests on the grep above plus `tsc --noEmit`
succeeding (a cycle here would not be a compile error, so this is a
structural check, not a type check) and the full test suite passing.

## Decision 4 — `metricsHistoryOrSnapshot` is the exact existing branch
structure, relocated

The instance `getMetrics`'s current body:

```ts
try {
  history = (await this.native.getMetricsHistory({ start, end, … })).map(metricsFromNative);
} catch (error) {
  if (!isHistoryUnavailable(error)) throw error;
  if (!unbounded) throw historyImageError("getMetrics({ start, end })", error);
  history = [];
}
if (history.length > 0 || !unbounded) return history;
return [metricsFromNative(await this.native.getMetrics({ … }))];
```

becomes a call to a pure `compat.ts` helper parameterised over two
closures (`history`, `snapshot`) and `{ ranged, feature }` where `ranged =
!unbounded`:

```ts
export async function metricsHistoryOrSnapshot(
  history: () => Promise<SandboxMetrics[]>,
  snapshot: () => Promise<SandboxMetrics>,
  opts: { readonly ranged: boolean; readonly feature: string },
): Promise<SandboxMetrics[]> {
  let result: SandboxMetrics[];
  try {
    result = await history();
  } catch (error) {
    if (!isHistoryUnavailable(error)) throw error;
    if (opts.ranged) throw historyImageError(opts.feature, error);
    result = [];
  }
  if (result.length > 0 || opts.ranged) return result;
  return [await snapshot()];
}
```

This is a mechanical translation (`!unbounded` → `ranged`, the two native
calls become the two closures), not a rewrite: the existing
`getMetrics`-through-a-fake-transport tests in `e2b-shim.test.ts`/
`metrics.test.ts`/`e2b-compat.test.ts` (unbounded empty → snapshot, pre-M9
unbounded → snapshot, pre-M9 ranged → `UnimplementedError`) pass unchanged
against the new call site, and new pure tests exercise the helper directly
with stub closures — including that `snapshot` is never invoked when
`history` returns non-empty or the query is ranged, and that a non-history
error propagates by reference identity (`toBe`, not `toEqual`) regardless
of `ranged`.

`metricsFor` (the static, class-level `Sandbox.getMetrics(sandboxId)`
variant) is deliberately **not** routed through this helper: its policy has
no snapshot fallback — any error from `getMetricsHistory` goes through
`historyImageError` and is thrown, full stop. Routing it through
`metricsHistoryOrSnapshot` would require inventing a `snapshot` closure
that never gets called for that variant's actual behaviour, which is more
indirection for no behavioural gain; the plan explicitly calls this out
("its policy differs: no snapshot fallback — do not route it through the
new helper").

`historyImageError`, `lifecycleImageError` and `LIFECYCLE_IMAGE_REASON`
move to `compat.ts` verbatim (same bodies, same JSDoc); `e2b/sandbox.ts`
re-exports `LIFECYCLE_IMAGE_REASON` from `./compat.js` so any code
importing it from `e2b/sandbox.ts` (none exists in this repo today, but
the plan asks the path stay stable) keeps working.

## Risks / trade-offs

- The `sharedWith === undefined ? … : sharedWith.#transfers` ternary reads
  slightly less idiomatically than optional chaining, but the alternative
  (`TS18030`) does not compile; this is the only legal shape for reaching
  a private field of a sibling instance without adding a public/protected
  accessor, which the plan explicitly rules out ("nothing else in the
  class changes").
- `stream-errors.ts` and `compat.ts` both grow by one file/one function
  respectively; neither changes any existing test's expectations, so the
  risk is purely "did every import path get re-pointed", checked by the
  full build + test suite, not by inspection alone.
