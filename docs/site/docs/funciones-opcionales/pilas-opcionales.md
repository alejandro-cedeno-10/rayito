# Pilas opcionales (`rayito stack`)

M15 (Rayito 0.6) introduce un convenio único para desplegar, consultar y
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
| `sizes-guard` | 1 política IAM que niega `RunMicrovm` fuera de las imágenes listadas | $0 | [Tamaños](tamanos.md) |
| `events-webhooks` | Secreto HMAC, tabla DynamoDB con streams, 3 Lambdas, filtro de suscripción, scheduler, cola SQS de fallos, roles IAM | ≈ $0,40/mes (el secreto) | [Eventos y webhooks](eventos-y-webhooks.md) |
| `otlp-export` | 1 política IAM (`cloudwatch:PutMetricData`) | $0 | [Exportación OTLP](exportacion-otlp.md) |
| `templates` | 1 política IAM para quien construye templates | $0 | [Templates](templates.md) |
| `efs-volumes` (experimental) | En una VPC existente: sistema de ficheros EFS cifrado, un mount target por subred, 2 grupos de seguridad nuevos, un conector de egress a la VPC, su rol y la política `RayitoEfsVolumeClient` | $0 con el sistema de ficheros vacío | [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md) |

Sólo `custom-domain` sigue pendiente: `rayito stack list` ya lo muestra
(con `supported` a `false`) y `deploy`/`status`/`destroy` sobre él fallan
con un error claro hasta que su propia función lo implemente ([Dominio
propio](dominio-propio.md)).

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
`parameterChanges(...)`) devuelve lo mismo sin desplegar nada.

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
