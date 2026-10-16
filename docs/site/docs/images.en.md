# Images and IAM

Every sandbox starts from a Lambda MicroVMs image published **in your
account** from `image/Dockerfile`. The SDK needs an image with a `rayd` of
its same series (`MAJOR.MINOR`, see
[Compatibility](limits.md#compatibilidad-sdk-rayd-imagen)) and, for two
families of features, a specific variant. `rayito doctor` checks the version
of the image's agent ([CLI](cli.md#rayito-doctor)).

<a id="que-imagen-necesita-cada-feature"></a>

## Which image each feature needs { #que-imagen-necesita-cada-funcion }

The three variants come from the same `Dockerfile`: `rayito-base-caps` is the
same zip as `rayito-base` published with `additionalOsCapabilities ALL`, and
`rayito-base-poly` adds a layer with `bash_kernel` and Deno 2.9.7. There are
also two optional images that you only publish if you use their feature:
`rayito-base-caps-efs` (`rayito-base-caps` plus `amazon-efs-utils`, for
`volumes=`) and the agent image built by `AgentTemplate` (`rayito-agent`, on
top of `rayito-base-caps`).

| Feature | `rayito-base` | `rayito-base-caps` | `rayito-base-poly` |
|---|---|---|---|
| `commands`, `files`, `pty`, `run_code` in Python, metadata, pool, `persist=` | yes | yes | yes |
| Server-side deadline: `max_lifetime`, `on_timeout`, `set_timeout`, `connect(timeout=)` ([Deadline](lifecycle.md)) | yes (0.3.0) | yes (0.3.0) | yes (0.3.0) |
| `upload_url`/`download_url`, large files over S3, `gzip`, `metadata` ([Files](files.md)) | yes (0.3.0) | yes (0.3.0) | yes (0.3.0) |
| `get_metrics_history()`, guest facts ([Observability](observability.md)) | yes (0.3.0) | yes (0.3.0) | yes (0.3.0) |
| `sbx.git` (`git-core` in the image, [Git](git.md)) | yes (0.3.0) | yes (0.3.0) | yes (0.3.0) |
| `rayito.e2b` / `rayito/e2b` shim ([Compatibility](e2b-compat.md)) | yes (0.3.0) | yes (0.3.0) | yes (0.3.0) |
| `run_code(language="bash")` | no: `UNIMPLEMENTED` | no | yes |
| `run_code(language="javascript")` or `"typescript"` ([Kernels](kernels.md)) | no: `UNIMPLEMENTED` | no | yes (0.3.0) |
| Egress policy: `network=`, `allow_internet_access=False`, `update_network()` ([Outbound network](network.md)) | no: the SDK terminates the VM and raises `UnimplementedError` | yes (0.3.0) | no: same as `rayito-base` |
| IMDS blocked for uid 1000–65535 (`get_health().imds_blocked`) | no | yes | no |
| `mounts=` ([S3 mounts](funciones-opcionales/montajes-s3.md)) | no: `UnimplementedError` | yes (0.6.0), with the bucket in `RAYITO_ALLOWED_MOUNT_BUCKETS` | no: same as `rayito-base` |
| `events=` ([Events and webhooks](funciones-opcionales/eventos-y-webhooks.md)), `gateways=` ([Secret gateway](funciones-opcionales/pasarela-de-secretos.md)) | yes (0.6.0) | yes (0.6.0) | yes (0.6.0) |
| `telemetry=` ([OTLP export](funciones-opcionales/exportacion-otlp.md)) | yes (0.6.0) with `OtlpAuth.bearer(...)` | yes (0.6.0), also with `OtlpAuth.execution_role()` | yes (0.6.0) with `OtlpAuth.bearer(...)` |
| `Template.set_start_cmd()` ([Templates](funciones-opcionales/templates.md)) as a base image | yes (0.6.0) | yes (0.6.0) | yes (0.6.0) |
| Reaping orphan zombies ([What's new in 0.6.1](novedades/0.6.1.md#rayd-recoge-los-procesos-zombi)) | yes (0.6.1) | yes (0.6.1) | yes (0.6.1) |
| `volumes=` ([EFS volumes](funciones-opcionales/volumenes-efs.md), experimental) | no: `UnimplementedError` | no: only the optional `rayito-base-caps-efs` variant (0.7.0, `rayito image publish --with-efs`) | no: `UnimplementedError` |
| Agent hardening: isolated hooks, persistence bound to `/run` ([What's new in 0.7.0](novedades/0.7.0.md#endurecimiento-de-seguridad)) | yes (0.7.0) | yes (0.7.0) | yes (0.7.0) |
| `sbx.agent` ([Agent in the sandbox](guias/agente-en-el-sandbox.md)) | no: fails with `reason="runtime_missing"` | no: same, unless you install the runtime yourself | no: same |

`size=` ([Sizes](funciones-opcionales/tamanos.md)) is not an image feature
but one more image per size: `rayito image publish --sizes` publishes
`rayito-base-4gb` (or `rayito-base-caps-4gb`) from the same artifact as the
variant.

"yes (0.3.0)" means you need an image with `rayd` 0.3.0 or later
(`agent_version`); against an earlier one, each feature fails closed with
`UnimplementedError` (`LifecycleUnsupportedException` for the server-side
deadline, a subclass of it since 0.4.0; the rest already were).
There is no variant that combines `-caps` and `-poly`: no `make` target
publishes it and the combination has not been measured.

## How the SDK recognizes the variant { #como-reconoce-el-sdk-la-variante }

Before launching, the SDK tries to deduce the variant from the image's
**name**, only to reject early (and without paying for the launch) what that
image cannot do. It only recognizes `rayito-<variant>` or
`rayito-<variant>-<size>`, with the variant in the closed catalog `base`,
`base-caps`, `base-caps-efs`, `base-poly` and `base-slim`, and the size among
those in [Sizes](funciones-opcionales/tamanos.md) (`512mb`, `1gb`, `2gb`,
`4gb`, `8gb`). With a known variant:

- `mounts=` and `telemetry=` with the execution role require `base-caps` or
  `base-caps-efs`;
- `volumes=` requires `base-caps-efs`: `base-caps` without `amazon-efs-utils`
  cannot mount EFS.

Any other name (an image of your own, even if it starts with `rayito-`, such
as `rayito-my-app`) or an ARN **is not interpreted**: the SDK launches the
sandbox and decides from what the agent announces in `Health.features`, which
remains the authority. If the image does not have the feature, the sandbox is
terminated (unless `keep_on_failure`) and `UnimplementedError` is raised. So,
an image of your own with caps and EFS layers published with `--image-name`
works without changing anything.

## Publish all three { #publicar-las-tres }

Without compiling anything, `rayito-base` and `rayito-base-caps` are
published from each release's signed `rayito-image.zip`
([Set up AWS](primeros-pasos/configurar-aws.md#4-publicar-la-imagen-rayito-base)).
From source (Linux or WSL2, to compile `rayd`):

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
make image-publish      BUCKET=amzn-s3-demo-bucket    # rayito-base
make image-publish-caps BUCKET=amzn-s3-demo-bucket    # rayito-base-caps (additionalOsCapabilities ALL)
make image-publish-poly BUCKET=amzn-s3-demo-bucket    # rayito-base-poly (bash, JavaScript, TypeScript)
make image-publish-caps-efs BUCKET=amzn-s3-demo-bucket  # optional: rayito-base-caps-efs, for volumes=
```

`BUCKET=` can also be exported as `RAYITO_BUCKET`; with neither, `make` stops
before compiling. `MOUNT_BUCKETS=b1,b2` on the `-caps` targets bakes in the
allowlist for [S3 mounts](funciones-opcionales/montajes-s3.md), and
`PUBLISH_ARGS=` passes extra options to `rayito image publish` (for example
`--force` or `--sizes 4gb`). The agent image does not come from `make`: it is
built with `rayito agent template build` on top of an already published
`rayito-base-caps` ([Agent templates](funciones-opcionales/templates-de-agente.md)).

Or with the CLI (`rayito image publish`, [CLI](cli.md#image-publish)); the
bucket is the one for image artifacts (prefix `rayito/`), not the transfer
one. Each new version costs ≈ $0.04/week of storage ([Costs](cost.md)).

Size (CPU/RAM) is also a property of the image, with `--memory-mib`
(`resources[0].minimumMemoryInMiB`, default 2048): to have sandboxes of
several sizes, publish one image per size with a name that says so
(`--image-name myimg-4gb --memory-mib 4096`) and choose the image when
creating the sandbox. Details, the size table (from AWS's documentation, not
measured except where stated) and the `$/h` per size are in
[Limits](limits.md#tamano-cpuram).

=== "Python"

    ```python
    from rayito import Sandbox

    base = Sandbox.create("rayito-base")                          # or RAYITO_TEMPLATE
    caps = Sandbox.create("rayito-base-caps", allow_internet_access=False)
    poly = Sandbox.create("rayito-base-poly")
    print(poly.run_code("1 + 1", language="typescript").text)
    for sbx in (base, caps, poly):
        sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const base = await Sandbox.create({ template: "rayito-base" }); // or RAYITO_TEMPLATE
    const caps = await Sandbox.create({ template: "rayito-base-caps", allowInternetAccess: false });
    const poly = await Sandbox.create({ template: "rayito-base-poly" });
    console.log((await poly.runCode("1 + 1", { language: "typescript" })).text);
    for (const sbx of [base, caps, poly]) {
      await sbx.kill();
    }
    ```

## Caller IAM { #iam-del-llamante }

The SDK runs with **your** credentials (the boto3 / AWS SDK v3 chain).
`infra/iam.yaml` ships `SandboxLauncherPolicy` (launch and manage sandboxes),
`ImagePublisherPolicy` (publish images) and `CallerPolicy`, the union of the
two. The full table, feature by feature and with the optional secrets and
index policies, is in [IAM](operacion/iam.md). What each feature on this page
adds:

| Feature | New permissions |
|---|---|
| Server-side deadline (`kill` and `pause`) | none: with `kill`, `rayd` exits and the VM ends on its own; with `pause`, the SDK uses `suspend-microvm`, which was already there |
| Class forms (`Sandbox.set_timeout(id)`, `get_metrics_history(id)`, `update_network(id)`) | none: `get-microvm` and `create-microvm-auth-token`, which were already there, plus the sandbox's access token |
| Egress policy | none on the caller; the `rayito-base-caps` image is published with `--os-capabilities ALL` |
| Metrics history, listing, git, JS/TS kernels | none |
| Transfers (`upload_url`, `download_url`, large files) | `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` on `<bucket>/<prefix>/*` and `s3:ListBucket` scoped to the prefix |

With the template, two parameters add them (empty = no transfer permissions):

```bash
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file infra/iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --profile <your-profile> \
  --parameter-overrides ArtifactBucket=amzn-s3-demo-bucket LogGroupPrefix=/rayito \
      TransferBucket=amzn-s3-demo-bucket TransferPrefix=rayito-transfer
```

Without the template, the equivalent to add to your policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TransferObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::amzn-s3-demo-bucket/rayito-transfer/*"
    },
    {
      "Sid": "TransferMissingKeyIs404",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::amzn-s3-demo-bucket",
      "Condition": { "StringLike": { "s3:prefix": "rayito-transfer/*" } }
    }
  ]
}
```

- `s3:PutObject` also covers `CreateMultipartUpload`, `UploadPart` and
  `CompleteMultipartUpload` (exports from 5 GiB).
- Without `s3:ListBucket`, a missing key gives 403 instead of 404 and the
  import treats it as pending until it expires.
- With SSE-KMS, add `kms:GenerateDataKey` (uploads) and `kms:Decrypt`
  (downloads) on the key; the template does not include it.
- The sandbox's execution role needs **nothing**: `rayd` never signs or
  stores credentials for transfers (see [Security](security.md)).

## The transfer bucket { #el-bucket-de-transferencias }

Full recipe in `infra/README.md`, "File transfers". The essentials:

- **Same region** as the sandboxes (or `S3Staging(region=)`), name without
  dots: the SDK signs with the regional virtual host
  (`amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com`) and `rayd` rejects any
  other form.
- **A prefix of its own** (`rayito-transfer` by default): never `rayito` (the
  image artifacts) nor the persistence one.
- **1-day lifecycle** on the prefix, with `AbortIncompleteMultipartUpload`:
  the objects are single-use.
- **Bucket policy** that rejects anything that is not SigV4
  (`s3:signatureversion`) and anything that does not go over TLS
  (`aws:SecureTransport`).
- **CORS** only if a browser uploads or downloads directly, with explicit
  origins.
- **Network**: with `INTERNET_EGRESS` there is nothing to do; with your own
  VPC connector (`infra/egress-connector.yaml`) the VM needs to reach S3
  (gateway endpoint or NAT). The guest's egress policy does not affect `rayd`
  (root), which is the one moving the bytes.

Configure it in the SDK with `transfer=S3Staging("amzn-s3-demo-bucket")` (TS
`transfer: { bucket: "amzn-s3-demo-bucket" }`) or with the variables
`RAYITO_TRANSFER_BUCKET`, `RAYITO_TRANSFER_PREFIX` and `RAYITO_TRANSFER_REGION`
(the only route in the E2B shim).
