# Environment variables

Rayito is configured with explicit arguments; these variables are shortcuts
so you do not have to repeat them. None of them turns on one of the
[optional features](../optional-features.md) (secrets, metadata index,
OpenTelemetry): those are only activated with an SDK option.

!!! warning "The exception: `RAYITO_TRANSFER_BUCKET`"
    With this variable exported, `files.write` / `files.read` of 8 MiB or
    more and signed URLs go through S3, which charges for its requests and
    for the temporary storage of the transfer prefix
    ([Files and S3](../files.md#coste-de-s3), [Pricing](../cost.md)).

## SDK (Python and TypeScript) { #sdk-python-y-typescript }

| Variable | What it does | Equivalent to |
|---|---|---|
| `RAYITO_TEMPLATE` | name or ARN of the default image for `create()` | `template=` / `template` |
| `RAYITO_ACCESS_TOKEN` | access token for `connect()` and the class forms (`set_timeout`, `get_metrics_history`, `update_network`) and for the CLI | `access_token=` / `accessToken` |
| `RAYITO_TRANSFER_BUCKET` | transfer bucket (URLs and large files via S3); the only route in the E2B shim | `transfer=S3Staging(bucket)` / `transfer: { bucket }` |
| `RAYITO_TRANSFER_PREFIX` | prefix inside the bucket (`rayito-transfer` by default) | `S3Staging(prefix=)` |
| `RAYITO_TRANSFER_REGION` | the bucket's region if it is not the sandbox's | `S3Staging(region=)` |

!!! warning "`create()` also reads `RAYITO_ACCESS_TOKEN`"
    If you export it, every `create()` in that process reuses the same
    secret, so a leak opens all of its sandboxes, not one. By default
    `create()` generates 32 random bytes per sandbox. Use it for `connect()`
    or the CLI from another process, not in the process that creates
    sandboxes. A custom token (here, in `access_token=`/`accessToken` or in
    `--token-file`) must be base64url of at least 16 random bytes
    (`ACCESS_TOKEN_MIN_BYTES`); a shorter one is `InvalidArgumentException`
    / `InvalidArgumentError`, without repeating the token in the message.

## CLI { #cli }

| Variable | What it does |
|---|---|
| `RAYITO_BUCKET` | image artifacts bucket for `rayito image publish` and the `bucket` check of `rayito doctor`; the bucket `rayito setup` reuses (if it does not exist, setup creates it with that name); and the build context bucket of `rayito agent deploy` and `deploy_agent()` / `deployAgent()`, also with `--target agentcore` (after `[deploy].bucket`). Equivalent to `--bucket` |
| `RAYITO_ACCESS_TOKEN` | token for `sandbox connect`, `exec` and `metrics` if you do not pass `--token-file` (the file wins); `sandbox create` reuses it as the new sandbox's token instead of generating one |
| `AWS_REGION` | default region for all commands (equivalent to `--region`); the CLI reads it even if `boto3` does not |

`rayito image publish` does not read these two, it **prints** them when a
launchable build finishes, so a script can pick them up from the last line:

| Printed line | What it is |
|---|---|
| `RAYITO_TEMPLATE=<ARN>` | ARN of the published baseline image |
| `RAYITO_TEMPLATE_<SIZE>=<ARN>` (e.g. `RAYITO_TEMPLATE_4GB`) | with `--sizes`, ARN of each additional-size image; the SDK does not read it: `size=` resolves the image by name ([Sizes](../funciones-opcionales/tamanos.md)) |

## MCP server { #servidor-mcp }

| Variable | Default | What it does |
|---|---|---|
| `RAYITO_TEMPLATE` | — | sandbox image (required on the first tool that uses it) |
| `RAYITO_TEMPLATE_VERSION` | the latest active | image version |
| `RAYITO_EXECUTION_ROLE_ARN` | — | sandbox execution role; with a role, logs in CloudWatch |
| `RAYITO_MCP_TIMEOUT_SECONDS` | 3600 | maximum sandbox lifetime (60–28,800) |
| `RAYITO_MCP_IDLE_SECONDS` | 300 | seconds without traffic before suspending; `0` disables it |
| `RAYITO_MCP_LOG_LEVEL` | `INFO` | log level (to stderr) |

Details: [MCP server](../mcp.md#variables-de-entorno).

## Image { #imagen }

| Variable | Where | What it does |
|---|---|---|
| `RAYITO_ALLOW_ROOT=1` | in the image (`rayito image publish --env RAYITO_ALLOW_ROOT=1`), never in the SDK | allows `user="root"` in commands and terminals; only the exact value `1` counts. By default everything runs as uid 1000 |
| `RAYITO_ALLOWED_MOUNT_BUCKETS` | in the image (`rayito image publish --env RAYITO_ALLOWED_MOUNT_BUCKETS=b1,b2`), never in the SDK | comma-separated list of the buckets that `mounts=` may mount; absent or empty, none ([S3 mounts](../funciones-opcionales/montajes-s3.md)) |
| `RAYD_LOG` | in the image (`rayito image publish --env RAYD_LOG=debug`) | filter for `rayd` logs (`tracing` syntax, `info` by default); only visible with `logging="cloudwatch"` |
| `AWS_REGION` | set by the platform in the MicroVM | region that `rayd` uses in its own calls to AWS (S3, CloudWatch) |

`rayd` passes none of these variables, nor any `AWS_*`, to the processes it
launches: a command only sees the `envs` you give it.

## AWS { #aws }

They are read by the AWS SDK that Rayito uses underneath (`boto3` in Python,
AWS SDK v3 in TypeScript):

| Variable | What it does |
|---|---|
| `AWS_PROFILE` | profile from `~/.aws/config` (including SSO ones) |
| `AWS_DEFAULT_REGION` | fallback if `AWS_REGION` is missing, in both SDKs |
| `AWS_REGION` | region of the sandboxes in Python, TypeScript and the CLI; `region=` / `region` overrides it |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | explicit credentials |

## End-to-end tests { #tests-de-extremo-a-extremo }

Only for those who develop Rayito: `RAYITO_E2E=1` and `RAYITO_TEMPLATE`
enable the suites against real AWS
([`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md)).
