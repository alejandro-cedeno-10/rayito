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

El sidecar es Python por decisión escrita (ADR-002 en `ARCHITECTURE.md`): el
kernel es Python de todos modos y `jupyter_client` es el cliente probado del
protocolo de Jupyter, así que lo que cuesta esfuerzo (formatters, gráficos,
warm-up, supervisión, reseed) queda en el mismo lenguaje. El salto por stdio
es despreciable frente a ejecutar una celda y el `.proto` no depende del
backend, así que la decisión es reversible sin tocar clientes. Consolidarlo
en Rust con `jupyter-zmq-client` sigue siendo una opción evaluada, no un
objetivo.

Si esta página y `ARCHITECTURE.md` difieren, manda `ARCHITECTURE.md`.

## Desde qué lenguajes se usa Rayito

`rayd` es un **servidor** gRPC dentro del MicroVM; el SDK es un cliente
generado desde `proto/rayito/v1/` más el ciclo de vida, los tokens y la
reconexión. Comparado con otros sandboxes para agentes:

| Producto | Agente dentro de la VM | SDKs oficiales de cliente |
|---|---|---|
| E2B | `envd` (Go) | Python, JS/TS (Go: forks comunitarios) |
| Daytona | daemon en Go | Python, TypeScript, Ruby, Go, Java + CLI |
| Modal | propietario | Python; JS y Go vía libmodal / modal-client |
| Rayito | `rayd` (Rust) | Python, TypeScript; cualquier otro lenguaje generando un cliente gRPC desde `proto/rayito/v1/` |

Fuentes: E2B (`github.com/e2b-dev/infra`, `envd` en Go; SDKs oficiales sólo
Python y JS/TS), Daytona (`daytona.io/docs/en/getting-started/`: Python,
TypeScript, Ruby, Go, Java y CLI), Modal (`modal.com/docs/guide/sdk-javascript-go`:
JS y Go vía libmodal / modal-client).

Para usar Rayito desde otro lenguaje hacen falta tres cosas:

1. **Llamar al plano de control** `lambda-microvms` de tu cuenta con el SDK
   de AWS de ese lenguaje: `run-microvm`, `get-microvm`, `suspend` /
   `resume` / `terminate` y `create-microvm-auth-token` para el JWE del proxy.
2. **Hablar gRPC sobre HTTPS/2** con `rayd` a través del proxy de AWS, con
   las cuatro cabeceras de metadata en minúsculas: `x-aws-proxy-auth` (el
   JWE), `x-aws-proxy-port` (`8080`), `x-aws-proxy-force-h2` (`true`) y
   `x-access-token` (el secreto del sandbox; `Health` es el único RPC que no
   lo exige).
3. **Implementar la readiness sobre `Health`** (`agent_ready`,
   `kernel_ready`) y, si se usa pause/resume, el contrato de reconexión
   (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`, `Reattach`; ver "Streams
   y reconexión" más abajo).

Posición del proyecto: un cliente **Rust** es trivial de construir
(`aws-sdk-lambdamicrovms` existe en crates.io y el cliente `tonic` ya está en
el workspace) y se publicará cuando un usuario de Rig lo pida; un cliente
**Go** no está previsto (trae el tuyo desde el `.proto`; el ejemplo de
`buf.gen.yaml` para Go se escribirá a petición); publicar `rayito-proto` en
crates.io queda diferido.

## Vida del sandbox vs. política de idle

Un sandbox es un MicroVM de Lambda con `maximumDurationInSeconds` fijado en
`create(timeout=)`: la vida máxima (running + suspended), tope 8 h, y **no se
puede cambiar después** (no existe `UpdateMicrovm`; por eso el `set_timeout`
de E2B es `UnimplementedError`). Para seguir más allá de las 8 h con los mismos
ficheros, `sbx.reincarnate()` guarda el `HOME` en S3 y lo restaura en un
sandbox nuevo ([Persistencia](persistence.md)); las variables del kernel, los
procesos y las PTY no sobreviven.

La política de idle (`create(idle=IdlePolicy(...))`, 300 s por defecto)
suspende el MicroVM cuando no recibe tráfico y lo reanuda con la siguiente
llamada (`auto_resume=True`). Un ciclo suspend/resume cuesta ≈ $0.011 a 2 GB
(≈ 5 min de cómputo), así que un `max_idle_seconds` menor que 300 no ahorra
dinero. `idle=None` desactiva la auto-suspensión.

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
`pause()`, el auto-resume o un 502 del proxy los corta, cada handle se
reengancha por su cuenta la próxima vez que se lee (`Connect(from_seq)`,
`Pty.Connect`, `WatchDir` de nuevo, `Reattach` para una celda en curso), y un
unario cortado se reintenta una vez. `reconnect_timeout` (60 s) acota cuánto
espera el SDK al agente una vez que el MicroVM vuelve a `RUNNING`; mientras
está suspendido, leer un handle en background **no** lo despierta.

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
