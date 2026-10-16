# Optional features and their cost

Rayito never charges by surprise. With no options, a sandbox costs only the
MicroVM itself: the SDK creates no other AWS resource and makes no
paid calls. Each feature on this page is **off by default** and is
turned on with an **explicit SDK option**, never with an environment
variable or a configuration file. What they need (a table, some IAM
policies) you deploy yourself in your account with a CloudFormation
template; Rayito hosts no server.

## At a glance { #de-un-vistazo }

| Feature | Default | What it turns on | Cost | IAM | How to turn it off |
|---|---|---|---|---|---|
| [Secret injection](secrets.md) | off | `secrets=` / `secrets`: the value of a Secrets Manager secret reaches a command, terminal or cell as an environment variable | $0.40 per secret per month + $0.05 per 10,000 reads (one per secret every 5 min with the default cache) | read the `rayito/*` secrets (`RayitoSecretsReader`, `secrets-access` stack) | do not pass `secrets=` |
| [Secret management (CRUD)](secrets.md) | off | `SecretStore(...)`: create, list, update and delete secrets (also the E2B shim's `Secret`) | $0.40 per secret per month **until you delete it** + $0.05 per 10,000 calls | administer the `rayito/*` secrets (`RayitoSecretsAdmin`, `secrets-access` stack) | do not instantiate `SecretStore` and `destroy()` the secrets created |
| [Metadata index](funciones-opcionales/indice-de-metadatos.md) | off | `index=DynamoDbIndex(...)`: filter `list()` by metadata also over paused sandboxes | ≈ $0.03/month for 10,000 sandboxes; empty table, $0 | write and read on your table (`RayitoIndexWriter`, `RayitoIndexReader`, `metadata-index` stack) | do not pass `index=`; `rayito stack destroy metadata-index` |
| [OpenTelemetry traces](funciones-opcionales/opentelemetry.md) | off | `tracer_provider=` / `tracerProvider`: `rayito.*` spans of your calls | $0 from Rayito; whatever your exporter costs | none of its own | do not pass `tracer_provider=` |
| [S3 mounts](funciones-opcionales/montajes-s3.md) | off | `mounts=` / `mounts`: an S3 bucket as a sandbox folder (`rayito-base-caps`) | $0 of its own; the bucket's S3 requests | `RayitoS3MountAccess` on the execution role (`s3-mounts` stack) | do not pass `mounts=` |
| [Sizes](funciones-opcionales/tamanos.md) | off | `size=` / `size`: launch the image of another size (512mb to 8gb) | more $/h the bigger the size; snapshot per published image | none additional (the optional `sizes-guard` stack restricts which sizes are launched) | do not pass `size=` |
| [Events and webhooks](funciones-opcionales/eventos-y-webhooks.md) | off | `events=` / `events`: lifecycle events stored and delivered to your webhooks | ≈ $0.40/month for the stack at rest; the rest by use | `EventsLauncherPolicy`, `EventsReaderPolicy` or `EventsWebhookAdminPolicy` depending on the task (`events-webhooks` stack) | do not pass `events=`; `rayito events destroy` |
| [OTLP export](funciones-opcionales/exportacion-otlp.md) | off | `telemetry=` / `telemetry`: sandbox metrics over OTLP to CloudWatch | ≈ $0.00002 per sandbox-hour | `RayitoOtlpExport` on the execution role (`otlp-export` stack) | do not pass `telemetry=` |
| [Secret gateway](funciones-opcionales/pasarela-de-secretos.md) | off | `gateways=` / `gateways`: use a secret from the sandbox without being able to read it (with presets for an agent's model: `bedrock_gateway`, `anthropic_gateway`, `openai_compatible_gateway` and those in [Agent providers](guias/agente-proveedores.md)) | $0.05 per 10,000 reads + the secret | read the secrets (`RayitoSecretsReader`) | do not pass `gateways=` |
| [Templates](funciones-opcionales/templates.md) | off | `Template.build()`: build an image from the DSL | ≈ $0.04/week per image version | `RayitoTemplateBuilder` (`templates` stack) | do not call `Template.build()` |
| [Agent templates](funciones-opcionales/templates-de-agente.md) (since 0.8.0) | off | `AgentTemplate(...).build()`: image with OpenCode/deepagents and their pins | ≈ $0.057/week per image version (≈ 3.1 GB; see [Pricing](cost.md#plaza-de-pool-de-agente-c)) | `RayitoTemplateBuilder` (`templates` stack) | do not build the template |
| [Agent in the sandbox](guias/agente-en-el-sandbox.md) (since 0.8.0) | off | `sbx.agent.run()`/`.stream()`/`.prepare()`: a code agent running inside the sandbox, with its model through the gateway | $0 of the SDK's own; the model by tokens (see [cost of an agent](cost.md#coste-de-un-agente-vm-frente-a-modelo)) and what the gateway already costs | that of the chosen secret gateway | do not call `sbx.agent` |
| [Pool: warm-up (`warmup`)](pool.md#calentamiento-warmup) (since 0.8.0) | off | `PoolConfig(warmup=agent_pool_warmup(...))`: pre-warm the agent runtime before parking each slot | ≈ $0.64/slot/month with `warmup` (versus $0.60 for a base slot; see [Pricing](cost.md#plaza-de-pool-de-agente-c)) | that of the image/pool you already use | do not pass `warmup=` |
| [EFS volumes](funciones-opcionales/volumenes-efs.md) (experimental) | off | `volumes=` / `volumes`: an EFS file system shared live between sandboxes (`rayito-base-caps-efs` image); `VolumeStore`/`EfsVolumes` for the volumes and the stack | $0 empty; $0.30/GB-month and $0.03/$0.06 per GB read/written (Elastic); the image takes ≈ 198 MB more | `CallerPolicyArn` of the `efs-volumes` stack on the execution role; `DescribeMountTargets` and the access point CRUD on the caller | do not pass `volumes=`; `EfsVolumes.destroy(delete_file_system=True)` |
| [Custom domain](funciones-opcionales/dominio-propio.md) (experimental) | off | `CustomDomain(...)`: an HTTPS URL under your domain for a sandbox port, without proxy headers (CloudFront distribution in your account, `custom-domain` stack); not yet verified end to end on real AWS | $0 at rest; ~$0.085/GB + $0.0075/10,000 HTTPS requests, plus the Function and one KeyValueStore read per request | CloudFormation and CloudFront for the stack; `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` for the routes | do not instantiate `CustomDomain`; `rayito domain destroy` |
| [Management dashboard (SDK and local dashboard)](funciones-opcionales/panel-de-gestion.md) | off | `Quotas`, `UsageCost`, `Fleet` and `rayito dashboard` (in both CLIs): quotas and allocated memory, billed and estimated cost, sandboxes, images and bulk actions (read-only unless `allow_actions=True` / `--allow-actions`); the local dashboard serves a page on `127.0.0.1` | $0 for the reads, `estimate()` and `Quotas.report()`; paid: `billed()` $0.01 per Cost Explorer request (24 h cache per instance, ≈ $0.30/month with a long-lived instance; in the dashboard only with `--cost-explorer`), `events()` DynamoDB reads (only with `--events-stack`); `resume` bills compute again | `servicequotas:ListServiceQuotas`, Lambda MicroVMs reads, `ce:GetCostAndUsage` only with `billed()` / `--cost-explorer` | do not call its methods; close the dashboard with Ctrl+C |
| [Dashboard in your account (`dashboard` stack)](funciones-opcionales/panel-en-tu-cuenta.md) | off | `rayito stack deploy dashboard` / `OptionalStacks().deploy("dashboard")`: the same page and API as the local dashboard, served from a Lambda with Function URL and Cognito, with separate read and actions roles and an append-only audit; own theme through `ThemeJson` or SSM | ≈ $0 at rest (Lambda, on-demand DynamoDB and Cognito only by use; the Function URL is public and unauthenticated traffic bills invocations, bound it with `ReservedConcurrency`); Cost Explorer $0.01 per query only with `EnableCostExplorer=true` (worst case ≈ $6/month); an `Advanced` SSM parameter for theme or logo, $0.05/month plus interactions | the stack's permissions (`CAPABILITY_IAM`); the function only reads MicroVMs and quotas, and acts only with the actions role from `AllowActions=true` | `rayito stack destroy dashboard` |
| [CloudWatch dashboard](funciones-opcionales/panel-extras.md#dashboard-de-cloudwatch) | off | `rayito dashboard cloudwatch` prints the JSON of a dashboard (Logs Insights over `/rayito/*`, OTLP metrics and quota usage) without calling AWS; the `dashboard-cloudwatch` stack deploys it | $0 with fewer than 4 dashboards, $3/month per dashboard from the fourth; Logs Insights bills the scanned GB on open or refresh | whoever deploys, `cloudwatch:PutDashboard`; whoever views, `logs:StartQuery` | `rayito stack destroy dashboard-cloudwatch` |
| [Multiple accounts in the dashboard](funciones-opcionales/panel-extras.md#varias-cuentas) | off | `rayito dashboard --account NAME=ROLE_ARN`: see other accounts, read-only, with a role that is assumed; the `dashboard-read-role` stack creates that role in each target account | $0 (IAM and STS do not bill) | `sts:AssumeRole` on the target roles; each role, five reads | do not pass `--account`; `rayito stack destroy dashboard-read-role` |
| [Quota alerts](funciones-opcionales/panel-extras.md#avisos-de-cuota) | off | `rayito stack deploy quota-alerts`: a scheduled Lambda publishes to SNS when allocated memory crosses a quota threshold (one alert per crossing) and publishes usage as a metric | ≈ $0.30/month (one custom metric); the rest in free tiers | `CAPABILITY_IAM`; the function reads MicroVMs and quotas and writes to its topic, its row and its metrics namespace | `rayito stack destroy quota-alerts` |
| [Local proxy](funciones-opcionales/proxy-local.md) (`rayito sandbox proxy`) | — (CLI only, in Python and in TypeScript) | serves a sandbox port on `127.0.0.1` | $0: it uses free Lambda calls | what `infra/iam.yaml` already gives (`SandboxLauncherPolicy`) | `Ctrl+C` |
| [MCP server](mcp.md) (`rayito-mcp`) | off | the executable (Python `mcp` extra or TypeScript optional peers `@modelcontextprotocol/server` and `zod`): one sandbox per server process, created on the first tool that needs it and destroyed on shutdown; with `--http`, loopback only and no authentication | a running MicroVM while it is used and one snapshot per suspend cycle; nothing lives beyond `RAYITO_MCP_TIMEOUT_SECONDS` | `SandboxLauncherPolicy` (`infra/iam.yaml`) | close the server from the MCP host |

Each name leads to the feature's full guide; below there is a
[short example](#ejemplos) of the most used ones. These are approximate figures for
us-east-1: the exact ones, with their lookup date and the link to AWS's
pricing page, are in [Features with AWS cost](#funciones-con-coste-aws)
and in each feature's guide. The price of MicroVMs, the pool and an
agent is in [Pricing](cost.md).

!!! info "You deploy the infrastructure yourself: `rayito stack`"
    The features that need something in your account (a table, some IAM
    policies, some Lambdas) ship it as a component of
    [`rayito stack`](funciones-opcionales/pilas-opcionales.md):
    `rayito stack deploy <component>` prints the "Cost and
    activation" block and asks for confirmation, `rayito stack status` says what is
    deployed and `rayito stack destroy` deletes it. Since 0.6.1, redeploying
    keeps the parameters you do not repeat. In the SDK, `OptionalStacks`.

The nine `rayito stack` components, and which feature from the table
above each one uses. None deploys itself:

| Component | For which feature | What it creates | Idle cost |
|---|---|---|---|
| `secrets-access` | [Secret injection and management](secrets.md), [secret gateway](funciones-opcionales/pasarela-de-secretos.md) | 2 IAM policies | $0 |
| `metadata-index` | [Metadata index](funciones-opcionales/indice-de-metadatos.md) | on-demand DynamoDB table + 2 IAM policies | $0 with the table empty |
| `s3-mounts` | [S3 mounts](funciones-opcionales/montajes-s3.md) | 1 IAM policy | $0 |
| `sizes-guard` | [Sizes](funciones-opcionales/tamanos.md) (optional) | 1 IAM policy | $0 |
| `events-webhooks` | [Events and webhooks](funciones-opcionales/eventos-y-webhooks.md) (also `rayito events deploy`) | secret, table, 3 Lambdas, queues, scheduler and policies | ≈ $0.40/month |
| `otlp-export` | [OTLP export](funciones-opcionales/exportacion-otlp.md) with the execution role | 1 IAM policy | $0 |
| `templates` | [Templates](funciones-opcionales/templates.md) and [agent templates](funciones-opcionales/templates-de-agente.md) | 1 IAM policy | $0 |
| `efs-volumes` (experimental) | [EFS volumes](funciones-opcionales/volumenes-efs-vpc.md) | EFS file system, mount targets, security groups, connector, role and policy, in your VPC | $0 with the file system empty |
| `custom-domain` (experimental) | [Custom domain](funciones-opcionales/dominio-propio.md) (also `rayito domain deploy`) | CloudFront distribution, Function and KeyValueStore | $0 with no traffic |

Parameters of each: [Optional stacks](funciones-opcionales/pilas-opcionales.md#parametros-de-cada-componente).

!!! note "Experimental"
    EFS volumes and custom domain, published in
    [0.7.0](novedades/0.7.0.md), are **experimental**: you can use them,
    but their API may change in a minor, and custom domain has not yet
    been verified end to end on real AWS (see the warning on
    [its page](funciones-opcionales/dominio-propio.md)). `domain=` in
    `Sandbox.create()` still raises `UnimplementedError` without calling AWS:
    use `CustomDomain`. Status:
    [What's new](novedades/index.md#disponible-como-experimental).

!!! warning "The metadata index is not yet accepted on real AWS"
    Secrets were accepted against real AWS with release 0.5.0, and
    OpenTelemetry traces do not call AWS, so their tests with an in-memory
    exporter are the complete proof. The index is tested
    with a simulated DynamoDB, but its test against real AWS has not yet been
    run: it is, together with custom domain (experimental), the only
    feature on this page without that acceptance.

!!! note "Outside this page: the transfer bucket"
    Exporting `RAYITO_TRANSFER_BUCKET` (or passing `transfer=`) makes files
    of 8 MiB or more and presigned URLs go through S3, which charges for its requests
    and temporary storage. It is not an optional feature in
    this list, but it is the only environment variable with a cost of its own:
    [Files and S3](files.md#coste-de-s3).

## Examples { #ejemplos }

A short example per feature. Each one has its full guide, with the
options, the detailed cost and the errors.

<a id="secrets-injection"></a>

### Secret injection { #inyeccion-de-secretos }

Full guide, cache rules and threat model: [Secrets](secrets.md).

=== "Python"

    ```python
    from rayito import Sandbox, SecretCache

    cache = SecretCache(ttl_seconds=300)  # optional; does not call AWS
    with Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache) as sbx:
        sbx.commands.run("python agent.py")  # 1 GetSecretValue per TTL, not per command
        sbx.commands.run("env", secrets={"GH_TOKEN": "gh"})
    ```

=== "TypeScript"

    ```ts
    // npm install @aws-sdk/client-secrets-manager   (optional peer)
    import { Sandbox, SecretCache } from "rayito";

    const secretCache = new SecretCache({ ttlSeconds: 300 });
    await using sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
    await sbx.commands.run("python agent.py");
    await sbx.commands.run("env", { secrets: { GH_TOKEN: "gh" } });
    ```

**The sandbox code can read an injected secret**: with untrusted
code, use short-lived, least-privilege credentials.

<a id="secrets-crud"></a>

<a id="secret-crud-secrets-manager"></a>

### Secret management (CRUD) { #gestion-de-secretos-crud }

Each secret is billed until you delete it: turning the feature off means no longer
instantiating `SecretStore` **and** `destroy()`ing the secrets created.

=== "Python"

    ```python
    import os

    from rayito import SecretStore

    store = SecretStore(region="us-east-1")  # does not call AWS until the first method
    store.create("openai", os.environ["OPENAI_API_KEY"], metadata={"team": "ml"})
    print([info.name for info in store.list(limit=20).items])
    store.destroy("openai")  # stops billing
    ```

=== "TypeScript"

    ```ts
    import { SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" });
    await store.create("openai", process.env.OPENAI_API_KEY ?? "", { metadata: { team: "ml" } });
    console.log((await store.list({ limit: 20 })).items.map((info) => info.name));
    await store.destroy("openai");
    ```

The E2B shim (`from rayito.e2b import Secret`) uses the same `SecretStore`;
differences in [E2B compatibility](e2b-compat.md#secretos-secret-asyncsecret).

<a id="metadata-index"></a>

### Metadata index (DynamoDB) { #indice-de-metadatos-dynamodb }

Deploy the table first (once, $0 at rest) with
`infra/metadata-index.yaml`. Full guide: [Metadata
index](funciones-opcionales/indice-de-metadatos.md).

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")  # does not call AWS yet
    with Sandbox.create(metadata={"user": "42"}, index=idx) as sbx:  # 1 PutItem
        sbx.pause()
        for item in Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx):
            print(item.sandbox_id, item.state)  # 1 BatchGetItem per page
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito";

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    await using sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
    ```

<a id="otel-sdk"></a>

### SDK OpenTelemetry traces { #trazas-opentelemetry-del-sdk }

Full guide, span names, attributes and what is never recorded:
[OpenTelemetry](funciones-opcionales/opentelemetry.md).

=== "Python"

    ```python
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from rayito import Sandbox

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with Sandbox.create(tracer_provider=provider) as sbx:  # span "rayito.sandbox.create"
        sbx.commands.run("echo hello")  # span "rayito.commands.run"
    print([span.name for span in exporter.get_finished_spans()])
    ```

=== "TypeScript"

    ```ts
    import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
    import { Sandbox } from "rayito";

    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });

    {
      await using sbx = await Sandbox.create({ tracerProvider: provider }); // span "rayito.sandbox.create"
      await sbx.commands.run("echo hello"); // span "rayito.commands.run"
    }
    console.log(exporter.getFinishedSpans().map((span) => span.name));
    ```

Without `tracer_provider=`/`tracerProvider` (the default): no span and no
`opentelemetry`/`@opentelemetry/api` import at run time.

<a id="local-proxy"></a>

### `rayito sandbox proxy` { #rayito-sandbox-proxy }

```bash
rayito sandbox proxy microvm-<id> --port 8000
# serves http://127.0.0.1:8000 -> guest port 8000,
# renewing the JWE before it expires; Ctrl+C to stop.
```

It listens only on loopback (`127.0.0.1`) unless you pass `--bind <ip>` together
with `--allow-remote`: whoever reaches that port uses the sandbox with your access.
No SDK option is needed: it is CLI-only, with no AWS cost of its own.
Guide: [Local proxy](funciones-opcionales/proxy-local.md).

## Features with AWS cost { #funciones-con-coste-aws }

The full sheet for each feature, with the exact AWS API calls
and the SDK file where it is documented.

| Feature | Status | Python option | TypeScript option | Default | What it turns on | AWS resources / calls | Approximate cost | IAM needed | How to turn it off | Where |
|---|---|---|---|---|---|---|---|---|---|---|
| [Secret injection](#secrets-injection) | available (0.5.0) | `secrets=` | `secrets` | `None` / `undefined` | Delivers the value of one or more secrets as an environment variable of a command, PTY, Python cell, code context or slot taken from the pool (the sandbox code can read it); the cache is set with `secret_cache=SecretCache(...)` / `secretCache` | `secretsmanager:GetSecretValue` once per secret and `SecretCache` TTL (300 s by default; a hit makes 0 calls), never once per call; no new resources | SM: $0.05/10,000 calls (≈ $0.04/month per secret and process with the default TTL) + the secret itself, $0.40/secret-month ([Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/), checked 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` (and `DescribeSecret`) on `…:secret:rayito/*` on the **caller's** credentials (`RayitoSecretsReader` policy from `infra/secrets-access.yaml`); `kms:Decrypt` only with a **customer-managed KMS key** | Do not pass `secrets=` / `secrets` or `secret_cache=` / `secretCache` (or pass `None`/`undefined`) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/inject.ts` |
| [Secret management (CRUD)](#secrets-crud) | available (0.5.0) | `SecretStore(...)` | `new SecretStore({...})` | not instantiated = no boto3/SDK client | Create, update, list and delete Rayito-native secrets under a prefix (and the E2B shim's `Secret`/`AsyncSecret`) | One Secrets Manager secret per `create`; `secretsmanager:CreateSecret/PutSecretValue/UpdateSecret/DescribeSecret/ListSecrets/DeleteSecret` (`AWS_API_NOTES.md` §19) | SM: $0.40/secret-month **until `destroy`** + $0.05/10,000 calls ([Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/), checked 2026-09-30, us-east-1) | Secrets Manager CRUD under the configured prefix (`rayito/` by default; `destroy` needs `DescribeSecret` in addition to `DeleteSecret`) and `ListSecrets` on `*` (`RayitoSecretsAdmin` policy from `infra/secrets-access.yaml`, which already includes both); adds `kms:GenerateDataKey`/`kms:Decrypt` on the key if `kms_key_id=`/`kmsKeyId` is passed (customer-managed key); without that parameter, the AWS-managed key charges nothing and requires no separate KMS permission | Do not instantiate `SecretStore` / `new SecretStore(...)` (explicit calls only) and `destroy()` the secrets created | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/store.ts` |
| [Metadata index (DynamoDB)](#metadata-index) | implemented in 0.5.0, pending acceptance on real AWS | `index=DynamoDbIndex(...)` | `index: new DynamoDbIndex({...})` | `None` / `undefined` | Writes an immutable row per sandbox (`metadata`, image, `startedAt`, TTL) to your DynamoDB table on creation (`create()`, `PoolConfig`) and joins it with `list-microvms` to filter `list()`/`paginate()` by metadata also over `SUSPENDED`, without probing `Health` or waking anything; also in the shim (`Sandbox.list(..., index=)`, `E2B(index=)`) and the CLI (`--index-table`) | `dynamodb:PutItem` once per created sandbox (conditional) and `dynamodb:BatchGetItem` once per `list-microvms` page when listing with `metadata` + `index`; you deploy the table (`infra/metadata-index.yaml`, on-demand, TTL on `expires_at`); never `DeleteItem` | DynamoDB on-demand ([DynamoDB on-demand pricing](https://aws.amazon.com/dynamodb/pricing/on-demand/), checked 2026-09-30, us-east-1): $0.625 per million WRUs (~1 per `create` ≈ $0.000000625) + $0.125 per million RRUs (`BatchGetItem` is billed per item read: 0.5 RRU per candidate sandbox, eventually consistent read of ≤ 4 KB) + $0.25/GB-month after 25 GB free; TTL deletion free. Example: 10,000 sandboxes/month ≈ 10,000 WRUs ($0.006) and a daily `list()` over them ≈ 300,000 items × 0.5 = 150,000 RRUs ($0.019): ≈ $0.03/month; empty table $0 | `dynamodb:PutItem` (writer, `RayitoIndexWriter` policy) and `dynamodb:BatchGetItem` (reader, `RayitoIndexReader`) on the table ARN, on the **caller's** credentials | Do not pass `index=` / `index` (or pass `None`/`undefined`); delete the `infra/metadata-index.yaml` stack to stop paying for storage | `clients/python/src/rayito/_index.py` / `clients/typescript/src/index/dynamodb.ts` |
| [SDK OpenTelemetry traces](#otel-sdk) | available (0.5.0) | `tracer_provider=` | `tracerProvider` | `None` / `undefined` | Instruments `create/connect/kill/pause/resume` (instance and class/static), `commands.run`, `run_code`/`runCode` and `files.*` with `rayito.*` spans (`SpanKind.CLIENT`) on the `TracerProvider` you already have configured | None of its own: Rayito creates or calls no AWS service for this — the cost depends on **your** exporter (CloudWatch, your own collector, …) | $0 from Rayito: the OTel exporter is paid for and configured by whoever turns it on, not this SDK ([CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) only if you export there, checked 2026-09-30) | None of Rayito's own; whatever your OTel exporter requires | Do not pass `tracer_provider=` / `tracerProvider` (or pass `None`/`undefined`) | `clients/python/src/rayito/_otel.py` / `clients/typescript/src/otel.ts` |
| [S3 mounts](funciones-opcionales/montajes-s3.md) | available (0.6.0) | `mounts={"/mnt/data": S3Mount(...)}` | `mounts: { "/mnt/data": new S3Mount({...}) }` | `None` / `undefined` | Mounts an S3 bucket (or a prefix) as a sandbox folder with `mount-s3`, only on `rayito-base-caps` and only the buckets in the image's allowlist (`RAYITO_ALLOWED_MOUNT_BUCKETS`) | No new resources: the normal S3 requests (`GetObject`/`ListObjectsV2`, and `PutObject`/`DeleteObject` with write access) with the execution role; the IAM policy comes from the `s3-mounts` stack (`rayito stack deploy s3-mounts`) | $0 of Rayito's own; the normal S3 requests and storage of the bucket ([S3 pricing](https://aws.amazon.com/s3/pricing/), checked 2026-10-01, us-east-1); the stack is $0 (IAM only) | `RayitoS3MountAccess` (`infra/s3-mounts.yaml`) on the **execution role**, scoped to a bucket and its prefixes | Do not pass `mounts=` / `mounts`; `rayito stack destroy s3-mounts` removes the policy (it deletes no objects) | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [Sizes](funciones-opcionales/tamanos.md) | available (0.6.0) | `size="4gb"` | `size: "4gb"` | `None` / `undefined` | Launches the image of the requested size (`rayito-base-4gb`) from the closed catalog of five sizes, resolved on the client; the image is published beforehand with `rayito image publish --sizes` | Nothing new on launch; `get_info()` makes at most one free `GetMicrovmImageVersion` per version and process; each published size is one more image (`create`/`update-microvm-image` when publishing it); the optional `sizes-guard` stack is an IAM policy | The bigger MicroVM costs more per hour: from $0.0315/h (512 MiB) to $0.5044/h (8192 MiB) at baseline ([Limits](limits.md#tamano-cpuram), checked 2026-09-30, us-east-1); each published size adds snapshot storage, ≈ $0.04/week per version | None additional to launch; `sizes-guard` (`RayitoRunAllowedSizes`) denies `RunMicrovm` outside the images you list | Do not pass `size=` / `size`; delete the sized images' versions with `rayito image prune --image-name ...` and the `sizes-guard` stack if you deployed it | `clients/python/src/rayito/_sizing.py` / `clients/typescript/src/sizing/sizing.ts` |
| [Events and webhooks](funciones-opcionales/eventos-y-webhooks.md) | available (0.6.0) | `events=LifecycleEvents(...)` | `events: new LifecycleEvents()` | `None` / `undefined` | `rayd` emits signed `created`/`paused`/`resumed`/`killed`; the stack stores them (`get_events`) and delivers them to your webhooks with E2B's signature | The `events-webhooks` stack (`rayito events deploy`): HMAC secret, DynamoDB table with streams, 3 Lambdas with their log groups, subscription filter, scheduler, failure SQS queues and 4 IAM policies; per sandbox, one `secretsmanager:GetSecretValue` of the stack key per `LifecycleEvents` instance | ≈ $0.40/month at rest (the secret); DynamoDB, Lambda and SQS by use ([Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/), checked 2026-09-30, us-east-1) | `EventsLauncherPolicy` (stack output) on the **caller's** credentials (`EventsReaderPolicy` for `get_events`, `EventsWebhookAdminPolicy` for the webhooks); the execution role writes to the image's log group | Do not pass `events=` / `events`; `rayito events destroy` deletes the stack | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [OTLP export](funciones-opcionales/exportacion-otlp.md) | available (0.6.0) | `telemetry=TelemetryExport(...)` | `telemetry: new TelemetryExport({...})` | `None` / `undefined` | `rayd` exports 7 CPU, memory and disk metrics over OTLP/HTTP every `interval_s` (60 s by default) | `PutMetricData` per exported batch from the sandbox; with `OtlpAuth.execution_role()`, the `otlp-export` stack's policy on the execution role | $0 for the option; CloudWatch bills OTLP ingestion at $0.50/GB: ≈ $0.00002 per sandbox-hour with `interval_s=60` (`AWS_API_NOTES.md` Q120, [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/), checked 2026-10-02) | `cloudwatch:PutMetricData` (`RayitoOtlpExport`) on the **execution role**; with `OtlpAuth.bearer(...)`, read the token's secret | Do not pass `telemetry=` / `telemetry`; delete the `otlp-export` stack if no sandbox uses it anymore | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [Secret gateway](funciones-opcionales/pasarela-de-secretos.md) | available (0.6.0) | `gateways={"anthropic": SecretGateway(...)}` | `gateways: { anthropic: new SecretGateway({...}) }` | `None` / `undefined` | `rayd` opens a gateway per route on loopback that injects the real header toward a fixed `upstream`: the sandbox code uses the secret without being able to read it | `secretsmanager:GetSecretValue` once per header and `SecretCache` TTL; no new resources | $0.05/10,000 calls plus the secret itself if it did not exist ([Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/), checked 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` on the **caller's** credentials (`RayitoSecretsReader`) | Do not pass `gateways=` / `gateways` | `clients/python/src/rayito/_secret_gateway/_domain.py` / `clients/typescript/src/secret-gateway/domain.ts` |
| [Templates](funciones-opcionales/templates.md) | available (0.6.0) | `Template.build(...)` | `Template.build(...)` | without calling `build` = no client | Builds a new image from the templates DSL, composed on top of a published `rayito-base` image (also `rayito template build`) | `GetMicrovmImageVersion`, `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`, `ListMicrovmImageVersions`; S3 `GetObject`/`HeadObject`/`PutObject` of the zip; logs only if the build fails (`AWS_API_NOTES.md` §27) | Snapshot storage per new version, ≈ $0.04/week per version; the build is not billed separately; the context zip stays in your bucket (standard S3) | `RayitoTemplateBuilder` (`infra/templates.yaml`, `rayito stack deploy templates`) on the **caller's** credentials | Do not call `Template.build()`; delete the versions with `rayito image prune` | `clients/python/src/rayito/_templates/__init__.py` / `clients/typescript/src/templates/dsl.ts` |
| [EFS volumes](funciones-opcionales/volumenes-efs.md) | experimental (0.7.0) | `volumes={"/mnt/data": EfsVolume(...)}`; `VolumeStore(...)`, `EfsVolumes(...)` | `volumes: { "/mnt/data": new EfsVolume({...}) }`; `new VolumeStore({...})`, `new EfsVolumes({...})` | `None` / `undefined` | `rayd` mounts each access point with `amazon-efs-utils` (TLS + IAM) in the same `ConfigureSandbox` as the rest; only on the optional `rayito-base-caps-efs` image, with `execution_role_arn` and a single own connector in `egress` (never `INTERNET_EGRESS`) | `create()`: one `DescribeMountTargets` per file system if the volume carries no `mount_target_ip`; `VolumeStore`: `CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`; the `efs-volumes` stack (`EfsVolumes.deploy`): encrypted file system, mount targets, security groups and connector, inside your VPC | $0 empty; $0.30/GB-month (Standard), $0.016/GB-month (IA), $0.03/GB read and $0.06/GB written (Elastic) ([EFS pricing](https://aws.amazon.com/efs/pricing/), checked 2026-09-11, us-east-1); the image with `amazon-efs-utils` takes ≈ 198 MB more (Q122) | The caller: access point CRUD and `DescribeMountTargets`; the **execution role**: the stack's `CallerPolicyArn` (`ClientMount`, `ClientWrite` unless read-only) | Do not pass `volumes=` / `volumes`; `EfsVolumes.destroy(delete_file_system=True)` deletes the stack and the data | `clients/python/src/rayito/_volumes/` / `clients/typescript/src/volumes/` |
| [Custom domain](funciones-opcionales/dominio-propio.md) | experimental (0.7.0, without end-to-end acceptance on real AWS) | `CustomDomain(public_domain=...)`, `AsyncCustomDomain(...)` | `new CustomDomain({ publicDomain })` | not instantiated = no client | A CloudFront distribution in your account whose CloudFront Function routes `https://<port>-<alias>.<your-domain>` to the sandbox, setting the AWS proxy headers itself; you register each route (`register()`/`unregister()`/`refresh()`) | The `custom-domain` stack (`CustomDomain.deploy`, `rayito domain deploy`): distribution, CloudFront Function and KeyValueStore; per route, `DescribeKeyValueStore`/`PutKey`/`DeleteKey` (signed with SigV4A) | $0 at rest; ~$0.085/GB + $0.0075/10,000 HTTPS requests, ~$0.10 per million Function invocations and ~$0.50 per million KeyValueStore reads; ~$5 per million `PutKey`/`DeleteKey` ([CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/), list figures not measured with a real distribution, us-east-1) | The caller: `cloudformation:*Stack*` and the CloudFront permissions to create and delete the distribution, the Function and the KeyValueStore; `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` on the stack's KeyValueStore | Do not instantiate `CustomDomain`; `CustomDomain.destroy()` or `rayito domain destroy` deletes the stack | `clients/python/src/rayito/_custom_domain/` / `clients/typescript/src/custom-domain/` |
| [Management dashboard (SDK)](funciones-opcionales/panel-de-gestion.md) | published in 0.12.0, accepted on real AWS on 2026-10-14 | `Quotas(...)`, `UsageCost(...)`, `Fleet(...)` (and `AsyncQuotas`, `AsyncUsageCost`, `AsyncFleet`) | `new Quotas({...})`, `new UsageCost({...})`, `new Fleet({...})` | not instantiated or not called = no client; actions, `allow_actions=False` / `allowActions: false` | Reads the applied quotas and the allocated memory against the region's quota, the billed MicroVM cost (Cost Explorer, by usage type, with a local cache) and the estimate per sandbox, and the inventory, images, events and bulk actions (kill, suspend, resume) with a queue that respects TPS | Reads only, no resources: `ListServiceQuotas`, `ListMicrovms`, `GetMicrovmImageVersion`, `ListMicrovmImages`, `ListMicrovmImageVersions` and, with `billed()`, `ce:GetCostAndUsage` (us-east-1); with `allow_actions=True`, `TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` (`AWS_API_NOTES.md` §30) | $0 the reads; Cost Explorer $0.01 per request ([Cost Explorer pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/), checked 2026-10-13, us-east-1), with a 24 h cache ≈ 30 requests/month ≈ $0.30; the per-sandbox estimate is free and never replaces the bill | `servicequotas:ListServiceQuotas` and the Lambda MicroVMs reads; `ce:GetCostAndUsage` only for `billed()`; `lambda:TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` only for the actions | Do not call its methods; do not pass `allow_actions=True` | `clients/python/src/rayito/_dashboard/` / `clients/typescript/src/dashboard/` |
| [Dashboard in your account (stack)](funciones-opcionales/panel-en-tu-cuenta.md) | published in 0.12.0, accepted on real AWS on 2026-10-14, also the `AuthMode=aws-iam` variant | `OptionalStacks().deploy("dashboard", artifact_bucket=...)` | `new OptionalStacks().deploy("dashboard", { artifactBucket })` | not deployed = no resources; deployed, read-only and without Cost Explorer | A Lambda with Function URL serves the dashboard's page and API with Cognito sign-in (token validated in the function before routing), separate read and actions roles (`AllowActions=true`), an audit of every action in DynamoDB, a billed-cost cache and an own theme (`ThemeJson` / SSM) | `AWS::Lambda::Function` and `Url`, `AWS::Cognito::UserPool` (+ client, domain and group), 2 `AWS::DynamoDB::Table`, `AWS::IAM::Role` (1 or 2), `AWS::Logs::LogGroup`, and with `ThemeJson` an `AWS::SSM::Parameter`; no CloudFront or buckets | ≈ $0 at rest; by use: Lambda per request (public URL: unauthenticated traffic also bills, bound it with `ReservedConcurrency`), DynamoDB per action, with `EnableCostExplorer=true` Cost Explorer $0.01 per query with a 24 h cache (worst case ≈ $6/month) and, with a theme or logo in `Advanced` SSM parameters, $0.05/month each plus API interactions ([Lambda](https://aws.amazon.com/lambda/pricing/), [Cognito](https://aws.amazon.com/cognito/pricing/), [SSM](https://aws.amazon.com/systems-manager/pricing/), [Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/), checked 2026-10-13, us-east-1) | `cloudformation:*Stack` with `CAPABILITY_IAM` to deploy; MicroVM and quota reads for the function; `ce:GetCostAndUsage` only with `EnableCostExplorer=true`; `TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` only in the actions role | Do not deploy it, or `rayito stack destroy dashboard` | `clients/python/src/rayito/_stacks/components/dashboard.py` / `clients/typescript/src/stacks/components/dashboard.ts` |
| [CloudWatch dashboard](funciones-opcionales/panel-extras.md#dashboard-de-cloudwatch) | published in 0.12.0, accepted on real AWS on 2026-10-14; the OTLP charts' PromQL queries were checked through the API, not how the console draws them | `OptionalStacks().deploy("dashboard-cloudwatch")` (or `rayito dashboard cloudwatch`) | `new OptionalStacks().deploy("dashboard-cloudwatch")` | not deployed = no resources; `rayito dashboard cloudwatch` only prints | The JSON of a CloudWatch dashboard with Logs Insights widgets over `/rayito/*` and, with `IncludeOtlp`/`IncludeQuota` (`--otlp`/`--quota`), charts of the OTLP metrics and of quota usage | One `AWS::CloudWatch::Dashboard`; printing it makes no call | $0 with fewer than 4 dashboards, $3/month per dashboard from the fourth; Logs Insights $0.005 per GB scanned on open or refresh ([CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), checked 2026-10-13, us-east-1) | `cloudwatch:PutDashboard`, `GetDashboard`, `DeleteDashboards` to deploy; `logs:StartQuery` and `logs:GetQueryResults` to view it | Do not deploy it, or `rayito stack destroy dashboard-cloudwatch` | `clients/python/src/rayito/_dashboard/_cloudwatch_board.py` / `clients/typescript/src/stacks/components/dashboard-cloudwatch.ts` |
| [Multiple accounts in the dashboard](funciones-opcionales/panel-extras.md#varias-cuentas) | published in 0.12.0, accepted on real AWS on 2026-10-14 | `rayito dashboard --account NAME=<ARN>`; `OptionalStacks().deploy("dashboard-read-role", parameters={...})` | `new OptionalStacks().deploy("dashboard-read-role", { parameters })` | without `--account` STS is not called; the role does not exist until deployed | A view of other accounts in the local dashboard, read-only and without billed cost, with a role that is assumed and an optional `ExternalId` | In the target account one `AWS::IAM::Role`; in the dashboard one `sts:AssumeRole` per account and the dashboard's reads | $0 ([IAM](https://aws.amazon.com/iam/pricing/), checked 2026-10-13) | `sts:AssumeRole` on the roles; each role only `ListMicrovms`, `ListMicrovmImages`, `ListMicrovmImageVersions`, `GetMicrovmImageVersion` and `ListServiceQuotas` | Do not pass `--account`, or `rayito stack destroy dashboard-read-role` in each target account | `clients/python/src/rayito/_dashboard/_accounts.py` / `clients/python/src/rayito/_stacks/components/dashboard_read_role.py` |
| [Quota alerts](funciones-opcionales/panel-extras.md#avisos-de-cuota) | published in 0.12.0, accepted on real AWS on 2026-10-13 | `OptionalStacks().deploy("quota-alerts", artifact_bucket=...)` | `new OptionalStacks().deploy("quota-alerts", { artifactBucket })` | not deployed = no resources | A scheduled check that publishes to SNS when allocated memory crosses a threshold of the region's quota (one alert per crossing) and publishes usage as a custom metric | `AWS::Lambda::Function`, `AWS::Events::Rule`, `AWS::SNS::Topic`, 1 `AWS::DynamoDB::Table`, `AWS::IAM::Role`, `AWS::Logs::LogGroup` | ≈ $0.30/month for the metric; Lambda, EventBridge, DynamoDB and SNS in free tiers ([CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), [SNS](https://aws.amazon.com/sns/pricing/), checked 2026-10-13, us-east-1) | `CAPABILITY_IAM` to deploy; the function, MicroVM and quota reads, `sns:Publish` on its topic, `GetItem`/`PutItem` on its table and `cloudwatch:PutMetricData` with a fixed `cloudwatch:namespace` | Do not deploy it, or `rayito stack destroy quota-alerts` | `clients/python/src/rayito/_stacks/components/quota_alerts.py` / `clients/typescript/src/stacks/components/quota-alerts.ts` |

A row becomes "available" when the feature is accepted against real
AWS and published in a version (see the warning in [At a glance](#de-un-vistazo));
between acceptance and release it says "accepted on real AWS" and the date.

## No AWS cost { #sin-coste-aws }

| Feature | Option | What it turns on | AWS resources | Cost | Where |
|---|---|---|---|---|---|
| [`rayito sandbox proxy`](#local-proxy) | CLI `rayito sandbox proxy <id> --port N` (Python and TypeScript) | Serves a guest port on `127.0.0.1` (another interface only with `--bind` + `--allow-remote`), renewing the `x-aws-proxy-auth` header's JWE before it expires | `lambda:GetMicrovm` (once, for the endpoint) + `lambda:CreateMicrovmAuthToken` (~1 every 45 min per running proxy); IAM: `lambda:GetMicrovm` and `lambda:CreateMicrovmAuthToken` on the MicroVM (already in `infra/iam.yaml`'s `CallerPolicy`) | $0: both calls are free (quotas of 100 and 50 TPS, `AWS_API_NOTES.md` §11); if the sandbox was suspended, waking it through auto-resume bills its normal compute plus the snapshot read on resume, not the proxy itself | `clients/python/src/rayito/cli/_proxy.py` (CLI) |
| [`rayito-mcp`](mcp.md) | `rayito-mcp` executable (Python: `pip install "rayito[mcp]"`; TypeScript: `npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp`) | Creates one sandbox per process on the first tool that needs it and terminates it on shutdown; six tools (`run_code`, `run_command`, `read_file`, `write_file`, `list_files`, `list_sandboxes`) | `RunMicrovm` on first use, `TerminateMicrovm` on shutdown, and each tool's calls; no resources that persist | that of a running MicroVM while it is used plus one snapshot per suspend ([cost model](cost.md)); with `RAYITO_MCP_IDLE_SECONDS=0`, no auto-suspend | `clients/python/src/rayito/mcp/` / `clients/typescript/src/mcp/` |

It consumes no additional quota or money: it reuses free calls the
SDK already makes. Guide: [Local proxy](funciones-opcionales/proxy-local.md).

<a id="convencion-de-opt-in"></a><a id="la-plantilla-de-cada-opcion-de-coste"></a>

??? info "For whoever adds an optional feature to Rayito"
    This page's rule is ADR-014 of
    [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md#adr-014--componentes-opcionales-en-la-cuenta-del-cliente).
    Every new optional feature follows four rules, the same in Python and
    TypeScript (`clients/python/src/rayito/_optional.py` and
    `clients/typescript/src/optional.ts`):

    - **(a) One named kwarg per feature.** At most, a second kwarg with the
      configuration object. `None`/`undefined` is "off"; no environment
      variable, configuration file or global setter turns it on; turning on
      an option never changes the meaning of another kwarg.
    - **(b) Immutable, reusable configuration object.** Its class
      name says the AWS service (`DynamoDbIndex`, `SecretCache`,
      `SecretStore`) and it creates its boto3 / AWS SDK client **lazily, on its
      first use** — never in the configuration object's constructor or in
      the `Sandbox`'s.
    - **(c) TypeScript optional peers loaded on demand.** The AWS SDK v3
      clients that only these features use (`@aws-sdk/client-secrets-manager`,
      `@aws-sdk/client-dynamodb`) are **optional** peerDependencies: they are loaded
      with a dynamic `import()` only inside the already activated function
      (`loadOptionalPeer`), never at the top level of a module.
      `@opentelemetry/api` is also an optional peer, but only for types:
      `src/otel.ts` uses it with `import type` (erased at build), the SDK uses
      the `tracerProvider` the caller passes directly and loads nothing
      from `@opentelemetry/api` at run time. In Python, the equivalent
      is `rayito[otel]` via `require_module`, which imports `opentelemetry.trace`
      only when `tracer_provider=` is turned on.
    - **(d) "Cost and activation" docstring block.** Every cost option carries it,
      with the following template.

    **The template.** The docstring (Python) or TSDoc (TypeScript) of the
    option that turns on each feature carries a block titled literally
    **"Coste y activación"** (the SDK's docstrings are written in Spanish):

    ```text
    Coste y activación
    -------------------
    Activa: <qué hace exactamente esta opción>
    Recursos y llamadas AWS: <qué crea o llama, con nombres de API reales>
    Coste aproximado: <cifra, región y fecha, con enlace a la página de precios>
    IAM: <permisos mínimos necesarios>
    Cómo apagarla: <qué valor la deja apagada — normalmente None/undefined>
    Ejemplo:
        <fragmento runnable de ≤ 10 líneas>
    ```

    `scripts/tests/test_optional_features_docs.py` checks, for each row
    marked "disponible" in the [Features with AWS cost](#funciones-con-coste-aws) table, that the cited option symbol
    appears in the SDK file of its "Where" column within a single "Coste y
    activación" block that also carries the template's six sections.
