## Why

Agents built on Rayito already run inside the caller's own observability
stack; today the only way to see what the SDK itself is doing (how long
`create()` took, whether a command exited non-zero, how many bytes a
`files.write` moved) is to wrap every call by hand. E2B's SaaS offers OTel
telemetry export as an Enterprise feature of its hosted control plane
(`e2b-parity.md` row 108, "fuera por SPEC": no Rayito-hosted server exists to
export anything from). ADR-014 (M11) already lets an optional component live
entirely in the caller's account; here the "account resource" is simply the
caller's own `TracerProvider` — Rayito never creates, calls or pays for
anything AWS-side for this. The research read
(`docs/research/2026-10-e2b-out-of-scope.md` §6, §7) frames this as
client-side spans on the SDK's own operations, not sandbox-side telemetry,
and explicitly excludes propagating trace context into `rayd` or exporting
anything from inside the MicroVM.

## What Changes

- **`rayito._otel` / `src/otel.ts` (new).** An `Instrumentation` facade:
  `NOOP` (default, no `tracer_provider=`/`tracerProvider`) whose `span()`
  does nothing (Python: the same `contextlib.nullcontext(...)` singleton
  every call, wrapping a no-op span so callers never branch on `None`; TS:
  runs the callback directly, no allocation). With a provider, Python lazily
  imports `opentelemetry.trace` through `require_module(..., extra="otel")`
  (never at import time, never without a provider) and opens
  `SpanKind.CLIENT` spans on a `rayito` tracer; TypeScript only ever
  `import type`s `@opentelemetry/api` (erased by `tsdown`, so the published
  bundle carries no runtime `import`/`require` of it — asserted by a
  `pack:check` grep) and uses local numeric constants for `SpanKind.CLIENT`
  (2) and `SpanStatusCode.ERROR` (2), calling `provider.getTracer(...)` and
  `tracer.startActiveSpan(...)` directly. A closed attribute allow-list
  (`ALLOWED_SPAN_ATTRIBUTES`, identical set in both languages) is enforced
  by the facade itself; an error inside a span is recorded with the
  exception/error **class name only** — never its message or stack, which
  could themselves carry the sensitive data the allow-list excludes.
- **SDK option `tracer_provider=` / `tracerProvider`** (default
  `None`/`undefined`) on `Sandbox.create()` (also with `pool=`, which binds
  the instrumentation to the taken handle without its own span),
  `connect()` (both the static and the instance form — `None`/`undefined`
  on the instance form keeps the handle's current instrumentation) and the
  class/static `kill`/`pause`/`resume` (the instance forms reuse the
  handle's instrumentation from `create()`/`connect()`, no new parameter).
  Spans: `rayito.sandbox.{create,connect,kill,pause,resume}`,
  `rayito.code.run`, `rayito.commands.run` (closes when `start` returns the
  handle in the background, not when the process ends),
  `rayito.files.{read,write,write_files,list,exists,get_info,remove,
  rename,make_dir}`. `create()` is a single span wrapping `run-microvm`,
  readiness, the optional index write and the initial network policy.
- **Packaging.** Python `otel` extra (`opentelemetry-api>=1.27,<2`) plus
  `opentelemetry-api`/`opentelemetry-sdk` in the `dev` group;
  `scripts/check_wheel.py` asserts `Provides-Extra: otel` and its
  `Requires-Dist` line, with its own unit test. TypeScript
  `@opentelemetry/api` as an **optional** peerDependency (`^1.9.0`) plus
  `@opentelemetry/api`/`@opentelemetry/sdk-trace-base` as devDependencies;
  `scripts/pack-check.mjs` greps every built `.mjs`/`.cjs` chunk for a
  runtime reference to `@opentelemetry/api` and fails the build if it finds
  one; the existing "future optional peers" guard test is narrowed so a
  type-only `import type` of `@opentelemetry/api` is allowed while a real
  (value) import still fails it.
- **Docs**: `observability.md` new section "Trazas OpenTelemetry del SDK
  (opcional)" (install, example, span/attribute tables, what is never
  recorded, scope); `optional-features.md` row 4 → "disponible (0.5.0)"
  with runnable Python/TS examples (its acceptance gate is unit tests with
  an in-memory exporter, not a real-AWS e2e, since the feature makes no AWS
  call); `e2b-parity.md` row 108 → "divergente" plus the status counts
  (72/22/8/11); `e2b-compat.md` a short paragraph distinguishing the native
  spans from E2B's Enterprise sandbox telemetry and noting the shim is not
  instrumented.

## Migración

Nothing changes for code that does not opt in: `tracer_provider=`/
`tracerProvider` defaults to `None`/`undefined` everywhere, Python never
imports `opentelemetry` without it (asserted in a clean subprocess) and the
published TypeScript bundle never references `@opentelemetry/api` at
runtime regardless of whether a caller imports types from it. Nothing in
`rayd`, the wire protocol or `runHookPayload` changes; a span's attributes
are a strict subset of a fixed allow-list, so adopting this cannot
accidentally start exporting command text, code, paths, env values, secret
names/values, metadata values, the access token, the JWE or presigned URLs.

## Coste

- **AWS**: $0. Rayito creates no AWS resource and makes no AWS call for
  this option; it only opens spans on the `TracerProvider` the caller
  already has.
- **Exporter**: whatever the caller's own OTel backend costs (a Collector,
  Jaeger, X-Ray, CloudWatch via ADOT, …) — entirely outside Rayito and
  entirely the caller's choice.
- **Off**: no option set = zero extra imports, zero extra allocations in
  the hot path (Python: `NOOP.span()` returns the same singleton
  context manager every call; TS: the callback runs directly).

## Capabilities

### New Capabilities

- `otel-sdk`: the `Instrumentation` facade, the `tracer_provider=`/
  `tracerProvider` option on the lifecycle/commands/code/files operations
  listed above, the attribute allow-list and the error-recording contract.

## Impact

- **Python**: `rayito/_otel.py` (new), `sandbox_{sync,async}/
  {main,commands,code,filesystem}.py`, `pyproject.toml`,
  `scripts/check_wheel.py`.
- **TypeScript**: `src/otel.ts` (new), `sandbox/
  {sandbox,commands,code,filesystem}.ts`, `package.json`,
  `pnpm-lock.yaml`, `scripts/pack-check.mjs`,
  `tests/unit/optional.test.ts` (narrowed future-peer guard).
- **Tests**: `clients/python/tests/unit/test_otel_instrumentation.py`,
  `test_otel_sync.py`, `test_otel_async.py`;
  `clients/typescript/tests/unit/otel.test.ts`,
  `otel.integration.test.ts`; `scripts/tests/test_check_wheel.py` (new).
- **No changes** to `crates/`, the `.proto` wire contract, `runHookPayload`,
  trace-context propagation into `rayd`, any sandbox-side telemetry,
  `get_metrics_history()`, the E2B shim (`rayito.e2b`/`rayito/e2b`,
  deliberately not instrumented in this milestone), spans on
  `list`/`paginate`/pool/`git`/`pty`/transfer operations, or any
  `RAYITO_OTEL_*` environment variable (forbidden by ADR-014).
