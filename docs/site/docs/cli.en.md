# CLI

`rayito` is the SDK's command-line tool: it publishes and lists the image,
prepares the account (`rayito setup`), prunes old versions, lists, inspects
and kills sandboxes, reads their CloudWatch logs and diagnoses an account
before the first `Sandbox.create()`. It also deploys the optional stacks
(`rayito stack`, `rayito events`, `rayito domain`), builds declarative
templates (`rayito template`) and the AI agent as a project
(`rayito agent`). There is no `rayito.toml`: configuration is
options and environment variables.

!!! note "Messages stay in Spanish"
    The CLI's own messages are written in Spanish, so the literal texts quoted
    on this page (`sin región: …`, `falta el access token: …`, `sin logs: …`,
    `build fallido: …`) appear in Spanish. Each one is glossed where it
    appears.

| Group | Commands |
|---|---|
| [`rayito image`](#rayito-image) | `publish`, `list`, `sizes`, `prune`, `zip` |
| [`rayito sandbox`](#rayito-sandbox) | `list`, `info`, `kill`, `logs`, `create`, `connect`, `proxy`, `exec`, `metrics` |
| [`rayito stack`](#rayito-stack) | `list`, `status`, `deploy`, `destroy` |
| [`rayito domain`](#rayito-domain) (experimental) | `deploy`, `status`, `destroy` |
| [`rayito events`](#rayito-events) | `deploy`, `status`, `destroy`, `list`, `webhook add`, `webhook list`, `webhook remove` |
| [`rayito template`](#rayito-template) | `build`, `status`, `logs` |
| [`rayito agent`](#rayito-agent) | `init`, `build`, `run`, `detect`, `deploy`, `export`, `invoke`, `status`, `stop`, `list`, `delete`, `template build` |
| [`rayito dashboard`](#rayito-dashboard) | — |
| [`rayito setup`](#rayito-setup) | — |
| [`rayito doctor`](#rayito-doctor) | — |

There are two CLIs with the same surface, output and exit codes: the Python
one (the `cli` extra) and the TypeScript one (the `rayito` executable of the
npm package). What follows applies to both except where stated otherwise; the
two command trees are compared against `testdata/cli/surface.json` and
`scripts/check_sdk_parity.py --cli`. The MCP server is another executable in
the same package, `rayito-mcp` ([MCP server](mcp.md)).

## Installation { #instalacion }

=== "Python"

    The CLI is an extra of the `rayito` package (it adds `typer`):

    ```bash
    pip install "rayito[cli]"        # or: uv pip install "rayito[cli]"
    rayito --help
    ```

    From a checkout of the repository, without installing anything:

    ```bash
    uv run --project clients/python rayito --help
    ```

    Without the extra, `rayito` prints how to install it and exits with 2.

=== "TypeScript"

    The `rayito` npm package declares the `rayito` executable (Node 20 or
    later, ESM only, no CLI dependencies: it uses `node:util`):

    ```bash
    npx rayito --help                # without installing anything
    npm install --global rayito      # or: pnpm add --global rayito
    rayito --help
    ```

    The AWS clients that not every command needs are **optional peers** of the
    package (`@aws-sdk/client-cloudformation` for `stack`, `events` and
    `domain`, `@aws-sdk/client-cloudwatch-logs` for `sandbox logs`,
    `@aws-sdk/client-iam` for `doctor`'s IAM simulation,
    `@aws-sdk/client-service-quotas` and `@aws-sdk/client-cost-explorer` for
    the dashboard, among others). A command that needs a missing one fails and
    says which package to install. The TypeScript API is async-only: the CLI
    does not change because of that.

!!! warning "Two global installs"
    If you install both CLIs, the first one on the `PATH` wins.
    `rayito --version` says which one answers (`(python)` or `(node)`).

## Global options and credentials { #opciones-globales-y-credenciales }

```text
rayito [--profile P] [--region R] [--json] [--verbose] [--version] <group> <command> …
```

- `--profile` and `--region` build the `boto3` session; without them
  `AWS_REGION` is used and, if missing, the usual chain (`AWS_PROFILE`,
  `AWS_DEFAULT_REGION`, the config file, the machine's role). There is no
  Rayito API key.
- With no resolvable region: `sin región: pasa --region o exporta AWS_REGION`
  ("no region: pass --region or export AWS_REGION") and exit code 2, before
  any call. With no credentials or with an unknown profile, the same.
- `--json`: each command writes **a single JSON document** to stdout and
  progress to stderr, so `rayito --json sandbox list | jq` works.
- `--verbose`: `INFO` logs from the SDK (reconnections, probes) and the CLI.
- `--version`: prints `rayito <version> (python)`. The runtime in parentheses
  says which of the two CLIs answers when the Python one and the npm
  package's coexist (`rayito <version> (node)`): the first on the `PATH` wins.

Exit codes: `0` success; `1` the operation failed (non-launchable build, a
version that `prune` could not delete, an unknown id in `kill`, a `doctor`
`FAIL`, an AWS error printed as `AWS error <Code>: <Message>`); `2` usage or
environment (arguments, no region, no credentials, no extra).

## `rayito image` { #rayito-image }

### `image publish` { #image-publish }

```bash
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 \
    --bucket <bucket> [--variant full|slim|poly] [--image-name N] \
    [--os-capabilities ALL] [--build-role-arn ARN | --stack-name rayito-m0-iam] \
    [--memory-mib 2048] [--timeout-seconds 1800] [--force] \
    [--sizes 512mb,1gb,4gb,8gb] [--env K=V]... [--with-efs] \
    [--artifact-run-id RUN_ID]
```

`--base-image-version` is required and pins the version of the AWS managed
base (`imageVersion` of `list-managed-microvm-image-versions`) the image is
built on. There is no need to look it up by hand: with `latest` the newest one
AWS lists is used and printed (`base_image_version=<version>`); an explicit
number pins a reproducible build.

`--with-efs` publishes the image with the `amazon-efs-utils` that `volumes=`
needs ([EFS volumes](funciones-opcionales/volumenes-efs.md)): it requires a
zip made with `rayito image zip --with-efs`, `--os-capabilities ALL` (`rayd`
only mounts with `CAP_SYS_ADMIN`) and the `full` variant, and its default name
is `rayito-base-caps-efs`. A zip with the efs marker without `--with-efs`, or
`--with-efs` with a zip without it, is rejected before calling AWS.

`--sizes` publishes, in addition to the baseline (2048 MiB), one image with a
size suffix for each listed name from the same artifact; `--env KEY=VALUE`
(repeatable) bakes additional image variables into all the images the
invocation publishes. With `--sizes`, `--memory-mib` only accepts the default
value (2048): the baseline of `--sizes` is always the image without a suffix.
See [Sizes](funciones-opcionales/tamanos.md).

It reproduces the `make image-publish` pipeline:

1. Checks, before calling AWS, that the zip exists and that its variant marker
   (`warmup_variant` for `slim`, `kernels_variant` for `poly`) matches
   `--variant`, and that the `efs_variant` marker is present if and only if
   `--with-efs` is passed.
2. Uploads the zip to `s3://<bucket>/rayito/images/rayd-<12 hex of the sha256>.zip`,
   unless the key already exists (content-addressed key: same zip, same key).
   With `--artifact-run-id RUN_ID` the key is
   `rayito/images/runs/<RUN_ID>/rayd-<12 hex of the sha256>.zip`: the object
   belongs only to that run (see below).
3. Takes the build role from `--build-role-arn` or from the `BuildRoleArn`
   output of the `--stack-name` stack.
4. If there is already a `SUCCESSFUL`/`ACTIVE` version built from that zip
   with the same configuration (hooks, memory, log group,
   `additionalOsCapabilities` and `baseImageVersion`, the latter compared
   numerically because the API accepts `1` and returns `1.0`), it **reuses**
   it and builds nothing; `--force` builds anyway. Each version costs one
   week of snapshot storage.
5. Otherwise, `create-microvm-image` the first time or `update-microvm-image`
   afterwards, and polls every 10 s for the **three-state gate**: image
   `CREATED|UPDATED`, version `SUCCESSFUL`, status `ACTIVE`. Only then is the
   version launchable.
6. Prints the `snapshotBuild` summary and, on the last line,
   `RAYITO_TEMPLATE=<arn>`. If the build fails, it prints the `stateReason`s
   and the tail of the build logs of the `/rayito/<image>` group and exits
   with 1.

`--artifact-run-id` is for those who publish disposable images in parallel (a
release's acceptance, e2e): two runs of the same commit would share the
content-addressed key, and the first one's cleanup would delete the zip the
second one's image still points to. `RUN_ID` is a single key segment
(letters, digits and hyphens, 1 to 64, starting with a letter or digit);
any other value is a usage error (2) before calling AWS. The key stays under
`rayito/images/`, so the permissions below do not change. The `--json`
summary says in `artifactUploaded` whether this invocation uploaded the
object (`true`) or it already existed (`false`): a cleanup deletes only the
objects its run uploaded. With `--sizes`, the root (the baseline) is the one
that uploads; the sizes reuse that object and say `false`. Without the flag
the key is the usual shared one.

`--bucket` is resolved from the flag or from `RAYITO_BUCKET`; with neither
it is a usage error (2). The library ships no default bucket: the bucket
belongs to the account that publishes.

S3 permissions of the publishing principal (`ImagePublisherPolicy` or
`CallerPolicy` in `infra/iam.yaml` grant them as is): `s3:PutObject` and
`s3:GetObject` on `arn:aws:s3:::<bucket>/rayito/*` and `s3:ListBucket` on
`arn:aws:s3:::<bucket>`. `s3:ListBucket` is what makes the `head-object` of a
new artifact answer `404` instead of `403` (without it the CLI uploads the
artifact anyway and a genuinely missing permission surfaces at `put-object`)
and the only thing `rayito doctor`'s `head-bucket` needs.

### `image list` { #image-list }

```bash
rayito image list [--name-filter F]      # images: name, state, latestActive/FailedImageVersion, createdAt
rayito image list rayito-base            # versions of an image, newest first
```

With a name or ARN it lists `imageVersion`, `state`, `status`,
`baseImageVersion`, the S3 artifact name and `createdAt`.

### `image sizes` { #image-sizes }

```bash
rayito image sizes [--variant full|slim|poly] [--image-name <name>]
```

For each size in the closed catalog (`512mb`/`1gb`/`2gb`/`4gb`/`8gb`,
[Sizes](funciones-opcionales/tamanos.md)), which image of the variant
`rayito image publish --sizes` has already published (`name`, `published`,
`imageArn`, `state`, `sameArtifact`, `createdAt`) or whether none has. Always
one `list-microvm-images` filtered by the base name; if any additional size
is published, also one `GetMicrovmImageVersion` (with no quota of its own) per
published image for `sameArtifact` — with only the baseline published, no
additional call. It never builds, publishes or launches anything.
`sameArtifact` compares the `codeArtifact.uri` of each size's active version
against the baseline's: `false` detects a size published from a different
zip, without launching any sandbox. `--image-name` lists the family published
with `rayito image publish --image-name <name> --sizes ...` (`<name>`,
`<name>-4gb`, ...) instead of the variant's.

### `image prune` { #image-prune }

```bash
rayito image prune --dry-run                     # first: the plan, deleting nothing
rayito image prune [--image-name rayito-base] [--keep 5] [--wait-timeout 600]
```

Keeps the newest `--keep` launchable versions, every version that a
non-terminated MicroVM is using (read right before each deletion) and those
that are `PENDING`, `IN_PROGRESS`, `DELETING` or `DELETED`; deletes the rest
from oldest to newest, **one at a time** (the API answers `ConflictException`
while the image is `UPDATING`/`DELETING`), with 5/10/20/40/80 s backoff and
first deactivating an `ACTIVE` version that the API refuses to delete. It
prints the plan table and a JSON summary; exits with 1 if any candidate still
exists. It never deletes the image or its `/rayito/<image>` log group; when
retiring a whole image, delete both by hand
([Declarative templates](funciones-opcionales/templates.md)).

### `image zip` { #image-zip }

```bash
rayito image zip image image/rayito-image.zip [--variant full|slim|poly] [--sidecar kernel-sidecar] [--with-efs]
```

`--with-efs` adds the `kernel-sidecar/efs_variant` marker, which activates the
`Dockerfile`'s conditional `amazon-efs-utils` layer (without it, no image
changes). The `--json` summary reports it in `withEfs`.

Deterministic zip (fixed dates and modes; without `__pycache__`, `tests`,
`.venv`, caches, `uv.lock` or other zips) with the `Dockerfile` at the root.
With `--sidecar` it first copies `kernel-sidecar/` to
`image/kernel-sidecar/` with the same exclusions. It prints files, bytes,
variant and the zip's sha256 (with it the S3 key can be predicted). It is the
only command that does not need AWS.

If the directory carries `rayd`, it must also carry its license notices in
`licenses/` (`LICENSE`, `NOTICE` and `THIRD_PARTY_LICENSES.md`): the
`Dockerfile` copies them to `/usr/share/doc/rayd/` and the zip refuses to
package `rayd` without them. From the repository, `make image-licenses`
leaves them there, generating `THIRD_PARTY_LICENSES.md` with cargo-about from
`Cargo.lock` (the `make image-zip*` targets already do it).

## `rayito sandbox` { #rayito-sandbox }

```bash
rayito sandbox list [--template T] [--template-version V] [--all-states] [--state S]… [--metadata K=V]… [--index-table TABLE]
rayito sandbox info ID [--no-metadata]
rayito sandbox kill ID… | rayito sandbox kill --all [--template T] [--yes]
rayito sandbox logs ID [--log-group G] [--limit 1000] [--since 30m|2h|1d|ISO]
rayito sandbox create [TEMPLATE] [--timeout S] [--metadata K=V]… [--env K=V]… [--detach] [--token-file F] [--user U] [--cwd D]
rayito sandbox connect ID [--user U] [--cwd D] [--env K=V]… [--token-file F]
rayito sandbox exec ID [--background] [--cwd D] [--user U] [--env K=V]… [--timeout 0] [--token-file F] -- CMD…
rayito sandbox metrics ID [--follow] [--interval 5] [--token-file F]
rayito sandbox proxy ID --port N [--local-port M] [--bind 127.0.0.1] [--allow-remote] [--allowed-host H]… [--allow-origin O]… [--max-connections 8]
```

- `list` omits `TERMINATING` and `TERMINATED` (AWS keeps listing them for
  about 20 minutes) unless `--all-states`; it probes no endpoint. `--state`
  (repeatable, case-insensitive: `--state suspended`) filters by state.
  `--metadata K=V` (repeatable) filters by metadata: without `--index-table`
  it is the O(n) probe of `Sandbox.list(metadata=)` (one `Health` per
  `RUNNING` sandbox); with `--index-table TABLE` (optional, **off by
  default**) it uses the metadata index in that DynamoDB table
  (`infra/metadata-index.yaml`): one `dynamodb:BatchGetItem` per page, no
  probes, and also over paused sandboxes
  (`rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes`).
  Only sandboxes created with `index=` appear; with `--json` each row carries
  its `metadata`. Cost and IAM in
  [Metadata index](funciones-opcionales/indice-de-metadatos.md).
  `--index-table` without `--metadata` is a usage error (exit code 2) and does
  not call DynamoDB: the index is only useful for filtering by metadata.
  `--all-states` cannot be combined with `--state`, `--metadata` or
  `--index-table` (exit code 2).
- `info` does a `get-microvm` and, on a `RUNNING` sandbox, reads its metadata
  with a `Health` (a single-use JWE, dedicated channel); `--no-metadata`
  avoids it. Metadata are not secrets; the endpoint is a hostname; there is
  no token to print.
- `kill` terminates each id and prints `terminated` or `not found` (exit code
  1 if any did not exist). `--all` lists the live ones, asks for confirmation
  (unless `--yes`) and terminates them; it is mutually exclusive with ids.
- `logs` resolves the `/rayito/<image>` group (or `--log-group`) and the
  sandbox's `YYYY/MM/DD[<version>]<id>` stream; if the exact name does not
  exist it walks up to 10 pages of streams by last event looking for those
  ending in `]<id>`. It prints `<ISO timestamp> <message>` (`--json`: list of
  `{timestamp, message, stream}`). **There are runtime logs only if the
  sandbox was launched with `execution_role_arn` and `logging="cloudwatch"`**;
  if there is no stream or group: `sin logs: el sandbox se lanzó con logging
  disabled, sin executionRoleArn, o en otro grupo (--log-group)` ("no logs:
  the sandbox was launched with logging disabled, without executionRoleArn, or
  in another group") and exit code 1.

<a id="create-connect-exec-y-metrics-m9"></a>

### `create`, `connect`, `exec` and `metrics` { #create-connect-exec-y-metrics }

The four operational commands of E2B's CLI that Lambda MicroVMs can serve.
Those that need to talk to `rayd` use the sandbox's **access token**, which
never travels in argv (it would show up in `ps`) nor is printed:

- `--token-file F` (the file's contents, without surrounding whitespace) or
  `RAYITO_ACCESS_TOKEN`; if both are present, the file wins. With neither,
  exit code 2 with `falta el access token: --token-file o RAYITO_ACCESS_TOKEN`
  ("missing the access token: --token-file or RAYITO_ACCESS_TOKEN").
- `create --detach` writes the token to `--token-file` **before**
  `run-microvm` (with `O_CREAT | O_EXCL` and mode `0600`: an existing file is
  exit code 2), so a failure to write it never leaves an orphan VM. Without
  `RAYITO_ACCESS_TOKEN` or `--token-file`, `--detach` is exit code 2. On
  Windows the mode is ignored by the system: it leaves the file in your user
  profile.

Behavior:

- `create` without `--detach` opens an interactive terminal in the sandbox
  and terminates it on exit (like `e2b sandbox create`); with `--detach` it
  prints the `sandbox_id` (`--json`: `sandbox_id`, `endpoint`, `template`,
  `template_version` and `expires_at`, never the token) and exits with 0.
  `--timeout` is the native `timeout` (3600 by default).
- `connect` resumes a suspended sandbox and opens the terminal; it does not
  terminate it.
- `exec` runs `CMD…` (joined with `shlex.join`) and forwards stdout and stderr
  as they arrive; it exits with the remote code, or 124 if the server's
  `--timeout` expired (`timeout del comando` on stderr, "command timeout").
  With `--background` it prints the `pid` and exits with 0.
- `metrics` prints the `get_metrics()` snapshot (table, or a JSON object with
  `--json`); `--follow` repeats every `--interval` seconds until Ctrl-C (exit
  code 0) or until the sandbox disappears (exit code 1). Connecting wakes a
  suspended sandbox.

A complete walkthrough, with the token in a file that only your user can read:

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
mkdir -p ~/.rayito && chmod 700 ~/.rayito           # --detach creates the file, not the directory
rayito sandbox create rayito-base --detach --timeout 1800 \
    --metadata team=data --token-file ~/.rayito/demo.token      # prints microvm-<id>
rayito sandbox exec microvm-<id> --token-file ~/.rayito/demo.token -- python3 -c 'print(40 + 2)'
rayito sandbox exec microvm-<id> --background --token-file ~/.rayito/demo.token -- sleep 600
rayito --json sandbox metrics microvm-<id> --token-file ~/.rayito/demo.token
rayito sandbox connect microvm-<id> --token-file ~/.rayito/demo.token   # terminal; Ctrl-D to exit
rayito sandbox kill microvm-<id>
```

The same token works in the SDKs: `Sandbox.connect("microvm-<id>",
access_token=open(path).read().strip())`.

The terminal puts yours in raw mode on POSIX (restored on exit; Ctrl-C
travels to the sandbox as `\x03`, never as a local SIGINT) and forwards size
changes (`SIGWINCH`); on a Windows console it translates the arrows and
polls the size every second; with piped stdin it forwards the bytes as they
are and sends `\x04` at the end. The exit code is the remote shell's.

### `proxy` { #proxy }

Exposes a guest port at `http://<bind>:<local-port>` (by default
`--local-port` equals `--port`), for local development: curl, a browser or
any HTTP/1.1 client against a server that the sandbox is already serving.
WebSocket passthrough (upgrade) is implemented, not measured against AWS. The
sandbox's access token is never needed, only the proxy JWE.

```bash
rayito sandbox proxy microvm-<id> --port 8000
# http://127.0.0.1:8000 → microvm-<id>:8000
#   with cookies isolated from other local apps: http://microvm-<id>.localhost:8000
curl http://127.0.0.1:8000/
```

- It rejects `--port 9000` (the lifecycle hooks port) and any value outside
  1-65535, without calling AWS.
- `--bind` other than `127.0.0.1`/`::1`/`localhost` needs `--allow-remote`
  (otherwise, a usage exit): anyone who reaches that port uses the sandbox
  with the same access as whoever launched the proxy. `--bind 0.0.0.0`/`::`
  additionally needs `--allowed-host`.
- `Host` and `Origin`: it only forwards if `Host` is `127.0.0.1`, `localhost`
  or `[::1]` with the local port, the `--bind` address, `<id>.localhost` or an
  `--allowed-host` (otherwise, `421`), and if the `Origin`, when present, is
  one of those same origins or an `--allow-origin` (otherwise, `403`). So a
  web page open in your browser does not reach the sandbox through DNS
  rebinding, a cross-site POST or a `WebSocket` from another origin
  ([Local proxy](funciones-opcionales/proxy-local.md#que-peticiones-reenvia)).
- At most `--max-connections` (8) forwarded connections at a time; the next
  one gets `503`.
- Headers: it strips any `x-aws-proxy-*` the client brings, sets `Host` to the
  sandbox endpoint, adds `X-aws-proxy-auth` (the current JWE) and
  `X-aws-proxy-port`, and forces `Connection: close` except on an upgrade
  request (`Upgrade` + `Connection: upgrade`, WebSocket). It only forwards the
  first request of each connection, with its body delimited by
  `Content-Length` or `chunked` (`400` if ambiguous); an upgrade to which the
  sandbox does not answer `101` is forwarded with `Connection: close` and
  closed. With no current JWE or no connection to the sandbox it answers
  `502` and writes the reason to stderr; the header has to arrive within 30 s
  and the connection to the sandbox open within 30 s. It never logs the JWE,
  the headers, the bodies or the paths of what goes through the proxy
  ([Security](security.md#rayito-sandbox-proxy)).
- The JWE is renewed at 45 min with the same `TokenRefresher` that the SDK's
  gRPC channel uses: a new one is not minted on every request.
- A `SUSPENDED` sandbox with auto-resume is woken by the first request that
  reaches the proxy (which does bill compute, plus a snapshot read on resume:
  [Limits](limits.md#tamano-cpuram) and [Pricing](cost.md) for the price per
  GB).
- $0 from AWS beyond `GetMicrovm` + `CreateMicrovmAuthToken` (free, 50 TPS
  quota per account/region; ~1 mint every 45 min per running proxy); no new
  resources. Ctrl-C stops the refresher and closes the listener.
- IAM: `lambda:GetMicrovm` and `lambda:CreateMicrovmAuthToken` on the MicroVM
  (already in `SandboxLauncherPolicy` and in `CallerPolicy` of
  `infra/iam.yaml`; nothing new to deploy to use the proxy).

## `rayito stack` { #rayito-stack }

```bash
rayito stack list
rayito stack status <component> [--stack-name N]
rayito stack deploy <component> [--param K=V]... [--artifact-bucket B] [--tag K=V]... [--stack-name N] [--yes]
rayito stack destroy <component> [--stack-name N] [--yes]
```

The [`OptionalStack`](funciones-opcionales/pilas-opcionales.md) convention:
each optional feature with its own infrastructure is a CloudFormation stack
in your account, and nothing is deployed unless you ask for it.

- `list` prints the catalog (name, whether it is implemented and idle cost)
  **without calling AWS**.
- `deploy` prints the component's "Cost and activation" block and the
  parameters that change, and asks for confirmation unless `--yes` (or
  `--json`). On a stack that already exists, the parameters you do not repeat
  keep their current value; defaults only apply on creation.
  `--artifact-bucket` is required for a component with Lambda code
  (`events-webhooks`): that is where the code is uploaded, at
  `rayito/stacks/<component>/<sha256>.zip`, only if the bucket belongs to
  your account and comparing the content of an object that already exists.
- `status` is a `DescribeStacks`: the stack's state and outputs.
- `destroy` says what is kept and asks for confirmation unless `--yes`.

## `rayito domain` { #rayito-domain }

```bash
rayito domain deploy --public-domain D --certificate-arn ARN [--alternate-domain-name H]... [--stack-name N] [--yes]
rayito domain status | destroy [--stack-name N] [--yes]
```

[Custom domain](funciones-opcionales/dominio-propio.md) (**experimental**, not
yet verified end to end on real AWS): a facade over
`rayito stack deploy custom-domain` (and `status`/`destroy`) with
`CustomDomain`'s names. `deploy` validates `--public-domain` and each
`--alternate-domain-name` before calling AWS, asks for confirmation unless
`--yes` and prints what to point the `CNAME` at. The ACM certificate has to be
in `us-east-1`.

## `rayito events` { #rayito-events }

```bash
rayito events deploy --artifact-bucket B --log-group-name G [--reconciler-interval-minutes 5] [--tag K=V]... [--yes]
rayito events status | destroy [--yes]
rayito events list [--sandbox-id ID] [--type T]... [--limit 100] [--order desc|asc]
rayito events webhook add <url> --secret-name S --type T [--type T]...
rayito events webhook list
rayito events webhook remove <webhook-id>
```

A shortcut for `rayito stack deploy events-webhooks` plus webhook management
and event querying ([Events and webhooks](funciones-opcionales/eventos-y-webhooks.md)).
All accept `--stack-name` (default `rayito-events-webhooks`). `deploy` prints
the cost (≈ $0.40/month at rest, the HMAC secret) and asks for confirmation
like `rayito stack deploy`.

## `rayito template` { #rayito-template }

```bash
rayito template build <spec.py> --name N --bucket B [--memory-mb 2048] [--force] [--timeout 1800]
rayito template status <name> [--version V]
rayito template logs <name> [--limit 500]
```

Builds a [declarative template](funciones-opcionales/templates.md): `build`
runs `spec.py` (which must define a module variable `template` holding a
`rayito.Template`), uploads the context to `--bucket` under
`rayito/templates/` and creates or updates the `--name` image, printing the
build log. Each new version costs snapshot storage, just like
`rayito image publish`; old ones are deleted with
`rayito image prune --image-name <name>`. `status` and `logs` only read:
without `--version`, the most recent version.

## `rayito agent` { #rayito-agent }

```bash
rayito agent init [DIR] [--runtime R] [--language python|typescript] [--provider P] [--model ID]
                  [--region R] [--force]
rayito agent build --bucket B [--file PATH] [--force] [--timeout 1800]
rayito agent run PROMPT [--file PATH] [--session-id ID] [--model ID] [--stream]
rayito agent detect [PATH] [--framework F] [--language L] [--entry E] [--protocol P]
                    [--model-id ID] [--model-provider P] [--model-base-url URL] [--write]
rayito agent deploy [PATH] [--file PATH] [--timeout S] [--port N] [--token-file F] [--keep-on-failure]
                    [--framework F] [--language L] [--entry E] [--protocol P]
                    [--model-id ID] [--model-provider P] [--model-base-url URL]
                    [--memory MIB] [--max-sessions N] [--sessions shared|dedicated]
                    [--min-vms N] [--max-vms N] [--warm N] [--idle S] [--queue N] [--queue-timeout S]
                    [--env K=V]... [--allow-out DEST]... [--bucket B] [--secret PROVIDER=NAME]...
                    [--create-secret] [--secret-env NAME=SECRET]... [--secret-env-from SECRET]...
                    [--create-secrets-from FILE] [--check-env]
                    [--base-artifact ZIP] [--yes] [--dry-run]
                    [--target microvm|agentcore] [--tag K=V]... [--bedrock-model ID]...
                    [--readable-secret NAME]... [--idle-session-timeout S] [--max-session-lifetime S]
                    [--external-model]
rayito agent export --target docker|agentcore --out DIR [--file PATH] [--overwrite]
rayito agent invoke SANDBOX_ID [PROMPT] [--session-id ID] [--input JSON | --input-file F]
                    [--stream] [--token-file F] [--target microvm|agentcore]
rayito agent status SANDBOX_ID [--token-file F] [--target microvm|agentcore]
rayito agent stop SANDBOX_ID [--target microvm|agentcore] [--session-id ID]
rayito agent list --target agentcore
rayito agent delete NAME --target agentcore [--yes]
rayito agent template build --bucket B [--name rayito-agent] [--base rayito-base-caps] [--memory-mb 2048]
                            [--no-deepagents] [--no-prefetch] [--force] [--timeout 1800]
```

Since 0.14.0 the `init`, `build`, `run`, `deploy`, `export`, `invoke`,
`status` and `stop` commands work with an agent project (`rayito-agent.toml`,
`--file` defaults to the current directory): see
[Deploy your agent](guias/despliega-tu-agente.md). `init` and `export` make no
AWS call; `init` overwrites nothing without `--force` (exit code 2), `deploy`
prints the sandbox id, the URL and the port (with `--json`, `sandboxId`, `url`
and `port`) and never the access headers, and `--stream` prints the events as
they arrive (with `--json`, one JSON line per event). `deploy` saves the
sandbox access token to `--token-file` (or uses `RAYITO_ACCESS_TOKEN`) and
exits with 2 without creating anything if it has neither; `invoke` and `status`
read it the same way, and `stop` does not need it. If the startup fails,
`deploy` prints the cause (the failure code and the end of the app's standard
error, with no environment values or credentials) and terminates the sandbox,
unless `--keep-on-failure` is given, which leaves it alive for debugging (it
bills until `rayito agent stop`). With an `HttpApp` that declares its own
routes without `protocol = "raw"`, `deploy` warns that they are not reachable
from outside. `run` and `invoke` print `tokens=desconocido` ("unknown") (and
`usageReported: false` with `--json`) when the agent reported no tokens.
`init --runtime custom` writes an HTTP app (`[runtime.app]` with `protocol =
"raw"`, `requirements`, `install` and a `serve.py`), and `init --runtime
langgraph`, a plain LangGraph graph.

`rayito agent detect` and `rayito agent deploy` (since 0.14.0) work with an
agent you already have, with no `rayito-agent.toml`: they detect the language,
framework, entrypoint, install and provider by reading the files (they do not
run your code), and the flags set what detection gets wrong. `detect` makes no
AWS call and with `--write` saves what it detected as `rayito-agent.json`;
`deploy --dry-run` prints the detection, the plan and the cost without calling
AWS. `deploy` deploys a **fleet**
([Several sessions and several VMs](guias/flota-de-agentes.md)): `--min-vms`
live VMs and `--warm` parked slots; with `--secret provider=name` it uses an
existing secret and with `--create-secret` it creates a missing one from the
standard variable of the gateway's preset (`AWS_BEARER_TOKEN_BEDROCK` for a
`[gateways.model] preset = "bedrock"`), under exactly the name the gateway reads
(it costs $0.40 per month); it creates no buckets
(`rayito setup` does; use `--bucket`, `RAYITO_BUCKET`, the `cdk bootstrap` one
and, if it does not exist, the `BucketName` output of the `rayito-m0-artifacts`
stack that `rayito setup` created) and only publishes the base image with `--base-artifact` and `--yes`. `invoke`,
`status` and `stop` accept the fleet name (`rayito agent stop <name>` terminates
all its VMs); the `session_id` that `invoke` returns on a fleet with several VMs
identifies the VM, and passing it with `--session-id` continues the
conversation. Messages and help are in Spanish. See
[Deploy your agent](guias/despliega-tu-agente.md#sin-escribir-nada).

With several graphs in `langgraph.json`, `detect` and `deploy` accept
`--graph NAME` (exclusive with `--entry`). Instead of `PROMPT`, `invoke`
accepts `--input JSON` or `--input-file F` (`-` reads standard input): the input
object of a graph with its own schema; exactly one of the three, or it exits
with 2 before calling anything. The graph's output (`output`) is printed as
JSON, and with `--json` it goes in the `output` key. See
[Several graphs and structured input](guias/despliega-tu-agente.md#varios-grafos-y-entrada-estructurada).

With `--secret-env NAME=<secret>[#key]` and `--secret-env-from
<secret>[,prefix=P][,keys=A|B]` (repeatable, on both targets) the agent
receives Secrets Manager values as environment variables, added to the
`[secret_env]` and `[[secret_env_from]]` of the file; `--create-secrets-from
FILE.env` creates (or updates) a JSON secret with the values of that `.env`,
shows only the names and asks for `--yes` (it costs $0.40 per month), and
`--check-env` warns about the `.env.example` variables that receive no value.
See [Secrets as environment variables](guias/secretos-como-entorno.md).

`--protocol` accepts `agentcore`, `json`, `raw` and, since this version,
`langgraph-api` (experimental): on a project with `langgraph.json` it serves
the graph with `langgraph dev` and its LangGraph Platform API instead of
importing it; without `langgraph.json` it fails with `invalid_value`. See
[LangGraph Platform API](guias/despliega-tu-agente.md#langgraph-platform-api).

Builds the `AgentTemplate` image
([Agent templates](funciones-opcionales/templates-de-agente.md)): it starts
from `--base` (an image already published with `rayito image publish`) and
installs OpenCode, ripgrep and, unless `--no-deepagents`, the deepagents
environment, all pinned by hash. `--no-prefetch` omits the daemon that
preloads the binaries after each snapshot restore. No spec file: the recipe is
the SDK's. It prints `template_id=` and `build_id=` (with `--json`,
`templateId`, `buildId` and `alias`); a failed build is `build fallido: …`
("build failed") and exit code 1.

It is built once: the agent's normal start is
`Sandbox.create(template="rayito-agent", ...)` + `sbx.agent.run(...)`, with no
pool. Prefetch and warm pools are optional:
[What do I use?](guias/agente-en-el-sandbox.md#que-uso). Each new version
costs snapshot storage ([Pricing](cost.md#cuanto-cuesta-con-ejemplos)); old
ones are deleted with `rayito image prune --image-name <name>`.

`--model-id`, `--model-provider` and `--model-base-url` (on `detect` and
`deploy`) pin the model when your code
picks it from its configuration: the id, the gateway (`openai-compatible`,
`litellm`…) and the `https` URL of your LLM gateway, which becomes its
`upstream`. With a project file they behave like `--entry`: what the file says
wins. An unknown `--model-provider` gives `invalid_value` on `model.provider`.
See [Model chosen by your code](guias/despliega-tu-agente.md#modelo-desde-tu-codigo).

### `agent` with `--target agentcore` { #agent-agentcore }

Since 0.15.0, `rayito agent deploy --target agentcore`
deploys the agent to Amazon Bedrock AgentCore Runtime instead of a MicroVM
fleet, and `invoke`, `status`, `stop`, `list` and `delete` operate it. Without
`--target`, or with `--target microvm`, everything is as in 0.14. Full guide:
[Deploy to AgentCore](guias/desplegar-en-agentcore.md).

!!! info "Cost and activation"
    - **Turns it on:** `--target agentcore` in `rayito agent deploy`. `--dry-run`
      makes no AWS call.
    - **AWS resources and calls:** one CloudFormation stack per agent
      (`rayito-agentcore-<name>`), one CodeBuild build for each new content and
      one AgentCore runtime.
    - **Approximate cost** (us-east-1, list prices, checked on 2026-10-19):
      runtime at $0.0895 per vCPU-hour and $0.00945 per GB-hour of actual use;
      build at $0.0034 per minute (100 minutes a month free); image in ECR at
      $0.10 per GB-month. More in [Pricing](cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** [Before you start](guias/desplegar-en-agentcore.md#antes-de-empezar).
    - **How to turn it off:** `rayito agent delete <name> --target agentcore`.
    - **Example:** `rayito agent deploy ./my-agent --target agentcore --yes`.

| Option | Commands | What it does |
|---|---|---|
| `--target` (`microvm` or `agentcore`) | `deploy`, `invoke`, `status`, `stop` (on `list` and `delete` it is required and only `agentcore` is valid) | Where it is deployed. Default: `microvm` |
| `--tag K=V` | `deploy` | Tag for the `rayito-agentcore-<name>` stack and the runtime; repeatable; useful if the organization requires tags |
| `--bedrock-model ID` | `deploy` | Bedrock model or inference profile the runtime role may invoke; repeatable. Default: the project's model |
| `--readable-secret NAME` | `deploy` | Secrets Manager secret the runtime role may read; repeatable. Default: none |
| `--idle-session-timeout S` | `deploy` | Seconds without use after which AgentCore ends a session's microVM ({{agentcoreLifecycleMinSeconds}}-{{agentcoreLifecycleMaxSeconds}}, default {{agentcoreIdleSessionTimeoutDefaultSeconds}}) |
| `--max-session-lifetime S` | `deploy` | Maximum lifetime of a session's microVM ({{agentcoreLifecycleMinSeconds}}-{{agentcoreLifecycleMaxSeconds}}, default {{agentcoreLifecycleMaxSeconds}}) |
| `--external-model` | `deploy` | Allows a model outside Bedrock: the role gets no Bedrock permission you do not ask for and your code obtains the key (`--readable-secret`). Without it, a provider other than `bedrock` gives `provider_unsupported` ([guide](guias/desplegar-en-agentcore.md#modelos-fuera-de-bedrock)) |
| `--session-id ID` | `stop` | The session whose microVM is stopped (`StopRuntimeSession`); with `--target agentcore` it is required |
| `--yes` | `deploy`, `delete` | Do not ask for confirmation (required with no terminal or with `--json`) |

The new options, except `--target`, only apply with `--target agentcore`:
without it they give `option_requires_agentcore` (exit 2). With
`--target agentcore`, the MicroVM options that have no equivalent (`--min-vms`,
`--max-vms`, `--warm`, `--memory`, `--port`, `--timeout`, `--idle`, `--secret`,
`--create-secret`, `--token-file`, `--keep-on-failure`, `--base-artifact`,
`--allow-out`) give `option_not_supported` (exit 2) before calling AWS; the
[full table](guias/desplegar-en-agentcore.md#opciones-que-no-aplican) says what
each one does and why.

- **`deploy --target agentcore`** prints the plan (three steps: stack, image and
  runtime), the `Seguridad:` section (AgentCore has no Rayito secrets gateway:
  the agent gets credentials from the runtime role), the warnings and the
  approximate cost, and asks for confirmation (`¿Desplegar en AgentCore?`). With
  `--dry-run` it stops there without calling AWS; with no terminal or with
  `--json`, `--yes` is required (`needs_confirmation`, exit 2). When done it
  prints `target=agentcore`, `name`, `runtime_id`, `runtime_arn`, `version`,
  `region` and `logs`, and how to invoke it.
- **`invoke NAME PROMPT --target agentcore`**: the argument is the agent name;
  `--session-id` and `--stream` work as they are. `--token-file` gives
  `option_not_supported`: AgentCore authenticates with SigV4.
- **`status NAME --target agentcore`** prints the runtime's state, its version,
  the image, the region and the log group.
- **`stop NAME --target agentcore --session-id ID`** ends that session; without
  `--session-id` it gives `session_required` (exit 2).
- **`list --target agentcore`** lists the region's Rayito agents
  (`<name> runtime=… id=… version=… status=…`) or says there are none.
- **`delete NAME --target agentcore`** deletes the runtime, the
  `rayito-agentcore-<name>` stack (ECR repository with its images, CodeBuild
  project and logs, and the two roles) and the runtime's log group; only what
  Rayito created for that name. It asks for confirmation (`--yes` skips it; with
  no terminal or with `--json`, `delete_needs_confirmation`, exit 2). With
  nothing to delete, it exits 0.

**Exit 2** (usage or environment): `unsupported_region`, `option_not_supported`,
`option_requires_agentcore`, `target_unsupported`, `provider_unsupported`,
`network_policy_unsupported`, `name_invalid`, `name_conflict`, `model_required` (reserved: not raised today),
`image_missing`, `lifecycle_invalid`, `lifecycle_order`, `tags_invalid`,
`env_invalid`, `iam_resource_invalid`, `raw_protocol`, `no_bucket`, `bucket_region`, `needs_confirmation`,
`delete_needs_confirmation` and `session_required`. **Exit 1** (what fails in
AWS): `stack_blocked`, `stack_failed`, `image_build_failed`,
`image_build_timeout`, `build_quota`, `image_too_large`, `runtime_blocked`,
`runtime_failed`, `runtime_timeout`, `runtime_not_found`, `delete_failed`,
`access_denied` and `aws_error`. What each means:
[Errors](referencia/errores.md#agentcore).

With `--json`, each command prints a single document:

| Command | Keys |
|---|---|
| `deploy --dry-run` | `dryRun`, `target`, `detection`, `plan` (`target`, `name`, `region`, `runtimeName`, `stackName`, `repositoryName`, `buildProjectName`, `imageTag`, `bucket`, `bucketSource`, `network`, `bedrockModels`, `readableSecrets`, `environment`, `lifecycle`, `tags`, `steps`, `security`, `warnings`, `cost`) |
| `deploy` | `target`, `name`, `region`, `runtimeName`, `runtimeId`, `runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `stackName`, `logGroup`, `steps` |
| `status` | `target`, `name`, `region`, `runtimeName`, `runtimeId`, `runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `logGroup` |
| `list` | `target`, `region`, `agents` (`name`, `runtimeName`, `runtimeId`, `runtimeVersion`, `status`) |
| `delete` | `target`, `name`, `region`, `runtimeDeleted`, `stackDeleted`, `logGroupDeleted` |
| `stop` | `target`, `name`, `sessionId`, `stopped` |
| an error | `{"error": {"code", "message"}, "exitCode"}` on stdout and the line `rayito: <message>` on stderr, like `rayito setup` |

Each step (`steps`) carries `resource` (`stack`, `image` or `runtime`), `name`,
`action` and `status`; in the plan, `action` is always `planned`.

## `rayito dashboard` { #rayito-dashboard }

```bash
rayito dashboard [--port N] [--allow-actions] [--cost-explorer]
                 [--events-stack NAME] [--log-group GROUP]
                 [--theme FILE] [--logo FILE]
                 [--account NAME=ROLE_ARN ...] [--external-id ID] [--open]
rayito [--region R] dashboard cloudwatch [--log-group GROUP ...]
                 [--log-group-prefix PREFIX] [--otlp] [--quota] [--output FILE]
```

The local management dashboard, identical in both CLIs (same page, same
`/api/v1` API, same headers and same `Host`, `Origin` and CSRF checks): a server on `127.0.0.1` (random port unless
`--port`) with a page and the `/api/v1` API over your AWS credentials, until
you press Ctrl+C. It prints the bootstrap URL **only once**, with a
single-use token. By default it is **read-only**: `--allow-actions` enables
kill, suspend and resume with confirmation and a queue that respects the TPS;
`--cost-explorer` allows querying billed cost (each query costs $0.01, also
the "Ignorar la caché" (ignore cache) button, and startup announces it);
`--events-stack` enables the events view (DynamoDB reads, paid though
minimal); resuming with `--allow-actions` bills compute again; `--theme`
(`.json` or `.css`, or `RAYITO_DASHBOARD_THEME`) and `--logo` (PNG or SVG, or
`RAYITO_DASHBOARD_LOGO`) change the palette and the logo, and are validated
before opening the port. It does not support `--json`. Exit code 0 on close
and 2 if the port is invalid or busy or the region or credentials are
missing. `--account` (repeatable, opt-in) adds other read-only accounts
through assumed roles, with `--external-id` if the roles require it;
`rayito dashboard cloudwatch` prints the JSON of a CloudWatch dashboard
(`/rayito/*` logs, `--otlp`, `--quota`) without calling AWS or needing
credentials (generating it is free; deploying it costs $3 per month per
dashboard beyond the 3 free ones and $0.005 per GB scanned in each Logs
Insights widget, and `--quota` requires the `quota-alerts` stack, ~$0.30 per
month); see
[Dashboard: CloudWatch, accounts and alerts](funciones-opcionales/panel-extras.md).
Full guide in
[Management dashboard](funciones-opcionales/panel-de-gestion.md#panel-web-local),
with the [step-by-step walkthrough and screenshots](funciones-opcionales/panel-de-gestion.md#recorrido)
(the dashboard summarizes it in its **Help** entry),
defenses in [Security](security.md#panel-de-gestion-local). To have it always
available inside your account, with sign-in and audit:
`rayito stack deploy dashboard --artifact-bucket B`
([Dashboard in your account](funciones-opcionales/panel-en-tu-cuenta.md)).

## `rayito setup` { #rayito-setup }

```bash
rayito [--profile P] [--region R] [--json] [--verbose] setup
       [--bucket BUCKET] [--artifact PATH] [--tag K=V ...] [--yes] [--dry-run]
       [--no-caps-image]
```

Prepares the account and region for Rayito in one idempotent command, like
`cdk bootstrap`. Step-by-step tutorial: [Set up AWS](primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup).

| Option | What it does |
|---|---|
| `--bucket BUCKET` | Artifacts bucket in the same region; by default `RAYITO_BUCKET`, the IAM stack's one, the CDK bootstrap one or a new `rayito-artifacts-<account>-<region>`. If it does not exist, it is created with that name (`rayito-m0-artifacts` stack). |
| `--artifact PATH` | Already verified image zip (e.g. with cosign); without it, `rayito-image.zip` is downloaded from the `rayd-v<SDK version>` release and checked against its `SHA256SUMS`. Only the `full` variant zip, without EFS, is valid. |
| `--tag K=V` | Etiqueta K=V; repetible. Se aplica a las pilas que crea setup (rayito-m0-artifacts y rayito-m0-iam) y CloudFormation la propaga a sus recursos; útil si la organización exige etiquetas. (Help text, in Spanish like the rest of the CLI: K=V tag, repeatable; applied to the stacks setup creates and propagated by CloudFormation to their resources; useful if your organization requires tags. Reused stacks are not retagged and the image gets none.) |
| `--yes` | Do not ask for confirmation (required without a terminal or with `--json` if there is anything to create). |
| `--dry-run` | Only reads the account and shows the plan; creates or changes nothing. |
| `--no-caps-image` | Do not publish `rayito-base-caps`, the base of the MicroVM agent (the same image with `additionalOsCapabilities` `ALL`); by default it is published from the same zip. The step shows as `skip` and neither reads nor writes that image. |

`RAYITO_BUCKET` is read by the command, not by the parser, like `rayito doctor`.

With tags, if your organization requires them:

```bash
rayito setup --tag Owner=platform --tag Project=agents
```

**The plan** is always four steps, in this order:

| Resource | Possible actions | Writes | Asks for confirmation |
|---|---|---|---|
| `bucket` (`rayito-m0-artifacts` stack if created) | `create`, `reuse`, `wait` | `create` | `create` |
| `iamStack` (`rayito-m0-iam` stack) | `create`, `update`, `reuse`, `wait` | `create`, `update` | `create`, `update` |
| `baseImage` (`rayito-base` image) | `publish`, `reuse`, `wait` | `publish` | `publish` |
| `capsImage` (`rayito-base-caps` image) | `publish`, `reuse`, `wait`, `skip` | `publish` | `publish` |

- **Bucket**, in two phases. *Reuse:* the first that exists of `--bucket` or
  `RAYITO_BUCKET` (if you give an explicit one, it is the only candidate), the
  `ArtifactBucket` parameter of the IAM stack or the `cdk bootstrap` bucket of
  that account and region (default qualifier), each with one `HeadBucket`.
  Exists in the region: `reuse` (source `argument`, `environment`, `iamStack`
  or `cdkBootstrap`). Exists in another region: `bucket_region`. Exists but is
  not accessible (403): `bucket_unavailable`, except the `cdk bootstrap` one,
  which is skipped. Does not exist: with `--bucket` or `RAYITO_BUCKET`, it is
  created with that name; if it is the IAM stack's or the `cdk bootstrap` one,
  the next candidate is tried. An account with a reusable bucket never reaches
  the second phase or reads anything more. *Creation:* with no reusable
  bucket, `create` with the `rayito-m0-artifacts` stack (template
  `infra/artifact-bucket.yaml`), with the explicit name or, otherwise,
  `rayito-artifacts-<account>-<region>` (source `rayitoDefault`). If that stack
  already exists and is complete, `reuse` (source `artifactStack`); in
  progress, `wait`; blocked, `stack_blocked`. The created bucket is encrypted
  (SSE-S3), with all public access blocked, ACLs disabled, TLS only and the
  `rayito/images/` objects expiring after 7 days; deleting the stack keeps it.
  A bucket that already exists with the default name and does not belong to
  that stack is not adopted (`bucket_taken`): if it is yours, pass it with
  `--bucket`.
- **IAM stack:** if it does not exist, `create` with `ArtifactBucket=<bucket>`.
  In `CREATE_COMPLETE`, `UPDATE_COMPLETE` or `UPDATE_ROLLBACK_COMPLETE` and
  with the `BuildRoleArn` output, `reuse`; if its `ArtifactBucket` is another
  one, `update` (only that parameter changes). In a `*_IN_PROGRESS` state,
  `wait`; in `DELETE_IN_PROGRESS`, `ROLLBACK_COMPLETE` or `*_FAILED`,
  `stack_blocked`.
- **Image:** if `rayito-base` does not exist, `publish`; with a `PENDING` or
  `IN_PROGRESS` version, `wait`; with a `SUCCESSFUL` and `ACTIVE` version,
  `reuse` (whatever `rayd` it was built from; `rayito doctor --launch` warns
  about an old one and `rayito image publish` updates it); with only failed or
  inactive versions, `publish`. `publish` uses `--artifact` or the
  `rayd-v<SDK version>` release and the `latest` of AWS's managed base.
- **Image with capabilities** (since 0.17): `rayito-base-caps`, the base that
  `rayito agent deploy` uses on MicroVM. It is decided like the image and
  published from **the same zip** (one download) with
  `additionalOsCapabilities=["ALL"]`, like
  `rayito image publish --os-capabilities ALL`. With `--no-caps-image` the step
  is `skip`: it is neither read nor published.

The plan is `ready` when every step is `reuse` (or `skip`). It asks for confirmation
if any step is `create`, `update` or `publish`: on a terminal, with
`¿Preparar la cuenta? [y/N]:`; without a terminal or with `--json`, `--yes` is
required (`needs_confirmation`, exit 2). A `wait` costs nothing new and does not
ask. After applying, it runs `rayito doctor` (without `--launch`) on
`rayito-base` and the chosen bucket, and ends with `RAYITO_TEMPLATE=<arn>` and
`RAYITO_BUCKET=<bucket>`. The first run takes about 10–12 minutes, almost all of
it the image snapshot on AWS; an already prepared account finishes in seconds.

```bash
rayito setup --dry-run
```

On an account with no bucket at all (with a reusable one, the first line says
`reutilizar` (reuse) and there is no bucket cost line):

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<account>-<region>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.17.0 (≈ 10 min)
  imagen con capacidades rayito-base-caps: publicar desde rayd-v0.17.0 (≈ 10 min)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
  imagen con capacidades: ≈ $0,04 por semana por versión publicada ($0,08 por GB-mes, mínimo una semana); --no-caps-image la omite
no se creó ni cambió nada (--dry-run)
```

**Exit codes.** `0`: success, `--dry-run`, or nothing to do with a doctor
without `FAIL`. `1`: the operation fails (`stack_blocked`, `stack_incomplete`,
`release_download`, `checksum_missing`, `checksum_mismatch`,
`image_build_failed`, a "no" at the confirmation, a doctor `FAIL` or an
untranslated AWS error). `2`: usage or environment (`unsupported_region`,
`bucket_unavailable`, `bucket_region`, `bucket_taken`,
`artifact_stack_conflict`, `artifact_missing`, `artifact_variant`,
`needs_confirmation`, and the usual ones: no region, no
profile or no credentials). What to do with each:
[Troubleshooting](operacion/solucion-de-problemas.md#rayito-setup).

**With `--json`** a single document goes to stdout and progress to stderr. Its
keys, in this order: `rayito` (SDK version), `region`, `account` (`null` if the
plan failed), `dryRun`, `ready`, `steps`, `cost`, `result`, `doctor`, `error`
and `exitCode`. Each element of `steps` carries `resource` (`bucket`,
`iamStack`, `baseImage` or `capsImage`), `name`, `action` (`create`, `update`,
`reuse`, `wait`, `publish` or `skip`), `source` (`argument`, `environment`, `iamStack`,
`cdkBootstrap`, `artifactStack`, `rayitoDefault`, `release`, `artifact` or `null`), `status`, `imageVersion`,
`release` and `previousBucket`. `result` (`bucket`, `imageArn`,
`imageVersion`, `artifactUploaded`, `capsImageArn` and `capsImageVersion`, the
last two `null` with `--no-caps-image`) is `null` until the preparation finishes;
`doctor` is the document of `rayito --json doctor`, or `null` with `--dry-run`
and if the preparation stopped earlier; `error` is `{"code", "message"}` or
`null`. On an error the message also goes to stderr as `rayito: <message>`.

```json
{
  "rayito": "0.17.0",
  "region": "us-east-1",
  "account": "123456789012",
  "dryRun": true,
  "ready": false,
  "steps": [
    {"resource": "bucket", "name": "amzn-s3-demo-bucket", "action": "reuse",
     "source": "argument", "status": null, "imageVersion": null,
     "release": null, "previousBucket": null},
    {"resource": "iamStack", "name": "rayito-m0-iam", "action": "create",
     "source": null, "status": null, "imageVersion": null,
     "release": null, "previousBucket": null},
    {"resource": "baseImage", "name": "rayito-base", "action": "publish",
     "source": "release", "status": null, "imageVersion": null,
     "release": "rayd-v0.17.0", "previousBucket": null},
    {"resource": "capsImage", "name": "rayito-base-caps", "action": "publish",
     "source": "release", "status": null, "imageVersion": null,
     "release": "rayd-v0.17.0", "previousBucket": null}
  ],
  "cost": [
    "pila de IAM: gratis",
    "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
    "zip de la imagen en el bucket: céntimos",
    "imagen con capacidades: ≈ $0,04 por semana por versión publicada ($0,08 por GB-mes, mínimo una semana); --no-caps-image la omite"
  ],
  "result": null,
  "doctor": null,
  "error": null,
  "exitCode": 0
}
```

!!! info "Cost and activation"
    - **Turns on:** running `rayito setup` and confirming. `--dry-run` only
      reads the account; with an already prepared account it writes nothing
      either.
    - **AWS resources and calls:** with no reusable bucket, the
      `rayito-m0-artifacts` stack (`CreateStack` without capabilities: a bucket
      and its policy); the `rayito-m0-iam` stack
      (`CreateStack`/`UpdateStack` with `CAPABILITY_NAMED_IAM`); the
      `rayito-base` and `rayito-base-caps` images (one shared object in the
      bucket, and `CreateMicrovmImage` or `UpdateMicrovmImage` per image; the
      second with `additionalOsCapabilities=["ALL"]`); reads of `GetCallerIdentity`, `DescribeStacks`,
      `HeadBucket`, `GetMicrovmImage` and `ListMicrovmImageVersions`; the HTTPS
      download of `rayito-image.zip` and `SHA256SUMS`. The SDK does not call
      `CreateBucket`: CloudFormation creates the bucket with your credentials.
    - **Approximate cost:** IAM stack, free; the bucket costs nothing to exist
      and its zips expire after 7 days; each published version of each image
      ≈ $0.04 per week ($0.08 per GB-month, minimum one week; with
      `--no-caps-image`, one image less); the zip in the bucket, cents. See
      [Pricing](cost.md).
    - **IAM:** administrator or *bootstrap* credentials, once, like
      `cdk bootstrap`: CloudFormation, IAM (roles and policies), publishing the
      image and, if it creates the bucket, S3 (`s3:CreateBucket`,
      `s3:PutEncryptionConfiguration`, `s3:PutBucketPublicAccessBlock`,
      `s3:PutBucketOwnershipControls`, `s3:PutLifecycleConfiguration`,
      `s3:PutBucketTagging`, `s3:GetBucketPolicy`, `s3:PutBucketPolicy`). To use
      the SDK afterwards, the minimal policy of
      [Set up AWS](primeros-pasos/configurar-aws.md#3-la-pila-de-iam).
    - **How to turn it off:** do not run it; `--no-caps-image` does not publish
      `rayito-base-caps`; to undo it, delete the stacks
      (`rayito-m0-iam` and, if it exists, `rayito-m0-artifacts`) and the image
      versions. The created bucket is kept: empty and delete it by hand.
    - **Example:** `rayito setup --dry-run`.

!!! warning "The hash does not prove authenticity"
    The check against `SHA256SUMS` catches a truncated, corrupted or wrong zip,
    but not that it is authentic (`SHA256SUMS` comes from the same release).
    For the cosign signature, verify the zip and pass it with `--artifact`:
    [Verify a release](verify.md).

## `rayito doctor` { #rayito-doctor }

```bash
rayito [--json] doctor [--template rayito-base] [--template-version V] [--bucket B] [--launch]
                       [--efs-vpc-id V --efs-subnet-ids S1,S2]
```

Ten checks, in order, each with `OK`, `WARN`, `FAIL` or `SKIP`. An exception
inside a check is its `FAIL` (or `SKIP` when an `AccessDeniedException` only
prevents a diagnosis) naming the operation AWS rejected
(`details.operation`) next to the IAM permission the check needs
(`details.action`), and the others keep running. It exits with 1 only if any
is a `FAIL`.

| # | Check | What it looks at | `WARN` | `FAIL` | `SKIP` | What to do |
|---|---|---|---|---|---|---|
| 1 | `credentials` | `sts:GetCallerIdentity` and the session's region | region outside the ten with Lambda MicroVMs | no credentials, expired token, no region | — | `aws login` / export `AWS_PROFILE` and `AWS_REGION` |
| 2 | `managed-images` | `list-managed-microvm-images` and the newest `al2023-1` version | the call responds but `al2023-1` is not there | `AccessDenied`, unknown endpoint (old botocore), region without the service | — | update `boto3`; change region |
| 3 | `quotas` | Service Quotas of `lambda` with `microvm` in the name against the published defaults (`L-CD1C0CC4` memory, `L-535CA9B6` RunMicrovm...) | some adjustable quota below the default | — | `servicequotas:ListServiceQuotas` denied | request a quota increase in the console |
| 4 | `iam-simulation` | `iam:SimulatePrincipalPolicy` over the actions the SDK and the CLI use (image, lists, `PassNetworkConnector`, the bucket's S3, logs, quotas), one call per resource | some `implicitDeny` | `explicitDeny` or an SCP denial | root, non-simulable principal, `iam:GetRole`/`SimulatePrincipalPolicy` denied | indicative: checks 2, 5, 6 and 8 are the ones that count; `iam:PassRole` is not simulated. Measured 2026-09-16: the simulator returns `implicitDeny` for `lambda:PassNetworkConnector` even with `AdministratorAccess`, while `run-microvm` works: that `WARN` blocks nothing |
| 5 | `bucket` | a `head-bucket` of the artifacts bucket (`s3:ListBucket`, the same permission `publish` needs) and the region from its `x-amz-bucket-region` header | bucket in another region (`S3_CROSS_REGION_ACCESS_DENIED` when building); no region header | 403/404 (the summary names the rejected operation and `publish`'s S3 permissions) | no `--bucket` or `RAYITO_BUCKET` | grant `s3:ListBucket` on the bucket; use a bucket in the same region |
| 6 | `image-gate` | `get-microvm-image` + the version (`--template-version` or the latest active) through the three-state gate and its `snapshotBuild` | launchable but with a failed build listed | the image does not exist, is not `CREATED|UPDATED` or the version is not `SUCCESSFUL`/`ACTIVE` | — | `rayito image publish …`; `rayito image prune` for the failed build |
| 7 | `sandboxes` | non-terminated MicroVMs of the image (`list-microvms`) | 1–10 `RUNNING` (they bill) | more than 10 `RUNNING` | — | `rayito sandbox list`, `rayito sandbox kill --all` |
| 8 | `token` | `create-microvm-auth-token` (port 8080) for the `--launch` sandbox or the newest `RUNNING` one | — | `AccessDenied`, `ValidationException` | no `RUNNING` sandbox (use `--launch`) | check `lambda:CreateMicrovmAuthToken` |
| 9 | `agent` | a `rayd` `Health`: `agent_version`, `kernel_ready`, `imds_blocked`, `hook_anomalies` | `kernel_ready=false`, `hook_anomalies>0`, `imds_blocked=false` on a `-caps` image | `Health` fails (`UNAVAILABLE`, `UNAUTHENTICATED`, proxy 403) | no token | wait for the kernel; check who holds an `allPorts` token; republish the image |
| 10 | `compatibility` | the SDK ↔ `rayd` table in [Limits](limits.md); the image version (build counter per image and account) is only reported | `rayd` newer than the SDK in `MAJOR.MINOR` | `rayd` below the SDK's minimum | no `agent_version` | update the SDK or publish an image from the minimum `rayd` tag |

With `--efs-vpc-id` and `--efs-subnet-ids` (EFS volumes in a VPC that already
exists) the **`efs-network`** check is added after check 7: the same as
`EfsVolumes.check()`, read-only (`ec2:DescribeVpcs`, `DescribeVpcAttribute`,
`DescribeSubnets`, `DescribeRouteTables`). `FAIL` if the VPC or a subnet does
not exist, a subnet belongs to another VPC, two share an AZ or one has fewer
than 2 free IPs left; `WARN` with a single AZ or without DNS in the VPC. The
summary says what `rayito stack deploy efs-volumes` would create and its idle
cost; it creates nothing. See
[EFS volumes in your VPC](funciones-opcionales/volumenes-efs-vpc.md).

Without `--launch` the doctor **never creates a MicroVM**: checks 8–10 use the
newest `RUNNING` sandbox of the image or end up `SKIP`. With `--launch` it
creates a 300 s one (`idle=None`, `logging="disabled"`,
`metadata={"rayito": "doctor"}`), uses it for 8–10 and kills it in a `finally`
no matter what (also on Ctrl-C); it costs the snapshot read plus a few seconds
of compute ([Pricing](cost.md#cuanto-cuesta-con-ejemplos)) and its id appears
as `launched_sandbox_id`.

Human output: one line per check (`OK   credentials  cuenta …`, "cuenta" being
Spanish for "account"), the details indented under `WARN`/`FAIL`, the
compatibility table and `rayito doctor: N OK, N WARN, N FAIL, N SKIP`. With
`--json`:

```json
{
  "rayito": "0.11.0",
  "region": "us-east-1",
  "account": "123456789012",
  "principal_kind": "assumed-role",
  "checks": [{"name": "credentials", "status": "OK", "summary": "…", "details": {}}],
  "compatibility": [
    {"sdk_series": "0.1", "min_agent_version": "0.1.0", "note": "…"},
    {"sdk_series": "0.2", "min_agent_version": "0.2.0", "note": "…"},
    {"sdk_series": "0.3", "min_agent_version": "0.3.0", "note": "…"},
    {"sdk_series": "0.4", "min_agent_version": "0.4.0", "note": "…"},
    {"sdk_series": "0.5", "min_agent_version": "0.5.0", "note": "…"},
    {"sdk_series": "0.6", "min_agent_version": "0.6.0", "note": "…"},
    {"sdk_series": "0.7", "min_agent_version": "0.7.0", "note": "…"},
    {"sdk_series": "0.8", "min_agent_version": "0.8.0", "note": "…"},
    {"sdk_series": "0.9", "min_agent_version": "0.9.0", "note": "…"},
    {"sdk_series": "0.10", "min_agent_version": "0.10.0", "note": "…"},
    {"sdk_series": "0.11", "min_agent_version": "0.11.0", "note": "…"}
  ],
  "launched_sandbox_id": null,
  "exit_code": 0
}
```

Nothing the CLI prints is a secret: neither the proxy JWE, nor the access
token, nor the `runHookPayload` appear in any output (the tests check it with
canaries).

## Equivalents with `make` { #equivalencias-con-make }

The four scripts in `scripts/` still exist with the same arguments and are
*shims* over the CLI, so the `Makefile` targets do not change:

| `make` | Script | Equivalent |
|---|---|---|
| `make image-zip` | `python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar`, `make image-licenses` and `python scripts/image_zip.py image image/rayito-image.zip` | `rayito image zip image image/rayito-image.zip --sidecar kernel-sidecar` |
| `make image-zip-efs` | `python scripts/image_zip.py image image/rayito-image-efs.zip --with-efs` | `rayito image zip image image/rayito-image-efs.zip --sidecar kernel-sidecar --with-efs` |
| `make image-publish-caps-efs` | `… publish_image.py --artifact image/rayito-image-efs.zip --with-efs --os-capabilities ALL …` | `rayito image publish --artifact image/rayito-image-efs.zip --with-efs --os-capabilities ALL …` |
| `make image-publish` (`-slim`, `-poly`, `-caps`) | `uv run --project clients/python python scripts/publish_image.py --artifact … --bucket $(BUCKET) --base-image-version 1` | `rayito image publish --artifact … --bucket … --base-image-version 1` |
| `make image-publish-caps MOUNT_BUCKETS=b1,b2` (also `-caps-efs`) | `… publish_image.py … --env RAYITO_ALLOWED_MOUNT_BUCKETS=b1,b2` | `rayito image publish … --env RAYITO_ALLOWED_MOUNT_BUCKETS=b1,b2` |
| `make image-prune PRUNE_ARGS="--keep 5 --dry-run"` | `uv run --project clients/python python scripts/image_prune.py --image-name rayito-base --keep 5 --dry-run` | `rayito image prune --keep 5 --dry-run` |

`image_zip.py` and `copy_sidecar.py` only use the standard library (they load
`rayito/cli/_artifact.py` by file path) because CI and the release run them
with a bare `python3` without installing the SDK. `publish_image.py` and
`image_prune.py` need the client environment (`typer` and the SDK) and without
it they print the `uv run --project clients/python …` command and exit with 2.

## What the CLI does not do { #lo-que-la-cli-no-hace }

- `rayito.toml` or any configuration file of its own.
- Building the image beyond the zip: AWS builds the Dockerfile.
- `sandbox logs --follow`: that is what the SDKs and the
  [MCP server](mcp.md) are for.
- `auth`, `snapshots` and `fork` from E2B's CLI: they have no primitive or are
  out by `SPEC.md` §4 ([E2B compatibility](e2b-compat.md)).
- Wrapping the development tools (`bench_cold_start.py`, `hooks-sim.py`,
  `gen_limits.py`, `check_*.py`).
- Shell completion.
