# Set up AWS

Before the first sandbox, your account needs permissions (an IAM stack), a
bucket for the image artifacts and the `rayito-base` image published. There
are six steps and almost all of them are done **once per account and
region**, in about 15 minutes (most of it is waiting for AWS to build the
image).

| Step | Once per | Time | Cost |
|---|---|---|---|
| 1. Credentials and region | machine | 1 min | — |
| 2. A bucket | account and region | 1 min | cents |
| 3. The `rayito-m0-iam` IAM stack | account and region | 2 min | free |
| 4. Publish `rayito-base` | account, region and `rayd` version | ≈ 10 min | ≈ $0.037/week per version |
| 5. `RAYITO_TEMPLATE` | your app's environment | — | — |
| 6. `rayito doctor --launch` | whenever you want | 1 min | ≈ $0.002 |

After that, each application only installs the SDK (`pip install rayito` or
`pnpm add rayito`) and writes its code; there is no Rayito server to deploy.

!!! info "Everything stays in your account"
    Rayito has no server and no API key. What you create here are your own
    resources: a CloudFormation stack with roles and a policy, an S3 bucket
    and a Lambda MicroVMs image. Deleting the stack and the image leaves
    everything as it was.

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

These preparation steps need administrator permissions (creating IAM roles
and a bucket). To use the SDK afterwards, the minimal policy created by
step 3 is enough.

## 2. A bucket for the artifacts { #2-un-bucket-para-los-artefactos }

The image zip is uploaded to a bucket of yours, under the `rayito/` prefix.
Create it in the **same region** as the sandboxes:

```bash
export RAYITO_BUCKET=amzn-s3-demo-bucket
aws s3 mb "s3://$RAYITO_BUCKET" --region "$AWS_REGION"
```

Replace `amzn-s3-demo-bucket` with a unique name of your own. `RAYITO_BUCKET`
saves you from repeating `--bucket` in the CLI.

## 3. The IAM stack { #3-la-pila-de-iam }

`infra/iam.yaml` creates the role AWS uses to build the image, the optional
sandbox execution role and three minimal policies for whoever uses the SDK:
`SandboxLauncherPolicy` (`rayito-m0-launcher-<region>`, launch and manage
sandboxes only), `ImagePublisherPolicy` (`rayito-m0-publisher-<region>`,
publish images only) and `CallerPolicy` (`rayito-m0-caller-<region>`, both
together). Download the template from the repository and deploy it:

```bash
curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/iam.yaml
aws cloudformation deploy --stack-name rayito-m0-iam \
  --template-file iam.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ArtifactBucket="$RAYITO_BUCKET" LogGroupPrefix=/rayito
```

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

## 4. Publish the `rayito-base` image { #4-publicar-la-imagen-rayito-base }

Each sandbox starts from a Lambda MicroVMs image published in your account.
The shortest way is to publish the signed `rayito-image.zip` of the latest
release, without building anything:

=== "From the release (recommended)"

    ```bash
    RAYD_VERSION=0.12.0     # the same version as your SDK: python -c "import rayito; print(rayito.__version__)"  (TypeScript: npm ls rayito)
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

## 5. Point the SDK at the image { #5-apunta-el-sdk-a-la-imagen }

```bash
export RAYITO_TEMPLATE=rayito-base       # name or ARN of the image
```

With `RAYITO_TEMPLATE` you do not need to pass `template=` on every
`create()`. A name is resolved to the ARN in your account and region.

## 6. Diagnosis { #6-diagnostico }

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

## Next step { #siguiente-paso }

[First sandbox](../quickstart.md): commands, files and code in Python and
TypeScript.

??? info "Sources and measurements"
    - IAM template: [`infra/iam.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/iam.yaml)
      and [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Release assets and their verification: [Verify a release](../verify.md).
    - Regions: `SUPPORTED_REGIONS` in
      [`clients/python/src/rayito/_limits.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_limits.py).
