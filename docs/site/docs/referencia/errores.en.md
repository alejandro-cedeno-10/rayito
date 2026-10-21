# Errors

All the SDK's errors in one table: the Python name, the TypeScript name, the
gRPC code of the agent that produces it (if it comes from `rayd`), when it
happens and what to do.

## Hierarchy { #jerarquia }

=== "Python"

    <!-- noqa: example: class tree, not runnable code -->
    ```text
    SandboxException
    ├── TimeoutException
    ├── InvalidArgumentException
    ├── NotFoundException
    │   ├── FileNotFoundException
    │   ├── SandboxNotFoundException
    │   └── SecretNotFoundException   (also SecretException)
    ├── SandboxNotReadyException
    ├── SandboxStateException
    ├── SandboxLifetimeException
    ├── PoolClosedException
    ├── CommandExitException
    ├── PersistenceException
    ├── DiskFullException
    ├── TransferException
    │   └── FileUploadException
    ├── RateLimitException
    ├── GitUpstreamException
    ├── SecretException
    ├── SandboxIndexException
    │   └── IndexWriteException
    ├── MountException
    ├── VolumeException
    │   ├── VolumeMountException
    │   ├── VolumeNotFoundException
    │   └── VolumePathNotFoundException
    ├── BuildException
    ├── TemplateException
    ├── StackException
    ├── WebhookException
    ├── DashboardException
    ├── CustomDomainException
    └── AgentException
    AuthenticationException          (Exception: a credentials problem, not the sandbox's)
    └── GitAuthException
    QuotaExceededException           (Exception)
    CapacityException                (Exception)
    UnimplementedError               (NotImplementedError: the feature does not exist here)
    └── LifecycleUnsupportedException
    AgentCoreException               (Exception: the AgentCore deploy, not a sandbox)
    RayitoCdkException               (Exception, in rayito.cdk: a CDK construct that does not synthesize)
    RayitoCompatWarning              (UserWarning: a warning, not an exception)
    ```

=== "TypeScript"

    <!-- noqa: example: class tree, not runnable code -->
    ```text
    SandboxError
    ├── TimeoutError
    ├── InvalidArgumentError
    ├── NotFoundError
    │   ├── FileNotFoundError
    │   └── SandboxNotFoundError
    ├── SandboxNotReadyError
    ├── SandboxStateError
    ├── SandboxLifetimeError
    ├── PoolClosedError
    ├── CommandExitError
    ├── PersistenceError
    ├── DiskFullError
    ├── TransferError
    │   └── FileUploadError
    ├── RateLimitError
    ├── GitUpstreamError
    ├── SecretError
    │   └── SecretNotFoundError
    ├── SandboxIndexError
    │   └── IndexWriteError
    ├── MountError
    ├── VolumeError
    │   ├── VolumeMountError
    │   ├── VolumeNotFoundError
    │   └── VolumePathNotFoundError
    ├── BuildError
    ├── TemplateError
    ├── StackError
    ├── WebhookError
    ├── DashboardError
    ├── CustomDomainError
    └── AgentError
    AuthenticationError              (Error)
    └── GitAuthError
    QuotaExceededError               (Error)
    CapacityError                    (Error)
    UnimplementedError               (Error)
    └── LifecycleUnsupportedError
    AgentCoreError                   (Error)
    RayitoCdkError                   (Error, in rayito/cdk)
    ```

    `instanceof` works in ESM and in CommonJS. In TypeScript
    `SecretNotFoundError` cannot also be a `NotFoundError` (single
    inheritance); in Python `SecretNotFoundException` is both.

Catch `SandboxException` / `SandboxError` for "something failed in the
sandbox"; `AuthenticationException`, `QuotaExceededException` and
`CapacityException` are left out on purpose: they say something about your
account or AWS, not the sandbox. Neither is `AgentCoreException` /
`AgentCoreError`, the one of the [AgentCore deploy](#agentcore), which is not
attributable to a sandbox, nor `RayitoCdkException` / `RayitoCdkError`, the
one of the [CDK constructs](#cdk), which is raised at synth time. An error **inside `run_code`** is never an
exception: it arrives in `execution.error`.

## Full table { #tabla-completa }

| Python | TypeScript | gRPC / origin | When | What to do |
|---|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `INVALID_ARGUMENT`, `FAILED_PRECONDITION`; AWS `ValidationException`; or the SDK before calling | invalid argument: `envs` + `metadata` > 4096 characters, `timeout` out of range, `language` and `context` at once, repeated `envs` in `secrets`, an optional TypeScript peer that is not installed | read the message: it says which argument and why |
| `TimeoutException` | `TimeoutError` | `DEADLINE_EXCEEDED`; `sandbox_timeout` | the `timeout` of a command, the deadline of a call or the sandbox deadline expired | raise the `timeout`; for the sandbox deadline, [`set_timeout()`](../lifecycle.md) |
| `NotFoundException` | `NotFoundError` | `NOT_FOUND`, `OUT_OF_RANGE`; `not_found` in a stream | a process, context or checkpoint that does not exist | — |
| `FileNotFoundException` | `FileNotFoundError` | `NOT_FOUND` in `files` | the file does not exist | check the path (absolute or relative to `HOME`) |
| `SandboxNotFoundException` | `SandboxNotFoundError` | control-plane `ResourceNotFoundException` | the sandbox does not exist or has already ended (also by reaching its `timeout`) | create a new one |
| `SandboxNotReadyException` | `SandboxNotReadyError` | the SDK | the agent was not ready within `ready_timeout` (90 s) | `rayito doctor`; `keep_on_failure=True` to inspect it |
| `SandboxStateException` | `SandboxStateError` | `UNAVAILABLE` (phase gate), `suspending` in a stream; control-plane `ConflictException` | the sandbox is suspending, resuming or terminating | retry in a few seconds |
| `SandboxLifetimeException` | `SandboxLifetimeError` | the SDK | `timeout` / `max_lifetime` above 28,800 s | you cannot exceed 8 h: [Persistence](../persistence.md) |
| `PoolClosedException` | `PoolClosedError` | the SDK | `take()` on a pool that is not started or is closed | `start()` / `with` |
| `CommandExitException` | `CommandExitError` | the process | a command exited with a non-zero code (`exit_code`, `stdout`, `stderr`) | catch it if the failure is expected |
| `PersistenceException` | `PersistenceError` | the agent (`code`) | failed checkpoint or restore: `permission_denied`, `failed_precondition`, `internal`, `interrupted`, `unimplemented`, `resource_exhausted` | see [Persistence: errors](../persistence.md#errores) |
| `DiskFullException` | `DiskFullError` | `RESOURCE_EXHAUSTED` (disk: `disk_reserve`, `disk_full`) | the sandbox disk is full, also in the middle of a transfer | delete files or use a larger image |
| `TransferException` (`code`, `reason`) | `TransferError` (`code`, `reason`) | the agent | an export to S3 ended `FAILED`/`CANCELLED` with a `code` that has no exception of its own (see [Transfers](#transferencias-por-s3)) | read `reason`; check permissions and region of the transfer bucket |
| `FileUploadException` (`code`, `reason`) | `FileUploadError` (`code`, `reason`) | the agent | an import from S3 (`upload_url`, large write) ended the same way, without an exception of its own: `checksum_mismatch`, `s3_unavailable`, `destination_rejected`... | see [Files and S3](../files.md) |
| `RateLimitException` | `RateLimitError` | `RESOURCE_EXHAUSTED`; AWS `ThrottlingException` (`retry_after`/`retryAfter`) | more than 256 processes/PTYs, 8 contexts or 16 transfers; API TPS quota | free resources; space out the calls |
| `AuthenticationException` | `AuthenticationError` | `UNAUTHENTICATED`, `PERMISSION_DENIED`; proxy 403; AWS `AccessDeniedException` | wrong access token, proxy token rejected (`proxy_rejected`), expired AWS credentials | check the stored token; `aws sso login` |
| `GitAuthException` | `GitAuthError` | `git` | the remote asked for credentials | pass `username` and `password` (a token) |
| `GitUpstreamException` | `GitUpstreamError` | `git` | `push`/`pull` without a tracking branch | pass `remote` and `branch`, or set up the tracking branch |
| `SecretException` | `SecretError` | Secrets Manager | missing permission, already exists, version clash, binary secret | read `aws_code`; see [Secrets](../secrets.md#errores) |
| `SecretNotFoundException` | `SecretNotFoundError` | Secrets Manager | the secret does not exist or is being deleted | create the secret; mind the capitalization |
| `SandboxIndexException` | `SandboxIndexError` | DynamoDB | `BatchGetItem` failed or keys were left unread | check `RayitoIndexReader` and the table |
| `IndexWriteException` | `IndexWriteError` | DynamoDB | `PutItem` failed when creating with `index=` | check `RayitoIndexWriter`; the sandbox was terminated |
| `QuotaExceededException` (`quota_code`) | `QuotaExceededError` (`quotaCode`) | control-plane `ServiceQuotaExceededException` | account quota exhausted (memory, MicroVMs) | kill sandboxes or request a quota increase |
| `CapacityException` | `CapacityError` | control-plane `InsufficientCapacityException` | AWS temporarily out of capacity | retry with backoff |
| `UnimplementedError` | `UnimplementedError` | `UNIMPLEMENTED`; the SDK | the feature does not exist in this image (update it) or on the platform (`fork`, snapshots...) | read `feature`, `reason` and `doc` |
| `VolumeException` | `VolumeError` | the SDK (EFS) | `VolumeStore.create/get/list/destroy` failed (IAM, access point limit, file system unavailable), or `create(volumes=)` found no `available` mount target before launching | check the permission or the file system's state |
| `VolumeNotFoundException` | `VolumeNotFoundError` | the SDK (EFS) | `VolumeStore.get`/`destroy` on a name that does not exist | list with `VolumeStore.list()` |
| `VolumePathNotFoundException` | `VolumePathNotFoundError` | — | reserved under E2B's name: not raised today (a volume's content operations are `UnimplementedError`) | — |
| `VolumeMountException` (`code`) | `VolumeMountError` (`code`) | the agent (`efs_volumes`) | a volume from `volumes=` failed to mount (`network`, `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`, `invalid_path`, `unknown`); the sandbox was already terminated unless `keep_on_failure` | [EFS volumes](../funciones-opcionales/volumenes-efs.md#errores-y-solucion-de-problemas) |
| `LifecycleUnsupportedException` | `LifecycleUnsupportedError` | the SDK | server-side deadline against an image older than 0.3.0 | publish an image from the current release |
| `MountException` | `MountError` | the agent (`code`) | a `mounts=` mount failed or did not settle in 15 s: `network`, `iam_denied`, `not_found`, `not_allowed`, `invalid_path`, `helper_missing`, `timeout`, `unknown` (the sandbox was terminated) | see [S3 mounts](../funciones-opcionales/montajes-s3.md) |
| `WebhookException` | `WebhookError` | DynamoDB, Secrets Manager (`aws_code`) | `LifecycleEvents` could not read or write in its stack, or `create(events=)` without the stack deployed | check the policy for that call (`EventsLauncherPolicy`, `EventsReaderPolicy` or `EventsWebhookAdminPolicy`); see [Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md#errores-y-solucion-de-problemas) |
| `StackException` | `StackError` | CloudFormation (`code`) | `OptionalStacks.deploy/status/destroy` failed: `blocked` (stack in `ROLLBACK_COMPLETE`), `not_found`, `in_progress`, `failed` | see [Optional stacks](../funciones-opcionales/pilas-opcionales.md#como-se-comporta) |
| `BuildException` | `BuildError` | the image build (`reason`) | `Template.build()` failed: a Dockerfile step (`step`, `command`, `exit_code`, `log_tail`), the `ready_cmd`, the build quota or the deadline | see [Templates](../funciones-opcionales/templates.md#errores) |
| `TemplateException` | `TemplateError` | the SDK | invalid template name (1-64 `[A-Za-z0-9_-]`, no `:tag`) | fix the name; an invalid base image is `BuildException` with `reason="base_image_*"` |
| `DashboardException` | `DashboardError` | Service Quotas, Cost Explorer, `lambda-microvms` (`aws_code`) | `Quotas`, `UsageCost` or `Fleet` (management dashboard) received an AWS code with no exception of its own (for example `DataUnavailableException`); the message carries only the code | see [Management dashboard](../funciones-opcionales/panel-de-gestion.md#errores) |
| `CustomDomainException` | `CustomDomainError` | CloudFormation, CloudFront KVS | `CustomDomain` (experimental): stack deploy/status/destroy, or `expose()`/`get_host()` without a valid route | see [Custom domain](../funciones-opcionales/dominio-propio.md) |
| `SandboxException` with `output_truncated` | `SandboxError` | the agent | nobody read a command's output within 30 s and the buffer filled up | consume the handle or redirect to a file |
| `AgentException` | `AgentError` | the agent runtime (`reason`) | `sbx.agent.run()`/`.stream()` failed: see the next table | read `reason`, `session_id` and `usage`; it never contains the prompt or the response |
| `AgentCoreException` (`code`, `exit_code`) | `AgentCoreError` (`code`, `exitCode`) | the SDK, CloudFormation, CodeBuild, AgentCore | the AgentCore deploy failed | see [the AgentCore table](#agentcore) |
| `RayitoCdkException` (`code`, `fields`) | `RayitoCdkError` (`code`, `fields`) | the construct, at synth time | `RayitoAgent` or `RayitoMicrovmImage` cannot be synthesized with those props or that stack | see [the CDK table](#cdk) |

## How the exception is chosen { #como-se-elige-la-excepcion }

The SDK always decides by code, never by the message text, with the same
table in Python and TypeScript.

### Agent (`rayd`) errors { #errores-del-agente-rayd }

| Code | Unary (gRPC status) | In a stream (`StreamError.code`) |
|---|---|---|
| `INVALID_ARGUMENT`, `FAILED_PRECONDITION` / `invalid_argument`, `failed_precondition` | `InvalidArgumentException` | `InvalidArgumentException` |
| `UNIMPLEMENTED` / `unimplemented` | `UnimplementedError` | `InvalidArgumentException` |
| `UNAUTHENTICATED`, `PERMISSION_DENIED` / `permission_denied` | `AuthenticationException` | `AuthenticationException` |
| `NOT_FOUND` / `not_found` | `FileNotFoundException` in `sbx.files`, otherwise `NotFoundException` | same |
| `OUT_OF_RANGE` | `NotFoundException` | — |
| `RESOURCE_EXHAUSTED` / `resource_exhausted` | `DiskFullException` with disk detail, otherwise `RateLimitException` | same |
| `DEADLINE_EXCEEDED` / `deadline_exceeded`, `sandbox_timeout` | `TimeoutException` | `TimeoutException` |
| `UNAVAILABLE` from the phase gate / `suspending` | `SandboxStateException` | `SandboxStateException` |
| `CANCELLED` (only triggered by the client) | `SandboxException` | — |
| `output_truncated` | — | `SandboxException` with `output_truncated` in the message |
| any other | `SandboxException` | `SandboxException` with the code in front |

### AWS API errors { #errores-de-la-api-de-aws }

| AWS `Code` | Exception |
|---|---|
| `ResourceNotFoundException` | `SandboxNotFoundException` |
| `ValidationException` | `InvalidArgumentException` |
| `AccessDeniedException` | `AuthenticationException` |
| `ThrottlingException` | `RateLimitException` (`retry_after`) |
| `ConflictException` | `SandboxStateException` |
| `ServiceQuotaExceededException` | `QuotaExceededException` (`quota_code`) |
| `InsufficientCapacityException` | `CapacityException` |
| any other | `SandboxException` with `aws_code` |

`aws_code`/`awsCode` carries the `Code`; the message and the cause go through
sanitized (never the signature, the token or AWS's raw message).

### S3 transfers { #transferencias-por-s3 }

A transfer (`upload_url`, `download_url`, large reads and writes) that ends
`FAILED` or `CANCELLED` carries a `code` and a `reason`; the message always
starts with `"<reason>: "`.

| `code` | Typical `reason`s | Exception |
|---|---|---|
| `deadline_exceeded` | `expired` | `TimeoutException` |
| `invalid_argument` | `too_large`, `wrong_region`, `bucket_missing`, `signature_rejected`... | `InvalidArgumentException` |
| `resource_exhausted` | `disk_reserve`, `disk_full` | `DiskFullException` |
| `permission_denied` | `access_denied` | `AuthenticationException` |
| `not_found` | `no_object` | `FileNotFoundException` |
| `failed_precondition`, `unavailable`, `cancelled`, `internal` | `checksum_mismatch`, `file_shrank`, `s3_unavailable`, `cancelled`, `destination_rejected`... | `FileUploadException` on import, `TransferException` on export |

## `AgentException` / `AgentError` { #agentexception-agenterror }

Since 0.8.0. It is raised by `sbx.agent.run()` (`stream()` never raises on an
agent failure: it emits it as an `AgentFailed` event). Guide:
[Agent in the sandbox](../guias/agente-en-el-sandbox.md).

It carries `reason`, `session_id`/`sessionId`, `usage`, `exit_code`/`exitCode`
and `detail_code`/`detailCode`. The message comes from a fixed table, in
Spanish, keyed by `reason`; never the provider's raw text, the prompt or the
generated content.

| `reason` | When |
|---|---|
| `model_error` | the model responded with an error (`detail_code` carries only the name of the error class, such as `APIError`; never the message) |
| `runtime_error` | the runtime process failed for another cause (non-zero exit code without an `error` event) |
| `runtime_missing` | the runtime (`opencode` or deepagents) is not in the image |
| `runtime_version_mismatch` | reserved: not emitted today (`AgentSpec.runtime_version` is not compared yet) |
| `protocol_error` | a protocol line could not be interpreted (see also `dropped_lines` in `AgentResult`) |
| `timeout` | `AgentLimits.timeout_seconds`/`timeoutMs` expired |
| `max_steps` | the SDK's hard step limit was reached (when step `max_steps + 1` starts); the SDK stops the runtime and the processes it launched |
| `token_budget` | `max_total_tokens` was exceeded after a `StepFinished` (it can be overshot by up to one full step); the SDK stops the runtime and the processes it launched |
| `output_limit` | reserved: not emitted today |
| `aborted` | `stream.abort()` / cancellation / `AbortSignal` |
| `busy` | an execution is already in progress in that sandbox (one `run`/`stream` at a time per sandbox in this phase) |

With `secret_env` or `secret_env_from` ([Secrets as environment
variables](../guias/secretos-como-entorno.md)), the deployment fails closed
with `reason="runtime_error"` and one of these `detail_code`s, which never name
the secret or the value: `secret_env_unavailable` (they could not be read),
`secret_env_denied` (the role or your credentials cannot read a secret),
`secret_env_invalid` (the value is not what the reference asks for, or goes
over the cap), `secret_env_invalid_key` (a requested key is missing, or a key
is not a valid variable name) and `secret_env_reserved` (a key would give a
reserved name). On AgentCore it is the code of the host's failure marker.

## `AgentCoreException` / `AgentCoreError` { #agentcore }

Since 0.15.0. Raised by the AgentCore deploy
(`deploy_agent(target="agentcore")`, `AgentCoreFleet` and `rayito agent ...
--target agentcore`). They do not inherit from `SandboxException` /
`SandboxError`: nothing here is attributable to a sandbox (the same reasoning as
for `rayito setup`). They carry `code` (a closed list, the one in the table) and
`exit_code` / `exitCode` (the CLI exit code). The message is in Spanish and never
carries an ARN, a URL, a token or AWS's raw text. Guide:
[Deploy to AgentCore](../guias/desplegar-en-agentcore.md).

**Exit 2** (usage or environment: fix it before calling again):

| `code` | When | What to do |
|---|---|---|
| `unsupported_region` | the region has no AgentCore Runtime with CodeBuild arm64 builds | use one of the 21 regions in the [guide](../guias/desplegar-en-agentcore.md#antes-de-empezar) |
| `option_not_supported` | a MicroVM option in the call does not apply to AgentCore | remove it; the message says which one to use instead ([table](../guias/desplegar-en-agentcore.md#opciones-que-no-aplican)) |
| `option_requires_agentcore` | an AgentCore option (`--tag`, `--bedrock-model`…) with the `microvm` target | add `--target agentcore` or remove it |
| `target_unsupported` | `rayito agent list` or `delete` without `--target agentcore` | use `--target agentcore`; a MicroVM fleet is stopped with `rayito agent stop` |
| `provider_unsupported` | the project's model is not a Bedrock one | AgentCore only wires `bedrock`, with the runtime role |
| `network_policy_unsupported` | the project declares `egress` or `network` | remove it: in PUBLIC mode AgentCore does not filter egress |
| `name_invalid` | the agent name does not fit the derived names | up to 41 characters, lowercase letters and digits with single `-` or `_`; change it with `[image].template` |
| `name_conflict` | a runtime with that name already exists and is not Rayito's | change the name with `[image].template`; Rayito does not touch other runtimes |
| `model_required` | reserved: not raised today (the runtime role always includes the project's model) | — |
| `image_missing` | `--no-build` and there is no image with that content | deploy without `--no-build` |
| `lifecycle_invalid` | a lifecycle time out of range, or not an integer (in TypeScript, not a multiple of 1000 ms) | between 60 and 28,800 s |
| `lifecycle_order` | `idle_session_timeout` greater than `max_session_lifetime` | lower the idle timeout or raise the maximum lifetime |
| `tags_invalid` | an invalid tag, or more than 50 with the three fixed ones | `KEY=VALUE`, letters, digits, spaces and `_ . : / = + - @` |
| `iam_resource_invalid` | a readable secret, a Bedrock model or the project's model with wildcards (`*`, `?`), commas, or an ARN that is not of your account, region and partition | a name or id without wildcards, or a full ARN of your account and region; the field is in the message |
| `env_invalid` | more than 50 environment variables (with the two region ones) or a value longer than 5000 characters | reduce the project's `env` |
| `raw_protocol` | an app with `protocol = "raw"` | it does not meet the AgentCore contract (`/ping` and `/invocations`) |
| `no_bucket` | there is no bucket for the build context | `--bucket`, `[deploy].bucket`, `RAYITO_BUCKET` or `rayito setup` |
| `bucket_region` | the bucket is in another region | CodeBuild only reads code from a bucket in its own region |
| `needs_confirmation` | no terminal or with `--json` and no `--yes` | confirm with `--yes` |
| `delete_needs_confirmation` | the same for `rayito agent delete` | confirm with `--yes` |
| `session_required` | `rayito agent stop --target agentcore` without `--session-id` | pass `--session-id`; to remove the agent, `rayito agent delete` |

**Exit 1** (what fails in AWS):

| `code` | When | What to do |
|---|---|---|
| `stack_blocked` | the `rayito-agentcore-<name>` stack is in a state that accepts no changes | `rayito agent delete <name> --target agentcore` and deploy again |
| `stack_failed` | the stack could not be deployed, or it lacks outputs | look at its events in the CloudFormation console |
| `image_build_failed` | the CodeBuild build ended badly | the logs are in the build's log group (the message names it) |
| `image_build_timeout` | the build is still running past the deadline | follow it in its log group; raise `build_timeout_seconds` |
| `build_quota` | CodeBuild rejected the build because of the concurrent builds quota | wait for the running ones to finish or ask for more quota |
| `image_too_large` | the image exceeds AgentCore's maximum (2 GB) | reduce the project's dependencies |
| `runtime_blocked` | the runtime is in `CREATE_FAILED`, `DELETING` or `DELETE_FAILED` | `rayito agent delete <name> --target agentcore` and deploy again |
| `runtime_failed` | the runtime ended in a failed state after being created or updated | look at its logs (the message names the group) |
| `runtime_timeout` | the runtime is still in progress past the deadline | run the deploy again: it resumes |
| `runtime_not_found` | there is no Rayito agent with that name in the region | check the name and region with `rayito agent list --target agentcore` |
| `delete_failed` | a resource could not be deleted | retry `rayito agent delete`; the message says which |
| `access_denied` | your credentials lack a permission | see the [guide's permissions](../guias/desplegar-en-agentcore.md#antes-de-empezar); the message names the operation and the AWS code |
| `aws_error` | AWS rejected an operation for another reason | the message names the operation and the AWS code |

Invocation errors (`invoke()`, `stream()`) are not `AgentCoreException`, except
configuration ones: a `429` is `RateLimitException`, an exceeded session quota
is `QuotaExceededException`, a failure of the agent itself (the container
answered 4xx or 5xx) is `AgentException(reason="runtime_error")`, and a runtime
that does not exist, a missing permission or a rejected request are
`AgentCoreException` (`runtime_not_found`, `access_denied`, `aws_error`).

## `RayitoCdkException` / `RayitoCdkError` { #cdk }

Since 0.15.0. Raised by the CDK constructs: `RayitoCdkException` is imported from
`rayito.cdk` and `RayitoCdkError` from `rayito/cdk`. They are raised in the
constructor, at synth time and before any resource is added
(`release_download_failed` and the `artifact_*` ones come earlier, when the
`MicrovmImageArtifact` is prepared). They do not inherit from `SandboxException` /
`SandboxError`. They carry `code` (a closed list, the one in the table) and
`fields` (the values the message interpolates). The agent plan's errors
(`name_invalid`, `provider_unsupported`…) are still
[`AgentCoreException` / `AgentCoreError`](#agentcore). Guide:
[Deploy with CDK](../guias/desplegar-con-cdk.md#errores).

| `code` | When | What to do |
|---|---|---|
| `region_unresolved` | the stack has no concrete region (a stack without `env`) | set `env.region` on the stack |
| `account_invalid` | the stack's account is set and is not a 12-digit id | fix `env.account` or leave it unset |
| `image_name_invalid` | the image name, with the size suffix, is not letters, digits, `-` or `_`, or exceeds 64 characters | shorten it or remove the extra characters |
| `image_name_reserved` | the name starts with `rayito-base` (the one of `rayito setup` and `rayito image publish`) | pick another name |
| `artifact_not_found` | the `from_file` / `fromFile` zip does not exist | check the path, or build the zip with `rayito image zip` |
| `artifact_checksum_mismatch` | the zip's sha256 is not the expected one (the one you passed or the release's `SHA256SUMS`) | download it again; if you passed `sha256`, check it |
| `artifact_variant_mismatch` | the zip is of a different variant than `variant` | pass the zip's `variant`, or use the zip of that variant |
| `artifact_efs_mismatch` | the zip's EFS marker does not match `with_efs` / `withEfs` | set `with_efs` / `withEfs` to match the zip, or use the zip with or without EFS |
| `option_invalid` | an option is not valid: `latest` as base version, `with_efs` without the `full` variant and `os_capabilities`, sizes outside the catalog, memory that is not a positive integer or, with `sizes`, differs from the baseline, invalid `environment`, `allowed_mount_buckets` or `tags`, or a CodeBuild timeout outside 5-110 minutes | the message says which option and why |
| `release_download_failed` | `download_release` / `downloadRelease` could not download the release zip | check the network and the version; or download it by hand and use `from_file` / `fromFile` |

## E2B shim errors { #errores-del-shim-de-e2b }

`rayito.e2b` (and `rayito/e2b`) exports the names from `e2b.exceptions`
pointing at the native classes: `NotEnoughSpaceException` /
`NotEnoughSpaceError` is `DiskFullException` / `DiskFullError`, and
`ServiceBusyException` / `ServiceBusyError` is `CapacityException` /
`CapacityError`. The shim's `Template.build()` raises the native
`BuildException`/`TemplateException`, and the namesakes in `rayito.e2b`
(`BuildError`/`TemplateError` in `rayito/e2b` for TypeScript) are those same
classes: an `except rayito.e2b.BuildException` catches the failed build just
like `except rayito.BuildException`. Details:
[Differences with E2B](../e2b-compat.md#funciona-sin-cambios).

## See also { #ver-tambien }

- [Troubleshooting](../operacion/solucion-de-problemas.md): by symptom
- [Python reference: exceptions](python/excepciones.md)
- [TypeScript reference](typescript.md)
- [Deploy with CDK: errors](../guias/desplegar-con-cdk.md#errores)
