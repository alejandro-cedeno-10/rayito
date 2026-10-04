# otel-sdk Specification

## Purpose
TBD - created by archiving change m13-otel-sdk. Update Purpose after archive.

## Requirements

### Requirement: The instrumentation facade is opt-in, lazy and allocation-free when off
`tracer_provider=` (Python) / `tracerProvider` (TypeScript) SHALL default to `None`/`undefined` on `Sandbox.create()` (including with `pool=`), `connect()` (both the static and instance forms) and the class/static `kill`/`pause`/`resume`. Without it, `instrumentation_for(None)`/`instrumentationFor(undefined)` SHALL return a shared `NOOP` instrumentation whose `span()` SHALL NOT import `opentelemetry`/`@opentelemetry/api` and SHALL NOT allocate a new object per call (Python: the same `contextlib.nullcontext(...)` instance every call; TypeScript: the callback runs directly, synchronously or asynchronously, with no wrapper object created). `rayito._otel` SHALL import `opentelemetry.trace` only inside the code path that already received a provider (via `require_module`, never at module import time); `src/otel.ts` SHALL reference `@opentelemetry/api` only through `import type`, so the published bundle carries no runtime `import`/`require` of it.

#### Scenario: importing rayito never imports opentelemetry
- **WHEN** `import rayito` runs in a clean Python subprocess, followed by a `Sandbox.create()`/`commands.run()` against a fake agent with no `tracer_provider=`
- **THEN** `opentelemetry` is not in `sys.modules`

#### Scenario: the published TypeScript bundle has no runtime import of the peer
- **WHEN** `pnpm pack:check` builds the package and inspects every `.mjs`/`.cjs` file under `dist/`
- **THEN** none of them contains a `require("@opentelemetry/api")`, `from "@opentelemetry/api"` or `import("@opentelemetry/api")`

#### Scenario: NOOP never allocates a new span per call
- **WHEN** `NOOP.span(name, attributes)` is called twice with different arguments (Python)
- **THEN** both calls return the identical object

### Requirement: Span attributes are a closed, non-sensitive allow-list
Every span opened by `rayito._otel`/`src/otel.ts` SHALL only carry attributes whose keys are in `ALLOWED_SPAN_ATTRIBUTES` (`rayito.sandbox.id`, `rayito.region`, `rayito.template.name`, `rayito.resume_generation`, `rayito.operation`, `rayito.commands.exit_code`, `rayito.commands.background`, `rayito.code.language`, `rayito.files.operation`, `rayito.files.count`, `rayito.files.bytes`, `rayito.error.type`); a caller-reachable path that would set another key SHALL raise before the span opens. No span, event or status description SHALL ever carry command text or arguments, source code, file paths, `envs` values, secret names or values, `metadata` values, the access token, the JWE, or a presigned URL.

#### Scenario: an unknown attribute key is rejected before opening the span
- **WHEN** `instrumentation.span("rayito.commands.run", {"rayito.commands.cmd": "ls"}, ...)` is called with a `TracerProvider`
- **THEN** it raises `InvalidArgumentException` (TS: throws) naming the offending key, and no span is recorded

#### Scenario: a sentinel value never reaches any attribute or event
- **WHEN** a command, a file path or an exception message containing a planted sentinel string is used inside an instrumented operation that then fails
- **THEN** the sentinel does not appear in any attribute, any event, or the span's status description across the whole span tree

### Requirement: A span records only the exception's class name, never its message or stack
When an exception/error propagates out of an instrumented operation's span, the facade SHALL call `record_exception`/`recordException` with the exception's class name substituted for its message and stack trace, and SHALL set the span status to `ERROR` with that same class name as the status description — the real message and stack SHALL NOT be passed to either call, because they may themselves contain the sensitive data the attribute allow-list excludes.

#### Scenario: error status carries only the class name
- **WHEN** `commands.run("exit 7")` raises `CommandExitException` inside an instrumented span
- **THEN** the span's status is `ERROR`, its status description is exactly `"CommandExitException"`, and neither the status description nor the recorded exception event contains the exception's original message

### Requirement: create() is a single span wrapping provisioning, readiness, the index write and network setup
`Sandbox.create()` SHALL open exactly one `rayito.sandbox.create` span (`SpanKind.CLIENT`) that wraps `run-microvm`, the readiness wait, the optional metadata-index write and the initial network policy application; it SHALL carry `rayito.region` from the start and SHALL add `rayito.sandbox.id` as soon as `run-microvm` returns an id. `create(pool=...)` SHALL bind the resolved instrumentation to the handle `pool.take()` returns without opening a `create()` span of its own (the pool, not `create()`, provisioned it).

#### Scenario: one span per create(), nested under a caller span
- **WHEN** a caller's own span is active and `Sandbox.create(tracer_provider=provider)` runs against a fake control plane and agent
- **THEN** exactly one `rayito.sandbox.create` span is recorded, it is `SpanKind.CLIENT`, its parent is the caller's span, and its attributes include `rayito.sandbox.id` and `rayito.region`

#### Scenario: create(pool=) does not open its own span
- **WHEN** `Sandbox.create(pool=a_pool, tracer_provider=provider)` takes an already-provisioned slot
- **THEN** no `rayito.sandbox.create` span is recorded for this call, and the returned handle's later operations (e.g. `commands.run`) are instrumented with `provider`

### Requirement: Lifecycle, commands, code and files operations each open their named span
`connect()` (static and instance; `None`/`undefined` on the instance form keeps the handle's current instrumentation), the instance and class/static `kill`, `pause` and `resume` SHALL each open `rayito.sandbox.{connect,kill,pause,resume}`. `commands.run`/`run_code`/`runCode` SHALL open `rayito.commands.run` / `rayito.code.run`; in the background, `rayito.commands.run` SHALL close as soon as `run`/`start` returns the handle, not when the backgrounded process later exits, and `rayito.commands.exit_code` SHALL only be set on the foreground path, after `wait()` resolves. `files.read`, `write`, `write_files`, `list`, `exists`, `get_info`, `remove`, `rename` and `make_dir` SHALL each open `rayito.files.<op>` with `rayito.files.operation` set to that same op name; `write`/`write_files`/`list`/`read` (for `text`/`bytes` formats) SHALL additionally set `rayito.files.bytes`/`rayito.files.count` once the result is known. Every span SHALL be `SpanKind.CLIENT`. Without `tracer_provider=`/`tracerProvider` on `create()`/`connect()`, every existing unit test for these operations SHALL keep passing unchanged.

#### Scenario: a background command's span closes before the process ends
- **WHEN** `commands.run("sleep 30", background=True)` is called with instrumentation active
- **THEN** the `rayito.commands.run` span is already finished by the time `run` returns the handle, carries `rayito.commands.background=True`, and does not carry `rayito.commands.exit_code`

#### Scenario: a foreground command's span carries the exit code
- **WHEN** `commands.run("echo hola")` completes successfully with instrumentation active
- **THEN** its `rayito.commands.run` span carries `rayito.commands.background=False` and `rayito.commands.exit_code=0`

#### Scenario: existing unit tests are unaffected without the option
- **WHEN** the full unit test suite for `commands`, `code` and `filesystem` runs with no `tracer_provider=`/`tracerProvider` anywhere
- **THEN** every test passes exactly as before this change

### Requirement: Packaging declares the optional dependency without adding it to the base install
Python's `pyproject.toml` SHALL declare `[project.optional-dependencies] otel = ["opentelemetry-api>=1.27,<2"]` and include `rayito[otel]` plus `opentelemetry-sdk` in the `dev` dependency group; `scripts/check_wheel.py` SHALL assert the built wheel's `METADATA` contains `Provides-Extra: otel` and a `Requires-Dist` line for `opentelemetry-api>=1.27,<2` conditioned on `extra == 'otel'`. TypeScript's `package.json` SHALL declare `@opentelemetry/api` (`^1.9.0`) as an **optional** peerDependency (`peerDependenciesMeta` marks it optional) and as a devDependency alongside `@opentelemetry/sdk-trace-base`; the base runtime `dependencies` SHALL NOT change.

#### Scenario: the wheel declares the otel extra
- **WHEN** `scripts/check_wheel.py` inspects a wheel built from this `pyproject.toml`
- **THEN** it reports no problem for the `otel` extra's `Provides-Extra`/`Requires-Dist` lines

#### Scenario: a caller without the peer installed still gets a working NOOP
- **WHEN** `Sandbox.create()` runs in TypeScript with no `tracerProvider` and `@opentelemetry/api` is not installed in `node_modules`
- **THEN** the call succeeds exactly as in 0.4.0
