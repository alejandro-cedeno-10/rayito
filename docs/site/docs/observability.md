# Métricas y listado

M9 (`m9-sandbox-observability`) añade el historial de métricas de un
sandbox, los hechos del guest (CPU y memoria que ve) y un listado reanudable
con orden y filtros. Nada de esto necesita IAM nuevo: el historial viaja por
el mismo gRPC que el resto y el listado es `list-microvms`.

## Instantánea e historial

`get_metrics()` es una instantánea procfs (dos lecturas de `/proc/stat` a
100 ms). Desde M9 `rayd` además muestrea cada 5 s mientras el sandbox corre
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
  `mem_total_bytes`, `mem_cache_bytes` (M9), `disk_used_bytes`,
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
- En una imagen anterior a M9, `get_metrics_history()` (TS
  `getMetricsHistory()`), en instancia y en la forma de clase, lanza
  `UnimplementedError` (no es `SandboxException`) con el motivo «la imagen es
  anterior a M9 (rayd sin MetricsHistory): publica una imagen M9», como las
  transferencias y el plazo del servidor; el `UNIMPLEMENTED` de gRPC queda en
  `__cause__` (TS `cause`).

## Hechos del guest

`get_health()` trae `cpu_count` y `memory_total_bytes`, y `get_info()`
rellena `agent_version`, `cpu_count` y `memory_mb`: son la **vista del
guest**, que puede no coincidir con el tamaño de la imagen (el guest informó
8016 MiB y 4 CPU con una imagen de 2048 MiB, fila Q68 de
`AWS_API_NOTES.md`). En TypeScript: `agentVersion`, `cpuCount`, `memoryMb` y
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
| `metadata` | cliente, O(n) | una sonda `Health` por sandbox `RUNNING` (≈ 0,5-1 s cada una, cuenta como tráfico para su idle); nunca sondea un suspendido |
| `limit` (sólo `paginate`) | cliente | items por `next_items()`; todos si falta |

Un `next_token` sólo vale con los mismos filtros con los que se emitió; uno
manipulado o de otro filtro es `InvalidArgumentException`.

## Desde la CLI

```bash
rayito sandbox list --template rayito-base
rayito sandbox metrics microvm-<id> --token-file ~/.rayito/<id>.token
rayito --json sandbox metrics microvm-<id> --follow --interval 5 --token-file ~/.rayito/<id>.token
```

`metrics` imprime la instantánea de `get_metrics()` ([CLI](cli.md)); conectar
despierta un sandbox suspendido.

## En el shim de E2B

`sbx.get_metrics(start, end)` devuelve el historial como
`list[SandboxMetrics]` en el formato de E2B (con `mem_cache`);
`Sandbox.get_metrics(sandbox_id, access_token=...)` es la forma de clase, y
`Sandbox.list(query=SandboxQuery(state=..., started_after=..., template=...,
metadata=...), limit=, next_token=, order=)` devuelve un `SandboxPaginator`
([Compatibilidad con E2B](e2b-compat.md)).
