## ADDED Requirements

### Requirement: telemetry export is off by default and adds zero AWS calls without it
No SDK call SHALL build a `ConfigureService` stub request carrying a `telemetry_export` section, and `rayd` SHALL open no outbound connection beyond what 0.5.x already opens, unless the caller sets `telemetry=`/`telemetry` to a `TelemetryExport` instance.

#### Scenario: a sandbox with no telemetry option matches the 0.5.x golden trace
- **WHEN** a scripted session runs with `telemetry` absent
- **THEN** the recorded boto3/AWS-SDK-v3 operations and gRPC methods equal the checked-in `zero_cost_0_5_trace.json` fixture exactly, and `rayd` emits no `rayito.event.v1`-style line nor opens any new socket

### Requirement: `TelemetryExport` validates its own fields before any AWS or gRPC call
`TelemetryExport`/`new TelemetryExport(...)` SHALL raise `InvalidArgumentException`/`InvalidArgumentError` when `interval_s`/`intervalS` is outside `15..=300` inclusive, when `service_name`/`serviceName` is blank, or when `names` is not `"rayito"` or `"e2b"`. `OtlpAuth.bearer(secret_name)`/`OtlpAuth.bearer(secretName)` SHALL raise the same exception for a blank secret name.

#### Scenario: an out-of-range interval is rejected at construction
- **WHEN** `TelemetryExport(interval_s=5)` (or `301`, `0`, a negative value) is constructed
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised immediately, before `Sandbox.create()` does anything else

### Requirement: execution-role auth is gated to the caps image variant before launch when the image name allows it
`plan_features`/`planFeatures` SHALL raise `UnimplementedError` before `run-microvm`/`RunMicrovm` when `telemetry=TelemetryExport(auth=OtlpAuth.execution_role())` is combined with a template name that resolves (by Rayito's `rayito-<variant>[-<size>]` convention) to a variant other than `base-caps`, and SHALL defer the decision to the agent's post-boot `Health.features` check when the variant cannot be determined from the name.

#### Scenario: execution-role auth on a known non-caps image is rejected before any AWS call
- **WHEN** `Sandbox.create("rayito-base", telemetry=TelemetryExport(auth=OtlpAuth.execution_role()))` is called
- **THEN** `UnimplementedError` is raised before `run-microvm` is called, naming the `base-caps` requirement

#### Scenario: bearer auth needs no caps variant
- **WHEN** `Sandbox.create("rayito-base", telemetry=TelemetryExport(auth=OtlpAuth.bearer("otlp-key")))` is called
- **THEN** the pre-launch caps check does not raise

### Requirement: a non-TelemetryExport value is rejected as an invalid argument, not as an unimplemented feature
`plan_features`/`planFeatures` SHALL raise `InvalidArgumentException`/`InvalidArgumentError` (never `UnimplementedError`) when `telemetry=`/`telemetry` is set to anything other than a `TelemetryExport` instance.

#### Scenario: a plain object is rejected as invalid, not unimplemented
- **WHEN** `Sandbox.create(template, telemetry=object())` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised naming the expected type

### Requirement: the section is sent only after the sandbox is confirmed ready, and an unsupported agent terminates the sandbox
Once `Sandbox.create()`/`AsyncSandbox.create()`/`Sandbox.create()` (TypeScript) confirms the sandbox is ready, if `telemetry` was set, it SHALL build and send one `ConfigureRequest` carrying the `telemetry_export` section. If the agent predates 0.6.0 (`Health.features` absent) or is 0.6.0 with `telemetry_export` still unsupported, it SHALL terminate the `MicroVM` (unless `keep_on_failure`/`keepOnFailure`) and raise `UnimplementedError` naming this change, without ever having sent the section's secret value (if any) anywhere but to `rayd`.

#### Scenario: an unsupported 0.6.0 agent terminates the sandbox and raises
- **WHEN** `telemetry=TelemetryExport(...)` is set and the agent's `Health.features.telemetry_export` is `false`
- **THEN** the sandbox's `MicroVM` is terminated (unless `keep_on_failure`/`keepOnFailure` is set) and `UnimplementedError` is raised naming the image/build requirement

#### Scenario: a pre-0.6.0 agent is reported distinctly from a 0.6.0 agent with the feature unsupported
- **WHEN** `telemetry` is set against an agent whose `Health` response has no `features` field at all
- **THEN** `UnimplementedError` names "necesita una imagen 0.6.0 o posterior", distinct from the message raised when `features.telemetry_export` is merely `false`

### Requirement: a bearer token's value never appears in a log, an exception message or `ConfigureStatus`
When `OtlpAuth.bearer(secret_name)` is used, the resolved secret value SHALL travel only inside the `TelemetryExportBearerAuth.token` field of the `ConfigureRequest` sent to `rayd`; it SHALL NOT appear in any SDK log line, any raised exception's message, or any `TelemetryExportStatus` returned by `ConfigureStatus`. `rayd` SHALL hold it only in process memory (`Zeroizing`), never write it to a log line, and never echo it back in `ConfigureStatus`.

#### Scenario: a secret resolution failure never repeats the secret's name
- **WHEN** `resolve_bearer_token`/`resolveBearerToken` fails to read the named secret
- **THEN** the raised `SecretException`/`SecretError`'s message does not contain the secret's name

#### Scenario: the resolved section never prints its token
- **WHEN** a `TelemetryExportSection` holding a resolved token is passed to `repr()` (Python) or `util.inspect`/`JSON.stringify` (TypeScript)
- **THEN** the output does not contain the token

### Requirement: a bearer secret name resolves exactly like `secrets=`
`OtlpAuth.bearer(secret_name)` SHALL be read through the handle's shared `SecretCache` (the one `secrets=`/`secrets` uses when no `secret_cache` is given), so the name SHALL resolve under the same `rayito/` prefix (a full ARN is used as is), errors SHALL be translated the same way, and the value SHALL be fetched at most once per cache TTL.

#### Scenario: a bare name reads the prefixed secret
- **WHEN** `telemetry=TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))` is applied
- **THEN** the SDK calls `GetSecretValue` with `SecretId="rayito/otlp-key"`, the same id `secrets={"X": "otlp-key"}` would read

### Requirement: `traceparent` reaches `rayd` only with `tracer_provider=`
A handle created or connected with `tracer_provider=`/`tracerProvider` SHALL add W3C `traceparent` (and `tracestate` when the active propagator uses it, never `baggage`) to every RPC on its own channel, computed per call over the active span. Without that option, no RPC SHALL carry either header, even if the process has a global OpenTelemetry propagator registered.

#### Scenario: no trace header without the option
- **WHEN** the 0.5.x golden-trace session runs with `tracer_provider` absent
- **THEN** no request `rayd` receives carries `traceparent` or `tracestate`

#### Scenario: the active span's context travels with the option
- **WHEN** `commands.run(...)` is called on a handle created with `tracer_provider=`
- **THEN** the `ProcessService.Start` request `rayd` receives carries a `traceparent` of the form `00-<32 hex>-<16 hex>-<2 hex>`

### Requirement: SigV4 exports survive a skewed guest clock
`rayd` SHALL sign each execution-role export with its wall clock shifted by the skew the last AWS response's `Date` header revealed, whenever that skew exceeds 60 seconds, and SHALL report a refused export whose `Date` header changed that skew as a retryable network failure rather than a rejection.

#### Scenario: a clock left behind by a long suspension is corrected
- **WHEN** an export is refused and the response's `Date` is ten minutes ahead of the guest clock
- **THEN** the next export is signed ten minutes ahead and the refused points stay queued for it

### Requirement: `get_telemetry_status()`/`getTelemetryStatus()` is an explicit call, never part of `get_health()`/`getHealth()`
`get_health()`/`getHealth()` SHALL make exactly the RPCs it made before this change (one `HealthService.Health` call) regardless of whether `telemetry` was ever set. `get_telemetry_status()`/`getTelemetryStatus()` SHALL be a separate method that calls `ConfigureService.ConfigureStatus` and returns a `TelemetryHealth`/`TelemetryHealth` of all zeros/`undefined` when `rayd` never applied a `telemetry_export` section.

#### Scenario: get_health never calls ConfigureStatus
- **WHEN** `sbx.get_health()`/`sbx.getHealth()` is called, with or without `telemetry` ever having been set
- **THEN** no `ConfigureService.ConfigureStatus` call is made

#### Scenario: telemetry status defaults to all zero without a configured exporter
- **WHEN** `sbx.get_telemetry_status()`/`sbx.getTelemetryStatus()` is called on a sandbox that never received a `telemetry_export` section
- **THEN** it returns `TelemetryHealth(exported=0, dropped=0, last_error_class=None)` / `{ exported: 0n, dropped: 0n, lastErrorClass: undefined }`, never an exception

### Requirement: exported attributes are drawn only from a closed vocabulary
A batch `rayd` exports SHALL carry exactly 4 resource attributes (`sandbox_id`, `image_arn`, `image_version`, `image_memory_mib`) and each metric point SHALL be one of exactly 7 named gauges (`cpu.used_pct`, `cpu.count`, `memory.used_bytes`, `memory.total_bytes`, `memory.cache_bytes`, `disk.used_bytes`, `disk.total_bytes`). No command, path, `envs` value or `metadata` value SHALL ever appear in an exported attribute or metric name.

#### Scenario: resource attributes never include anything outside the closed set
- **WHEN** `rayd` builds an `ExportMetricsServiceRequest`
- **THEN** its `Resource.attributes` contains exactly `service.name`, `sandbox_id`, `image_arn`, `image_version` and `image_memory_mib`, and no other key

### Requirement: the OTLP exporter never blocks a lifecycle hook
The exporter's `/suspend` flush SHALL run concurrently with the existing per-filesystem `syncfs` work (never serially after it) and SHALL be capped to at most 2 seconds; a failed or timed-out flush SHALL re-queue its points for a later attempt and SHALL NOT cause `/suspend` to answer anything other than its existing 200.

#### Scenario: a hung export never delays the /suspend response
- **WHEN** `/suspend` fires while an export attempt is in flight and the endpoint never responds
- **THEN** `/suspend` still answers 200 within its existing budget, and the unsent batch is re-queued rather than lost

#### Scenario: a flush cut off by the hook's own timeout keeps its points
- **WHEN** the `/suspend` hook's outer timeout drops the flush after it drained the queue but before the send resolved
- **THEN** the drained points are back in the queue, and the dropped-points counter is unchanged

#### Scenario: /resume never waits on a send started before the suspension
- **WHEN** `/resume` fires while an export started before `/suspend` is still pending
- **THEN** the exporter is respawned over a fresh connection pool without awaiting that send, whose points return to the queue
