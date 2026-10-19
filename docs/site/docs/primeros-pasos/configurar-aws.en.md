---
description: "Prepare your AWS account for Rayito once with rayito setup: the IAM stack, the rayito-base image and the bucket (reused or created), with rayito doctor."
---

# Set up AWS

Before the first sandbox, your account needs permissions (an IAM stack), a
bucket for the image artifacts and the `rayito-base` image published.
`rayito setup` prepares all of it in one idempotent command, like `cdk
bootstrap`: **once per account and region**. The first run takes about 10–12
minutes, almost all of it waiting for AWS to build the image snapshot; later
runs, which have nothing to create, finish in seconds.

| Step | Once per | Time | Cost |
|---|---|---|---|
| 1. Credentials and region | machine | 1 min | — |
| 2. `rayito setup` | account and region | 10–12 min the first time; seconds after | IAM stack free; ≈ $0.04/week per image version; zip in the bucket, cents |
| 3. Assign the IAM policies | account and region | 2 min | free |
| 4. `RAYITO_TEMPLATE` | your app's environment | — | — |
| 5. `rayito doctor --launch` | whenever you want | 1 min | ≈ $0.002 |

Prefer each step by hand (bucket, stack, cosign-verified download,
`rayito image publish`)? It is still possible: [By hand, without `rayito setup`](#a-mano-sin-rayito-setup).

After that, each application only installs the SDK (`pip install rayito` or
`pnpm add rayito`) and writes its code; there is no Rayito server to deploy.

!!! info "Everything stays in your account"
    Rayito has no server and no API key. What is created here are your own
    resources: a CloudFormation stack with roles and policies and a Lambda
    MicroVMs image, plus a zip in an S3 bucket (yours, the `cdk bootstrap` one or,
    if there is none, one that `rayito setup` creates). Deleting the stacks and
    the image leaves everything as it was, except the created bucket, which is
    kept until you delete it yourself.

## 1. Credentials and region { #1-credenciales-y-region }

Pick one of the ten regions with Lambda MicroVMs (`us-east-1`,
`us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`,
`ap-northeast-1`, `ap-south-1`, `ap-southeast-1`, `ap-southeast-2`) and
export your profile:

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
aws sts get-caller-identity          # check that the credentials work
```

`AWS_REGION` is enough: both SDKs and the CLI read it. If it is missing,
they use `AWS_DEFAULT_REGION` and then the profile's region; `region=`
(Python) or `region` (TypeScript) wins over all of them.

These preparation steps need administrator or *bootstrap* credentials, **once**
and as with `cdk bootstrap`: CloudFormation, IAM (the stack creates roles and
policies), S3 when `rayito setup` has to create the bucket, and publishing
images. The `CallerPolicy` of step 3 is not enough for this: it is the minimum
for *using* the SDK afterwards, not for preparing it. You do not need to create
a bucket first: if you already have one, `rayito setup` reuses it, and if not,
it creates one.

## 2. Prepare the account with `rayito setup` { #2-prepara-la-cuenta-con-rayito-setup }

`rayito setup` looks at your account and region, shows the plan and the cost,
asks for confirmation and creates only what is missing. It handles three
resources, always in this order:

- **The artifacts bucket.** It reuses the first that exists of: `--bucket`,
  the `RAYITO_BUCKET` variable, the bucket of the IAM stack if you already have
  it, or the `cdk bootstrap` bucket of that account and region. **Only when
  there is none does it create one**, with the `rayito-m0-artifacts` stack
  (`infra/artifact-bucket.yaml`) and the name
  `rayito-artifacts-<account>-<region>`. A `--bucket` or `RAYITO_BUCKET` that
  does not exist is also created, with that name. The created bucket is
  encrypted (SSE-S3), private (all public access blocked, no ACLs), accepts TLS
  only and deletes the image zips after 7 days; deleting its stack keeps it.
- **The `rayito-m0-iam` IAM stack**, from the `infra/iam.yaml` template (roles
  and the three policies of [step 3](#3-la-pila-de-iam)). If it already exists
  and points at the chosen bucket, it leaves it alone.
- **The `rayito-base` image.** It downloads `rayito-image.zip` and `SHA256SUMS`
  from the `rayd-v<your SDK version>` release, checks the sha256 and publishes
  it. If a launchable version already exists it builds no other; if a build is
  in progress it waits for it.

Start with `--dry-run`: it only reads your account and creates or changes
nothing.

=== "Python"

    ```bash
    rayito setup --dry-run
    ```

=== "TypeScript"

    ```bash
    npx rayito setup --dry-run
    ```

On a new account with no bucket at all, the plan is (the CLI prints Spanish in
both SDKs; if you already have a bucket, the first line says `reutilizar`
(reuse) and that bucket's name, and the bucket cost line does not appear):

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<account>-<region>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.15.0 (≈ 10 min)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
no se creó ni cambió nada (--dry-run)
```

When the plan looks right, run it without `--dry-run`. It asks for
confirmation (`¿Preparar la cuenta? [y/N]:`); without a terminal, or with
`--json`, `--yes` is required.

=== "Python"

    ```bash
    rayito setup
    ```

=== "TypeScript"

    ```bash
    npx rayito setup
    ```

It prints the same plan and asks for confirmation; then the progress lines of
each step, the output of [`rayito doctor`](#5-diagnostico) and, at the end,
what your application needs. On the account with no bucket above:

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<account>-<region>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.15.0 (≈ 10 min)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
¿Preparar la cuenta? [y/N]: y
creando el bucket rayito-artifacts-<account>-<region> con la pila rayito-m0-artifacts
bucket rayito-artifacts-<account>-<region> listo (pila rayito-m0-artifacts)
creando la pila de IAM rayito-m0-iam
pila de IAM rayito-m0-iam lista
descargando SHA256SUMS de rayd-v0.15.0
descargando rayito-image.zip de rayd-v0.15.0
sha256 de rayito-image.zip verificado contra SHA256SUMS: <sha256>
base_image_version=<version>
uploaded s3://rayito-artifacts-<account>-<region>/rayito/images/rayd-<sha12>.zip (<bytes> bytes)
create-microvm-image accepted: version <version>
  image=<state> version=<version> state=<state> status=<state> (<s>s)
  ...                                (one line per poll until the version is launchable)
imagen rayito-base <versión> lanzable
...                                  (la salida de rayito doctor)
rayito setup: cuenta preparada en us-east-1
RAYITO_TEMPLATE=arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base
RAYITO_BUCKET=rayito-artifacts-<account>-<region>
```

The `uploaded`, `create-microvm-image` and `image=` lines come from the same
pipeline as `rayito image publish`, which is why they are in English. On an
account that already has a bucket, the first step is `reutilizar` and there are
no bucket lines; on an account that already has the image, there is no download
or build.

If `rayito doctor` finds any `FAIL`, instead of the last three lines you get
`rayito setup: rayito doctor encontró algún FAIL; la cuenta no está lista`
and the command exits with code 1. You can repeat `rayito setup` as often as
you like: on an already prepared account it observes three reusable resources
(`La cuenta ya está preparada: no hay nada que crear.`), writes nothing and
finishes in seconds. If
a run is cut short, the next one picks up whatever was left in progress (the
stack or the build) without starting a second build.

!!! warning "What the hash checks and what it does not"
    `rayito setup` downloads the zip from the GitHub release and checks its
    sha256 against the `SHA256SUMS` of that same release. That catches a
    truncated, corrupted or wrong file, but it **does not prove it is
    authentic**: the zip and `SHA256SUMS` come from the same place. The
    signature is checked with `cosign verify-blob`; if you want it, verify the
    zip by hand and pass it with `rayito setup --artifact rayito-image.zip`
    (see [By hand, without `rayito setup`](#a-mano-sin-rayito-setup) and
    [Verify a release](../verify.md)).

!!! info "Cost and activation"
    - **Activates:** running `rayito setup` (and confirming). `rayito setup
      --dry-run` only reads the account and creates nothing.
    - **AWS resources and calls:** if there is no bucket, the
      `rayito-m0-artifacts` CloudFormation stack (`CreateStack` without
      capabilities; an S3 bucket and its policy); the `rayito-m0-iam` stack
      (`CreateStack` or `UpdateStack` with `CAPABILITY_NAMED_IAM`; roles and
      three managed policies); the `rayito-base` image (one object in the
      artifacts bucket, `CreateMicrovmImage` or `UpdateMicrovmImage` and its
      build); reads of `GetCallerIdentity`, `DescribeStacks`, `HeadBucket`,
      `GetMicrovmImage` and `ListMicrovmImageVersions`; the HTTPS download of
      two files of the GitHub release. The SDK does not call `CreateBucket`:
      CloudFormation creates the bucket with your credentials.
    - **Approximate cost:** the IAM stack is free; the bucket costs nothing to
      exist and its zips expire after 7 days; each published version of the
      image ≈ $0.04 per week of storage (minimum one week); the zip in the
      bucket, cents. See [Pricing](../cost.md).
    - **IAM:** administrator or *bootstrap* credentials, once:
      `cloudformation:CreateStack` and stack reads, creating IAM roles and
      policies (`iam:CreateRole`, `iam:CreatePolicy`), publishing images and,
      only when it creates the bucket, the S3 permissions CloudFormation uses to
      create and protect it (`s3:CreateBucket`,
      `s3:PutEncryptionConfiguration`, `s3:PutBucketPublicAccessBlock`,
      `s3:PutBucketOwnershipControls`, `s3:PutLifecycleConfiguration`,
      `s3:PutBucketTagging`, `s3:GetBucketPolicy` and `s3:PutBucketPolicy`) and
      `s3:ListBucket`. To use the SDK afterwards the minimal policy of step 3 is
      enough.
    - **How to turn it off:** do not run `rayito setup`. To undo what it
      created, delete the `rayito-m0-iam` stack and, if it exists,
      `rayito-m0-artifacts`, and the image versions you published. The created
      bucket is kept when its stack is deleted: empty and delete it by hand if
      you no longer want it.
    - **Example:** `rayito setup --dry-run`.

## 3. Assign the IAM policies { #3-la-pila-de-iam }

The `rayito-m0-iam` stack (the one `rayito setup` creates or the one you deployed
by hand) brings the role AWS uses to build the image, the optional sandbox
execution role and three minimal policies for whoever uses the SDK:
`SandboxLauncherPolicy` (`rayito-m0-launcher-<region>`, launch and manage
sandboxes only), `ImagePublisherPolicy` (`rayito-m0-publisher-<region>`,
publish images only) and `CallerPolicy` (`rayito-m0-caller-<region>`, both
together).

Assign each identity only what it does:

- **Your development user** (publishes the image and tries sandboxes): the
  `CallerPolicyArn` output.
- **Your service's or agent's role** (only creates sandboxes): the
  `SandboxLauncherPolicyArn` output. Never `CallerPolicy`: a compromised
  service could publish a backdoored version of the image that all your
  sandboxes will use.
- **Your CI or release pipeline** (publishes images): the
  `ImagePublisherPolicyArn` output (and the launcher one if it also runs
  tests).

```bash
policy_arn() {
  aws cloudformation describe-stacks --stack-name rayito-m0-iam \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# your IAM user:
aws iam attach-user-policy --user-name <your-user> --policy-arn "$(policy_arn CallerPolicyArn)"
# your service's role:
aws iam attach-role-policy --role-name <your-role> --policy-arn "$(policy_arn SandboxLauncherPolicyArn)"
```

If you already have administrator permissions, you can skip assigning it to
your user; always do it for your service's role.

With IAM Identity Center (SSO) the policy is not assigned to a user: add it
to your access's *permission set* from the Identity Center console.

Optional template parameters: `TransferBucket` and `TransferPrefix` for
[large files and S3 URLs](../files.md), and `PersistenceBucket` and
`PersistencePrefix` for [persistence](../persistence.md). Which permission
each feature needs: [IAM](../operacion/iam.md).

`rayito setup` only sets `ArtifactBucket`; every other parameter stays as it
is. If you later deploy the stack by hand with those optional parameters, a
later `rayito setup` keeps them.

## 4. Point the SDK at the image { #4-apunta-el-sdk-a-la-imagen }

```bash
export RAYITO_TEMPLATE=rayito-base       # name or ARN of the image
```

With `RAYITO_TEMPLATE` you do not need to pass `template=` on every
`create()`. A name is resolved to the ARN in your account and region.

## 5. Diagnosis { #5-diagnostico }

`rayito doctor` runs ten account checks (credentials, managed images, quotas,
IAM, bucket, image, live sandboxes, token, agent and SDK ↔ `rayd`
compatibility) and tells you what is missing. Without `--launch`, the last
three only run if there is already a `RUNNING` sandbox of that image; with
`--launch` it creates an ephemeral 300 s one and kills it when done:

```bash
rayito doctor --template rayito-base --launch     # --launch: an ephemeral sandbox (≈ $0.002)
```

A typical output on a freshly prepared account (with SDK 0.11.0). The CLI
prints its messages in Spanish, so the output is shown as it appears:

<!-- noqa: example: salida de consola, no es código -->
```text
OK   credentials     cuenta 123456789012, us-east-1, assumed-role
OK   managed-images  al2023-1 disponible, versión más nueva 1.0
OK   quotas          18 cuotas de MicroVM, ninguna por debajo del default
WARN iam-simulation  sin permiso explícito para lambda:PassNetworkConnector; simulación orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan
       policy_source_arn: arn:aws:iam::123456789012:role/<tu-rol>
       explicit_deny: []
       denied_by_organizations: []
       implicit_deny: ['lambda:PassNetworkConnector']
OK   bucket          s3://amzn-s3-demo-bucket accesible en us-east-1
OK   image-gate      rayito-base 1.0 lanzable
OK   sandboxes       ningún MicroVM RUNNING de rayito-base
OK   token           token acuñado por create() para microvm-00000000-0000-0000-0000-000000000001
OK   agent           rayd 0.11.0 agent_ready, kernel_ready=True, imds_blocked=False
OK   compatibility   SDK 0.11.0, agent_version 0.11.0; imagen 1.0 (contador de builds, informativo)

SDK   rayd mínimo  Estado  Nota
0.1   0.1.0                M6: imds_blocked, hook_anomalies y metadata exigen el rayd del tag rayd-v0.1.0
0.2   0.2.0                M7: Checkpoint/Restore (persist=) y language= exigen el rayd del tag rayd-v0.2.0
0.3   0.3.0                M9: max_lifetime/on_timeout, set_timeout, get_metrics_history, network= y los kernels Deno
                           exigen el rayd del tag rayd-v0.3.0
0.4   0.4.0                0.4: UnimplementedError único, SetTimeout validado en el dominio y mensajes del agente en
                           español exigen el rayd del tag rayd-v0.4.0
0.5   0.5.0                0.5: /suspend con sync acotado por sistema de ficheros y las funciones opcionales (secretos,
                           índice, OTel) se validan con el rayd del tag rayd-v0.5.0
0.6   0.6.0                0.6: ConfigureSandbox y las funciones 0.6 (montajes S3, eventos, OTLP, pasarela de secretos,
                           start/ready de templates) exigen el rayd del tag rayd-v0.6.0
0.7   0.7.0                0.7: los volúmenes EFS (create(volumes=)) y el endurecimiento del agente (hooks aislados,
                           persistencia ligada al /run) exigen el rayd del tag rayd-v0.7.0
0.8   0.8.0                0.8: el agente de IA (sbx.agent, AgentTemplate y el warmup de los pools) se valida con el
                           rayd del tag rayd-v0.8.0
0.9   0.9.0                0.9: connect() recupera sbx.gateways desde ConfigureStatus y el arranque rápido del agente
                           sin la opción D se validan con el rayd del tag rayd-v0.9.0
0.10  0.10.0               0.10: el timeout y abort() del agente paran todo el árbol de procesos (kill_tree) y los
                           proveedores del agente se validan con el rayd del tag rayd-v0.10.0
0.11  0.11.0       OK      0.11: el arranque rápido de AgentTemplate (kernel_warmup=False por defecto) y las claves de
                           artefacto por ejecución se validan con el rayd del tag rayd-v0.11.0

rayito doctor: 9 OK, 1 WARN, 0 FAIL, 0 SKIP
sandbox de --launch terminado: microvm-00000000-0000-0000-0000-000000000001
```

Each line is `OK`, `WARN`, `FAIL` or `SKIP`; a `WARN` or `FAIL` lists its
details below. After that comes the SDK ↔ `rayd` compatibility table, with
the status on your SDK's row, and the tally. The command exits with code 1
only if there is a `FAIL`. `--json` gives the same as a JSON document.

The `iam-simulation` `WARN` about `lambda:PassNetworkConnector` is expected:
the IAM simulator flags it even with administrator permissions and it
blocks nothing (the checks that count are managed images, bucket, image and
token). What to do about each `FAIL`:
[Troubleshooting](../operacion/solucion-de-problemas.md#rayito-doctor).

## By hand, without `rayito setup` { #a-mano-sin-rayito-setup }

The steps `rayito setup` chains, one by one. Use them if you prefer to control
each step or if you want to verify the signature with cosign before publishing
(afterwards, `rayito setup --artifact rayito-image.zip` publishes that verified
zip instead of downloading the release one).

??? example "Bucket, IAM stack and image publication, step by step"
    **A bucket for the artifacts**

    The image zip is uploaded to a bucket of yours, under the `rayito/` prefix.
    Create it in the **same region** as the sandboxes:

    ```bash
    export RAYITO_BUCKET=amzn-s3-demo-bucket
    aws s3 mb "s3://$RAYITO_BUCKET" --region "$AWS_REGION"
    ```

    Replace `amzn-s3-demo-bucket` with a unique name of your own. `RAYITO_BUCKET`
    saves you from repeating `--bucket` in the CLI.

    **The IAM stack**

    Download the template from the repository and deploy it (it is the same stack
    `rayito setup` creates; then continue at [step 3](#3-la-pila-de-iam)):

    ```bash
    curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/iam.yaml
    aws cloudformation deploy --stack-name rayito-m0-iam \
      --template-file iam.yaml --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides ArtifactBucket="$RAYITO_BUCKET" LogGroupPrefix=/rayito
    ```

    **Publish the `rayito-base` image**

    Each sandbox starts from a Lambda MicroVMs image published in your account.
    The shortest way is to publish the signed `rayito-image.zip` of the latest
    release, without building anything:

    === "From the release (recommended)"

        ```bash
        RAYD_VERSION=0.14.0     # the same version as your SDK: python -c "import rayito; print(rayito.__version__)"  (TypeScript: npm ls rayito)
        BASE=https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v${RAYD_VERSION}
        curl -fsSLO "$BASE/rayito-image.zip"
        curl -fsSLO "$BASE/rayito-image.zip.sigstore.json"    # its Sigstore signature
        curl -fsSLO "$BASE/SHA256SUMS"

        cosign verify-blob --bundle rayito-image.zip.sigstore.json \
          --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
          --certificate-oidc-issuer https://token.actions.githubusercontent.com \
          rayito-image.zip
        sha256sum -c --ignore-missing SHA256SUMS   # on macOS: shasum -a 256 -c --ignore-missing SHA256SUMS

        rayito image publish --artifact rayito-image.zip --base-image-version 1
        ```

        Check the signature **before** publishing: `rayito image publish` does
        not verify anything, and a release zip could be replaced with a
        repository token. If `cosign verify-blob` does not say `Verified OK`, do
        not publish that file. You need [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
        ≥ 2.x; what the signature proves and how to verify the standalone `rayd`
        binary are in [Verify a release](../verify.md).

        Use the `rayd` of the same version as the installed SDK: if the image is
        older, the `compatibility` check of `rayito doctor` flags it. All
        versions are in the
        [`rayd-v*` releases](https://github.com/alejandro-cedeno-10/rayito/releases).

        With the GitHub CLI (`gh`, authenticated with `gh auth login`) the
        download is one line:
        `gh release download "rayd-v${RAYD_VERSION}" --repo alejandro-cedeno-10/rayito --pattern 'rayito-image.zip*' --pattern SHA256SUMS`.

    === "From source"

        ```bash
        git clone https://github.com/alejandro-cedeno-10/rayito.git && cd rayito
        make image-publish BUCKET="$RAYITO_BUCKET"
        ```

        It builds `rayd` for `aarch64-unknown-linux-musl`: you need Linux or WSL2
        (on macOS, a Linux VM) with Rust and `cargo-zigbuild`, as
        [`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md)
        explains.

    `rayito image publish` uploads the zip, asks AWS to build the image and
    waits until the version is launchable (about 10 minutes). The last line is
    `RAYITO_TEMPLATE=<arn>`. Each published version costs ≈ $0.04 per week of
    storage ([Pricing](../cost.md#componentes-del-precio)).

    `--base-image-version` is the version of the AWS managed base the image is built
    on. There is no need to look it up by hand: with `--base-image-version latest`,
    `rayito image publish` uses the newest one AWS lists and prints it
    (`base_image_version=1`); an explicit number (`--base-image-version 1`) pins a
    reproducible build.

    ??? example "The other variants: `-caps`, `-poly` and the agent image"
        - `rayito-base-caps` is the same zip published with
          `additionalOsCapabilities ALL`; it is required for the
          [outbound network policy](../network.md) and is the base of the agent
          image:

            ```bash
            rayito image publish --artifact rayito-image.zip --base-image-version 1 \
              --image-name rayito-base-caps --os-capabilities ALL
            ```

        - `rayito-base-poly` adds the bash, JavaScript and TypeScript kernels
          ([Languages and kernels](../kernels.md)). It is built from source:
          `make image-publish-poly BUCKET=amzn-s3-demo-bucket`.

        - `rayito-agent`, for the [agent in the sandbox](../guias/agente-en-el-sandbox.md),
          is built in your account on top of `rayito-base-caps` (publish it
          first) with OpenCode, ripgrep and deepagents
          ([Agent templates](../funciones-opcionales/templates-de-agente.md)):

            ```bash
            rayito agent template build --bucket amzn-s3-demo-bucket
            ```

        Which image each feature needs: [Images](../images.md).


## Next step { #siguiente-paso }

[First sandbox](../quickstart.md): commands, files and code in Python and
TypeScript.

??? info "Sources and measurements"
    - IAM template: [`infra/iam.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/iam.yaml)
      and [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Release assets and their verification: [Verify a release](../verify.md).
    - Regions: `SUPPORTED_REGIONS` in
      [`clients/python/src/rayito/_limits.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_limits.py).
