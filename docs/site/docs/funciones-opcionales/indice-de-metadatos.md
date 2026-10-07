# Índice de metadatos

Una tabla DynamoDB **en tu cuenta** con una copia de los `metadata` de cada
sandbox, para filtrar el listado por metadatos también sobre sandboxes en
pausa, sin despertarlos y sin sondear uno a uno.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `index=` (TypeScript: `index`) el SDK no
      construye ningún cliente de DynamoDB ni hace ninguna llamada.
    - **Activa**: `index=DynamoDbIndex("rayito-sandboxes")` en
      `Sandbox.create()`, `Sandbox.list()`, `Sandbox.paginate()`,
      `PoolConfig`, el shim de E2B (`Sandbox.list(..., index=)`,
      `E2B(index=)`) y la CLI (`rayito sandbox list --index-table`).
    - **Recursos y llamadas AWS**: la tabla la despliegas tú
      (`rayito stack deploy metadata-index`, plantilla
      `infra/metadata-index.yaml`, on-demand, TTL en `expires_at`).
      `dynamodb:PutItem` una vez por sandbox creado y `dynamodb:BatchGetItem`
      una vez por página de `list-microvms` al listar con `metadata` e
      `index`. Nunca `DeleteItem`: el TTL borra las filas gratis.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30,
      [precios de DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/)):
      $0,625 por millón de WRU (≈ 1 por `create`) y $0,125 por millón de RRU
      (0,5 por sandbox candidato al listar), más $0,25/GB-mes tras 25 GB
      gratis. 10 000 sandboxes al mes con un listado diario ≈ $0,03/mes. Una
      tabla vacía cuesta $0.
    - **IAM** (credenciales de quien llama al SDK, no el rol del sandbox):
      `dynamodb:PutItem` (política `RayitoIndexWriter`) y
      `dynamodb:BatchGetItem` (política `RayitoIndexReader`) sobre el ARN de
      la tabla.
    - **Cómo apagarla**: deja de pasar `index=`; para dejar de pagar el
      almacenamiento, borra la pila: `rayito stack destroy metadata-index`
      (borra la tabla y sus dos políticas).

!!! warning "Pendiente de aceptación en AWS real"
    El índice está implementado y probado con DynamoDB simulado en los dos
    SDK. Su prueba contra AWS real (que el `startedAt` de `run-microvm` y el
    de `list-microvms` coinciden y que listar no despierta a nadie) todavía no
    se ha ejecutado.

## Cuándo usarlo

- Filtras por metadatos (`user`, `run`, `tenant`) una flota de decenas o
  cientos de sandboxes: sin índice, cada sandbox `RUNNING` cuesta una sonda
  de ≈ 0,5–1 s.
- Necesitas encontrar sandboxes **suspendidos** por sus metadatos: sin
  índice, `list(metadata=)` nunca los ve, porque sondearlos los despertaría.
- **Cuándo no**: pocos sandboxes y siempre `RUNNING`; `list(metadata=)` sin
  índice basta y no cuesta nada.

## Ejemplo rápido

Despliega la tabla una vez (componente `metadata-index` de
[`rayito stack`](pilas-opcionales.md); imprime el coste y pide
confirmación):

```bash
rayito stack deploy metadata-index --param TableName=rayito-sandboxes
rayito stack status metadata-index   # salidas: la tabla y las políticas lector/escritor
```

Desde el SDK, `OptionalStacks().deploy("metadata-index")` hace lo mismo. La
plantilla es `infra/metadata-index.yaml`, por si prefieres desplegarla con
CloudFormation a mano.

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")  # (1)!
    with Sandbox.create(metadata={"user": "42"}, index=idx) as sbx:  # (2)!
        sbx.pause()
        for item in Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx):
            print(item.sandbox_id, item.state, item.metadata)  # (3)!
    ```

    1. Construirlo no llama a AWS: el cliente de DynamoDB se crea en su
       primer uso.
    2. Un `PutItem` condicional tras `run-microvm`.
    3. Un `BatchGetItem` por página de `list-microvms`; ninguna sonda.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, DynamoDbIndex


    async def main() -> None:
        idx = DynamoDbIndex("rayito-sandboxes")
        async with await AsyncSandbox.create(metadata={"user": "42"}, index=idx) as sbx:
            await sbx.pause()
            paused = await AsyncSandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx)
            print([item.sandbox_id for item in paused])


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito"; // + npm i @aws-sdk/client-dynamodb

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    await using sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state, item.metadata);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
    ```

## Cómo funciona

1. `create(index=)` escribe una fila inmutable (`metadata`, imagen,
   `startedAt`, TTL) tras `run-microvm`, antes de esperar al agente. Un pool
   la escribe al rellenar si su `PoolConfig` lleva `index=`.
2. `list(metadata=, index=)` recorre `list-microvms` y, por cada página,
   lee las filas de los candidatos con un `BatchGetItem` y las une.

Reglas de la unión (una fila nunca inventa un sandbox):

- El **estado** sale siempre de `list-microvms`; la fila sólo aporta los
  metadatos.
- Un sandbox aparece si su fila tiene el mismo id, el mismo ARN de imagen y
  el mismo `startedAt` (±1 s), no ha caducado y sus metadatos contienen cada
  par pedido.
- Un sandbox **sin fila** (creado sin `index=`) se omite: sus metadatos son
  desconocidos y nunca se sondea.
- Con índice, `states` admite cualquier estado no terminal (por defecto
  todos); `TERMINATING`/`TERMINATED` son `InvalidArgumentException`.
- `index=` sin `metadata=` no cambia nada y no toca DynamoDB.
- `kill()` no borra la fila: el TTL (`startedAt` + vida máxima +
  `ttl_margin_seconds`, 3600 por defecto) la borra gratis, y el SDK descarta
  al leer las filas vencidas que DynamoDB aún no borró.

## Opciones de `DynamoDbIndex`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `table_name` (1.er argumento) | `tableName` | — | nombre de la tabla |
| `region` | `region` | la de la sesión | región de la tabla |
| `session` | `credentials` | la sesión por defecto | credenciales de AWS |
| `on_write_failure` | `onWriteFailure` | `"terminate"` | si `PutItem` falla: `"terminate"` mata el sandbox recién lanzado y lanza `IndexWriteException`; `"warn"` avisa y devuelve el sandbox |
| `ttl_margin_seconds` | `ttlMarginSeconds` | 3600 | margen del TTL sobre la vida máxima |

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `IndexWriteException` | `IndexWriteError` | `PutItem` falló al crear (permiso, tabla inexistente) con `on_write_failure="terminate"` | revisa `RayitoIndexWriter` y el nombre de la tabla |
| `SandboxIndexException` | `SandboxIndexError` | `BatchGetItem` falló, o quedaron claves sin procesar tras 5 reintentos | revisa `RayitoIndexReader`; nunca devuelve una lista incompleta en silencio |
| `InvalidArgumentException` | `InvalidArgumentError` | `states` con `TERMINATING`/`TERMINATED`; falta el peer `@aws-sdk/client-dynamodb` en TypeScript | corrige el filtro; `npm i @aws-sdk/client-dynamodb` |

## Diferencias con E2B

E2B filtra por metadatos en su servidor. Rayito no tiene servidor: sin
índice, el filtro sondea cada sandbox; con índice, la tabla vive en tu
cuenta. En el shim, `Sandbox.list(query=SandboxQuery(metadata=...,
state=[SandboxState.PAUSED]), index=idx)` usa el mismo índice; sólo
aparecen los sandboxes creados con el `create(index=)` del SDK nativo o un
`PoolConfig(index=)`.

## Ver también

- [Funciones opcionales](../optional-features.md#metadata-index)
- [Métricas y listado](../observability.md#listado-reanudable)
- [IAM](../operacion/iam.md)
- Plantilla: [`infra/metadata-index.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/metadata-index.yaml)

??? info "Fuentes y mediciones"
    - Contrato de DynamoDB y la medida pendiente: `AWS_API_NOTES.md` §20
      (IDX-1), en [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Despliegue y borrado: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Decisión de diseño (opcional, en tu cuenta, sin servidor): ADR-014 en
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
