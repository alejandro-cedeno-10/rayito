# Conceptos

## Qué corre dónde

Un sandbox son dos mundos: tu proceso, donde vive el SDK, y un MicroVM de
Lambda en tu cuenta, donde vive todo lo demás. Dentro del MicroVM corren
estos procesos, con este usuario y hablando por estos transportes:

```
AWS Lambda MicroVM (Firecracker, ARM64)
│
├─ rayd (Rust, estático musl, root)
│    :8080  gRPC h2c  ── lo llama el SDK a través del proxy de AWS
│    :9000  HTTP/1.1  ── lo llama sólo Lambda (hooks /ready /run /suspend /resume /terminate)
│    │
│    └─ hijo: kernel-sidecar (Python 3.12, uid 1000)
│         stdin/stdout JSON lines  ◄──►  rayd
│         │
│         └─ hijos: ipykernel × contexto (Python, uid 1000)
│              ZMQ ipc:// bajo /run/rayito/k/<contexto>/
│              ►► aquí corre el código del usuario (run_code)
│
└─ procesos y PTYs de commands.run / pty.create (uid 1000, hijos directos de rayd)
```

| Pieza | Lenguaje | Dónde corre | Quién la usa |
|---|---|---|---|
| `rayd` | Rust | dentro del MicroVM, como root, un proceso por VM | el SDK (gRPC :8080) y Lambda (hooks :9000) |
| kernel-sidecar | Python 3.12 | dentro del MicroVM, hijo de `rayd`, uid 1000 | sólo `rayd` (JSON lines por stdio) |
| ipykernel (uno por contexto) | Python | dentro del MicroVM, hijo del sidecar, uid 1000; **aquí corre el código del usuario** | el sidecar (`jupyter_client`, ZMQ `ipc://`) |
| procesos y PTYs (`commands`, `pty`) | lo que el usuario lance | dentro del MicroVM, hijos de `rayd`, uid 1000 | el SDK vía `ProcessService` / `PtyService` |
| SDK Python (`rayito`) | Python ≥ 3.11 | **fuera** del MicroVM, en el proceso del cliente (tu app, tu agente, tu CI) | tu código |
| SDK TypeScript (`rayito`) | TypeScript / Node ≥ 20 | **fuera**, en el proceso del cliente | tu código |
| llamadas al plano de control | boto3 / AWS SDK JS v3 dentro del SDK | **fuera**, desde el proceso del cliente hacia la API `lambda-microvms` | el SDK (`create`, `list`, `pause`, `resume`, `kill`, tokens) |

El sidecar es Python por decisión escrita: el
kernel es Python de todos modos y `jupyter_client` es el cliente probado del
protocolo de Jupyter, así que lo que cuesta esfuerzo (formatters, gráficos,
warm-up, supervisión, reseed) queda en el mismo lenguaje. El salto por stdio
es despreciable frente a ejecutar una celda y el `.proto` no depende del
backend, así que la decisión es reversible sin tocar clientes. Consolidarlo
en Rust con `jupyter-zmq-client` sigue siendo una opción evaluada, no un
objetivo.

Si esta página y `ARCHITECTURE.md` difieren, manda `ARCHITECTURE.md`.

## Desde otros lenguajes

Los SDK oficiales son Python y TypeScript. `rayd` es un servidor gRPC
generado desde `proto/rayito/v1/`, así que cualquier lenguaje con cliente
gRPC puede hablar con él: qué hace falta, en
[Otros lenguajes (gRPC)](referencia/otros-lenguajes.md).

## Plazo, tope e idle

Tres relojes distintos gobiernan la vida de un sandbox:

| | Quién lo impone | ¿Se mueve? | Qué pasa al vencer |
|---|---|---|---|
| **Plazo lógico** (`timeout`) | `rayd`, dentro del VM, aunque tu proceso muera | sí: `set_timeout()` (exacto, puede acortarlo) y `connect(timeout=)` (sólo alarga) | `on_timeout='kill'`: `rayd` sale y la VM pasa a `TERMINATED` ≈ 15 s después; `on_timeout='pause'`: se suspende |
| **Tope** (`max_lifetime` = `maximumDurationInSeconds`) | la plataforma | no: no existe `UpdateMicrovm` | la VM termina, esté corriendo o suspendida |
| **Idle** (`idle=IdlePolicy(...)`) | la plataforma | no | suspende tras `max_idle_seconds` sin tráfico por el endpoint |

- Con `max_lifetime` u `on_timeout` en `create()` (exige una imagen 0.3.0 o
  posterior), `timeout` es el plazo lógico y `max_lifetime` (120–28 800 s, por
  defecto `timeout + 60`) el tope; el plazo nunca pasa de `max_lifetime − 60 s`
  desde el arranque. `get_info().expires_at` es el plazo lógico.
- Sin ninguno de los dos, `timeout` es la vida máxima (running +
  suspendido, tope 8 h) y no se mueve.
- El tope **cuenta el tiempo suspendido**: un sandbox pausado sigue muriendo
  a las 8 h desde su arranque. En modo `pause` los procesos siguen corriendo
  entre el vencimiento y la suspensión (≈ 3 s con un cliente vivo, hasta
  `max_idle` sin él), y la política de idle puede suspender antes del plazo.
  Una suspensión real de menos de 2 s que cruza el plazo no se reconoce como
  tal.
- Cómo usarlo, en Python (sync y async) y TypeScript:
  [Plazo del servidor](lifecycle.md).
- Para seguir más allá de `max_lifetime` con los mismos ficheros,
  `sbx.reincarnate()` guarda el `HOME` en S3 y lo restaura en un sandbox nuevo
  ([Persistencia](persistence.md)); las variables del kernel, los procesos y
  las PTY no sobreviven.

La política de idle (`create(idle=IdlePolicy(...))`, 300 s por defecto)
suspende el MicroVM cuando no recibe tráfico y lo reanuda con la siguiente
llamada (`auto_resume=True`). Un ciclo suspend/resume cuesta ≈ $0,0049 a
2 GB, lo mismo que ≈ 140 s de cómputo, así que un `max_idle_seconds` por
debajo de ≈ 150 s nunca ahorra dinero ([Costes](cost.md#precios)).
`idle=None` desactiva la auto-suspensión. Guía: [Pausar y
reanudar](guias/pausar-reanudar.md).

`pause()` y `resume()` hacen lo mismo a mano: procesos, PTYs, watches y las
variables del kernel siguen vivos al otro lado del snapshot.

## Dos tokens

| Token | Quién lo emite | Para qué | Vida |
|---|---|---|---|
| JWE del proxy (`x-aws-proxy-auth`) | `create-microvm-auth-token` (AWS) | atravesar el proxy de AWS hasta un puerto del MicroVM | 60 min; el SDK lo renueva a los 45 |
| Access token (`x-access-token`) | el SDK, en `create()` | autenticar cada RPC ante `rayd`; sólo su sha256 viaja en `runHookPayload` | la vida del sandbox |

`Health` es el único RPC sin `x-access-token`: es la sonda de readiness y la
que lee los metadatos. Guarda `sbx.access_token` junto al `sandbox_id` para
poder hacer `connect()` después. `RAYITO_ACCESS_TOKEN` existe, pero la leen
las dos llamadas: si la exportas, cada `create()` de ese proceso reutiliza el
mismo secreto y una fuga abre todos sus sandboxes, no uno (por defecto
`create()` genera 32 bytes frescos por sandbox). Úsala para `connect()` desde
otro proceso; para fijar el secreto de un sandbox concreto, pásalo con
`access_token=` / `accessToken`.

## Dos canales por sandbox

Un MicroVM de 1 vCPU admite 8 conexiones concurrentes por el proxy. El SDK usa
como máximo dos canales HTTP/2: uno para unarios y comandos en foreground y
otro, abierto perezosamente, para streams largos (comandos en background,
PTYs, `watch_dir`). Rotar el JWE no reconstruye canales ni corta streams.

## Streams y reconexión

Los comandos, las PTYs, `watch_dir` y `run_code` son server-streams. Cuando un
`pause()`, el auto-resume, un 502 del proxy o un `RST_STREAM` del proxy antes
del plazo real (`CANCELLED "Stream removed"` en grpcio, `Canceled` con
`http/2 stream closed` en Connect-ES) los corta, cada handle se reengancha
por su cuenta la próxima vez que se lee (`Connect(from_seq)`, `Pty.Connect`,
`WatchDir` de nuevo, `Reattach` para una celda en curso), y un unario cortado
se reintenta una vez. Un handle que el propio cliente cancela (`disconnect()`,
un `AbortSignal`) nunca reconecta, aunque el corte llegara justo después con
la forma de un reset. `reconnect_timeout` (60 s) acota cuánto espera el SDK
al agente una vez que el MicroVM vuelve a `RUNNING`; mientras está
suspendido, leer un handle en background **no** lo despierta.

`get_health().resume_generation` cuenta los `/resume` aceptados;
`kernel_state_lost` avisa si un kernel no sobrevivió al resume.

## Contextos de código

`run_code` ejecuta en un kernel Jupyter con estado (el contexto `default`).
`create_code_context(cwd=...)` arranca otro kernel con su propio scope
(máximo 8 por sandbox). Un error del kernel es dato (`execution.error`), nunca
excepción; un timeout termina en `error.name == "ExecutionTimeout"`.

## PTY

`sbx.pty.create(size=PtySize(cols, rows))` abre una terminal real con el shell
de login del usuario (uid 1000). El `PtyHandle` es un `CommandHandle` que
entrega bytes crudos; `send_input`, `resize` y `kill` son unarios.

## Metadatos

`create(metadata={...})` viaja en el único canal por MicroVM (`runHookPayload`,
4096 caracteres junto a `envs`) y `rayd` lo devuelve en `Health`. Es inmutable,
no es secreto (cualquier principal que pueda acuñar un JWE para el sandbox lo
lee) y `Sandbox.list(metadata=...)` lo filtra en cliente sondeando cada
sandbox `RUNNING`: O(n), ≈ 0,5-1 s por sandbox.

## Historial de métricas

`get_metrics()` es una instantánea procfs (dos lecturas de `/proc/stat` a
100 ms). `rayd` además muestrea cada 5 s mientras el sandbox corre
y guarda un anillo de 8 h (5 760 muestras) que `get_metrics_history(start=,
end=, max_points=)` devuelve en orden ascendente: `cpu_used_pct` de cada
muestra es la media desde la anterior, y `max_points` reduce la serie a tramos
(la última muestra de cada tramo con la CPU promediada). Mientras el sandbox
está suspendido no se muestrea: la serie tiene un **hueco**. El muestreo no
toca la red, así que no cuenta como actividad para la política de idle.
`cpu_count` y `memory_total_bytes` (en `get_health()`) y `memory_mb` son la
**vista del guest**, que puede no coincidir con el tamaño de la imagen
(medido: 8016 MiB y 4 CPU con una imagen de 2048 MiB). La forma de clase
(`Sandbox.get_metrics_history(sandbox_id, access_token=...)`) necesita el
access token y nunca despierta un sandbox suspendido.

Ejemplos en Python y TypeScript: [Métricas y listado](observability.md).

## Listado

`Sandbox.list()` pagina `list-microvms` (páginas de 50) de forma perezosa.
`Sandbox.paginate(limit=, next_token=, order=, started_after=, states=,
metadata=)` da un `SandboxListPaginator` reanudable: `next_items()`,
`has_next` y `next_token`, un cursor **opaco** (lleva el `nextToken` de AWS y
el filtro, nunca se interpreta) que puedes guardar y pasar a otro proceso.

- `order="asc" | "desc"` por `startedAt` se calcula en cliente: AWS no
  ordena, así que el primer item llega tras recorrer **todas** las páginas
  (O(páginas)). Un token reanudado con orden salta por identidad los items ya
  entregados.
- `states`, `started_after` y `template` filtran; `template` va al servidor.
- `metadata=` es O(n): una sonda `Health` por sandbox `RUNNING`
  (ver [Metadatos](#metadatos)).

??? info "Fuentes y mediciones"
    - Arquitectura y decisiones (sidecar en Python, plazo del servidor, vida
      inmutable): [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md),
      ADR-002, ADR-007 y ADR-011. Si esta página y `ARCHITECTURE.md`
      difieren, manda `ARCHITECTURE.md`.
    - Vista del guest frente al tamaño de la imagen: fila Q68 de
      [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Coste del ciclo suspend/resume: [Costes](cost.md).
