# Optional stacks (`rayito stack`)

Since 0.6.0 there is a single convention to deploy, query and delete the
optional infrastructure of the cost-bearing features (ADR-016):
`OptionalStacks` in the SDK and `rayito stack` in the CLI. No feature
deploys itself — it is always an explicit call of yours — and the SDK
itself hosts no server: everything lives in your account, as a
CloudFormation stack.

!!! info "Cost and activation"
    - **Default**: `rayito stack list` makes no AWS call at all
      (it is catalog metadata); `deploy`/`status`/`destroy` only act
      when you invoke them.
    - **Turns on**: `OptionalStacks().deploy("<component>")` or
      `rayito stack deploy <component>`.
    - **Resources and AWS calls**: CloudFormation `CreateStack`/`UpdateStack`/
      `DescribeStacks`/`DeleteStack` on the component's template
      (`AWS_API_NOTES.md` §21); each component also documents its own.
    - **Approximate cost**: that of the resources each component creates
      (`rayito stack list` prints it); CloudFormation itself does not charge.
    - **IAM**: `cloudformation:CreateStack/UpdateStack/DescribeStacks/
      DeleteStack` on the stack, plus whatever the template creates
      (`CAPABILITY_IAM` when it creates policies).
    - **How to turn it off**: `rayito stack destroy <component>` (or
      `OptionalStacks().destroy(...)`); each component says what it keeps.

## Today's components { #componentes-de-hoy }

| Component | What it creates | Idle cost | What it is for |
|---|---|---|---|
| `metadata-index` | DynamoDB table + 2 IAM policies (reader/writer) | $0 (on-demand, empty table) | [Metadata index](indice-de-metadatos.md) |
| `secrets-access` | 2 IAM policies over a Secrets Manager prefix | $0 | [Secrets](../secrets.md) |
| `s3-mounts` | 1 IAM policy over a bucket (and its prefixes) | $0 | [S3 mounts](montajes-s3.md) |
| `sizes-guard` | 1 IAM policy that denies `RunMicrovm` outside the listed images and publishing images | $0 | [Sizes](tamanos.md) |
| `events-webhooks` | HMAC secret, DynamoDB table with streams, 3 Lambdas with their log groups, subscription filter, scheduler, failure SQS queues, roles and 4 IAM policies | ≈ $0.40/month (the secret) | [Events and webhooks](eventos-y-webhooks.md) |
| `otlp-export` | 1 IAM policy (`cloudwatch:PutMetricData`) | $0 | [OTLP export](exportacion-otlp.md) |
| `templates` | 1 IAM policy for whoever builds templates | $0 | [Templates](templates.md) |
| `custom-domain` (experimental) | CloudFront distribution, routing CloudFront Function and KeyValueStore | $0 with no traffic | [Custom domain](dominio-propio.md) |
| `dashboard` | Lambda with Function URL (the page and the dashboard API), Cognito user pool with its client and domain, 2 DynamoDB tables (audit and cache), read role (and, with `AllowActions`, actions role), log group; no CloudFront or buckets | ≈ $0 (everything on demand) | [Dashboard in your account](panel-en-tu-cuenta.md) |
| `dashboard-cloudwatch` | 1 CloudWatch dashboard (`/rayito/*` logs, OTLP metrics and optional quota usage) | $0 with fewer than 4 dashboards | [Dashboard: CloudWatch, accounts and alerts](panel-extras.md#dashboard-de-cloudwatch) |
| `dashboard-read-role` | 1 read-only IAM role, in the target account of the multi-account view | $0 | [Dashboard: CloudWatch, accounts and alerts](panel-extras.md#varias-cuentas) |
| `quota-alerts` | Scheduled Lambda with its EventBridge rule, SNS topic, DynamoDB state table and role | ≈ $0.30/month (one metric) | [Dashboard: CloudWatch, accounts and alerts](panel-extras.md#avisos-de-cuota) |
| `efs-volumes` (experimental) | In an existing VPC: encrypted EFS file system, one mount target per subnet, 2 new security groups, a VPC egress connector, its role and the `RayitoEfsVolumeClient` policy | $0 with the file system empty | [EFS volumes in your VPC](volumenes-efs-vpc.md) |

`custom-domain` is experimental: the template has not yet been fully
deployed on real AWS (see [Custom domain](dominio-propio.md)). Besides
`rayito stack`, it has its own CLI, `rayito domain deploy|status|destroy`.

## Parameters of each component { #parametros-de-cada-componente }

They are passed with `parameters={...}` (TypeScript: `{ parameters: {...} }`)
or `--param K=V`. A parameter the component does not declare is
`InvalidArgumentException` before calling AWS.

| Component | Parameter | Default on create |
|---|---|---|
| `metadata-index` | `TableName`, `PointInTimeRecovery`, `DeletionProtection` | `rayito-sandboxes`, `false`, `false` |
| `secrets-access` | `SecretPrefix`, `KmsKeyArn` | `rayito/`, empty (no `kms:Decrypt`) |
| `s3-mounts` | `BucketName` (required), `Prefixes`, `ReadOnly` | —, `*` (the whole bucket), `true` |
| `sizes-guard` | `ImageArns` (required) | — |
| `events-webhooks` | `LogGroupName` (required), `ReconcilerIntervalMinutes`; `ArtifactBucket` is filled in by `artifact_bucket=`/`--artifact-bucket` | —, `5` |
| `otlp-export` | none | — |
| `templates` | `ArtifactBucketArn`, `BuildRoleArn` (required), `ImageLogGroupPrefix`, `BaseImageBucketArn`, `ProtectedImageNamePrefix` | —, —, `/rayito`, empty (the artifact bucket), `rayito-base` |
| `custom-domain` | `PublicDomain`, `CertificateArn` (required, ACM certificate in us-east-1), `AlternateDomainNames` | —, —, empty (wildcard alias `*.<PublicDomain>`) |
| `dashboard` | `ArtifactBucket` (filled in by `artifact_bucket=`/`--artifact-bucket`), `AllowActions`, `EnableCostExplorer`, `ImageNamePrefix`, `InitialUserEmail`, `DomainPrefix`, `ThemeJson`, `ThemeParameterName`, `LogoParameterName`, `ReservedConcurrency`, `LogRetentionDays` | —, `false`, `false`, `rayito-`, empty, empty, empty, empty, empty, empty, `30` |
| `dashboard-cloudwatch` | `DashboardName`, `LogGroupPrefix`, `IncludeOtlp`, `IncludeQuota` | empty (the stack name), `/rayito/`, `false`, `false` |
| `dashboard-read-role` | `TrustedPrincipalArn` (required), `ExternalId`, `MaxSessionSeconds` | —, empty, `3600` |
| `quota-alerts` | `ArtifactBucket` (filled in by `artifact_bucket=`/`--artifact-bucket`), `ScheduleExpression`, `WarningPercent`, `CriticalPercent`, `NotificationEmail`, `TopicArn`, `LogRetentionDays` | —, `rate(1 hour)`, `80`, `95`, empty, empty, `30` |
| `efs-volumes` | `VpcId`, `SubnetIds` (required, 1 to 3), `ConnectorName`, `AllowWrite`, `AccessPointArns`, `ReadOnlyAccessPointArns` | —, —, `rayito-efs`, `true`, empty (any access point of the file system), empty |

`stacks.components()` returns this same catalog (with each parameter's
description and the cost) without calling AWS.

## Redeploying does not undo the configuration { #redesplegar-no-deshace-la-configuracion }

`deploy` on a stack that already exists updates it. Parameters you do not
pass again **keep the value the stack is deployed with** (CloudFormation
`UsePreviousValue`); the catalog defaults only apply on creation, or to a new
parameter the stack does not have yet. So redeploying `s3-mounts` without
repeating `--param Prefixes=...` does not reopen the whole bucket, nor does
redeploying `metadata-index` without `TableName` replace the table.
`rayito stack deploy` prints which parameters change before asking for
confirmation, and `OptionalStacks().parameter_changes(...)` (TypeScript:
`parameterChanges(...)`) returns the same without deploying anything (just a
`DescribeStacks`). <small>Since 0.6.1; in 0.6.0, redeploying without
repeating a parameter reset it to its default value.</small>

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks(region="us-east-1")
    current = stacks.status("s3-mounts")
    if current is not None:
        print(current.state, current.parameters)  # {"BucketName": "...", "Prefixes": "runs/*", ...}
    for change in stacks.parameter_changes("s3-mounts", parameters={"ReadOnly": "false"}):
        print(f"{change.name}: {change.before} -> {change.after}")
    stacks.deploy("s3-mounts", parameters={"ReadOnly": "false"})  # Prefixes is kept
    ```

=== "TypeScript"

    ```ts
    import { OptionalStacks } from "rayito";

    const stacks = new OptionalStacks({ region: "us-east-1" });
    const current = await stacks.status("s3-mounts");
    console.log(current?.state, current?.parameters);
    const changes = await stacks.parameterChanges("s3-mounts", {
      parameters: { ReadOnly: "false" },
    });
    for (const change of changes) console.log(`${change.name}: ${change.before} -> ${change.after}`);
    await stacks.deploy("s3-mounts", { parameters: { ReadOnly: "false" } });
    ```

If a deployment ends in `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` or
`UPDATE_ROLLBACK_COMPLETE`, `rayito stack deploy` prints the reason of the
first resource that failed on stderr. From code, `OptionalStacks.failure_reason()`
(TypeScript: `failureReason()`) returns that text, already redacted. It needs
`cloudformation:DescribeStackEvents` and costs nothing (one read).

=== "Python"

    ```python
    from rayito import OptionalStacks, StackException

    stacks = OptionalStacks(region="us-east-1")
    try:
        stacks.deploy("s3-mounts", parameters={"BucketName": "amzn-s3-demo-bucket"})
    except StackException:
        print(stacks.failure_reason("s3-mounts"))  # "<resource> (CREATE_FAILED): <reason>"
        raise
    ```

=== "TypeScript"

    ```ts
    import { OptionalStacks, StackError } from "rayito";

    const stacks = new OptionalStacks({ region: "us-east-1" });
    try {
      await stacks.deploy("s3-mounts", { parameters: { BucketName: "amzn-s3-demo-bucket" } });
    } catch (error) {
      if (error instanceof StackError) {
        console.log(await stacks.failureReason("s3-mounts")); // "<resource> (CREATE_FAILED): <reason>"
      }
      throw error;
    }
    ```

## CLI { #cli }

```bash
rayito stack list
rayito stack deploy metadata-index --param TableName=rayito-sandboxes
rayito stack status metadata-index
rayito stack destroy metadata-index
```

`deploy` always prints the component's "Cost and activation" block and the
parameters that change, and asks for confirmation unless `--yes`; `destroy`
says what is kept.

## Python and TypeScript { #python-y-typescript }

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks(region="us-east-1")
    for component in stacks.components():  # pure metadata, no AWS call
        print(component.name, component.supported)
    status = stacks.deploy("metadata-index", parameters={"TableName": "rayito-sandboxes"})
    print(status.state, status.outputs)
    stacks.destroy("metadata-index")
    ```

    `AsyncOptionalStacks` has the same surface, in `async`/`await`.

=== "TypeScript"

    ```typescript
    import { OptionalStacks } from "rayito";

    const stacks = new OptionalStacks({ region: "us-east-1" });
    for (const component of stacks.components()) {
      console.log(component.name, component.supported);
    }
    const status = await stacks.deploy("metadata-index", {
      parameters: { TableName: "rayito-sandboxes" },
    });
    console.log(status.state, status.outputs);
    await stacks.destroy("metadata-index");
    ```

## How it behaves { #como-se-comporta }

- **Stack name**: `rayito-<component>` (`rayito-s3-mounts`), unless
  `stack_name=`/`stackName`/`--stack-name`. With different names you can
  deploy the same component several times, for example one `s3-mounts` stack
  per bucket.
- **Waiting**: `deploy()` and `destroy()` wait for the stack to settle
  (at most 600 s, `wait_timeout=`/`waitTimeoutMs`); with `wait=False`
  they return as soon as CloudFormation accepts the request.
- **Tags**: each stack carries `rayito:component`, `rayito:managed-by` and
  `rayito:sdk-version`, plus the ones you pass in `tags=`/`--tag K=V` (for
  organizations that require tags when creating resources). The three fixed
  ones always win.
- **A failed stack** (`ROLLBACK_COMPLETE`) cannot be updated:
  `deploy()` raises `StackException(code="blocked")`
  (TypeScript: `StackError`) without calling `UpdateStack`. Delete it with
  `destroy()` and deploy it again. To learn why it failed, use
  `failure_reason()` (above) or the `motivo:` line the CLI prints.
- **Lambda code**: a component with code (`events-webhooks`) uploads it
  to `rayito/stacks/<component>/<sha256>.zip` of
  `artifact_bucket=`/`--artifact-bucket`, which must be in your account (each
  call carries `ExpectedBucketOwner`). If the key already exists, the SDK
  compares the content: equal, it does not upload it again; different (a
  tampered object), it overwrites it and warns. It needs `s3:GetObject` and
  `s3:PutObject` on the bucket's `rayito/*` (those of `CallerPolicy` or
  `ImagePublisherPolicy`).
- **TypeScript**: `OptionalStacks` loads the optional peer
  `@aws-sdk/client-cloudformation` on the first AWS call
  (`pnpm add @aws-sdk/client-cloudformation`).
