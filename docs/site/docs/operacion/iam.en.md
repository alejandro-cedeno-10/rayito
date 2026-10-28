# IAM

The SDK runs with **your** AWS credentials (those of whoever calls it: your
user, your service's role, your CI's role). This page says which permission
each feature needs and which template in [`infra/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/infra)
grants it.

## The templates { #las-plantillas }

| Template | Creates | Cost | When |
|---|---|---|---|
| `infra/iam.yaml` | the image build role, the optional execution role and three caller policies: `rayito-m0-launcher-<region>` (`SandboxLauncherPolicy`, launch sandboxes only), `rayito-m0-publisher-<region>` (`ImagePublisherPolicy`, publish images only) and `rayito-m0-caller-<region>` (`CallerPolicy`, both together) | $0 | always ([Set up AWS](../primeros-pasos/configurar-aws.md)) |
| `infra/artifact-bucket.yaml` | the `rayito-m0-artifacts` stack: an S3 bucket for the image zips (encrypted, no public access, TLS only, the zips expire after 7 days) and its policy; no IAM role or policy | $0 to exist; the zips, cents | only if there is no bucket to reuse: `rayito setup` creates it ([Set up AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)). Deleting the stack keeps the bucket |
| `infra/secrets-access.yaml` | the `RayitoSecretsReader` and `RayitoSecretsAdmin` policies | $0 | only if you use [secrets](../secrets.md) |
| `infra/metadata-index.yaml` | the DynamoDB table and the `RayitoIndexWriter` and `RayitoIndexReader` policies | $0 at rest | only if you use the [metadata index](../funciones-opcionales/indice-de-metadatos.md) |
| `infra/egress-connector.yaml` | a VPC network connector with a deny-all security group | that of your VPC | only if you want to control internet egress from outside the sandbox ([Outbound network](../network.md#la-alternativa-de-plataforma)) |
| `infra/templates.yaml` | the `RayitoTemplateBuilder` policy | $0 | only if you build templates (`Template.build`, `AgentTemplate`) ([Templates](../funciones-opcionales/templates.md)) |
| `infra/s3-mounts.yaml` | the `RayitoS3MountAccess` policy, for the execution role | $0 | only if you use `mounts=` ([S3 mounts](../funciones-opcionales/montajes-s3.md)) |
| `infra/otlp-export.yaml` | the `RayitoOtlpExport` policy, for the execution role | $0 | only if you use `telemetry=` with `OtlpAuth.execution_role()` ([OTLP export](../funciones-opcionales/exportacion-otlp.md)) |
| `infra/sizes-guard.yaml` | the `RayitoRunAllowedSizes` policy (a Deny) | $0 | only if you want to limit the sizes that get launched ([Sizes](../funciones-opcionales/tamanos.md)) |
| `infra/events-webhooks.yaml` | the events stack and the `EventsLauncherPolicy`, `EventsReaderPolicy`, `EventsWebhookAdminPolicy` and `EventsOperatorPolicy` policies | see [Pricing](../cost.md) | only if you use `events=` ([Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md)) |
| `infra/efs-volumes.yaml` (experimental) | EFS in your VPC, its connector and the `RayitoEfsVolumeClient` policy for the execution role | see [Pricing](../cost.md) | only if you use EFS volumes ([EFS volumes in your VPC](../funciones-opcionales/volumenes-efs-vpc.md)) |
| `infra/custom-domain.yaml` (experimental) | CloudFront, its Function and a KeyValueStore; no role or policy | see [Pricing](../cost.md) | only if you use `CustomDomain` ([Custom domain](../funciones-opcionales/dominio-propio.md)) |
| `infra/agentcore-runtime.yaml` | an agent's `rayito-agentcore-<name>` stack: the ECR repository, the CodeBuild project, its log group and two roles (the build role and the one the runtime assumes) | see [Pricing](../cost.md#coste-de-un-agente-en-agentcore) | only if you deploy with `rayito agent deploy --target agentcore`, which creates it by itself; it is not a `rayito stack deploy` component ([Deploy to AgentCore](../guias/desplegar-en-agentcore.md)). `RayitoAgent` (CDK) uses the same template as a nested stack |

The optional templates are deployed with `rayito stack deploy <component>`
(or `aws cloudformation deploy`), which needs
`cloudformation:CreateStack/UpdateStack/DescribeStacks/DeleteStack` and the
permissions of whatever the template creates
([Optional stacks](../funciones-opcionales/pilas-opcionales.md)).
`cloudformation:DescribeStackEvents` is optional: with it, the CLI prints the
reason of the first resource that failed when a `deploy` ends in
`ROLLBACK_*` (`OptionalStacks.failure_reason`); without it, the CLI does not
print the reason and the original error does not change.

!!! warning "A production service only launches sandboxes"
    Attach `SandboxLauncherPolicy` (output `SandboxLauncherPolicyArn`) to the
    role of a service or an agent that creates sandboxes, never
    `CallerPolicy`: with it, whoever compromises that service (an RCE or an
    SSRF in the agent's backend) could publish a backdoored version of
    `rayito-base` or of any template, which would run in all of the account's
    future sandboxes. Publishing images is the job of the maintainer or the
    release pipeline, with `ImagePublisherPolicy`.

## Permissions by feature { #permisos-por-funcion }

| Feature | Caller permissions | Where they are |
|---|---|---|
| Create, connect, pause, resume, list and kill sandboxes | `lambda:RunMicrovm`, `GetMicrovm`, `ListMicrovms`, `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`, `CreateMicrovmAuthToken`; the image reads the SDK uses to resolve names and sizes (`ListMicrovmImages`, `GetMicrovmImage`, `GetMicrovmImageVersion`, `ListMicrovmImageVersions`, `ListTags` and `ListManagedMicrovmImages`/`ListManagedMicrovmImageVersions`); `lambda:PassNetworkConnector` (managed connectors) | `SandboxLauncherPolicy` (or `CallerPolicy`) in `infra/iam.yaml` |
| Publish and prune images (`rayito image`) | `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `UpdateMicrovmImageVersion`, `DeleteMicrovmImageVersion`, `GetMicrovmImage*`, `ListMicrovmImage*`, `GetMicrovmImageBuild`, `ListMicrovmImageBuilds`, `TagResource`; `iam:PassRole` of the build role; `s3:PutObject`/`GetObject` on `<bucket>/rayito/*` and `s3:ListBucket` | `ImagePublisherPolicy` (or `CallerPolicy`) |
| Preparing the account (`rayito setup`, since 0.15.0) | administrator or *bootstrap* credentials, once: `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks` with `CAPABILITY_NAMED_IAM`, creating IAM roles and policies, publishing images and `sts:GetCallerIdentity`; only when it creates the bucket, the S3 permissions CloudFormation uses to create and protect it (`s3:CreateBucket`, `PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `PutBucketOwnershipControls`, `PutLifecycleConfiguration`, `PutBucketTagging`, `GetBucketPolicy`, `PutBucketPolicy`) and `s3:ListBucket` | not in the templates: it is the step before them ([Set up AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)). `CallerPolicy` is not enough |
| `rayito doctor` | the above plus `servicequotas:ListServiceQuotas` (in all three policies) and, optionally, `iam:SimulatePrincipalPolicy` and `iam:GetRole`; with `--efs-vpc-id`, `ec2:Describe*` | `CallerPolicy` (except the simulation, which is skipped if missing, and `ec2:Describe*`). With only `SandboxLauncherPolicy`, the simulation warns (`WARN`) about the missing publishing actions: that is expected |
| Run code, commands, files, PTY, git, metrics | none beyond that: they travel over the sandbox channel with its access token | — |
| Server-side deadline, `update_network`, class shapes | none beyond that | — |
| `execution_role_arn=` (credentials inside the sandbox) | `iam:PassRole` on that role | `SandboxLauncherPolicy` or `CallerPolicy` (for the template's `ExecutionRole`) |
| S3 URLs and large files (`transfer=`) | `s3:PutObject`, `GetObject`, `DeleteObject`, `AbortMultipartUpload` on `<bucket>/<prefix>/*` and `s3:ListBucket` scoped to the prefix | `SandboxLauncherPolicy` or `CallerPolicy` with `TransferBucket` and `TransferPrefix` |
| Persistence (`persist=`) | none on the caller; the **execution role** needs `s3:PutObject`, `GetObject`, `AbortMultipartUpload` and `ListBucket` on the prefix | `ExecutionRole` with `PersistenceBucket` and `PersistencePrefix` |
| Secrets: inject (`secrets=`) | `secretsmanager:GetSecretValue` and `DescribeSecret` on `secret:rayito/*`, except `rayito/webhooks/*` (the webhook signing secrets: denied, and the SDK rejects them) | `RayitoSecretsReader` |
| Secrets: CRUD (`SecretStore`) | additionally `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DeleteSecret` and `ListSecrets` | `RayitoSecretsAdmin` |
| Secrets with your own CMK | `kms:Decrypt` (and `kms:GenerateDataKey` to write), only through Secrets Manager | `KmsKeyArn` parameter of `infra/secrets-access.yaml` |
| Metadata index: create with `index=` | `dynamodb:PutItem` on the table | `RayitoIndexWriter` |
| Metadata index: list with `index=` | `dynamodb:BatchGetItem` on the table | `RayitoIndexReader` |
| Events: launch with `events=` | `secretsmanager:GetSecretValue` on the stack's key and `cloudformation:DescribeStacks` on the stack | `EventsLauncherPolicy` (`events-webhooks` stack) |
| Events: read (`get_events`) | `dynamodb:Query` on the event rows and the `gsi1` index, and `DescribeStacks` | `EventsReaderPolicy` |
| Events: webhooks (`register_webhook`, `list_webhooks`, `delete_webhook`) | `dynamodb:PutItem`/`DeleteItem`/`Query` only on `WEBHOOK` rows, and `DescribeStacks` | `EventsWebhookAdminPolicy` (operator-level: webhooks belong to the whole stack) |
| Templates (`Template.build`, `rayito template build`, `AgentTemplate`, `rayito agent template build`) | `lambda:CreateMicrovmImage`/`UpdateMicrovmImage` (never on `rayito-base*`), `iam:PassRole` of the build role, S3 on `<bucket>/rayito/templates/*` and read access to the build logs | `RayitoTemplateBuilder` (`infra/templates.yaml`) |
| Deploy to AgentCore (`--target agentcore`, since 0.15.0) | `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks`/`DeleteStack` on `rayito-agentcore-*` and what is needed to create what the template contains (ECR, CodeBuild, logs, two IAM roles, with `CAPABILITY_IAM` and `iam:PassRole`); `s3:ListBucket` and `s3:PutObject`/`DeleteObject` on `rayito/images/agentcore/*`; `codebuild:StartBuild`/`BatchGetBuilds`; `ecr:DescribeImages`; `bedrock-agentcore:CreateAgentRuntime`/`UpdateAgentRuntime`/`GetAgentRuntime`/`ListAgentRuntimes`/`DeleteAgentRuntime`/`InvokeAgentRuntime`/`StopRuntimeSession`; `logs:DeleteLogGroup` on `/aws/bedrock-agentcore/runtimes/*`; `sts:GetCallerIdentity`. It uses no `lambda:*` or secrets gateway ([guide](../guias/desplegar-en-agentcore.md#antes-de-empezar)) | not in the `infra/iam.yaml` templates: add it to your policy |
| CDK constructs (`RayitoAgent`, `RayitoMicrovmImage`, since 0.15.0) | whoever runs `cdk deploy` only assumes the CDK bootstrap roles; CloudFormation deploys with the bootstrap's execution role. With the default bootstrap nothing else is needed | if you restrict the bootstrap, the permissions in [Restricted bootstrap](../guias/desplegar-con-cdk.md#bootstrap-restringido) |
| Agent (`sbx.agent.run`/`.stream`) | none beyond that: it travels over the sandbox channel. The model credential arrives through the gateway (`gateways=`, `bedrock_gateway` presets...): `secretsmanager:GetSecretValue` on that secret, in the caller's credentials, not in the execution role. Bedrock permissions are those of the API key you store in the secret, not yours | `RayitoSecretsReader` ([Agent in the sandbox](../guias/agente-en-el-sandbox.md), [Secret gateway](../funciones-opcionales/pasarela-de-secretos.md)) |
| S3 mounts (`mounts=`), telemetry with `OtlpAuth.execution_role()`, EFS volumes | none on the caller except EFS (`elasticfilesystem:CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint` for `VolumeStore`, see [EFS volumes](../funciones-opcionales/volumenes-efs.md)); the **execution role** needs the stack's policy | `RayitoS3MountAccess`, `RayitoOtlpExport`, `RayitoEfsVolumeClient` |
| Limit the sizes that can be launched | a Deny on `RunMicrovm` outside the list and on publishing images | `RayitoRunAllowedSizes` (`sizes-guard` stack): only for the launcher, never the publisher |
| Fleet recipe (`deploy_agent()` saves it, `AgentFleet.connect(name)` reads it, `stop()` deletes it; since 0.17.0) | `ssm:PutParameter`, `ssm:GetParameter` and `ssm:DeleteParameter` on `arn:aws:ssm:<region>:<account>:parameter/rayito/agents/*`. No KMS permission: the `SecureString` uses the AWS managed key `aws/ssm`. Without them, deploy and reconnect continue with a `recipe_access_denied` warning ([Fleet recipe](../guias/flota-de-agentes.md#la-receta-de-la-flota)) | `AgentRecipe` statement: `CallerPolicy` all three; `SandboxLauncherPolicy` only `GetParameter` and `DeleteParameter`, and `ImagePublisherPolicy` only `PutParameter` (`AgentRecipeWrite` statement), because writing the recipe amounts to deploy rights (it decides the image, network, `env` and gateways of whoever connects by name) |
| Renewing the Bedrock key (`renew_gateway_keys`, since 0.17.0, optional) | `secretsmanager:DescribeSecret` and `secretsmanager:PutSecretValue` on the `bedrock` gateway's secret; to use the key, `bedrock:CallWithBearerToken` (condition `bedrock:bearerTokenType` = `SHORT_TERM`) and the model invocation permissions on the signing principal ([Secret gateway](../funciones-opcionales/pasarela-de-secretos.md#renovar-la-clave-de-bedrock)) | `RayitoSecretsAdmin` covers both Secrets Manager actions; `bedrock:CallWithBearerToken` is not in the templates: add it to your policy |
| Local proxy (`rayito sandbox proxy`) | `lambda:GetMicrovm` and `CreateMicrovmAuthToken` | `SandboxLauncherPolicy` or `CallerPolicy` |
| CloudWatch logs (`rayito sandbox logs`) | `logs:DescribeLogStreams` and `logs:GetLogEvents` on the `/rayito/<image>` group | not in the template: add it to your policy |

!!! note "The sandbox's execution role needs none of this"
    The SDK signs the transfers with your credentials and `rayd` never stores
    credentials. The execution role is only needed for persistence and to
    give your code credentials inside the sandbox; by default there is none.

## Checking it { #comprobarlo }

```bash
rayito doctor --template rayito-base
```

The `iam-simulation` check simulates the SDK and CLI actions. It is
indicative: the simulator marks `lambda:PassNetworkConnector` as
`implicitDeny` even with administrator permissions, and that warning blocks
nothing.

## See also { #ver-tambien }

- [Set up AWS](../primeros-pasos/configurar-aws.md)
- [Images](../images.md)
- [Security](../security.md)
- [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md)
