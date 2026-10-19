# Troubleshooting

Look up the symptom; each entry gives the cause and the fix. First thing,
almost always:

```bash
rayito doctor --template rayito-base           # add --launch to try a real sandbox
```

!!! note "Messages stay in Spanish"
    The SDK and CLI messages are written in Spanish, so the literal texts
    quoted on this page (`sin región: …`, `denegado: …`, `actualiza la
    imagen`) appear in Spanish. Each entry says what they mean.

## `rayito doctor` { #rayito-doctor }

Each check ends in `OK`, `WARN`, `FAIL` or `SKIP`. A denied permission shows
up as `<operation> denegado: …` ("denied"); with `rayito --json doctor`,
`details.operation` names the call AWS rejected and `details.action` the
permission that is missing. The first seven are read-only; checks 8 to 10
need a `RUNNING` sandbox of the image, or `--launch` to create a 300 s one
that is killed at the end.

| # | Check | Typical `FAIL` (or `WARN`) | Fix |
|---|---|---|---|
| 1 | `credentials` | no credentials, expired token, no region; `WARN` if the region does not have Lambda MicroVMs | `aws sso login --profile <your-profile>`; export `AWS_PROFILE` and `AWS_REGION` with one of the ten regions |
| 2 | `managed-images` | `AccessDenied`, unknown endpoint or a region without the service | update `boto3` (`pip install -U boto3`); change region |
| 3 | `quotas` | `WARN`: a MicroVM quota below the default value (new accounts); `SKIP` if the permission is missing | request the increase in Service Quotas |
| 4 | `iam-simulation` | `WARN` for `lambda:PassNetworkConnector`; `SKIP` without `iam:SimulatePrincipalPolicy` | it is a false positive of the simulator: ignore it. Other `implicitDeny`: review the [policy](iam.md) |
| 5 | `bucket` | 403/404 on the artifacts bucket, or a bucket in another region; `SKIP` without `--bucket` or `RAYITO_BUCKET` | grant `s3:ListBucket`; use a bucket in the same region |
| 6 | `image-gate` | the image does not exist or its version cannot be launched | `rayito image publish …`; if the build failed, look at its `stateReason` |
| 7 | `sandboxes` | more than 10 `RUNNING` sandboxes of that image (`WARN` from 1 to 10): they bill | `rayito sandbox list`; `rayito sandbox kill --all --template <image>` |
| 8 | `token` | `AccessDenied` when minting the proxy token; `SKIP` if there is no `RUNNING` sandbox | grant `lambda:CreateMicrovmAuthToken`; use `--launch` |
| 9 | `agent` | `rayd responde pero no está agent_ready` (rayd responds but is not agent_ready), or no response (`UNAVAILABLE`, proxy 403); `WARN` with `kernel_ready=false`, `hook_anomalies` greater than 0 or `imds_blocked=false` on a `-caps` image; `SKIP` without a token | wait a few seconds; republish the image; with `hook_anomalies`, check who mints `allPorts` tokens ([Security](../security.md)) |
| 10 | `compatibility` | the image's `rayd` is older than the one the SDK requires ([compatibility table](../limits.md#compatibilidad-sdk-rayd-imagen)) | publish the image from your SDK's release ([Set up AWS](../primeros-pasos/configurar-aws.md#a-mano-sin-rayito-setup)) |
| — | `efs-network` (only with `--efs-vpc-id` and `--efs-subnet-ids`) | the VPC or the subnets are not suitable for `rayito stack deploy efs-volumes` | fix what it names; it creates nothing ([EFS volumes in your VPC](../funciones-opcionales/volumenes-efs-vpc.md)) |

## `rayito setup` { #rayito-setup }

Every error comes out as `rayito: <message>` on stderr (with `--json`, also in
the document: `error: {"code", "message"}` and `exitCode`). The messages are in
Spanish, like the rest of the CLI; the table glosses each one. Nothing has been
created after a `--dry-run`; the rest of the errors can be retried: `rayito
setup` observes the account again and resumes whatever was left half done.

| Code | Exit | What happens | What to do |
|---|---|---|---|
| `unsupported_region` | 2 | the region has no Lambda MicroVMs (the message lists the ten that do) | pick one of them with `--region` or `AWS_REGION` |
| `no_bucket` | 2 | there is no bucket for the artifact: no `--bucket`, no `RAYITO_BUCKET`, none in the stack, no `cdk bootstrap` one | pass `--bucket`, export `RAYITO_BUCKET` or run `cdk bootstrap` in that account and region; Rayito does not create buckets |
| `bucket_unavailable` | 2 | the bucket (`--bucket`, `RAYITO_BUCKET` or the stack's) does not exist or is not accessible with these credentials | check the name and that you have `s3:ListBucket` on it |
| `bucket_region` | 2 | the bucket is in another region and the build would fail with `S3_CROSS_REGION_ACCESS_DENIED` | use a bucket in the same region as the account and `setup` region |
| `artifact_missing` | 2 | the `--artifact` file does not exist | fix the path |
| `artifact_variant` | 2 | `--artifact` is not the `full` variant zip without EFS | use the release's `rayito-image.zip`; the other variants are published with `rayito image publish` |
| `needs_confirmation` | 2 | there is something to create and there is no terminal (or you use `--json`) | confirm with `--yes`, or run it from a terminal |
| `stack_blocked` | 1 | the `rayito-m0-iam` stack is in a state that cannot be reused or waited for (`ROLLBACK_COMPLETE`, `*_FAILED`, `DELETE_IN_PROGRESS`) | `aws cloudformation delete-stack --stack-name rayito-m0-iam`, wait for it to disappear and run `rayito setup` again |
| `stack_incomplete` | 1 | the stack has no `BuildRoleArn` output: it is not Rayito's `infra/iam.yaml` template | deploy Rayito's template (or delete it and let `rayito setup` create it) |
| `release_download` | 1 | `rayito-image.zip` or `SHA256SUMS` could not be downloaded from the release (`HTTP <status>`, `tiempo agotado` (timeout) or `error de red` (network error)) | check the network or proxy (`HTTPS_PROXY`); or download the zip, verify it and pass `--artifact` |
| `checksum_missing` | 1 | the release's `SHA256SUMS` does not list `rayito-image.zip`; nothing is published | use the release of your SDK's version; or `--artifact` with a verified zip |
| `checksum_mismatch` | 1 | the zip's sha256 does not match `SHA256SUMS`; nothing is published | download again; if it persists, do not publish that file and [verify it](../verify.md) |
| `image_build_failed` | 1 | the `rayito-base` image was not launchable after the build | run `rayito setup` again (it will ask to confirm a new `publish`); if it repeats, `rayito doctor` and `rayito image list` show the state |

Other failures: a `rayito doctor` `FAIL` at the end leaves the exit at 1 with
`la cuenta no está lista` (the account is not ready) (fix it with
[`rayito doctor`](#rayito-doctor) and repeat); a "no" at the confirmation exits
with 1 without creating anything; no region, no profile or no credentials exit
with 2 like the rest of the CLI; an untranslated AWS error (for example a
permission denied when creating the stack) exits with 1 and its code. When a
later command (`Sandbox.create()`, `rayito agent deploy`) says there is no image
or bucket, the message ends with the hint "si la cuenta o la región aún no
están preparadas, ejecuta `rayito setup`" (if the account or region are not
prepared yet, run `rayito setup`).

## Credentials and region { #credenciales-y-region }

**`ExpiredTokenException`, `Token has expired`, `UnauthorizedSSOTokenError`.**
The SSO session expired. Sign in again with
`aws sso login --profile <your-profile>`. S3 URLs signed with temporary
credentials stop working when those credentials expire, even if the URL
says otherwise.

**`sin región: pasa --region o exporta AWS_REGION` ("no region: pass --region or export AWS_REGION"; CLI, exit code 2).** The
SDK and the CLI need a region: `export AWS_REGION=us-east-1` (both SDKs and the CLI read `AWS_REGION`; if it is missing, they use `AWS_DEFAULT_REGION` and then the profile's region).

**`AuthenticationException` / `AuthenticationError` when connecting.** The
access token is not the sandbox's (you lost it or it belongs to another).
It cannot be recovered: create a new sandbox and store `sbx.access_token`
next to the id.

## The image { #la-imagen }

**`UnimplementedError: … actualiza la imagen` (or "publica una imagen
nueva"), meaning "update the image" / "publish a new image".** The image's
`rayd` is older than the feature you ask for (server-side deadline, S3 URLs,
metrics history, Deno kernels...). Publish the image from your SDK's release
and check with `rayito doctor` (`compatibility` check).

**`UnimplementedError` naming `rayito-base-poly` or `rayito-base-caps`.**
The feature needs another image variant: bash, JavaScript and TypeScript
live in `rayito-base-poly`; the network policy, in `rayito-base-caps`
([Images](../images.md)).

**The image build fails.** `rayito image publish` prints the
`stateReason`s and the last lines of the build log (group
`/rayito/<image>` in CloudWatch). Usual causes: the build role cannot read
the bucket, or the zip is of another variant (`--variant`).

**Building `rayd` on macOS or Windows.** You need Linux or WSL2 (on macOS, a
Linux VM). To get started you do not need it: publish the release's signed
`rayito-image.zip` ([Set up AWS](../primeros-pasos/configurar-aws.md#a-mano-sin-rayito-setup)).

## Quotas and pacing { #cuotas-y-ritmo }

**`RateLimitException` / `RateLimitError` with `ThrottlingException`.** The
Lambda MicroVMs APIs have per-account, per-region quotas: 5 `run-microvm`
per second, 2 `suspend`, 5 `resume`, 10 `terminate`, 50 proxy tokens and
100 `get-microvm`. The SDK already rate-limits per process; several
processes at once can exceed them. Space out the launches or use a
[pool](../pool.md).

**`QuotaExceededException` / `QuotaExceededError`.** You have reached the
region's memory quota (for example 1,024 GB in `us-east-1`, counting
`RUNNING` and `SUSPENDED` sandboxes). Kill sandboxes or request an increase.

**`RateLimitException` inside the sandbox.** More than 256 live processes and
PTYs, more than 8 code contexts or more than 16 active transfers. Release the
ones you do not use.

## Files and S3 { #ficheros-y-s3 }

**A missing key gives 403 instead of 404.** Your credentials lack
`s3:ListBucket` on the bucket (scoped to the prefix). Without it, S3 cannot
say "does not exist" and the import waits until it expires. Add it
([IAM](iam.md)).

**`UnimplementedError` on `upload_url` / `download_url`.** There is no
transfer bucket: pass `transfer=S3Staging("amzn-s3-demo-bucket")` or export
`RAYITO_TRANSFER_BUCKET`.

**The E2B shim does not accept `transfer=`.** That is on purpose (E2B does
not have it): in `rayito.e2b` the bucket comes from `RAYITO_TRANSFER_BUCKET`
(and `RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`).

**`FileUploadException` with `wrong_region`.** The bucket is in a different
region than the sandbox: use one in the same region or `S3Staging(region=)`.

## Execution { #ejecucion }

**A cell or a silent command stops halfway or takes too long.** Auto-suspend
counts the endpoint's traffic: something that prints nothing for
`max_idle_seconds` (300 s by default) can be suspended while it runs. The SDK
reattaches on resume, but raise `max_idle_seconds` or pass `idle=None` for
long, silent tasks ([Pause and
resume](../guias/pausar-reanudar.md)). In the MCP server:
`RAYITO_MCP_IDLE_SECONDS=0`.

**`SandboxException: output_truncated`.** A command produced output and
nobody read it within 30 s: the agent retains 64 chunks of 32 KiB and then
cuts it off. Read the handle (by iterating it or with `wait()`) or redirect
the output to a file.

**`TimeoutException` on a command.** The default `timeout` is 60 s. For long
processes, `timeout=None` (TypeScript: `timeoutMs: 0`) or
`background=True`.

**`execution.error.name == "ExecutionTimeout"`.** The cell exceeded its
`timeout` (300 s by default): raise it.

**`<defunct>` processes in `ps` after launching a daemon.** With `rayd` 0.6.0
or earlier, a process that daemonizes (double `fork`, `nohup … &` inside a
subshell, `mount-s3` without `--foreground`) was left as a zombie hanging off
`rayd`, which is the sandbox's PID 1. `rayd` 0.6.1 reaps them as soon as they
finish without touching your commands' exit code
([What's new in 0.6.1](../novedades/0.6.1.md#rayd-recoge-los-procesos-zombi));
with an older image, republish it on `rayd` 0.6.1. Zombies use no CPU or
memory, only an entry in the process table.

## Network { #red }

**`UnimplementedError` when passing `network=` or `allow_internet_access=False`.**
The network policy is only enforced on `rayito-base-caps`; on another image
the SDK terminates the sandbox before returning it, so you are not handed one
with open network.

**A client fails with the policy active even though the host is allowed.**
Hostname rules go through a local proxy; clients that do not honor
`HTTPS_PROXY` (raw sockets, `ssh`, `git://`) fail closed
([Outbound network](../network.md)).

**`403` or `502` on `get_host(port)`.** The proxy headers are missing
(`host.headers`) or nothing is listening on that port ([Ports and
host](../guias/puertos-y-host.md)).

## TypeScript { #typescript }

**`InvalidArgumentError: <función> necesita el paquete opcional '<paquete>': instala npm install <paquete>`** (the function needs the optional package; install it).
The AWS clients for the optional features (secrets and gateway,
index, optional stacks, events, EFS volumes, custom domain,
OpenTelemetry...) are optional *peerDependencies* of the package: they are
only loaded when you use that feature. Install the one the message names (for
example, `npm i @aws-sdk/client-secrets-manager` for `secrets=` or
`gateways=`).

**`await using` does not compile.** You need TypeScript ≥ 5.2 with
`"lib": ["ES2022", "ESNext.Disposable"]`. Without `await using`, use
`try { … } finally { await sbx.kill(); }`.

## Agent { #agente }

Failures of `sbx.agent.run()`/`.stream()` are `AgentException`
(TypeScript: `AgentError`) with a closed `reason`; the full table is
in [Errors](../referencia/errores.md#agentexception-agenterror).

**`reason="runtime_missing"`.** The image does not ship the runtime
(`opencode` is not on the `PATH`). Create the sandbox from an image built with
`AgentTemplate`
([Agent templates](../funciones-opcionales/templates-de-agente.md)).

**`reason="busy"`.** A `run`/`stream` is already in progress in that
sandbox: one at a time per sandbox. Wait for it to finish or use another
sandbox.

**`reason="model_error"`.** The model responded with an error. Check that the
gateway secret holds the full credential (for Bedrock,
`Bearer <key>`, which expires: rotate it with `sbx.gateways.refresh()`) and
that the model is in the preset's `models=`
([Agent in the sandbox](../guias/agente-en-el-sandbox.md)).

## Secrets { #secretos }

!!! danger "botocore's DEBUG log prints the secrets"
    Rayito never writes a secret's value or name to its logs.
    But if you enable the AWS SDK's DEBUG log (`logging.DEBUG` on the root
    logger or on `botocore`, `boto3.set_stream_logger()`, or a `logger` in the
    AWS SDK v3 client), **that** log includes the Secrets Manager bodies with
    the `SecretString` in the clear. Limit DEBUG to the `rayito` logger:
    `logging.getLogger("rayito").setLevel(logging.DEBUG)`.

**`SecretNotFoundException` with a secret created from the shim.** The shim
lowercases names (`Secret.create("OpenAI", …)` stores `rayito/openai`); the
native SDK does not. Refer to it in lowercase.

**`SecretException` with `InvalidRequestException` when recreating a name.**
AWS takes ≈ 20–30 s to release a deleted name. `create` already retries
for up to 60 s; if that runs out, wait and try again.

## Unexpected bills { #facturas-inesperadas }

**Sandboxes nobody killed.** `rayito sandbox list` shows them with their
age; `rayito sandbox kill --all` terminates them. To keep it from happening
again, always use `with` / `await using`, a tight `timeout` and the
[server-side deadline](../lifecycle.md) with `on_timeout="kill"`, which is
enforced even if your process dies.

**Accumulated image versions.** Each one costs ≈ $0.04 per week
([Pricing](../cost.md#cuanto-cuesta-con-ejemplos)):
`rayito image prune --dry-run` and then `rayito image prune --keep 5`.

## See also { #ver-tambien }

- [Errors](../referencia/errores.md): every exception and what to do.
- [FAQ](faq.md)
- [Open an issue](https://github.com/alejandro-cedeno-10/rayito/issues)
  with the output of `rayito --json doctor` (it carries no secrets).
