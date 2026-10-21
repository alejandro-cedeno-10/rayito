# Optional

Features with their own cost on AWS, **off by default**: they are only
activated by passing the option or instantiating the class. Each docstring
carries its "Coste y activación" (cost and activation) block. Guides:
[Secrets](../../secrets.md),
[Metadata index](../../funciones-opcionales/indice-de-metadatos.md),
[S3 mounts](../../funciones-opcionales/montajes-s3.md),
[Sizes](../../funciones-opcionales/tamanos.md),
[Events and webhooks](../../funciones-opcionales/eventos-y-webhooks.md),
[OTLP export](../../funciones-opcionales/exportacion-otlp.md),
[Secret gateway](../../funciones-opcionales/pasarela-de-secretos.md),
[Declarative templates](../../funciones-opcionales/templates.md),
[Optional stacks](../../funciones-opcionales/pilas-opcionales.md),
[EFS volumes](../../funciones-opcionales/volumenes-efs.md) (experimental),
[Custom domain](../../funciones-opcionales/dominio-propio.md) (experimental),
[Management dashboard](../../funciones-opcionales/panel-de-gestion.md) and
[Optional features](../../optional-features.md).

## Optional stacks { #pilas-opcionales }

::: rayito.OptionalStacks

::: rayito.AsyncOptionalStacks

::: rayito.StackComponent

::: rayito.StackParameter

::: rayito.StackStatus

::: rayito.CostStatement

::: rayito.StackArtifact

## S3 mounts { #montajes-s3 }

::: rayito.S3Mount

::: rayito.MountStatus

## Events and webhooks { #eventos-y-webhooks }

::: rayito.LifecycleEvents

::: rayito.AsyncLifecycleEvents

::: rayito.WebhookInfo

::: rayito.EventRecord

## Declarative templates { #templates-declarativos }

::: rayito.Template

::: rayito.AsyncTemplate

::: rayito.BuildInfo

::: rayito.BuildHandle

::: rayito.BuildStatus

::: rayito.ReadyCommand

::: rayito.ReadyPoll

::: rayito.TemplateSpec

::: rayito.BaseImageRef

::: rayito.StartSpec

::: rayito.RunStep

::: rayito.CopyStep

::: rayito.EnvStep

::: rayito.WorkdirStep

::: rayito.UserStep

::: rayito.wait_for_port

::: rayito.wait_for_url

::: rayito.wait_for_process

::: rayito.wait_for_file

## Secrets { #secretos }

::: rayito.SecretStore

::: rayito.SecretCache

::: rayito.SecretRef

::: rayito.SecretInfo

::: rayito.SecretPage

## Metadata index { #indice-de-metadatos }

::: rayito.DynamoDbIndex

## Sizes { #tamanos }

::: rayito.SizeRequest

## Secret gateway { #pasarela-de-secretos }

::: rayito.SecretGateway

::: rayito.GatewayStatus

::: rayito.GatewayHandle

## OTLP export { #exportacion-otlp }

Guide: [OTLP export](../../funciones-opcionales/exportacion-otlp.md).

::: rayito.TelemetryExport

::: rayito.OtlpAuth

::: rayito.TelemetryHealth

## EFS volumes (experimental) { #volumenes-efs-experimental }

::: rayito.VolumeStore

::: rayito.AsyncVolumeStore

::: rayito.EfsVolume

::: rayito.VolumeStatus

::: rayito.EfsVolumes

::: rayito.AsyncEfsVolumes

::: rayito.EfsNetworkReport

::: rayito.NetworkFinding

## Custom domain (experimental) { #dominio-propio-experimental }

Guide: [Custom domain](../../funciones-opcionales/dominio-propio.md). Not yet
verified end to end on real AWS.

::: rayito.CustomDomain

::: rayito.AsyncCustomDomain

::: rayito.CustomDomainRoute

## Management dashboard { #panel-de-gestion }

::: rayito.Quotas

::: rayito.AsyncQuotas

::: rayito.QuotaReport

::: rayito.QuotaEntry

::: rayito.QuotaWarning

::: rayito.MemoryUsage

::: rayito.TpsLimit

::: rayito.UsageCost

::: rayito.AsyncUsageCost

::: rayito.BilledCost

::: rayito.BilledLine

::: rayito.CostEstimate

::: rayito.SandboxCostEstimate

::: rayito.Fleet

::: rayito.AsyncFleet

::: rayito.SandboxView

::: rayito.ImageSummary

::: rayito.ImageVersionSummary

::: rayito.BulkPlan

::: rayito.BulkProgress

::: rayito.BulkItemResult

::: rayito.BulkResult

## CDK constructs (`rayito.cdk`) { #cdk }

Since 0.15.0, behind the `cdk` extra
(`pip install "rayito[cdk]"`: `aws-cdk-lib` {{cdkAwsCdkLibMinVersion}} or later
and `constructs` {{cdkConstructsMinVersion}} or later). It is a separate
module: `import rayito` does not load it, and without the extra
`import rayito.cdk` fails with a message saying it is missing. The TypeScript
equivalents are in the [TypeScript reference](../typescript.md#cdk), and usage,
cost and errors in [Deploy with CDK](../../guias/desplegar-con-cdk.md).

::: rayito.cdk.RayitoAgent

::: rayito.cdk.AgentImageBuild

::: rayito.cdk.RayitoMicrovmImage

::: rayito.cdk.MicrovmImageArtifact

::: rayito.cdk.RayitoCdkException
