# Métricas y listado

El historial de métricas de un sandbox (CPU, memoria, disco), los hechos del
guest (CPU y memoria que ve) y un listado reanudable con orden y filtros. Nada de esto necesita IAM nuevo: el historial viaja por
el mismo gRPC que el resto y el listado es `list-microvms`.

## Instantánea e historial

`get_metrics()` es una instantánea procfs (dos lecturas de `/proc/stat` a
100 ms). `rayd` además muestrea cada 5 s mientras el sandbox corre
y guarda un anillo de 8 h (5 760 muestras); `get_metrics_history(start=,
end=, max_points=)` lo devuelve en orden ascendente.

=== "Python"

    ```python
    from datetime import datetime, timedelta, timezone

    from rayito import Sandbox

    with Sandbox.create() as sbx:
        now = sbx.get_metrics()
        print(now.cpu_used_pct, now.mem_used_bytes, now.mem_cache_bytes, now.disk_used_bytes)

        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        for sample in sbx.get_metrics_history(start=since, max_points=60):
            print(sample.timestamp, sample.cpu_used_pct, sample.mem_used_bytes)

        # Desde otro proceso: la forma de clase necesita el access token
        series = Sandbox.get_metrics_history(sbx.sandbox_id, access_token=sbx.access_token)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            print((await sbx.get_metrics()).cpu_used_pct)
            series = await sbx.get_metrics_history(max_points=30)
            print(len(series))
            again = await AsyncSandbox.get_metrics_history(
                sbx.sandbox_id, access_token=sbx.access_token
            )
            print(len(again))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const now = await sbx.getMetrics();
    console.log(now.cpuUsedPct, now.memUsedBytes, now.memCacheBytes);

    const history = await sbx.getMetricsHistory({
      start: new Date(Date.now() - 600_000),
      maxPoints: 60,
    });
    for (const sample of history) {
      console.log(sample.timestamp.toISOString(), sample.cpuUsedPct);
    }
    const series = await Sandbox.getMetricsHistory(sbx.sandboxId, {
      accessToken: sbx.accessToken,
    });
    console.log(series.length);
    ```

- `SandboxMetrics`: `cpu_used_pct`, `cpu_count`, `mem_used_bytes`,
  `mem_total_bytes`, `mem_cache_bytes`, `disk_used_bytes`,
  `disk_total_bytes` y `timestamp`. En el historial, `cpu_used_pct` es la
  media desde la muestra anterior (≈ 5 s), no la ventana de 100 ms de la
  instantánea.
- `start` y `end` son inclusivos (un `datetime` naive es hora local);
  `max_points` reduce la serie a ese número de tramos (la última muestra de
  cada tramo con la CPU promediada). Una respuesta completa pesa ≈ 350 KB.
- **Hueco mientras está suspendido**: no se muestrea, así que la serie salta
  de la última muestra antes de la pausa a la primera tras el resume. El
  muestreo no toca la red y no cuenta como actividad para la política de
  idle.
- La forma de clase necesita el access token (`access_token=` o
  `RAYITO_ACCESS_TOKEN`) y **nunca despierta** un sandbox suspendido.
- En una imagen anterior a 0.3.0, `get_metrics_history()` (TS
  `getMetricsHistory()`), en instancia y en la forma de clase, lanza
  `UnimplementedError` (no es `SandboxException`) pidiendo que publiques una
  imagen nueva, como las
  transferencias y el plazo del servidor; el `UnimplementedError` genérico
  queda en `__cause__` (TS `cause`), y el `UNIMPLEMENTED` de gRPC un nivel más
  abajo, en `__cause__.__cause__` (TS `cause.cause`).

## Hechos del guest

`get_health()` trae `cpu_count` y `memory_total_bytes`, y `get_info()`
rellena `agent_version`, `cpu_count` y `memory_mb`: son la **vista del
guest**, que puede no coincidir con el tamaño de la imagen (el guest informó
8016 MiB y 4 CPU con una imagen de 2048 MiB). En TypeScript: `agentVersion`, `cpuCount`, `memoryMb` y
`memoryTotalBytes`.

## Listado reanudable

`Sandbox.list()` recorre `list-microvms` (páginas de 50) de forma perezosa.
`Sandbox.paginate()` da un paginador reanudable: `next_items()`,
`has_next` y `next_token`, un cursor **opaco** (lleva el `nextToken` de AWS
y el filtro) que puedes guardar y pasar a otro proceso.

=== "Python"

    ```python
    from datetime import datetime, timedelta, timezone

    from rayito import Sandbox

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    pages = Sandbox.paginate(limit=20, order="desc", started_after=since, states=["RUNNING"])
    first = pages.next_items()
    for item in first:
        print(item.sandbox_id, item.state, item.started_at, item.template_version)

    if pages.has_next:
        cursor = pages.next_token                     # guárdalo donde quieras
        more = Sandbox.paginate(limit=20, order="desc", started_after=since,
                                states=["RUNNING"], next_token=cursor).next_items()

    for item in Sandbox.list(template="rayito-base", order="asc"):
        print(item.sandbox_id)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        pages = AsyncSandbox.paginate(limit=20, order="desc")
        while pages.has_next:
            for item in await pages.next_items():
                print(item.sandbox_id, item.state)
        print(len(await AsyncSandbox.list(states=["RUNNING"])))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const pages = Sandbox.paginate({ limit: 20, order: "desc", states: ["RUNNING"] });
    const first = await pages.nextItems();
    console.log(first.map((item) => item.sandboxId));
    if (pages.hasNext) {
      const cursor = pages.nextToken;
      const more = await Sandbox.paginate({
        limit: 20,
        order: "desc",
        states: ["RUNNING"],
        nextToken: cursor,
      }).nextItems();
      console.log(more.length);
    }
    for await (const item of Sandbox.list({ template: "rayito-base" })) {
      console.log(item.sandboxId, item.state);
    }
    ```

| Filtro (Python / TS) | Dónde se aplica | Nota |
|---|---|---|
| `template`, `template_version` / `templateVersion` | servidor (`list-microvms`) | nombre o ARN de la imagen |
| `states` | cliente | por defecto todo menos `TERMINATING`/`TERMINATED` |
| `started_after` / `startedAfter` | cliente | incluido |
| `order="asc"` o `"desc"` | cliente, por `startedAt` | AWS no ordena: el primer item llega tras recorrer **todas** las páginas (O(páginas)); un token reanudado salta por identidad los items ya entregados |
| `metadata` | cliente, O(n) | una sonda `Health` por sandbox `RUNNING` (≈ 0,5-1 s cada una, cuenta como tráfico para su idle); nunca sondea un suspendido. Con `index=` (opcional, abajo): sin sondas y sobre cualquier estado no terminal |
| `index` (opcional) | tu tabla DynamoDB | `DynamoDbIndex(...)`: un `BatchGetItem` por página; ver [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md) |
| `limit` (sólo `paginate`) | cliente | items por `next_items()`; todos si falta |

Un `next_token` sólo vale con los mismos filtros con los que se emitió; uno
manipulado o de otro filtro es `InvalidArgumentException`.

## Listado por metadatos con índice (opcional)

Sin índice, `list(metadata=...)` sólo ve sandboxes `RUNNING`: los metadatos
viven en el agente y sondear uno suspendido lo despertaría. El **índice de
metadatos** (opcional, apagado por defecto, con coste de DynamoDB) guarda una
copia de los `metadata` de cada sandbox en una tabla de tu cuenta, así
puedes filtrar también los `SUSPENDED` sin sondear ni despertar ninguno.

```python
from rayito import DynamoDbIndex, Sandbox

idx = DynamoDbIndex("rayito-sandboxes")
paused = list(Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx))
```

Guía completa, reglas de la unión, coste e IAM:
[Índice de metadatos](funciones-opcionales/indice-de-metadatos.md).

## Desde la CLI

```bash
rayito sandbox list --template rayito-base
rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
rayito sandbox metrics microvm-<id> --token-file ~/.rayito/<id>.token
rayito --json sandbox metrics microvm-<id> --follow --interval 5 --token-file ~/.rayito/<id>.token
```

`metrics` imprime la instantánea de `get_metrics()` ([CLI](cli.md)); conectar
despierta un sandbox suspendido.

<a id="instalacion"></a><a id="ejemplo"></a><a id="nombres-de-span"></a><a id="atributos"></a><a id="que-no-incluye-m13b"></a>

## Trazas OpenTelemetry del SDK (opcional)

Spans del lado del SDK (`rayito.sandbox.create`, `rayito.commands.run`,
`rayito.code.run`, `rayito.files.*`…) sobre las llamadas que ya haces,
exportados a donde configures tu `TracerProvider`. Apagado por defecto: sin
`tracer_provider=` / `tracerProvider` no se importa OpenTelemetry. No es
telemetría del sandbox (para eso está el [historial de
métricas](#instantanea-e-historial)).

Guía completa, nombres de span, atributos y lo que nunca se registra:
[OpenTelemetry](funciones-opcionales/opentelemetry.md).

Enviar **desde el sandbox** las métricas del historial (CPU, memoria y
disco) a tu colector OTLP es otra función opcional, también apagada por
defecto: `create(telemetry=TelemetryExport(...))`, y
`sbx.get_telemetry_status()` (TypeScript: `getTelemetryStatus()`) cuenta lo
exportado y lo descartado. Ver
[Exportación OTLP](funciones-opcionales/exportacion-otlp.md).

## En el shim de E2B

`sbx.get_metrics(start, end)` devuelve el historial como
`list[SandboxMetrics]` en el formato de E2B (con `mem_cache`);
`Sandbox.get_metrics(sandbox_id, access_token=...)` es la forma de clase, y
`Sandbox.list(query=SandboxQuery(state=..., started_after=..., template=...,
metadata=...), limit=, next_token=, order=)` devuelve un `SandboxPaginator`
([Compatibilidad con E2B](e2b-compat.md)). Con la extensión `index=` (o
`E2B(index=...)`), `query.metadata` también filtra `state=[PAUSED]`
([Compatibilidad con E2B](e2b-compat.md#con-el-indice-de-metadatos-index-opcional)).
