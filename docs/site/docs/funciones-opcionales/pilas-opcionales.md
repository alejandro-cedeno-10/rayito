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

| Componente | Qué crea | Para qué |
|---|---|---|
| `metadata-index` | Tabla DynamoDB + 2 políticas IAM | [Índice de metadatos](indice-de-metadatos.md) |
| `secrets-access` | 2 políticas IAM | [Secretos](../secrets.md) |

Los otros siete (`s3-mounts`, `efs-volumes`, `sizes-guard`,
`events-webhooks`, `otlp-export`, `templates`, `custom-domain`) están
reservados para las funciones 0.6 de esta misma página de navegación;
`rayito stack list` ya los muestra, marcados como pendientes, y
`deploy`/`status`/`destroy` sobre ellos fallan con un error claro hasta que
su propia función los implemente.

## CLI

```bash
rayito stack list
rayito stack deploy metadata-index --param TableName=rayito-sandboxes
rayito stack status metadata-index
rayito stack destroy metadata-index
```

`deploy` imprime siempre el bloque "Coste y activación" del componente y
pide confirmación salvo `--yes`; `destroy` dice qué se conserva.

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
