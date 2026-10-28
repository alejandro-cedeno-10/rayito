# Pilas opcionales (`rayito stack`)

Desde 0.6.0 hay un convenio único para desplegar, consultar y
borrar la infraestructura opcional de las funciones de coste (ADR-016):
`OptionalStacks` en el SDK y `rayito stack` en la CLI. Ninguna función se
despliega sola — siempre es una llamada explícita tuya — y el propio SDK no
hospeda ningún servidor: todo vive en tu cuenta, como pila de
CloudFormation.

!!! info "Coste y activación"
    - **Por defecto**: `rayito stack list` no hace ninguna llamada a AWS
      (es metadata del catálogo); `deploy`/`status`/`destroy` sólo actúan
      cuando los invocas tú.
    - **Activa**: `OptionalStacks().deploy("<componente>")` o
      `rayito stack deploy <componente>`.
    - **Recursos y llamadas AWS**: `CreateStack`/`UpdateStack`/
      `DescribeStacks`/`DeleteStack` de CloudFormation sobre la plantilla
      del componente (`AWS_API_NOTES.md` §21); cada componente documenta
      además los suyos propios.
    - **Coste aproximado**: el de los recursos que cree cada componente
      (`rayito stack list` lo imprime); CloudFormation en sí no cobra.
    - **IAM**: `cloudformation:CreateStack/UpdateStack/DescribeStacks/
      DeleteStack` sobre la pila, más lo que la plantilla cree
      (`CAPABILITY_IAM` cuando crea políticas).
    - **Cómo apagarla**: `rayito stack destroy <componente>` (o
      `OptionalStacks().destroy(...)`); cada componente dice qué conserva.

## Componentes de hoy

| Componente | Qué crea | Coste en reposo | Para qué |
|---|---|---|---|
| `metadata-index` | Tabla DynamoDB + 2 políticas IAM (lector/escritor) | $0 (on-demand, tabla vacía) | [Índice de metadatos](indice-de-metadatos.md) |
| `secrets-access` | 2 políticas IAM sobre un prefijo de Secrets Manager | $0 | [Secretos](../secrets.md) |
| `s3-mounts` | 1 política IAM sobre un bucket (y sus prefijos) | $0 | [Montajes S3](montajes-s3.md) |
| `sizes-guard` | 1 política IAM que niega `RunMicrovm` fuera de las imágenes listadas y publicar imágenes | $0 | [Tamaños](tamanos.md) |
| `events-webhooks` | Secreto HMAC, tabla DynamoDB con streams, 3 Lambdas con sus log groups, filtro de suscripción, scheduler, colas SQS de fallos, roles y 4 políticas IAM | ≈ $0,40/mes (el secreto) | [Eventos y webhooks](eventos-y-webhooks.md) |
| `otlp-export` | 1 política IAM (`cloudwatch:PutMetricData`) | $0 | [Exportación OTLP](exportacion-otlp.md) |
| `templates` | 1 política IAM para quien construye templates | $0 | [Templates](templates.md) |
| `custom-domain` (experimental) | Distribución CloudFront, CloudFront Function de enrutado y KeyValueStore | $0 sin tráfico | [Dominio propio](dominio-propio.md) |
| `dashboard` | Lambda con Function URL (la página y la API del panel), grupo de usuarios de Cognito con su cliente y dominio, 2 tablas DynamoDB (auditoría y caché), rol de lectura (y, con `AllowActions`, rol de acciones), log group; sin CloudFront ni buckets | ≈ $0 (todo bajo demanda) | [Panel en tu cuenta](panel-en-tu-cuenta.md) |
| `dashboard-cloudwatch` | 1 dashboard de CloudWatch (logs de `/rayito/*`, métricas OTLP y uso de cuota opcionales) | $0 con menos de 4 dashboards | [Panel: CloudWatch, cuentas y avisos](panel-extras.md#dashboard-de-cloudwatch) |
| `dashboard-read-role` | 1 rol de IAM de sólo lectura, en la cuenta destino de la vista multicuenta | $0 | [Panel: CloudWatch, cuentas y avisos](panel-extras.md#varias-cuentas) |
| `quota-alerts` | Lambda programada con su regla de EventBridge, tema SNS, tabla DynamoDB de estado y rol | ≈ $0,30/mes (una métrica) | [Panel: CloudWatch, cuentas y avisos](panel-extras.md#avisos-de-cuota) |
| `efs-volumes` (experimental) | En una VPC existente: sistema de ficheros EFS cifrado, un mount target por subred, 2 grupos de seguridad nuevos, un conector de egress a la VPC, su rol y la política `RayitoEfsVolumeClient` | $0 con el sistema de ficheros vacío | [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md) |

`custom-domain` es experimental: la plantilla aún no se ha desplegado
entera en AWS real (ver [Dominio propio](dominio-propio.md)). Además de
`rayito stack`, tiene su propia CLI, `rayito domain deploy|status|destroy`.

## Parámetros de cada componente

Se pasan con `parameters={...}` (TypeScript: `{ parameters: {...} }`) o
`--param K=V`. Un parámetro que el componente no declara es
`InvalidArgumentException` antes de llamar a AWS.

| Componente | Parámetro | Por defecto al crear |
|---|---|---|
| `metadata-index` | `TableName`, `PointInTimeRecovery`, `DeletionProtection` | `rayito-sandboxes`, `false`, `false` |
| `secrets-access` | `SecretPrefix`, `KmsKeyArn` | `rayito/`, vacío (sin `kms:Decrypt`) |
| `s3-mounts` | `BucketName` (obligatorio), `Prefixes`, `ReadOnly` | —, `*` (todo el bucket), `true` |
| `sizes-guard` | `ImageArns` (obligatorio) | — |
| `events-webhooks` | `LogGroupName` (obligatorio), `ReconcilerIntervalMinutes`; `ArtifactBucket` lo rellena `artifact_bucket=`/`--artifact-bucket` | —, `5` |
| `otlp-export` | ninguno | — |
| `templates` | `ArtifactBucketArn`, `BuildRoleArn` (obligatorios), `ImageLogGroupPrefix`, `BaseImageBucketArn`, `ProtectedImageNamePrefix` | —, —, `/rayito`, vacío (el bucket de artefactos), `rayito-base` |
| `custom-domain` | `PublicDomain`, `CertificateArn` (obligatorios, certificado ACM en us-east-1), `AlternateDomainNames` | —, —, vacío (alias comodín `*.<PublicDomain>`) |
| `dashboard` | `ArtifactBucket` (lo rellena `artifact_bucket=`/`--artifact-bucket`), `AllowActions`, `EnableCostExplorer`, `ImageNamePrefix`, `InitialUserEmail`, `DomainPrefix`, `ThemeJson`, `ThemeParameterName`, `LogoParameterName`, `ReservedConcurrency`, `LogRetentionDays` | —, `false`, `false`, `rayito-`, vacío, vacío, vacío, vacío, vacío, vacío, `30` |
| `dashboard-cloudwatch` | `DashboardName`, `LogGroupPrefix`, `IncludeOtlp`, `IncludeQuota` | vacío (el nombre de la pila), `/rayito/`, `false`, `false` |
| `dashboard-read-role` | `TrustedPrincipalArn` (obligatorio), `ExternalId`, `MaxSessionSeconds` | —, vacío, `3600` |
| `quota-alerts` | `ArtifactBucket` (lo rellena `artifact_bucket=`/`--artifact-bucket`), `ScheduleExpression`, `WarningPercent`, `CriticalPercent`, `NotificationEmail`, `TopicArn`, `LogRetentionDays` | —, `rate(1 hour)`, `80`, `95`, vacío, vacío, `30` |
| `efs-volumes` | `VpcId`, `SubnetIds` (obligatorios, de 1 a 3), `ConnectorName`, `AllowWrite`, `AccessPointArns`, `ReadOnlyAccessPointArns` | —, —, `rayito-efs`, `true`, vacío (cualquier access point del sistema de ficheros), vacío |

`stacks.components()` devuelve este mismo catálogo (con la descripción de
cada parámetro y el coste) sin llamar a AWS.

## Redesplegar no deshace la configuración

`deploy` sobre una pila que ya existe la actualiza. Los parámetros que no
vuelves a pasar **conservan el valor con el que está desplegada**
(CloudFormation `UsePreviousValue`); los valores por defecto del catálogo
sólo se aplican al crearla, o a un parámetro nuevo que la pila todavía no
tenga. Así, redesplegar `s3-mounts` sin repetir `--param Prefixes=...` no
vuelve a abrir todo el bucket, ni redesplegar `metadata-index` sin
`TableName` reemplaza la tabla. `rayito stack deploy` imprime qué
parámetros cambian antes de pedir confirmación, y
`OptionalStacks().parameter_changes(...)` (TypeScript:
`parameterChanges(...)`) devuelve lo mismo sin desplegar nada (sólo un
`DescribeStacks`). <small>Desde 0.6.1; en 0.6.0, redesplegar sin repetir un
parámetro lo devolvía a su valor por defecto.</small>

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks(region="us-east-1")
    current = stacks.status("s3-mounts")
    if current is not None:
        print(current.state, current.parameters)  # {"BucketName": "...", "Prefixes": "runs/*", ...}
    for change in stacks.parameter_changes("s3-mounts", parameters={"ReadOnly": "false"}):
        print(f"{change.name}: {change.before} -> {change.after}")
    stacks.deploy("s3-mounts", parameters={"ReadOnly": "false"})  # Prefixes se conserva
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

Si un despliegue termina en `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` o
`UPDATE_ROLLBACK_COMPLETE`, `rayito stack deploy` imprime en stderr el motivo
del primer recurso que falló. Desde código, `OptionalStacks.failure_reason()`
(TypeScript: `failureReason()`) devuelve ese texto, ya redactado. Necesita
`cloudformation:DescribeStackEvents` y no cuesta nada (una lectura).

=== "Python"

    ```python
    from rayito import OptionalStacks, StackException

    stacks = OptionalStacks(region="us-east-1")
    try:
        stacks.deploy("s3-mounts", parameters={"BucketName": "amzn-s3-demo-bucket"})
    except StackException:
        print(stacks.failure_reason("s3-mounts"))  # "<recurso> (CREATE_FAILED): <motivo>"
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
        console.log(await stacks.failureReason("s3-mounts")); // "<recurso> (CREATE_FAILED): <motivo>"
      }
      throw error;
    }
    ```

## CLI

```bash
rayito stack list
rayito stack deploy metadata-index --param TableName=rayito-sandboxes
rayito stack status metadata-index
rayito stack destroy metadata-index
```

`deploy` imprime siempre el bloque "Coste y activación" del componente y
los parámetros que cambian, y pide confirmación salvo `--yes`; `destroy`
dice qué se conserva.

## Python y TypeScript

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks(region="us-east-1")
    for component in stacks.components():  # metadata pura, sin llamar a AWS
        print(component.name, component.supported)
    status = stacks.deploy("metadata-index", parameters={"TableName": "rayito-sandboxes"})
    print(status.state, status.outputs)
    stacks.destroy("metadata-index")
    ```

    `AsyncOptionalStacks` tiene la misma superficie, en `async`/`await`.

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

## Cómo se comporta

- **Nombre de la pila**: `rayito-<componente>` (`rayito-s3-mounts`), salvo
  `stack_name=`/`stackName`/`--stack-name`. Con nombres distintos puedes
  desplegar el mismo componente varias veces, por ejemplo una pila
  `s3-mounts` por bucket.
- **Espera**: `deploy()` y `destroy()` esperan a que la pila se asiente
  (como mucho 600 s, `wait_timeout=`/`waitTimeoutMs`); con `wait=False`
  devuelven en cuanto CloudFormation acepta la petición.
- **Etiquetas**: cada pila lleva `rayito:component`, `rayito:managed-by` y
  `rayito:sdk-version`, más las que pases en `tags=`/`--tag K=V` (para
  organizaciones que exigen etiquetas al crear recursos). Las tres fijas
  siempre ganan.
- **Una pila fallida** (`ROLLBACK_COMPLETE`) no se puede actualizar:
  `deploy()` lanza `StackException(code="blocked")`
  (TypeScript: `StackError`) sin llamar a `UpdateStack`. Bórrala con
  `destroy()` y vuelve a desplegarla. Para saber por qué falló, mira
  `failure_reason()` (arriba) o el `motivo:` que imprime la CLI.
- **Código Lambda**: un componente con código (`events-webhooks`) lo sube
  a `rayito/stacks/<componente>/<sha256>.zip` de
  `artifact_bucket=`/`--artifact-bucket`, que debe ser de tu cuenta (cada
  llamada lleva `ExpectedBucketOwner`). Si la clave ya existe, el SDK
  compara el contenido: igual, no vuelve a subirlo; distinto (un objeto
  manipulado), lo sobrescribe y avisa. Necesita `s3:GetObject` y
  `s3:PutObject` sobre `rayito/*` del bucket (los de `CallerPolicy` o
  `ImagePublisherPolicy`).
- **TypeScript**: `OptionalStacks` carga el peer opcional
  `@aws-sdk/client-cloudformation` en la primera llamada a AWS
  (`pnpm add @aws-sdk/client-cloudformation`).
