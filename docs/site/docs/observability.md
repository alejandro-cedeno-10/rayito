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
  transferencias y el plazo del servidor; el `UnimplementedError` genérico
  queda en `__cause__` (TS `cause`), y el `UNIMPLEMENTED` de gRPC un nivel más
  abajo, en `__cause__.__cause__` (TS `cause.cause`).

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
| `metadata` | cliente, O(n) | una sonda `Health` por sandbox `RUNNING` (≈ 0,5-1 s cada una, cuenta como tráfico para su idle); nunca sondea un suspendido. Con `index=` (opcional, abajo): sin sondas y sobre cualquier estado no terminal |
| `index` (opcional, M14) | tu tabla DynamoDB | `DynamoDbIndex(...)`: un `BatchGetItem` por página; ver la sección siguiente |
| `limit` (sólo `paginate`) | cliente | items por `next_items()`; todos si falta |

Un `next_token` sólo vale con los mismos filtros con los que se emitió; uno
manipulado o de otro filtro es `InvalidArgumentException`.

## Listado por metadatos con índice (opcional)

Sin índice, `list(metadata=...)` sólo ve sandboxes `RUNNING`: los metadatos
viven en el agente y sondear uno suspendido lo despertaría. El **índice de
metadatos** (M14, apagado por defecto) guarda una copia inmutable de
`metadata` por sandbox en una tabla DynamoDB **de tu cuenta**, así puedes
filtrar también los `SUSPENDED` sin sondear ni despertar ninguno. Coste,
IAM y cómo apagarlo: [Funciones opcionales](optional-features.md#metadata-index).

1. Despliega la tabla una vez (`infra/metadata-index.yaml`: on-demand, TTL en
   `expires_at`, $0 en reposo) y da a tus credenciales las políticas
   `RayitoIndexWriter` (`dynamodb:PutItem`) y `RayitoIndexReader`
   (`dynamodb:BatchGetItem`) que crea la plantilla ([infra](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md)).
2. Crea los sandboxes con `index=`: tras `run-microvm`, y antes de esperar
   a `Health`, `create()` escribe la fila con `PutItem` condicional. Un pool
   la escribe al rellenar si su `PoolConfig` lleva `index=`.
3. Lista con `metadata=` e `index=`: por cada página de `list-microvms`, un
   `BatchGetItem` de los candidatos y la unión con sus filas.

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")             # reutilizable; no llama a AWS
    sbx = Sandbox.create(metadata={"user": "42"}, index=idx)
    sbx.pause()

    paused = list(Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx))
    pages = Sandbox.paginate(metadata={"user": "42"}, index=idx, limit=20)
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito";       // + npm install @aws-sdk/client-dynamodb

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    const sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state, item.metadata);
    }
    ```

Reglas de la unión (una fila nunca inventa un sandbox):

- El **estado** sale siempre de `list-microvms`; la fila sólo aporta los
  metadatos.
- Un item se queda si hay una fila con su mismo id, su mismo ARN de imagen y
  su mismo `startedAt` (±1 s), no caducada, y cuyos metadatos contienen cada
  par pedido. Un sandbox **sin fila** (creado sin `index=`, o con
  `on_write_failure='warn'` y una escritura fallida) se omite: sus metadatos
  son desconocidos y nunca se sondea.
- Con índice, `states` admite cualquier estado no terminal (por defecto
  todos); `TERMINATING`/`TERMINATED` son `InvalidArgumentException`.
- `index=` sin `metadata=` no cambia nada: el listado es el de siempre y no
  toca DynamoDB. El `next_token` de un listado con índice queda ligado a la
  tabla.
- `kill()` no borra la fila: el TTL (`expires_at` = `startedAt` + vida
  máxima + `ttl_margin_seconds`, 3600 por defecto) la borra gratis y, como
  DynamoDB puede tardar días, el SDK descarta al leer las filas vencidas.
- Si `PutItem` falla, `on_write_failure='terminate'` (por defecto; TS
  `onWriteFailure: "terminate"`) termina el VM recién lanzado (salvo
  `keep_on_failure=True`) y lanza `IndexWriteException` (TS
  `IndexWriteError`); `'warn'` avisa en el logger y devuelve el sandbox.
  Un `BatchGetItem` que falla, o claves que siguen sin procesar tras 5
  reintentos, es `SandboxIndexException` (TS `SandboxIndexError`), nunca una
  lista incompleta en silencio.

## Desde la CLI

```bash
rayito sandbox list --template rayito-base
rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
rayito sandbox metrics microvm-<id> --token-file ~/.rayito/<id>.token
rayito --json sandbox metrics microvm-<id> --follow --interval 5 --token-file ~/.rayito/<id>.token
```

`metrics` imprime la instantánea de `get_metrics()` ([CLI](cli.md)); conectar
despierta un sandbox suspendido.

## Trazas OpenTelemetry del SDK (opcional)

Spans del lado del SDK para las operaciones que ya haces (M13b, apagado por
defecto; coste e IAM: [Funciones opcionales](optional-features.md#otel-sdk)).
Nunca propagan `traceparent`/`tracestate` hacia `rayd` (el servidor no sabe
nada de esto) y no hay telemetría del sandbox: son spans sobre lo que hace
*tu proceso* al llamar al SDK, exportados a donde tú configures.

### Instalación

=== "Python"

    ```bash
    pip install 'rayito[otel]'
    ```

=== "TypeScript"

    ```bash
    npm install @opentelemetry/api
    ```

### Ejemplo

=== "Python"

    ```python
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    from rayito import Sandbox

    sbx = Sandbox.create(tracer_provider=trace.get_tracer_provider())
    sbx.commands.run("echo hola")       # span "rayito.commands.run"
    sbx.run_code("1 + 1")               # span "rayito.code.run"
    sbx.files.write("/tmp/x", "hola")   # spans "rayito.files.write" + "rayito.files.write_files"
    sbx.kill()                          # span "rayito.sandbox.kill"
    ```

=== "TypeScript"

    ```ts
    import { trace } from "@opentelemetry/api";
    import { BasicTracerProvider, ConsoleSpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
    import { Sandbox } from "rayito";

    const provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
    });
    trace.setGlobalTracerProvider(provider);

    const sbx = await Sandbox.create({ tracerProvider: provider });
    await sbx.commands.run("echo hola"); // span "rayito.commands.run"
    await sbx.runCode("1 + 1");          // span "rayito.code.run"
    await sbx.kill();                    // span "rayito.sandbox.kill"
    ```

Sin `tracer_provider=`/`tracerProvider` (el valor por defecto) no se importa
`opentelemetry`/`@opentelemetry/api` en tiempo de ejecución y no se crea
ningún span: el único coste por llamada es el pequeño objeto de atributos
que cada método construye antes de pasar por el camino no-op (y, en
TypeScript, el cierre `async` que envuelve la operación).

### Nombres de span

| Span | Dónde |
|---|---|
| `rayito.sandbox.create` | `create()` (envuelve `run-microvm`, la espera de `Health`, el índice y la red) |
| `rayito.sandbox.connect` | `connect()`, ambas formas |
| `rayito.sandbox.kill` | `kill()`, instancia y clase/estático |
| `rayito.sandbox.pause` | `pause()`, instancia y clase/estático |
| `rayito.sandbox.resume` | `resume()`, instancia y clase/estático |
| `rayito.code.run` | `run_code()`/`runCode()` |
| `rayito.commands.run` | `commands.run()` (en segundo plano, el span se cierra en cuanto `start` devuelve el handle, no cuando el proceso termina) |
| `rayito.files.read`, `write`, `write_files`, `list`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | `files.*` |

Todos son `SpanKind.CLIENT`.

### Atributos

`ALLOWED_SPAN_ATTRIBUTES` es una **lista cerrada de claves permitidas**
(cualquier otra clave es un error antes de abrir el span):
`rayito.sandbox.id`, `rayito.region`, `rayito.template.name`,
`rayito.resume_generation`, `rayito.operation`,
`rayito.commands.exit_code`, `rayito.commands.background`,
`rayito.code.language`, `rayito.files.operation`, `rayito.files.count`,
`rayito.files.bytes`, `rayito.error.type`. Que una clave esté permitida no
significa que algún span la lleve: hoy `rayito.template.name`,
`rayito.resume_generation` y `rayito.error.type` están reservadas y **ningún
span las emite** (el tipo de error va en el estado del span y en el evento
`exception`, no como atributo). Lo que cada span lleva de verdad:

| Span | Atributos |
|---|---|
| `rayito.sandbox.create` | `rayito.region`, `rayito.operation`, y `rayito.sandbox.id` en cuanto `run-microvm` responde |
| `rayito.sandbox.connect`, `kill`, `pause`, `resume` | `rayito.sandbox.id`, `rayito.operation` |
| `rayito.commands.run` | `rayito.commands.background`; en foreground, `rayito.commands.exit_code` al terminar (también si sale con código distinto de cero) |
| `rayito.code.run` | `rayito.code.language` cuando se conoce el lenguaje |
| `rayito.files.*` | `rayito.files.operation`; `read`/`write` añaden `rayito.files.bytes`; `write_files` añade `rayito.files.count` y `rayito.files.bytes`; `list` añade `rayito.files.count` |

**Nunca se registra**: el texto de un comando o su `cmd`/args, código
fuente, rutas de ficheros, valores de `envs`, nombres o valores de
secretos, valores de `metadata`, el access token, el JWE, ni URLs
prefirmadas. Un error dentro de un span registra la excepción
(`record_exception`/`recordException`) y pone el estado en `ERROR`, pero el
único dato que entra es el **nombre de la clase** de la excepción: ni su
mensaje ni su traza (que podrían llevar cualquiera de los datos de arriba).

### Qué no incluye (M13b)

- **No propaga** `traceparent`/`tracestate` hacia `rayd`: un span de rayito
  nunca es el padre de nada dentro del sandbox.
- **No hay telemetría del sandbox**: ni métricas ni logs del propio
  MicroVM viajan por aquí (ver [Métricas](#instantanea-e-historial) para
  eso).
- El **shim de E2B** (`rayito.e2b`/`rayito/e2b`) no está instrumentado.
- La exportación de los spans (consola, OTLP, Jaeger, X-Ray...) la configura
  tu `tracer_provider`/`TracerProvider`, no rayito.

## En el shim de E2B

`sbx.get_metrics(start, end)` devuelve el historial como
`list[SandboxMetrics]` en el formato de E2B (con `mem_cache`);
`Sandbox.get_metrics(sandbox_id, access_token=...)` es la forma de clase, y
`Sandbox.list(query=SandboxQuery(state=..., started_after=..., template=...,
metadata=...), limit=, next_token=, order=)` devuelve un `SandboxPaginator`
([Compatibilidad con E2B](e2b-compat.md)). Con la extensión `index=` (o
`E2B(index=...)`), `query.metadata` también filtra `state=[PAUSED]`
([Compatibilidad con E2B](e2b-compat.md#con-el-indice-de-metadatos-index-opcional-m14)).
