# OTLP export

`rayd` (the agent inside the `MicroVM`) exports 7 CPU, memory and disk
metrics to CloudWatch every `interval_s` (60 s by default, 15..=300 s) over
OTLP/HTTP, signed with SigV4 or with a bearer token. <small>Since 0.6.0</small>

!!! info "Cost and activation"
    - **Default**: off. Without `telemetry=` (TypeScript: `telemetry`)
      the SDK sends no `ConfigureSandbox` section and `rayd` opens no new
      outbound connection: zero cost, zero AWS calls.
    - **Turns on**: `telemetry=TelemetryExport(...)` in `Sandbox.create()` /
      `AsyncSandbox.create()` (TypeScript: `telemetry: new TelemetryExport({...})`).
      It is sent as a `ConfigureSandbox` section right after the agent is
      ready, never in `run-microvm` itself.
    - **Resources and AWS calls**: `rayd` makes one `PutMetricData` per
      exported batch (one every `interval_s`, grouping the 7 gauges). With
      `OtlpAuth.execution_role()` you need the IAM policy
      `RayitoOtlpExport` (`infra/otlp-export.yaml`,
      `rayito stack deploy otlp-export`) on the execution role.
    - **Approximate cost** (measured, Q120): $0 for the option itself;
      CloudWatch bills OpenTelemetry metrics at **$0.50 per GB
      ingested** ([CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/),
      us-east-1, checked 2026-10-02). Each batch is 7 points in a
      639-byte protobuf request (353 bytes with gzip): with
      `interval_s=60`, 60 batches ≈ 38 KB per sandbox-hour ≈ **$0.00002 per
      sandbox-hour** counting the uncompressed bytes (the upper bound: AWS
      does not publish whether it bills on the compressed body). Example: 1,000
      sandboxes a day of one hour each = 30,000 sandbox-hours a month ≈
      1.15 GB ≈ **$0.58/month**; with `interval_s=15`, 4 times more (≈ $2.30/month).
      Plus the IAM policy, which costs nothing.
    - **Cardinality**: `sandbox_id` is a resource attribute, so
      each sandbox creates its own 7 series. Over OTLP you pay by bytes,
      not by series, so many short sandboxes do not multiply the
      ingestion bill; but if you forward these metrics as classic
      custom metrics ($0.30 per metric per month, prorated by
      the hour) each sandbox would cost 7 × $0.30 / 730 h ≈ $0.0029 per
      started hour, about 20 times more, and dashboards that aggregate by
      `sandbox_id` grow with the number of sandboxes.
    - **IAM**: `cloudwatch:PutMetricData` on the account's default OTLP
      dataset — CloudWatch's OTLP endpoint does not support scoping by
      namespace (researched, see "Sources and measurements" below). With
      `OtlpAuth.bearer(...)` instead you need permission to read the
      secret that holds the token (`secretsmanager:GetSecretValue` on
      `rayito/*`, the `RayitoSecretsReader` policy).
    - **How to turn it off**: do not pass `telemetry=` (default `None`/`undefined`);
      delete the `otlp-export` stack (`rayito stack destroy otlp-export`) if
      no sandbox uses it anymore.

## When to use it { #cuando-usarlo }

- You want to see CPU, memory and disk of your sandboxes in a CloudWatch
  dashboard next to the rest of your infrastructure, without setting up your
  own collector.
- **When not to**: if you only need a one-off snapshot or a short history
  inside your own process, [`get_metrics_history()`](../observability.md#instantanea-e-historial)
  has no AWS cost and requires no special image.

## What it exports { #que-exporta }

Exactly 7 gauges, sampled from the same 5 s ring that `get_metrics_history()`
already serves — no additional probing of the guest:

| Metric (`names="rayito"`) | `names="e2b"` | What it is |
|---|---|---|
| `rayito.sandbox.cpu.used_pct` | `e2b.sandbox.cpu.used_pct` | CPU % in use |
| `rayito.sandbox.cpu.count` | `e2b.sandbox.cpu.count` | CPUs the guest sees |
| `rayito.sandbox.memory.used_bytes` | `e2b.sandbox.memory.used_bytes` | Memory in use |
| `rayito.sandbox.memory.total_bytes` | `e2b.sandbox.memory.total_bytes` | Guest total memory |
| `rayito.sandbox.memory.cache_bytes` | `e2b.sandbox.memory.cache_bytes` | Page cache |
| `rayito.sandbox.disk.used_bytes` | `e2b.sandbox.disk.used_bytes` | Disk in use |
| `rayito.sandbox.disk.total_bytes` | `e2b.sandbox.disk.total_bytes` | Total disk |

Each batch carries 4 resource attributes, a closed list (never a path,
a command or a `metadata` value): `sandbox_id`, `image_arn`,
`image_version` and `image_memory_mib` (the image's **declared** memory,
not what the guest sees: the guest sees 4× that figure, so the SDK
divides by that factor before sending it; see `limits.md`).

!!! note "`get_telemetry_status()` is a separate call"
    The original architecture sketch envisioned `sbx.get_health().telemetry`.
    That would have forced `get_health()` -- which *every* sandbox calls, whether
    or not it uses `telemetry=` -- to make an extra call to `ConfigureStatus`,
    conditional (or always), risking the "zero calls without the
    option" guarantee. `get_telemetry_status()`/`getTelemetryStatus()` is,
    instead, a new explicit method: it is never called unless you ask, so
    that guarantee holds trivially (`design.md` D7 of
    `m15-rayd-otlp` records the decision).

## Authentication { #autenticacion }

=== "Execution role (`OtlpAuth.execution_role()`)"

    SigV4 over the execution role's IMDS credentials. It requires the
    `rayito-base-caps` variant (or a size-derived one, e.g.
    `rayito-base-caps-4gb`): without it, `Sandbox.create()` raises
    `UnimplementedError` before `run-microvm` when the image name
    already makes that knowable. With an ARN or a custom name the SDK
    cannot know the variant and `rayd` only checks that there is an execution
    role: without a role it rejects the section (`role_not_permitted`), the SDK terminates
    the `MicroVM` and raises `SandboxException`; with a role on an image without
    caps it exports anyway, but its credentials are readable by uid 1000
    (T1). Use the name `rayito-base-caps` so the check happens up front.

    CloudWatch's minimal policy cannot be scoped by namespace: with
    this option, a compromised sandbox can write arbitrary metrics
    under any name it wants (not just the 7 `rayito` ones). If that
    worries you, use `OtlpAuth.bearer(...)` or a `rayito-base-caps` image with no
    more privileges than needed.

=== "Bearer token (`OtlpAuth.bearer(...)`, experimental)"

    A Secrets Manager secret whose value is a CloudWatch
    Metrics API key: a service-specific credential
    (`iam create-service-specific-credential --service-name
    cloudwatch.amazonaws.com`) of an IAM user with
    `cloudwatch:CallWithBearerToken` and `cloudwatch:PutMetricData`. It is not
    scoped to a log group or a namespace: it only works for the metrics OTLP
    ingestion endpoint, account-wide. Always set
    `--credential-age-days`. The name is resolved the same way as in `secrets=` and
    `SecretStore`: under the `rayito/` prefix, so
    `OtlpAuth.bearer("otlp-key")` reads `rayito/otlp-key` (a full ARN is
    used as is). The SDK reads it through the same secret cache as
    `secrets=` (at most one `GetSecretValue` every 5 minutes), when sending the
    section, and pushes it to `rayd` through `ConfigureSandbox` — never through an
    environment variable (ADR-014 rule 4) or in plain text anywhere;
    `rayd` keeps it only in memory. It works on `rayito-base`, without caps.

## Installation { #instalacion }

No extra package is needed: `TelemetryExport`/`OtlpAuth` are already part of
the SDK. If you use `OtlpAuth.execution_role()`, deploy the IAM policy
once:

```bash
rayito stack deploy otlp-export
```

## `TelemetryExport` options { #opciones-de-telemetryexport }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `interval_s` | `intervalS` (also in seconds) | `60` | how often it exports a batch, from 15 to 300 s |
| `service_name` | `serviceName` | `"rayito"` | the OTLP resource's `service.name` |
| `names` | `names` | `"rayito"` | metric prefix: `"rayito"` or `"e2b"` (see [What it exports](#que-exporta)) |
| `auth` | `auth` | `OtlpAuth.execution_role()` / `OtlpAuth.executionRole()` | how it is signed: the execution role or `OtlpAuth.bearer(...)` ([Authentication](#autenticacion)) |

`sbx.get_telemetry_status()` (`await sbx.getTelemetryStatus()`) returns a
`TelemetryHealth` with `exported`, `dropped` and `last_error_class`/
`lastErrorClass`.

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import OtlpAuth, Sandbox, TelemetryExport

    role_arn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps"

    sbx = Sandbox.create(
        "rayito-base-caps",
        execution_role_arn=role_arn,
        telemetry=TelemetryExport(
            interval_s=60,
            service_name="agent",
            auth=OtlpAuth.execution_role(),
        ),
    )
    sbx.commands.run("python agent.py")
    status = sbx.get_telemetry_status()
    print(status.exported, status.dropped, status.last_error_class)
    sbx.kill()
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, OtlpAuth, TelemetryExport

    role_arn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps"


    async def main() -> None:
        sbx = await AsyncSandbox.create(
            "rayito-base-caps",
            execution_role_arn=role_arn,
            telemetry=TelemetryExport(auth=OtlpAuth.execution_role()),
        )
        await sbx.commands.run("python agent.py")
        print(await sbx.get_telemetry_status())
        await sbx.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { OtlpAuth, Sandbox, TelemetryExport } from "rayito";

    const roleArn = "arn:aws:iam::123456789012:role/rayito-sandbox-caps";

    await using sbx = await Sandbox.create({
      template: "rayito-base-caps",
      executionRoleArn: roleArn,
      telemetry: new TelemetryExport({
        intervalS: 60,
        serviceName: "agent",
        auth: OtlpAuth.executionRole(),
      }),
    });
    await sbx.commands.run("python agent.py");
    const status = await sbx.getTelemetryStatus();
    console.log(status.exported, status.dropped, status.lastErrorClass);
    ```

With the bearer token, only the authentication changes:

=== "Python"

    ```python
    from rayito import OtlpAuth, Sandbox, TelemetryExport

    sbx = Sandbox.create(
        "rayito-base",
        telemetry=TelemetryExport(auth=OtlpAuth.bearer(secret_name="otlp-key")),
    )
    sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { OtlpAuth, Sandbox, TelemetryExport } from "rayito";

    await using sbx = await Sandbox.create({
      template: "rayito-base",
      telemetry: new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") }),
    });
    ```

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Symptom | Cause | What to do |
|---|---|---|
| `UnimplementedError` before `run-microvm`, names the caps variant | `OtlpAuth.execution_role()` on an image that is not `rayito-base-caps` (or derived) | use a caps image, or switch to `OtlpAuth.bearer(...)` |
| `UnimplementedError` right after creating the sandbox (the VM was already terminated, unless `keep_on_failure=True`) | the image runs a `rayd` older than 0.6.0, or a 0.6 `rayd` that started without `AWS_REGION` | publish an image with the `rayd` of tag `rayd-v0.6.0` or later |
| `SecretNotFoundException` / `SecretException` when sending the section | the secret `rayito/<name>` does not exist (the prefix is added automatically) or has no `SecretString` | create `rayito/<name>` (`SecretStore().create(...)`) or pass the full ARN |
| `last_error_class="rejected"` right after a long resume | the guest clock fell behind by more than 5 minutes | it fixes itself: `rayd` measures the skew with AWS's `Date` header and retries; if it persists, open an issue |
| `SandboxException: telemetry_export: role_not_permitted` (the VM was already terminated) | `OtlpAuth.execution_role()` without `execution_role_arn=` | pass the execution role with `RayitoOtlpExport`, or use `OtlpAuth.bearer(...)` |
| `last_error_class="rejected"` from the first batch with `OtlpAuth.bearer(...)` | CloudWatch rejects the API key (expired, inactive or without `cloudwatch:CallWithBearerToken`) | generate a new one and update the secret (`SecretStore().update(...)`) |
| `get_telemetry_status()` always at zero | you never passed `telemetry=`, or the section did not apply | check `create()`'s result (if it did not raise, it applied) |

## Differences from E2B { #diferencias-con-e2b }

E2B's sandbox telemetry export is Enterprise-only, E2B configures it during
customer onboarding and it has no API or kwarg: here it is
explicit and opt-in. The E2B shim adds no new kwarg for this;
`names="e2b"` is the only concession to its naming convention.

## See also { #ver-tambien }

- [Optional features](../optional-features.md)
- [Optional stacks (`rayito stack`)](pilas-opcionales.md)
- [OpenTelemetry (SDK spans)](opentelemetry.md) — complementary, not the
  same: that one instruments your SDK calls, this one exports metrics from
  *inside* the sandbox.
- [Metrics and listing](../observability.md)

??? info "Sources and measurements"
    - Implementation: [`crates/rayd-core/src/telemetry/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/crates/rayd-core/src/telemetry),
      [`crates/rayd/src/adapters/cloudwatch_otlp_sink.rs`](https://github.com/alejandro-cedeno-10/rayito/blob/main/crates/rayd/src/adapters/cloudwatch_otlp_sink.rs),
      [`clients/python/src/rayito/_telemetry_export/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/clients/python/src/rayito/_telemetry_export)
      and [`clients/typescript/src/telemetry-export/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/clients/typescript/src/telemetry-export).
    - Research: `docs/research/2026-10-e2b-out-of-scope.md` §6 (options
      B1/B1'), with OT1/OT9 confirming that CloudWatch's OTLP endpoint
      accepts SigV4 on `monitoring.<region>.amazonaws.com/v1/metrics` and
      that the `cloudwatch:PutMetricData` action cannot be scoped by
      namespace.
    - Tested with `TelemetrySink`/`OtlpEncoder` doubles in both SDKs;
      the SigV4 signature is done by `aws-sigv4`, the same signer
      `aws-sdk-s3` uses.
    - Acceptance against real AWS (2026-10-02, `AWS_API_NOTES.md` Q109–Q113, Q120):
      7 points per batch, 639 bytes (353 with gzip) and 0.01 s of `rayd` CPU
      per minute with `interval_s=15` (Q120); SigV4 from `rayito-base-caps`
      exports without errors and without `telemetry=` `create()` makes no new
      AWS call (Q109); `/suspend` is not delayed, no point is lost
      and the first batch after 10 and 56 minutes suspended goes out with
      the pool rebuilt (Q110); a 403 for an expired signature carries the
      `Date` header from which `rayd` corrects its clock (Q112); each call's
      `traceparent` reaches `rayd`'s logs (Q113). API key
      authentication is verified up to the rejection of an invalid
      key (Q111).
