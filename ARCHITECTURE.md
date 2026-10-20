# ARCHITECTURE

> Fuentes de verdad por encima de este documento: `proto/rayito/v1/*.proto`
> (contrato) y `AWS_API_NOTES.md` (API de AWS). Si algo aquí las contradice,
> ganan ellas y este fichero se corrige.

## Vista general

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CAPA 3 — SDK (Python `rayito` / TypeScript)                             │
│                                                                          │
│  Sandbox.create() ──► boto3 `lambda-microvms`: run-microvm               │
│                   ──►                          create-microvm-auth-token │
│  sbx.commands.*   ──► gRPC ProcessService    ┐                           │
│  sbx.files.*      ──► gRPC FilesystemService ├─ máx. 2 canales HTTP/2    │
│  sbx.run_code()   ──► gRPC CodeService       │  por Sandbox              │
│  sbx.pty.*        ──► gRPC PtyService        ┘                           │
│  is_running()     ──► gRPC HealthService (sin x-access-token)            │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ HTTPS/2 (ALPN) a <id>.lambda-microvm.<region>.on.aws:443
                               │ metadata gRPC en minúsculas:
                               │   x-aws-proxy-auth: <JWE>      x-aws-proxy-port: 8080
                               │   x-aws-proxy-force-h2: true   x-access-token: <secreto>
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Proxy de AWS: termina TLS, valida el JWE, elimina `X-aws-proxy-*`,      │
│  reenvía h2c en texto plano al puerto pedido (403 si no está en          │
│  allowedPorts). Cap: 8 conexiones concurrentes a 1 vCPU.                 │
└──────────────────────────────┬───────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AWS Lambda MicroVM (Firecracker, ARM64 Graviton, AL2023)                │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  CAPA 2 — agente `rayd` (Rust, estático musl)                      │  │
│  │                                                                    │  │
│  │  :8080  gRPC h2c (tonic)   HealthService  ProcessService           │  │
│  │                            FilesystemService  PtyService           │  │
│  │                            CodeService ─────────────────┐          │  │
│  │  :9000  HTTP/1.1 (axum)    POST /aws/lambda-microvms/   │          │  │
│  │         sólo lo llama       runtime/v1/{ready,validate, │          │  │
│  │         Lambda; nunca en    run,resume,suspend,terminate}│         │  │
│  │         allowedPorts                                     │          │  │
│  └──────────────────────────────────────────────────────────┼─────────┘  │
│                                    hijo de rayd, JSON lines │ por stdio  │
│  ┌──────────────────────────────────────────────────────────▼─────────┐  │
│  │  kernel-sidecar (Python 3.12, jupyter_client AsyncKernelManager)   │  │
│  │  ipykernel por contexto, transporte ipc:// bajo /run/rayito/k/     │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  CAPA 1 — imagen: al2023-minimal + Python 3.12 + rayd + sidecar + libs   │
│           snapshot de memoria+disco tomado tras `/ready` en el build     │
└──────────────────────────────────────────────────────────────────────────┘
```

## Qué corre dónde

El diagrama anterior muestra la superficie RPC; este muestra los procesos de
un MicroVM, con qué usuario corren y por qué transporte hablan entre sí.

```
AWS Lambda MicroVM (Firecracker, ARM64)
│
├─ rayd (Rust, estático musl, root)
│    :8080  gRPC h2c  ── lo llama el SDK a través del proxy de AWS
│    :9000  HTTP/1.1  ── lo llama sólo Lambda (hooks /ready /run /suspend /resume /terminate)
│    127.0.0.1:<efímero>  proxy de egress (M9, sólo rayito-base-caps con política, ADR-012)
│    │
│    └─ hijo: kernel-sidecar (Python 3.12, uid 1000)
│         stdin/stdout JSON lines  ◄──►  rayd
│         │
│         └─ hijos: un kernel Jupyter por contexto (uid 1000)
│              ipykernel / bash_kernel: ZMQ ipc:// bajo /run/rayito/k/<contexto>/
│              Deno (javascript/typescript, sólo rayito-base-poly): ZMQ tcp://127.0.0.1 (ADR-013)
│              ►► aquí corre el código del usuario (run_code)
│
└─ procesos y PTYs de commands.run / pty.create (uid 1000, hijos directos de rayd)
```

| Pieza | Lenguaje | Dónde corre | Quién la usa |
|---|---|---|---|
| `rayd` | Rust | dentro del MicroVM, como root, un proceso por VM | el SDK (gRPC :8080) y Lambda (hooks :9000) |
| kernel-sidecar | Python 3.12 | dentro del MicroVM, hijo de `rayd`, uid 1000 | sólo `rayd` (JSON lines por stdio) |
| Kernel Jupyter (uno por contexto) | ipykernel (Python), `bash_kernel` y, en `rayito-base-poly`, Deno 2.9.7 (`javascript`/`typescript`) | dentro del MicroVM, hijo del sidecar, uid 1000; **aquí corre el código del usuario** | el sidecar (`jupyter_client`): ZMQ `ipc://` para Python y bash, TCP de loopback `127.0.0.1` con HMAC para Deno, que no sabe abrir endpoints `ipc` ([ADR-013](#adr-013--kernels-deno-sobre-tcp-de-loopback-con-hmac)) |
| procesos y PTYs (`commands`, `pty`) | lo que el usuario lance | dentro del MicroVM, hijos de `rayd`, uid 1000 | el SDK vía `ProcessService` / `PtyService` |
| SDK Python (`rayito`) | Python ≥ 3.11 | **fuera** del MicroVM, en el proceso del cliente (tu app, tu agente, tu CI) | tu código |
| SDK TypeScript (`rayito`) | TypeScript / Node ≥ 20 | **fuera**, en el proceso del cliente | tu código |
| llamadas al plano de control | boto3 / AWS SDK JS v3 dentro del SDK | **fuera**, desde el proceso del cliente hacia la API `lambda-microvms` | el SDK (`create`, `list`, `pause`, `resume`, `kill`, tokens) |

**Por qué el sidecar es Python.** Es la decisión de [ADR-002](#adr-002--kernel-de-jupyter-en-sidecar-python):
el kernel que ejecuta `run_code` es Python de todos modos (ipykernel), y
`jupyter_client` es el cliente probado del protocolo wire de Jupyter (ZeroMQ,
cinco sockets, firma HMAC). Todo lo que de verdad cuesta esfuerzo también es
Python: los formatters de IPython, la extracción de gráficos con
`e2b_charts`, el warm-up, la supervisión y el reinicio de kernels, el reseed
tras `/resume`. El salto por stdio entre `rayd` y el sidecar es despreciable
frente a la latencia de ejecutar una celda, y el `.proto` no depende del
backend, así que la decisión es reversible sin tocar clientes; el precio es
un segundo proceso que `rayd` supervisa. Consolidarlo todo en Rust con
`jupyter-zmq-client` sigue siendo una opción evaluada (ADR-002;
`MILESTONES.md`, "opciones a evaluar"), no un objetivo.

---

## Capa 1 — Imagen base

`FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:<digest>` (ARM64).
`al2023-minimal` viene **sin** `tar`, `gzip` ni `procps`; se instalan con `dnf`.

### Dockerfile de producto (`image/Dockerfile`)

```dockerfile
FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:<digest>
RUN dnf install -y --setopt=install_weak_deps=0 \
      python3.12 python3.12-pip tar gzip procps-ng shadow-utils util-linux-core \
    && dnf clean all \
    && useradd -m -u 1000 -s /bin/bash user
COPY rayd /usr/local/bin/rayd
RUN chmod 0755 /usr/local/bin/rayd
COPY kernel-sidecar/ /opt/rayito/sidecar/
RUN python3.12 -m pip install --no-cache-dir -r /opt/rayito/sidecar/requirements.txt
EXPOSE 8080 9000
CMD ["/usr/local/bin/rayd", "--grpc-port", "8080", "--hooks-port", "9000"]
```

**Nunca se compila Rust dentro del Dockerfile.** El binario `rayd` llega
precompilado en el zip; AWS ejecuta el Dockerfile en su propia VM Graviton.

El Dockerfile real añade a ese esqueleto (M9) `git-core` fijado por NEVRA
(`git-core-2.50.1-1.amzn2023.0.1`, para `sbx.git`) y, sólo con el marcador
`kernels_variant` de la variante poly, `bash_kernel` y **Deno 2.9.7**: el zip
oficial `deno-aarch64-unknown-linux-gnu.zip` descargado con `curl -o`,
comprobado con `sha256sum -c` contra un sha256 fijado en el propio fichero y
extraído a `/opt/rayito/deno/deno` (root, 0755). `scripts/check_pins.py` exige
esa forma a cada descarga del Dockerfile (`SECURITY.md` T10) y la versión
exacta de cada paquete dnf vigilado; subir Deno es cambiar versión y sha256 y
publicar una versión de imagen nueva.

### Pipeline de imagen

```
cargo zigbuild (aarch64-musl) ─► image/rayd
                                  + image/Dockerfile + kernel-sidecar/
                                  ─► zip (Dockerfile en la raíz)
                                  ─► s3://<bucket>/images/rayito-<sha256[:12]>.zip
                                  ─► create-microvm-image (1ª vez) / update-microvm-image (PUT)
                                  ─► gate de tres estados
```

- La clave S3 es el hash del contenido; si existe, `image-publish` es no-op.
- `update-microvm-image` es PUT: repite `--base-image-arn`, `--build-role-arn`,
  `--code-artifact` y `--base-image-version` explícita en cada llamada (las
  versiones base caducan: `DEPRECATED` 60 d → `EXPIRING` 30 d → `EXPIRED`).
- **Gate de tres estados** antes de declarar la imagen usable: imagen
  `CREATED|UPDATED` **y** versión `SUCCESSFUL` **y** status `ACTIVE`. Sondear
  sólo el estado de la imagen lanza contra una versión `FAILED`. Nombres de enum
  según el modelo (`CREATE_FAILED`, `--status`), no según la prosa de docs.
- `image-publish` imprime `snapshotBuild.memorySnapshotSizeInBytes` y
  `diskSnapshotSizeInBytes`: son la base del coste y del tiempo de arranque.
- `image-prune` (`rayito image prune`, `scripts/image_prune.py` como shim, M6) conserva las 5 versiones
  lanzables más recientes más las que tengan MicroVMs vivos o estén en vuelo
  (cuota: 50 por imagen, 5 builds concurrentes por región, mínimo 1 semana de
  storage por versión, ≈ $0,04/semana cada una). Borra **de una en una**:
  `delete-microvm-image-version` deja la imagen en `UPDATING` y la siguiente
  llamada responde `ConflictException`, así que espera a que la imagen y la
  versión salgan de `UPDATING`/`DELETING` (y reintenta con 5/10/20/40/80 s)
  antes de la próxima; una versión `ACTIVE` se desactiva primero si el
  servicio la rechaza. `--dry-run` imprime el plan; el resumen JSON final
  lista `kept`/`deleted`/`reports` (`AWS_API_NOTES.md` §16 Q49).
- Docker local (`image-local`) es opcional y nunca está en el camino crítico.

### Variantes de imagen

Una imagen tiene tres variantes construidas desde el mismo árbol, el mismo
`rayd`, el mismo Dockerfile y los mismos hooks: **full** (`rayito-base`, el
template de producto, con el warm-up del kernel), **slim**
(`rayito-base-slim`, imagen de **medición**, no un template de producto) y
**caps** (`rayito-base-caps`, M6: el mismo zip que full publicado con
`additionalOsCapabilities: ["ALL"]` por `rayito image publish
--os-capabilities ALL` (`scripts/publish_image.py`), `make image-publish-caps`). En caps `rayd` arranca
con `CapEff` completo y cgroup2 montado, e instala antes de los listeners la
ruta de política que bloquea IMDS para uid 1000-65535 (`Health.imds_blocked`;
sección "Lifecycle hooks"); en full el mismo binario registra
`imds_block_unavailable` y sigue (fail-open). Es el template a elegir cuando
se pasa un `execution_role_arn` (`SECURITY.md` T1). Entre full y slim la
única diferencia es un marcador dentro del zip,
`kernel-sidecar/ipython/startup/warmup_variant` con el contenido `slim`, que
`0004_warmup.py` lee antes de importar nada; el repositorio nunca contiene
ese fichero (una variable de entorno de imagen no serviría: `rayd` construye
desde cero el entorno del sidecar y el del kernel). `rayito image zip
--variant slim` (`scripts/image_zip.py`, `make image-zip-slim`) escribe la
entrada sintética en el archivo (fecha fija, 0644; el zip full de un árbol es
idéntico byte a byte al de antes) y `rayito image publish --variant slim`
(`scripts/publish_image.py`, `make image-publish-slim`) publica bajo el nombre
`rayito-base-slim`, comprobando
antes de cualquier llamada a AWS que el marcador del artefacto coincide con
el flag. Medido el 2026-09-16 (`docs/benchmarks/2026-09-cold-start.md`):
`rayito-base` 10.0 = 919 146 496 B de memoria, `rayito-base-slim` 2.0 =
693 841 920 B (−225 MB: el warm-up y los módulos que `0002_data.py` cargaba
al arrancar; el resto del delta con la imagen 4.0 de Q34 es la page cache de
los wheels instalados, que la slim conserva), build 174,8 s frente a 215,8 s.

Desde M9, caps es además la única variante que **aplica una política de
egress en el guest** (ADR-012): con `CAP_NET_ADMIN` en `CapEff`, `rayd`
instala las tablas de rutas por uid y arranca el proxy local cuando el SDK
pide `network=`/`allow_internet_access=False`; en full y slim `rayd` publica
`Health.egress_enforcement = NONE` y el SDK termina el VM y lanza
`UnimplementedError` antes de devolver un sandbox sin la política pedida. La
variante **poly** (`rayito-base-poly`, M7) es la única que lleva `bash_kernel`
y, desde M9, los kernels Deno de `javascript` y `typescript` (ADR-013).

### Configuración `hooks` en `create-microvm-image`

```json
{ "port": 9000,
  "microvmImageHooks": { "ready": "ENABLED", "readyTimeoutInSeconds": 900,
                         "validate": "ENABLED", "validateTimeoutInSeconds": 600 },
  "microvmHooks": { "run": "ENABLED", "runTimeoutInSeconds": 10,
                    "resume": "ENABLED", "resumeTimeoutInSeconds": 30,
                    "suspend": "ENABLED", "suspendTimeoutInSeconds": 30,
                    "terminate": "ENABLED", "terminateTimeoutInSeconds": 10 } }
```

Timeouts **siempre explícitos**: el default cuando un hook está `ENABLED` sin
timeout no aparece en la documentación pública.

### Tamaño y variables

- **El tamaño se fija en la imagen** (`resources[0].minimumMemoryInMiB`, máx. 1
  entrada), no en `run-microvm`. Un template = un tamaño; el nombre lo codifica:
  `rayito-base-2gb`, `rayito-base-4gb`. A 2 GB: 1 vCPU baseline, 8 GB de disco,
  4 MB/s de ancho de banda por sentido, 8 conexiones concurrentes.
- `environmentVariables` (máx. 50) son de nivel imagen, quedan congeladas en el
  snapshot y **las comparten todos los MicroVMs**. Nunca llevan secretos.

### Qué captura el snapshot y reglas de unicidad

`create-microvm-image` ejecuta el Dockerfile, arranca `CMD`, espera el 200 de
`/ready` y **toma un snapshot de memoria + disco con todos los procesos vivos**.
Es el equivalente a los pre-warmed snapshots de E2B, gestionado por AWS: lo que
el sidecar importe antes de `/ready` no se paga en cada arranque.

La contrapartida: **todo lo que exista en memoria o disco cuando `/ready`
devuelve 200 se clona en cada MicroVM de esa versión y en cada resume**:
tokens, UUIDs, semillas PRNG, la clave HMAC del connection file de Jupyter,
conexiones TCP. Reglas:

1. `rayd` no genera secretos ni identificadores antes de `/run`, y usa
   `rand::rngs::OsRng` / `getrandom` por llamada, nunca un RNG sembrado al
   arrancar.
2. El sidecar precarga módulos antes de `/ready`, pero en `/run` y en `/resume`
   ejecuta una celda silenciosa `import random; random.seed()` (+
   `numpy.random.seed()` si está cargado) en cada kernel vivo.
3. La clave HMAC del kernel se rota en `/run`: el kernel precalentado se
   reinicia (o se arranca) con un connection file nuevo. Coste a medir en M4.
4. El snapshot de memoria crece con lo precargado y AWS estima ≈1 s por cada
   500 MB leídos en run/resume: medir el coste de la precarga (M0 Q21, M4)
   antes de ampliarla.
5. `/validate` ejecuta un `run_code` real (pandas + matplotlib) para que Lambda
   muestree y prefetchee esas páginas.

Una imagen por template. No capturar estado de sesión en la imagen.

---

## Capa 2 — Agente `rayd`

Rust, binario estático `aarch64-unknown-linux-musl`. Dos listeners:

| Puerto | Protocolo | Quién lo usa | Qué sirve |
|---|---|---|---|
| `:8080` | gRPC h2c (`tonic`) | el SDK, a través del proxy de AWS | los siete servicios del `.proto` |
| `:9000` | HTTP/1.1 (router `axum` sobre hyper) | sólo Lambda (hooks de ciclo de vida) | `POST /aws/lambda-microvms/runtime/v1/<hook>` |

El puerto 9000 **nunca entra en `allowedPorts` de ningún token** (ADR-006).

Los dos listeners escuchan en `0.0.0.0` dentro del netns del sandbox y `rayd`
no puede subir su `RLIMIT_NOFILE` de 1024 (`AWS_API_NOTES.md` §9), así que
ambos pasan por el mismo tope de conexiones (`adapters::CappedListener`,
`rayd_core::listeners`): 256 conexiones a la vez en `:8080` y 32 en `:9000`;
una conexión de más espera en el backlog del kernel hasta que otra se cierre,
y un `accept` sin descriptores espera 100 ms antes de reintentar. Los hooks
se sirven con hyper directamente (`hooks::serve`) en vez de `axum::serve`,
para poder fijar un plazo de 10 s a la cabecera de cada petición y cerrar la
conexión tras su única respuesta (`SECURITY.md` T7).

### Servicios

Ver `proto/rayito/v1/`. Resumen:

| Servicio | RPCs | Notas de implementación |
|---|---|---|
| `HealthService` | `Health`, `Metrics`, `MetricsHistory` (M9) | `Health` es el único RPC sin `x-access-token`: sonda de readiness/liveness del SDK (`get-microvm` es eventualmente consistente). Expone `resume_generation`, `clock_offset_ms`, `kernel_state_lost`, `sandbox_id` y, desde M6, `imds_blocked` (campo 9: ruta de IMDS instalada **y** verificada), `hook_anomalies` (campo 10) y `metadata` (campo 11, el mapa del `runHookPayload`, vacío antes de `/run`); M9: `lifecycle` (campo 12, el plazo lógico de ADR-011: fase, `deadline`, `cap`, `extensions`), `egress_enforcement` (13, ADR-012), `cpu_count` y `memory_total_bytes` (14 y 15, la vista del guest, que la carga de trabajo ya lee de `/proc`). `MetricsHistory` exige `x-access-token` y sirve el anillo que el muestreador de métricas (`lifecycle/metrics_sampler.rs`) llena cada 5 s **sólo** en `running`/`resumed` (5 760 muestras = 8 h; sin red, así que no cuenta como actividad para la política de idle; una suspensión deja un hueco), con rango inclusivo y reducción a `max_points` |
| `ProcessService` | `Start` (server-stream), `Connect(pid, from_seq)`, `SendInput`, `CloseStdin`, `SendSignal`, `List` | `tokio::process::Command` + `process_group(0)`; `timeout_ms` impuesto por el agente sobre el **reloj de ejecución** (monotónico menos el tiempo suspendido, M5: un `timeout=60` pausado a los 10 s conserva 50 s tras el resume; SIGTERM al grupo, SIGKILL 5 s después, `EndEvent{status:"timeout"}`); `stdin=false` ⇒ `/dev/null`; ring de 1 MiB por pid para `from_seq`; canal acotado por suscriptor (64 eventos × 32 KiB) con backpressure real: si sigue lleno 30 s el suscriptor se desengancha con `EndEvent{status:"output_truncated"}` y el proceso sigue; máx. 8 suscriptores por pid; `StartEvent`+`EndEvent` retenidos 30 s tras salir (máx. 256 entradas terminadas); `kill_tree` hace al proceso *child subreaper* (`prctl` en `pre_exec`) y lleva el timeout y `SendSignal` a todo su árbol, `setsid` y demonios incluidos: SIGTERM a los descendientes antes que al grupo, y SIGSTOP + SIGKILL por pasadas al vencer la gracia (`rayd_core::process::tree`; lo usa cada ejecución de `sbx.agent`) |
| `FilesystemService` | `Read` (stream 256 KiB), `Write` (client-stream multi-fichero), `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir` (server-stream) | `std::fs` bloqueante bajo `spawn_blocking` con identidad de fichero por hilo (`setfsuid`/`setfsgid` del usuario, nunca root; `tokio::fs` no puede fijar la identidad de un hilo entre awaits); lista de denegación sobre la ruta **canónica** (`/proc`, `/sys`, `/dev`, `/etc`, `/usr`, `/run/rayito`, `/opt/rayito`, el binario de `rayd`; `/root` y los `HOME` ajenos quedan cubiertos por los permisos POSIX bajo la identidad del usuario, …) ⇒ `PERMISSION_DENIED`, `..` ⇒ `INVALID_ARGUMENT`, rutas relativas al `HOME` del usuario; `Read` con `O_NOFOLLOW` y sólo ficheros regulares; escritura a temporal `.rayito-tmp-*` en el mismo directorio + fsync + fchmod/fchown + rename, padres creados, temporal borrado en cualquier fallo o cancelación; chunks ≤ 1 MiB; `ListDir` nunca sigue symlinks y corta a 10 000 entradas (`RESOURCE_EXHAUSTED`); `WatchDir` con `notify` (inotify, un watch por directorio, sin seguir symlinks), primer mensaje `WatchStarted` sólo con el watch instalado, sin debounce, cola de 1024 eventos con `RESOURCE_EXHAUSTED` al desbordar, máx. 64 watches por sandbox, `KeepAlive` cada 50 s; los mensajes de error nunca incluyen la ruta. M7 (ADR-009): `Checkpoint(CheckpointRequest) → stream CheckpointEvent` y `Restore(RestoreRequest) → stream RestoreEvent`: tar.gz del `HOME` del usuario (lista de exclusión fija + `exclude` ≤ 64, symlinks tal cual, especiales y no legibles contados en `skipped`, leído bajo `FsIdentityGuard`) subido/bajado por `rayd` como root a `s3://<bucket>/<key_prefix>/home.tar.gz` + `manifest.json` con las credenciales IMDSv2 del execution role; un checkpoint o restore a la vez (`FAILED_PRECONDITION`), `NOT_FOUND` sin manifest, `PERMISSION_DENIED` sin rol, con `user=root` o con un destino fuera del ámbito `persist` ligado en el `/run` (C-07), `started → progress* → done | error`, `KeepAlive` cada 30 s, `/suspend` cierra con `suspending` y aborta el multipart. M9 (ADR-010): `StartImport`, `StartExport`, `GetTransfer`, `WatchTransfer` (server-stream) y `CancelTransfer` mueven bytes entre el fichero y **URLs de S3 prefirmadas por el SDK** con las credenciales del llamante (`rayd` no guarda ninguna): la política de URL (`transfer::url_policy`: https, puerto 443, host virtual-hosted regional exacto del bucket, ruta = la clave ligada al sandbox, SigV4, sin literales IP) se aplica antes de cualquier E/S, el adaptador `HyperSignedHttp` resuelve con un filtro que descarta loopback, link-local e IMDS y no sigue redirecciones; la importación sondea el `GET` (1 s los primeros 600 s, luego 5 s) y escribe por el mismo `WriteSink` que `Write`; la exportación hace `pread` sobre un descriptor abierto con `O_NOFOLLOW` como el usuario; 16 transferencias activas, 2 en curso, 64 terminadas retenidas 30 min; `GetTransfer("")` = `NOT_FOUND` es la sonda de capacidad del SDK; barrera de lectura tras subida (`Read`, `Stat`, `ListDir`, `Process.Start`, `Execute` y `Pty.Create` esperan hasta 2 s a una importación armada). `Write`/`Read` aceptan además `grpc-encoding: gzip` sólo si el cliente lo pide (cabecera `rayito-compress`), y `Write` y `Stat`/`ListDir` llevan `metadata` por fichero como xattrs `user.rayito.*` (≤ 64 claves, ≤ 4 000 B) aplicados sobre el descriptor |
| `LifecycleService` (M9) | `SetTimeout` | el plazo lógico de ADR-011 (`sandbox_timeout` en `rayd-core`): `EXACT` (`set_timeout`) o `AT_LEAST` (`connect(timeout=)`), exige `x-access-token` (el código del sandbox no puede alargarse a sí mismo); más allá del tope `INVALID_ARGUMENT` "timeout beyond cap; cap_unix_ms=<n>", sin bloque `lifecycle` en el `runHookPayload` `FAILED_PRECONDITION` `lifecycle_unmanaged`. Pasado el plazo, la capa `timeout_gate` (dentro de la del token) responde `FAILED_PRECONDITION` `sandbox_timeout` a todo RPC salvo `Health` y `SetTimeout` |
| `NetworkService` (M9) | `UpdateNetwork`, `GetNetwork` | la política de egress en el guest de ADR-012 (`network` en `rayd-core`, `network::manager` en `rayd`): valida `allow_out`/`deny_out` (CIDR, IP o nombre de host, un permitido gana a un denegado), planifica las rutas por uid, las cambia de forma atómica y las verifica; `FAILED_PRECONDITION` sin `CAP_NET_ADMIN` para una política que restringe; los mensajes nunca llevan una entrada, una dirección ni una credencial; `NetworkState` nunca devuelve la dirección ni las credenciales del proxy del operador |
| `PtyService` | `Create` (server-stream), `Connect(pid, from_seq)`, `SendInput`, `Resize`, `Kill` | forma server-stream + unarios como `envd` (M5 ✔); backend `nix::pty::openpty` con el `Winsize` inicial + `tokio::process::Command` (ADR-005): el esclavo se `fchown`/`fchmod 0o620` al usuario, el hijo hace `setsid` + `ioctl(TIOCSCTTY)` en `pre_exec` (sin `process_group(0)`: la sesión de la terminal es el grupo) y arranca el shell de login del usuario (`pw_shell`, `/bin/sh` si falta) con `-i -l`, `TERM=xterm-256color`, `LANG=LC_ALL=C.UTF-8`, `SHELL`, más los `envs` de la petición; el maestro se bombea en chunks de 16 KiB al mismo registro que los procesos (`ProcessKind::Pty`, `List` los muestra, `Connect(from_seq)` con el mismo ring y `OUT_OF_RANGE`, máx. 8 suscriptores, 256 vivos entre procesos y PTYs); `Resize` = `TIOCSWINSZ`; `Kill` = SIGKILL al grupo (`PtyExited{signal:9}`); `timeout_ms` en el reloj de ejecución; `EIO` del maestro = EOF + 500 ms de drenaje; cruzar servicios (`SendInput` de proceso a una PTY o viceversa) ⇒ `FAILED_PRECONDITION`; reutiliza `ConnectRequest`/`SendInputRequest` de `process.proto`; `PtyServerMessage.seq` numera `data` |
| `CodeService` | `CreateContext`, `Execute` (server-stream), `Reattach`, `ListContexts`, `DestroyContext`, `RestartContext` | proxy al sidecar (M4 ✔); `Execute` emite `ExecutionStarted`, `OutputChunk` (≤ 64 KiB), `ExecutionResult`, `ExecutionError` (nunca terminal), `ExecutionEnd` y `KeepAlive` cada 5 s (`seq` 0); cola de 256 eventos por suscriptor y `OutputTruncated` inmediato para el suscriptor cuya cola se llena (el recorder nunca espera a un cliente); `timeout_ms` = interrupt al kernel y, si no queda idle en 5 s, `RestartContext` + `ExecutionError{name:"ExecutionTimeout"}`; cancelar el stream `Execute` de origen interrumpe la ejecución (un suscriptor de `Reattach` nunca); máx. 8 contextos, `default` indestructible, código ≤ 1 MiB. Cada ejecución vive fuera de su stream (M5 ✔): un `ExecutionRecorder` por ejecución graba los eventos en un **ring de 4 MiB** por ejecución (≤ 32 retenidas, 30 s de reloj de ejecución tras el `End`) y `Reattach(context_id, execution_id, from_seq)` reengancha desde `last_seq + 1` (`OUT_OF_RANGE` si el ring ya no lo tiene, `NOT_FOUND` si expiró o el contexto no coincide, `RESOURCE_EXHAUSTED` al noveno suscriptor) |

Convenciones transversales:

- Todo server-stream emite `KeepAlive` cuando lleva 30 s sin mensajes (5 s
  durante `Execute`). Los clientes lo ignoran.
- Errores dentro de streams: `StreamError` con el conjunto cerrado de códigos de
  `common.proto`. Errores unarios: status gRPC estándar.
- Servidor tonic: `http2_keepalive_interval(30 s)`, `http2_keepalive_timeout(10 s)`,
  `tcp_nodelay(true)`, `max_concurrent_streams(256)`.

### Lifecycle hooks

AWS invoca hooks HTTP **POST** en `/aws/lambda-microvms/runtime/v1/<hook>` sobre
el puerto `hooks.port` declarado en `create-microvm-image` (sin default en la
API; la consola y el sample `claude-managed-agents` usan 9000; el sample
multi-tenant usa 8080 compartido con la app, lo que expone los hooks a
cualquier token del puerto 8080). Cada hook debe estar `ENABLED` explícitamente
o no se invoca. Tabla completa, JSON de configuración y roles: `AWS_API_NOTES.md` §8.

| Hook | Fase | Rol | Timeout | Body | Contrato de `rayd` |
|---|---|---|---|---|---|
| `/ready` | build | build role | 1–3600 s | ninguno | 200 sólo cuando el sidecar tiene el contexto por defecto **idle tras el warm-up**; hasta entonces **503 inmediato** (`kernel_warming`, nunca retener la petición). Válvula de escape: a los 300 s devuelve 200 y lo loguea como error (`ready_escape`). Medido: `warmup_ms` 9,3–9,7 s en la VM de build, 200 tras dos 503 |
| `/validate` | build, en una VM nueva desde el snapshot | build role | 1–3600 s | ninguno | reinicia el kernel por defecto (el mismo `restart_context` que hará `/run`) y ejecuta una celda real con pandas + matplotlib; responde 503 (`validating`) hasta terminar y 200 (`validated`/`validate_failed`), para que Lambda prefetchee esas páginas. Tras el primer `/run`, o si la conexión pertenece a un uid del sandbox, no hace nada (200 `validate_skipped` / `peer_refused`) y se audita como anomalía. Sin el reinicio aquí la rotación en `/run` costaba 45 s (`AWS_API_NOTES.md` §16 Q35); con él, 2–4 s |
| `/run` | arranque desde el snapshot; **el tráfico externo sólo llega tras el 200** | execution role | 1–60 s | `{"microvmId": "...", "runHookPayload": "..."}` | parsea el envelope, instala `sandbox_id`, el hash del token, `metadata`, `limits.cpu_seconds` (M6) y, si viene, el ámbito `persist` (bucket y base del prefijo; fuera de él `Checkpoint`/`Restore` responden `PERMISSION_DENIED`, C-07), responde 200; se acepta **una vez por arranque** (los siguientes devuelven 200 `already_ran`, se auditan como anomalía y no tocan nada). Tras el 200: rotación del kernel y, si el bloqueo de IMDS está instalado, la verificación `imds_probe` (≤ 10 s). Si falla o expira, el MicroVM pasa a `TERMINATING` sin haber estado `RUNNING` (`stateReason`). M9: si el payload trae el bloque `lifecycle` instala el plazo lógico (ADR-011) y despierta a su vigilante; si trae `network.enforce` y hay `CAP_NET_ADMIN`, **antes** del 200 arranca el proxy local e instala y verifica el deny-all de egress en 1,5 s (ADR-012); en cualquier caso responde 200, y mientras ese deny-all no se asienta `Health` dice `agent_ready=false` (medido: `Health` llega durante `/run`, `AWS_API_NOTES.md` §16 fila 66); la tarea lo asienta al terminar de cualquier modo, también si entra en pánico, y cada `ip` que lanza muere a los 5 s, así que un `ip` colgado no deja `Health` sin listo para siempre |
| `/suspend` | antes del checkpoint | execution role | 1–60 s | ninguno | checklist de la sección "Suspend / resume" (M9: también recoloca en espera las transferencias en curso; sigue respondiendo **siempre** 200); desde M6 se audita (`hook_audit`; los repetidos son `unchanged` y no cuentan; ninguna transición se rechaza) y arma el watchdog de suspensión estancada (20 s sin salto de `CLOCK_MONOTONIC` ⇒ `stale_suspend_recovered`: puerta reabierta, sin nueva generación, +1 anomalía) |
| `/resume` | tras restaurar; la VM sigue `SUSPENDED` hasta el 200 | execution role | 1–60 s | ninguno | ídem; el reseed es advisory (el sidecar responde en el acto `reseeded`/`deferred`/`failed`, su timeout nunca cuenta para el kill switch) y se recomprueba la ruta de IMDS (`imds_rule_missing` si desapareció). M9: llama a `timeout_resumed` con el veredicto del vigilante del plazo (sólo un salto de `CLOCK_MONOTONIC` ≥ 2 s abre la gracia de 30 s o aplica la regla de auto-resume; una sola gracia por plazo y nunca más allá del tope), re-verifica la política de egress en ≤ 3 s y despierta los sondeos de las importaciones armadas |
| `/terminate` | antes de liberar recursos | execution role | 1–60 s | ninguno | ACK 200; nada que persistir. Si la conexión pertenece a un uid del sandbox, 200 `peer_refused` sin hacer nada (`sec-sandbox-isolation`) |

**Origen de los hooks (M6, `SECURITY.md` T2).** No es validable: hooks y
tráfico del proxy llegan ambos desde `127.0.0.1` por HTTP/1.1, el proxy
elimina las cabeceras `X-aws-proxy-*` y AWS no comparte ningún secreto por
arranque. El control es el puerto (9000 nunca en `allowedPorts`, ADR-006) y,
tras el primer `/run` aceptado, cada hook de runtime se audita (`hook_audit`
con `hook`, `outcome`, `calls_since_run`, `anomaly`, nunca el body) y las
anomalías se cuentan en `Health.hook_anomalies` (`/run` repetido y
recuperaciones del watchdog); el SDK avisa una vez por generación. Ese
control acota el origen externo. No acota el de dentro de la VM: `rayd`
escucha en `0.0.0.0:9000` en el netns del sandbox, así que un proceso uid
1000 alcanza los hooks por loopback; `/terminate` (irreversible: `rayd` es el
`CMD` de la imagen) y `/validate` (reiniciaría el contexto `default`) son los
dos más dañinos. Contra ese origen, `rayd` comprueba el uid del par
(`sec-sandbox-isolation`, C-01, `rayd_core::hook_peer`): busca el extremo
cliente de cada conexión en `/proc/net/tcp` y `/proc/net/tcp6`, y un
`/terminate` o un `/validate` de un uid del sandbox (1000-65535) responde
200 `peer_refused` sin hacer nada; un `/suspend` o un `/resume` de un uid del
sandbox se acepta pero cuenta como anomalía, y un `/terminate` que la
búsqueda no puede atribuir se acepta y cuenta como anomalía. El guard pasa el origen al handler de
`/run` (`sec-rayd-agent-hardening`): un `/run` desde un uid del sandbox se
rechaza con `sandbox_origin` sin consumir el `/run` del arranque, así que un
proceso que llegue antes que la plataforma (el `start_cmd` de una
plantilla, que se descongela con el snapshot, ADR-022) ya no instala su
token. Además, `/ready`
y `/validate` son hooks de build: tras el primer `/run` no tienen llamante
legítimo, así que no hacen nada (`illegal` / `validate_skipped`, sin
reiniciar ningún kernel) y se auditan como anomalía. Ninguna transición de
`/suspend` o `/resume` se rechaza ni se limita: `rayd` no distingue un hook
forjado por la plataforma del genuino que llega justo detrás, el uid con el
que la plataforma envía los hooks no está medido, y rechazar el genuino
dejaría un checkpoint real sin preparar (revisión de M6; `SECURITY.md` T2).

Implementar `/suspend` y `/terminate` **idempotentes por precaución** (AWS puede
reintentarlos); que los reintente de verdad es una medida de M0 (Q10).

**Presupuestos** (cada handler envuelto en `tokio::time::timeout` al 80 % del
valor declarado en la imagen, y siempre responde):

| Hook | Presupuesto | Trabajo permitido |
|---|---|---|
| `/run` | < 2 s | parsear, instalar hash, 200. La rotación del kernel por defecto (`restart_context` con los `envs` del payload: clave HMAC y PRNG nuevos) va a una task; `kernel_ready=false` mientras dura (`restart_ms` 2–4 s medidos) |
| `/suspend` | < 5 s | no esperar ejecuciones ni procesos |
| `/resume` | < 20 s | `kernel_info` al sidecar con timeout 5 s, máx. 8 contextos; reseed de `random`/`numpy.random` en cada kernel vivo en una task |
| `/terminate` | < 5 s | — |

Sin `runHookPayload` válido (`v` desconocida, hash ausente) `/run` responde
200 igualmente pero `rayd` queda en modo *sin token*: rechaza todo RPC salvo
`Health` con `UNAUTHENTICATED`. Un 4xx en `/run` mataría la VM sin diagnóstico.

### Auth interna

Patrón de dos niveles, como `envd` de E2B:

1. AWS valida el JWE de `x-aws-proxy-auth` antes de que el tráfico llegue al
   agente. AWS elimina las cabeceras `X-aws-proxy-*` antes de reenviar: el agente
   no las ve y no intenta leerlas.
2. El agente exige además `x-access-token` en la metadata gRPC de **todo RPC
   salvo `HealthService.Health`**. Defensa en profundidad: quien consiga un token
   de AWS para el puerto 8080 aún necesita el secreto del sandbox. La capa
   rechaza antes de decodificar nada, pero **drena el body de la petición
   rechazada** (≤ 1 MiB, ≤ 2 s) antes de responder: una respuesta trailers-only
   sobre un stream h2 a medio abrir llega al cliente como `CANCELLED` a través
   del proxy (`AWS_API_NOTES.md` §16 Q29).

Canal de entrega (ADR-004): `run-microvm` **no tiene** parámetro de variables de
entorno, y las `environmentVariables` de la imagen son compartidas, así que el
único canal per-VM es `runHookPayload`:

- El SDK genera `secret = secrets.token_bytes(32)` (o toma `access_token=`) y
  envía `runHookPayload = json.dumps({"v": 1, "token_sha256": sha256(secret).hex(),
  "envs": {...}, "user": "user", "workdir": "/home/user", "metadata": {...}})`.
  Límite 4096 caracteres según el modelo (validado en cliente, el error nombra
  `envs` y `metadata`; la prosa dice 16 KB, M0 Q11). `metadata` (M6) son
  etiquetas no secretas que `rayd` guarda en la sesión desde el único `/run`
  aceptado y devuelve tal cual en `Health` (`HealthResponse.metadata`); se
  omite cuando está vacío y `rayd` sólo loguea el número de claves.
- AWS lo reenvía como body de `POST /run`. `rayd` guarda **sólo el hash** en
  memoria (`zeroize` al reemplazar); sobrevive suspend/resume porque vive en el
  snapshot de memoria.
- Cada RPC lleva `x-access-token: <base64url(secret)>`; `rayd` compara
  `sha256(secret)` con el hash instalado en **tiempo constante**
  (`subtle::ConstantTimeEq`).
- Sólo el hash viaja por AWS porque `runHookPayload` puede quedar registrado en
  eventos de datos de CloudTrail (`sensitive: true` en el modelo, pero no se
  confía en ello).
- Nada secreto se genera antes de `/ready` (reglas de unicidad de Capa 1).

### Suspend / resume

Lo que AWS documenta y lo que decide el diseño:

| Sobrevive al snapshot | Muere en run / resume |
|---|---|
| memoria de todos los procesos (rayd, sidecar, kernels, hijos), disco, descriptores, pipes, PTYs, inotify | **conexiones salientes no locales** (AWS las mata explícitamente) |
| sockets Unix y TCP loopback dentro del guest (rayd ↔ sidecar, ZMQ `ipc://` del kernel) | streams entrantes proxy → app: `rayd` los cierra en `/suspend` con su forma `suspending` antes del checkpoint (medido, `AWS_API_NOTES.md` Q38) y el cliente se reengancha tras el resume |
| el hash del token instalado en `/run` | credenciales cacheadas del execution role (IMDS sirve las mismas tras el resume, M0 Q1; `rayd` no cachea ninguna) |
| | **el tiempo**: `CLOCK_MONOTONIC` avanza lo suspendido y AWS corrige `CLOCK_REALTIME` al reanudar (M0 Q19) ⇒ todo `sleep`/timeout de tokio armado antes de la pausa vence nada más reanudar |
| | estado PRNG: idéntico al del snapshot ⇒ reseed |

Por eso **`/suspend` no cierra el enlace con el sidecar ni mata procesos**:
sobreviven, y cerrarlos destruiría el único valor diferencial del producto.

**Reloj de ejecución** (decisión M5 sobre Q19): todo plazo del lado del
servidor mide **tiempo de ejecución**, no tiempo monotónico. `SandboxSession`
acumula `suspended_total` en cada `/resume` y expone `running_now()` =
monotónico − suspendido; los deadlines (`Deadline::after(running_now,
timeout)`) de procesos, PTYs, ejecuciones (interrupt y restart), la gracia
de SIGKILL y la retención de 30 s se expresan en ese reloj y duermen con
`running_sleep` (un `sleep` que al despertar re-lee el reloj y vuelve a
dormir lo que falte). No hay registro de timers ni bucle de rearme: un
`timeout=25` pausado 30 s sigue vivo al reanudar y vence cuando le quedaba.
Los timeouts de ops del sidecar (`OpTimeouts`, tokio `timeout`) siguen
siendo monotónicos, pero uno que vence con la generación de resume cambiada
se registra como `op_timeout_across_resume` y no cuenta para el kill-switch
de tres timeouts. Los deadlines gRPC del cliente corren en el reloj del
cliente y los reexpide el contrato de reconexión.

**Checklist `/suspend`** (objetivo < 5 s, tope 24 s, idempotente, siempre 200):

1. `session.suspend()`: `suspend_generation++`, fase `Suspending`; la puerta
   de streams rechaza `Start`, `Connect`, `Create`, `WatchDir`, `Read`,
   `Write`, `Execute` y `Reattach` con `UNAVAILABLE` `suspending`.
2. `detach_for_suspend()` (los suscriptores de origen de `Execute` quedan
   desenganchados: su cierre ya no manda `interrupt`) y `SuspendSignal::
   broadcast(generation)`: cada stream vivo (`SuspendableStream`) emite su
   forma de cierre y suelta el stream interior; `wait_streams_closed` espera
   hasta 2 s y loguea `streams_closed`/`streams_pending`. **Sin** matar
   procesos, PTYs ni kernels; las grabadoras de ejecución siguen grabando.

   | Stream | Cierre en `/suspend` | Cierre al vencer el plazo (`sandbox_timeout`, M9, ADR-011) |
   |---|---|---|
   | `Process.Start`/`Connect` | `EndEvent{exited:false, status:"suspending", error:{code:"suspending"}}` y fin OK | `EndEvent{exited:false, status:"sandbox_timeout", error:{code:"sandbox_timeout"}}` y fin OK |
   | `Pty.Create`/`Connect` | `PtyExited{…status:"suspending"…}` y fin OK | `PtyExited{exited:false, status:"sandbox_timeout", error.code:"sandbox_timeout"}` y fin OK |
   | `WatchDir`, `Read`, `Checkpoint`, `Restore`, `WatchTransfer` | status gRPC `UNAVAILABLE` `suspending` (no hay mensaje terminal en el esquema) | `FAILED_PRECONDITION` `sandbox_timeout` (nunca `UNAVAILABLE`: arrancaría el bucle de reconexión del SDK) |
   | `Execute`, `Reattach` | status gRPC `UNAVAILABLE` `suspending`, **sin `ExecutionEnd`** (`ExecutionError` sigue sin ser terminal) | `FAILED_PRECONDITION` `sandbox_timeout`, sin `ExecutionEnd` |
   | `Write` (client-stream) | la sesión de escritura se aborta (temporal borrado, destino intacto), el resto del body se drena ≤ 200 ms, `UNAVAILABLE` `suspending` | la misma aborción, `FAILED_PRECONDITION` `sandbox_timeout` |

   Al vencer el plazo con `on_timeout='kill'`, `rayd` cierra así los
   streams, manda `SIGTERM` a cada grupo de procesos, PTYs, kernels y al
   sidecar (sin relanzarlo), `SIGKILL` tras la gracia y sale con código 124:
   es PID 1, así que la VM pasa a `TERMINATED` ≈ 15 s después sin llamada al
   plano de control ni IAM (`AWS_API_NOTES.md` Q58). Con `on_timeout='pause'`
   cierra los streams y el SDK (o, sin cliente, la política de idle) suspende
   el VM.

3. `quiesce` al sidecar (≤ 2 s, mejor esfuerzo).
4. `syncfs(2)` por sistema de ficheros escribible (uno por dispositivo, de
   `/proc/self/mountinfo`), cada uno en un hilo desechable; el hook espera
   como mucho el plazo de `SuspendBudget` (5 s, `rayd_core::suspend_sync`) y
   lo que siga colgado (un montaje de red o FUSE) no retrasa el 200. Nunca
   `sync(2)` sin plazo: un `/suspend` fuera de tiempo termina la VM.
5. 200 con `streams_closed`; `suspend_ms` en el log. No espera ejecuciones
   ni procesos en curso; un `/suspend` repetido no cambia nada.

**Checklist `/resume`** (objetivo < 1 s, tope 24 s; la VM sigue `SUSPENDED`
hasta el 200):

1. `session.resume()`: `resume_generation++`, `suspended_total += Δ`,
   `clock_offset_ms` (salto `CLOCK_REALTIME` vs `CLOCK_MONOTONIC` desde el
   snapshot) en `Health`, la puerta de streams se reabre. Un `/resume`
   repetido responde 200 sin volver a sondear.
2. Credenciales: `rayd` no cachea ninguna ni tiene conexiones salientes en
   M1–M5 (AWS: "refresh credentials, re-establish network connections,
   validate state"), así que el paso es un no-op documentado.
3. `probe_after_resume` (tope 12 s): op `resume` al sidecar, que sondea cada
   kernel vivo (máx. 8, `kernel_info` con 5 s); los que no responden se
   reinician en background (`restart_context`, las ejecuciones en curso de
   ese contexto terminan con `KernelRestarted`) y `kernel_state_lost=true`
   queda enganchado en `Health` hasta el siguiente `/resume` (también si el
   sondeo agotó su tope: estado desconocido).
4. Reseed de `random`/`numpy.random` en cada kernel vivo, en background
   (se encola tras la celda que estuviera corriendo; medido: tras una celda
   de 25 s agota el `OpTimeouts` de 15 s y cuenta un timeout hacia el
   kill-switch aunque el sidecar lo ejecute después, pendiente de M6).
5. 200 con `kernel_state_lost`; en el log `resume recorded` con
   `resume_generation`, `clock_offset_ms`, `suspended_ms`, `probe_ms`,
   `kernels_alive`, `kernels_lost` (`warn!` si `|clock_offset_ms| > 5000`).

**Contrato de reconexión del SDK** (M5 ✔, `Sandbox._reconnect`):

- Ante un corte reconectable (`UNAVAILABLE`, RST/GOAWAY/EOF, 502 del proxy
  o el final `suspending` de un stream; nunca `DEADLINE_EXCEEDED` ni un 403,
  que reacuña) el SDK sondea `HealthService.Health` con backoff con jitter
  0,5 s → 4 s hasta `agent_ready` y `kernel_ready` (y, si el motivo fue un
  `/suspend`, una `resume_generation` nueva) durante `reconnect_timeout`
  (60 s = `resumeTimeoutInSeconds` + 30, kwarg de `create()`/`connect()`);
  un solo sondeo por sandbox bajo un lock; cada 5 s `get-microvm` y para en
  `TERMINATING|TERMINATED` (`SandboxNotFoundException`) o en `SUSPENDED`
  sin `auto_resume` (`SandboxStateException`, "llama a `resume()`").
- **Quién despierta a un VM suspendido.** Con `auto_resume` cualquier
  `Health` es la petición que lo reanuda (Q4), así que sólo sondean
  (`wake=True`) las **peticiones nuevas** (un unario reintentado una vez,
  el primer mensaje de un stream que se abre) y un `run`/`run_code` en
  foreground **salvo que este `Sandbox` tenga un `pause()` pendiente**. Los
  handles en background, las PTYs, los watches y el foreground cortado por
  el propio `pause()` duermen (`wake=False`) leyendo `get-microvm` cada 5 s
  mientras diga `SUSPENDING|SUSPENDED`, sin tocar `Health` ni consumir el
  presupuesto, y despiertan a la vez con el `resume()` (o el auto-resume de
  otra llamada): leer un handle nunca deshace una pausa ni una suspensión
  por inactividad.
- La re-suscripción es **perezosa y por handle** en su siguiente lectura:
  `Process.Connect(pid, from_seq = last_seq + 1)`, `Pty.Connect(pid,
  from_seq)` (con vuelta a `from_seq=0` y warning si el ring lo perdió,
  `OUT_OF_RANGE`), `WatchDir` reemitido con la misma petición y el deadline
  restante (los eventos de la pausa se pierden; `on_exit` no se llama) y
  `Code.Reattach(context_id, execution_id, last_seq + 1)` sólo después de
  `ExecutionStarted` (antes, el fallo se propaga: una celda nunca corre dos
  veces). `reconnects` cuenta las reconexiones del handle.
- `resume()` reacuña el JWE, espera a `Health` y registra la generación;
  `pause()` marca la pausa pendiente antes de `suspend-microvm`. Todo
  `Health` pasa por `_record_health`: warning por `kernel_state_lost` y por
  `|clock_offset_ms| > 5000`, una vez por generación.
- El refresher de JWE **sigue corriendo durante la pausa**: la expiración es en
  reloj de pared.

### Diseño hexagonal de `rayd`

Tres crates en el workspace; la dependencia va siempre hacia dentro.

```
crates/rayito-proto     generado. build.rs: protox::compile(6 .proto) → tonic_prost_build
                        (sin protoc). tonic 0.14.6, tonic-prost 0.14.6, prost 0.14.4;
                        build-deps tonic-prost-build 0.14.6, protox 0.9.1.
crates/rayd-core        dominio + puertos. Sin tonic, axum ni rayito-proto.
crates/rayd             adaptadores + main. Único sitio con tonic/axum/tokio-process/nix/notify.
```

**`rayd-core` (dominio)**. Tipos y reglas de negocio con `thiserror`:

| Módulo | Qué modela |
|---|---|
| `session` | `SandboxSession`: `sandbox_id`, hash del token, `resume_generation`, `suspend_generation`, `clock_offset`, fase (`Booting → Ready → Running → Suspending → Suspended`) y sus transiciones legales |
| `auth` | `AccessTokenHash`, comparación en tiempo constante, política "Health es el único RPC anónimo" |
| `process` | `ProcessRegistry`: pids, `ProcessKind`, tag, ring de salida con `seq`, retención 30 s (máx. 256 entradas terminadas, se desalojan las más antiguas), límite de 256 vivos, slots de suscriptor reclamados en cuanto el cliente se va (`SubscriberSlot`), máquina de estados del timeout (SIGTERM → SIGKILL) |
| `pty` | `PtySize` (1–4096 validado, 80×24 por defecto), `resolve_shell` (shell de login del usuario, `/bin/sh` de respaldo, nunca root sin `RAYITO_ALLOW_ROOT`), `pty_base_env` (`TERM`, `LANG`/`LC_ALL`, `SHELL`, `PATH`, `HOME`, `USER`, `LOGNAME` + `envs`, el último gana), `plan_pty` → `SpawnSpec` con `["-i","-l"]`, `PtyError` sin envs ni bytes; puerto `PtyBackend`/`PtyChild`; la PTY vive en el mismo `ProcessRegistry` que los procesos (`ProcessKind::Pty`, `kind(pid)`, `WrongKind`) |
| `filesystem` | `RequestPath` (vacío, NUL y `..` rechazados; relativas → `HOME`), `DenyList` por componentes sobre la ruta canónica, `FsIdentity{uid, gid, home}`, `Entry`/`EntryKind`/`permissions_string` (forma `ls -l`) con `NameCache` sobre el puerto `NameResolver`, `walk_listing` (DFS preorden, nombres por bytes, profundidad, sin descender en symlinks ni directorios denegados, tope `MAX_LIST_ENTRIES`), `WriteSession` (máquina de estados de un stream con N ficheros: `Begin`/`Append`/`Commit`, `MissingPath`, `ChunkTooLarge`, `InvalidMode`), `FilesystemOps` (parse → canónica → deny → puerto) sobre los puertos `FileSystem`/`WriteSink`, `FilesystemError` sin rutas en los mensajes |
| `watch` (en `filesystem::events`) | `WatchTranslator` sobre el puerto `Watcher`/`WatchSubscription`: nombres relativos a la raíz canónica, fuera de la raíz y temporales `.rayito-tmp-*` descartados, `RawWatchKind` → `Create`/`Write`/`Remove`/`Rename`/`Chmod`, el rename que aterriza un temporal sobre su destino (emparejado por la cookie de inotify) → un único `Write` (M9), `RootGone`/`QueueOverflow` terminan el stream |
| `code` | `protocol` (JSON lines v1 con el sidecar: `SidecarRequest`/`SidecarOp`/`SidecarEvent`, golden en `kernel-sidecar/tests/fixtures/protocol_v1.jsonl`), `context` (`ContextId` `ctx-<12 hex>`, `ContextRegistry` con tope 8 y `default` protegido), `execution` (`ExecutionId`, `ExecuteOutput` con `seq`, `ResultBundle::from_mime`, `ExecutionTracker` que antepone `ExecutionTimeout` al `End` y sintetiza `KernelDied`/`ContextDestroyed`/`OutputTruncated`), `timeout` (`plan_timeout`: interrupt en `timeout_ms`, restart a +5 s, tope 8 h), `readiness` (`SidecarState` `Starting → Warming → Ready → Rotating`/`Exited{attempt, backoff}` y la decisión de `/ready`), `hooks` (spec de spawn del sidecar, `RESEED_CELL`, `VALIDATE_CELL`, `probe_outcome`/`restart_after_resume` para `/resume`), `ring` (`ExecuteRing` de 4 MiB por ejecución: coste por evento, desalojo del más antiguo, `replay_from` con `ReplayOutOfRange`), `executions` (`ExecutionRegistry` con `ExecutionLimits{8 suscriptores, 30 s, 32 retenidas}`: `register`/`push`/`attach`/`detach`/`mark_ended`/`reap_expired`), `CodeError` sin código ni `envs` en los mensajes |
| `hooks` | estado del ciclo de vida y los checklists de `/run`, `/suspend` (`suspend_actions`: cerrar streams sólo cuando la transición cambió) y `/resume` como funciones puras sobre `SandboxSession`; `lifecycle` acumula `suspended_total` y `clock` define `Deadline` en el reloj de ejecución. M6: `HookAudit` (contador por hook desde el `/run` aceptado, `AuditEntry`, `hook_anomalies`; sin limitador: ninguna transición se rechaza); `lifecycle` añade `SUSPEND_GATE_TIMEOUT` 20 s / `FREEZE_THRESHOLD` 5 s / `WATCHDOG_TICK` 1 s, `Transition::after_stale_recovery` y `recover_from_stale_suspend` (`Suspending → Resumed` sin generación nueva; el `/resume` posterior sí cuenta como real, sin sumar nada a `suspended_total`) |
| `capabilities` | `CapSet::from_mask`/`parse_cap_eff` sobre `/proc/self/status` (`CAP_NET_ADMIN` 12, `SYS_PTRACE` 19, `SYS_ADMIN` 21, `SYS_RESOURCE` 24) para la línea `capabilities` del arranque |
| `process::budget` | `OutputBudget`: presupuesto de bytes de salida por sandbox (128 MiB, marca alta 96 MiB) compartido por todos los rings de procesos, PTYs y ejecuciones; `charge`/`release` atómicos, cada ring desaloja primero sus propios chunks y el reaper suelta entradas terminadas por encima de la marca alta; la entrega en vivo nunca se toca |
| `process::limits` | `ResourceLimits.cpu_seconds` (`RunDefaults.cpu_seconds` del payload, 1–28 800, `cpu_rlimit()` = soft y hard = soft + 5 s) aplicado a procesos y PTYs, nunca al sidecar ni a los kernels |
| `persistence` (M7) | `BucketName`/`KeyPrefix`/`ObjectKeys` (reglas D2 de `m7-s3-persistence`), `ArchivePlan`+`IgnoreRules`+`ExcludeList` (`should_skip` por componentes), `Manifest` v1 (serde, claves desconocidas ignoradas), `Counters`/`ProgressSampler` (una muestra por tick y sólo al cambiar), `PersistenceGate` (una operación por sandbox, lease liberado en `Drop`), `PersistenceError` → `StatusKind` antes de `started` / código de `StreamError` después, y los dos flujos en dos mitades: `prepare_checkpoint`/`prepare_restore` (validación, identidad sin root, lease, sonda de credenciales, pre-walk o manifest) y `run_checkpoint`/`run_restore` (archivo en hilo bloqueante ↔ multipart por un `PartSource`, descarga por un `ChunkSink` ↔ unpack, manifest al final, sha256 verificado) |
| `transfer` (M9, ADR-010) | `url_policy` (la guardia SSRF sobre cada URL prefirmada: https, 443, host virtual-hosted regional exacto, ruta = la clave ligada al sandbox, SigV4, sin literales IP; `is_forbidden_address` para el resolvedor del adaptador), `request` (comprobaciones sin URL: caducidad, `max_bytes`, modo, sha256, partes), `poll` (calendario de sondeo del `GET` de una importación armada), `s3_error` (primer `<Code>`/`<Message>`/`<RequestId>` de un cuerpo de error, sin crate XML), `import` (clasificación de cada respuesta y comprobaciones de admisión antes de escribir un byte), `export` (plan de partes, `file_shrank`, sha256 por parte aceptada), `registry` (`TransferRegistry`: 16 activas, 2 en curso, 64 terminadas durante 30 min), `barrier` (qué tickets armados espera cada RPC), puerto `SignedHttp` |
| `sandbox_timeout` (M9, ADR-011) | `SandboxTimeout`: máquina de fases (`Unmanaged → Active → Expired`, `ResumeGrace`), `LifecycleSpec` del `runHookPayload`, `TimeoutPolicy`/`TimeoutAction`/`TimeoutMode`, `set_timeout` `EXACT`/`AT_LEAST` acotado por `cap = max_lifetime − 60 s`, `admits` (qué RPC pasa tras el plazo), todo en el lado monotónico **crudo** del `Clock` (el tiempo suspendido cuenta, como en el tope de la plataforma); puerto `SelfTerminator` |
| `network` (M9, ADR-012) | gramática de `allow_out`/`deny_out` (`cidr`, `entry`: CIDR, IP, `*.dominio`, `ALL_TRAFFIC`), `policy` (semántica de E2B: un permitido gana, sin `deny_out` no se restringe nada; modos `Unrestricted`/`Routes`/`ProxyOnly`; `UpstreamProxy` con credenciales `Zeroizing`), `route_plan` (tablas 101/102/103, prioridades 150/151/149; `IMDS_TABLE`/`IMDS_PRIORITY` 100, por delante de todo slot), `swap` (cambio atómico, rollback, recuperación con deny-all de emergencia en `RECOVERY_SLOT`), `installation` (`Installation`: política, plan, slot y guardia DNS instalados, con sus transiciones), `probe` (`ip route get` por uid, `ip -o addr show`; el veredicto de verificación por observación, `VerifyFailure`), `special_address` (`SpecialAddress`: la única clasificación de loopback, no especificada, IMDS, link-local, multicast y broadcast), `guard` (`TargetGuard`: las direcciones especiales y las propias del guest; `UpstreamGuard`), `proxy_protocol` (HTTP CONNECT/forward, SOCKS5 servidor y cliente RFC 1928/1929; `ConnectFailure` con su respuesta HTTP y SOCKS), `proxy_env` (las ocho variables `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY`), `state` (`EgressEnforcement`), `error` (`NetworkError::status_class`) |
| `metrics_history` (M9) | `MetricsHistory`: anillo de 5 760 muestras procfs (5 s × 8 h), rango inclusivo, reducción a `max_points` (última muestra de cada tramo con `cpu_used_pct` promediado) y el estado puro del muestreador |
| `filesystem::metadata` (M9) | `FileMetadata`: claves de caracteres de token HTTP en minúsculas, valores ASCII imprimible, ≤ 64 claves y ≤ 4 000 B, nombre `user.rayito.<clave>`; errores fijos que nunca citan una clave ni un valor |
| `orphans` (Q80, `rayd-orphan-reaper`) | qué zombis puede recoger el PID 1: `ProcEntry` (pid, ppid, zombi, hora de arranque del campo 22 de `/proc/[pid]/stat`), `OwnedChildren` (los hijos que lanzó el propio `rayd`, por pid **y** hora de arranque, para que un pid reciclado nunca pase por propio; se olvidan solos cuando salen de la tabla de procesos), `orphans_to_reap`/`sweep` (olvidar los que ya salieron y recoger con un `waitpid(pid, WNOHANG)` exacto cada zombi reasignado a `rayd` que no es suyo, nunca `waitpid(-1)`) y `adopts_orphans` (PID 1 o *child subreaper*) |
| `filesystem::write` | `DISK_RESERVE_BYTES` 256 MiB: `check_disk_reserve(free)` con el `free_bytes` del puerto (`statvfs` del ancestro existente más profundo) antes de crear el temporal (`DiskReserve`/`DiskFull` → `RESOURCE_EXHAUSTED` con detalle `disk_reserve`/`disk_full`) |

Puertos (traits) que el dominio necesita del mundo exterior:

| Trait | Responsabilidad |
|---|---|
| `ProcessSpawner` | lanzar un proceso con uid/gid, cwd, env construido desde cero, rlimits, grupo de procesos; señalar al grupo |
| `PtyBackend` | `openpty`, lanzar el shell con la PTY como terminal de control, `resize`, lectura/escritura |
| `FileSystem` / `WriteSink` | operaciones de fichero con identidad de usuario, escritura atómica, `free_bytes` (M6); M9: `WriteSink::set_metadata` (`fsetxattr` sobre el temporal antes del rename), `FileSystem::read_metadata` (`llistxattr`/`lgetxattr` sin seguir symlinks) y `FileSystem::open_snapshot` (`O_RDONLY | O_NOFOLLOW`, sólo ficheros regulares, `pread` para exportar) |
| `Watcher` | instalar/quitar watches y entregar eventos |
| `NameResolver` | uid/gid → nombre de usuario/grupo para `EntryInfo` (con fallback numérico) |
| `KernelSidecar` / `SidecarLink` | lanzar el sidecar con identidad (`SpawnSpec`) y hablarle por su `SidecarLink` (`send` de líneas JSON con backpressure, `kill` al grupo de procesos); los eventos y la salida llegan por `SidecarEventSink`/`SidecarExitSink` |
| `KernelStatus` | `kernel_ready()` para `Health` (contexto por defecto `Ready`, ni `Warming` ni `Rotating` ni sidecar caído) y `kernel_state_lost()` (enganchado en cada `/resume`) |
| `RandomSource` | bytes aleatorios del SO para `ContextId`/`ExecutionId` (`getrandom`, nunca un PRNG sembrado antes del snapshot) |
| `Clock` | monotónico + reloj de pared, para timeouts y `clock_offset_ms` (y tests deterministas) |
| `HomeArchiver` (M7) | `count` (pre-walk), `archive` (tar+gzip nivel 1 hacia un `ArchiveSink`, sha256 del comprimido) y `extract` (desde un `Read`) del `HOME` bajo la identidad del usuario |
| `ObjectStore` / `ObjectBody` / `PartSource` / `ChunkSink` (M7) | `probe_credentials`, `get`, `put`, `put_multipart` (partes secuenciales de 8 MiB, abort en todo fallo, interrupción o drop) contra `StoreTarget{bucket, region}`; los extremos de los dos canales acotados sin `tokio` en el dominio |
| `BlockingRunner` (M7) | ejecuta el archivado/unpack en el pool bloqueante y devuelve un futuro (`spawn_blocking` en `rayd`, un hilo std en los tests del dominio) |
| `SignedHttp` (M9) | `send(SignedRequest{GET/PUT/DELETE, url Zeroizing}, body)` → cabecera + cuerpo en streaming; `HttpError{kind}` sin URL, host ni dirección (ADR-010) |
| `SelfTerminator` (M9) | `begin`/`force` la salida de `rayd` al vencer el plazo en modo `kill` (ADR-011) |
| `MetricsProbe` (M9) | una lectura procfs (CPU, memoria con caché, disco) para el muestreador de `MetricsHistory` |
| `ProcessTable` (`rayd-orphan-reaper`) | instantánea de la tabla de procesos, una entrada por pid y `reap(pid)` sobre exactamente ese pid |

**`rayd` (adaptadores)**:

- `grpc/`: un módulo por servicio; convierte tipos de `rayito-proto` ↔ dominio,
  añade el interceptor de `x-access-token` y los `KeepAlive`.
- `hooks/`: router `axum` con las seis rutas; sólo parsea HTTP y delega en
  `rayd-core::hooks`. `hooks::serve` es su listener: hyper HTTP/1.1 con el
  tope de conexiones compartido con el gRPC, un plazo para la cabecera y
  una petición por conexión.
- `lifecycle/`: `SuspendSignal` (`watch` de la generación de suspend + contador
  de streams abiertos), `SuspendableStream` (envuelve cada server-stream y
  emite su forma de cierre al `broadcast`), `running_sleep` (dormir en el
  reloj de ejecución) y `suspend_watchdog` (M6: tick de 1 s en
  `CLOCK_MONOTONIC`; un salto > 5 s es la firma del checkpoint real y termina
  el watchdog; 20 s sin salto con la misma `suspend_generation` ⇒
  `recover_from_stale_suspend`).
- `adapters/`: `TokioProcessSpawner` (`tokio::process` + `nix` para `setsid`,
  `setrlimit`, `killpg`), `NixPtyBackend` (`openpty`, `setsid` + `TIOCSCTTY`
  en `pre_exec`, maestro `O_NONBLOCK` en un `AsyncFd`, `TIOCSWINSZ`),
  `StdFileSystem` (`std::fs` bloqueante
  bajo `spawn_blocking`; cada operación entra en un `FsIdentityGuard` que hace
  `setfsgid`/`setfsuid` al usuario y lo restaura al salir), `NotifyWatcher`
  (`notify` 8.x, el hilo de inotify hereda la identidad), `NixNameResolver`
  (uid/gid → nombre), `TokioSidecarLauncher` (`tokio::process` con el mismo
  `PreExecPlan` que los procesos: uid 1000, grupo de procesos, rlimits; stdin
  por `mpsc` de 1024 líneas, stdout con `LinesCodec` de 16 MiB, stderr
  reemitido sólo con los campos JSON permitidos), `OsRandomSource`
  (`getrandom`), `SystemClock`; M6: `capabilities` (`GuestCapabilities` desde
  `/proc/self/status` + `/sys/fs/cgroup/cgroup.controllers`) e `imds_block`
  (`ip -4 rule add uidrange 1000-65535 lookup 100` + `ip -4 route replace
  blackhole 169.254.169.254/32 table 100`, mismo par IPv6 best effort;
  `rule_present` por `ip … show`, `probe_root` = connect como root sin enviar
  nada, la sonda como uid 1000 es un `python3 -c` por el propio
  `ProcessSpawner`; `ImdsState` atómico que `Health` refleja). El bloqueo es
  una ruta de política y no `iptables -m owner` porque el kernel del guest no
  trae `xt_owner` (`AWS_API_NOTES.md` Q48), y empieza en uid 1000 porque el
  agente de la plataforma usa uids 991–994 con su propio canal a IMDS. M7
  (ADR-009): `TarHomeArchiver` (`tar` 0.4 + `flate2` `rust_backend` +
  `sha2`; DFS ordenado por bytes, sin cruzar `st_dev` ni seguir symlinks,
  lector con tope y relleno para ficheros vivos; unpack con `unpack_in`,
  sólo regular/dir/symlink, modo enmascarado a `0o777`, `overwrite`) y
  `S3ObjectStore` (`aws-sdk-s3` 1.148 + `aws-config` 1.12 con
  `ImdsCredentialsProvider` del perfil `execution_role`, o la cadena por
  defecto sólo con `--persistence-credentials default`; un cliente por
  región, `BehaviorVersion::latest()`, reintentos estándar, checksums
  `WhenRequired`, multipart con guardia que aborta en `Drop` con 5 s de
  presupuesto; clasifica `NoSuchKey`/`AccessDenied`/`ExpiredToken`/
  `PermanentRedirect`); `persistence/` (`PersistenceManager`: `prepare_*` en
  línea → status, el resto en una tarea que alimenta el stream de eventos con
  muestreo de progreso cada 1 s; `PartWriter`/`ChannelParts` y
  `ChannelSink`/`ChunkReader` con marco `End` explícito; `TokioRunner`).
  M9: `HyperSignedHttp` (`adapters/signed_http.rs`: `hyper-util` + `hyper-rustls`
  con el proveedor `aws-lc-rs` nombrado explícitamente, sólo HTTPS y HTTP/1.1,
  `FilteringResolver` que descarta toda dirección que `is_forbidden_address`
  rechaza, sin redirecciones, 5 s de conexión y 30 s de inactividad) y
  `transfer/` (`TransferManager`, importación, exportación, `WatchTransfer` y
  la barrera); `lifecycle/timeout_watcher.rs` (hilo `rayd-timeout` fuera del
  runtime que avanza el plazo cada 500 ms y decide si hubo un checkpoint real),
  `lifecycle/exit_terminator.rs` (`ExitTerminator`, la salida con código 124),
  `grpc/timeout_gate.rs` (la capa `sandbox_timeout`) y `grpc/lifecycle.rs`;
  `lifecycle/metrics_sampler.rs` (la tarea de 5 s de `MetricsHistory`) y
  `adapters/procfs_metrics.rs`; `adapters/ip_command.rs` (el ejecutor de `ip`
  compartido con `imds_block`), `adapters/egress_routes.rs` (tablas, reglas y
  sondas de ADR-012) y `network/` (`NetworkManager`, el proxy local
  `127.0.0.1:0` con 128 conexiones, la cadena SOCKS5 al proxy del operador y
  los contadores `egress_proxy_stats`); `grpc/compression.rs`
  (`CompressionOptInLayer`: gzip sólo con `rayito-compress: gzip`).
  `rayd-orphan-reaper` (Q80): `adapters/child_registry.rs` (`ChildRegistry`,
  uno por proceso como la lista de hijos del kernel: **todo** hijo de `rayd`
  —procesos, shells de PTY, el sidecar, `mount-s3`, la sonda `stat` del
  montaje, `ip`, el `ready_cmd` de un template— se lanza con
  `ChildRegistry::spawn`, que sostiene un candado compartido desde antes del
  `fork` hasta registrar el hijo; la pasada de recogida lo toma en exclusiva,
  así que nunca ve un hijo que existe y aún no está registrado; el
  `clippy.toml` del workspace prohíbe `Command::spawn`/`status`/`output` en
  cualquier otro sitio), `adapters/procfs_process_table.rs`
  (`ProcfsProcessTable`), `adapters/orphan_reaper.rs` (`OrphanReaper`, activo
  sólo si `rayd` es PID 1 o *child subreaper*) y
  `lifecycle::spawn_child_reaper` (una pasada en el pool bloqueante en cada
  `SIGCHLD` y cada 5 s). Así un demonio con doble `fork` no deja `<defunct>`
  colgado de `rayd`, y tokio sigue recibiendo el estado de salida de cada
  hijo propio.
- `main.rs`: parseo de flags, wiring, arranque de los dos listeners, `anyhow`
  sólo aquí.

Reglas: `unwrap_used`/`expect_used` denegados en el workspace (permitidos en
tests vía `clippy.toml`); `Command::spawn`/`status`/`output` de `std` y de
`tokio` prohibidos fuera de `ChildRegistry::spawn` (`disallowed-methods`); los tests de dominio corren en Windows con puertos
falsos; los de adaptadores necesitan Linux (WSL2 o CI).

### Política de egress (M9)

[ADR-012](#adr-012--política-de-egress-en-el-guest-sobre-rayito-base-caps-conectores-de-plataforma-sólo-por-vpc-del-cliente).
Dos capas que comparten un único objeto de política en `rayd`, y sólo donde
`CapEff` tiene `CAP_NET_ADMIN` (`rayito-base-caps`):

1. **Rutas del kernel**: una tabla por uid (`uidrange 1000-65535`) decide cada
   paquete del código del sandbox. Sólo hacen falta `blackhole`s: tras restar
   los permitidos de los denegados, lo que no casa cae a `main` (permitido), y
   un permitido gana aunque sea más amplio que el denegado.
2. **Proxy local de `rayd`** (root, `127.0.0.1:<efímero>`): decide las reglas
   por nombre de host (sólo puertos 80/443) y encadena al proxy SOCKS5 del
   operador (`egress_proxy`).

| Hueco | Tabla | Prioridad de la regla | Uso |
|---|---|---|---|
| `A` | 101 | 150 | política activa o siguiente |
| `B` | 102 | 151 | política activa o siguiente |
| emergencia | 103 | 149 | deny-all transitorio durante una recuperación |

Las prioridades 149–151 van detrás de `local` (0) y de la regla de IMDS de
M6 (100, tabla 100) y antes de `main` (32766); la regla de IMDS y su tabla no
se tocan nunca. Un cambio instala la política nueva en el hueco libre, cambia
la regla y borra el viejo: nunca hay un instante sin tabla completa, y un
fallo deja la anterior o, si tampoco se puede, el deny-all de emergencia.

| Modo | Cuándo | Qué instala |
|---|---|---|
| `Unrestricted` | sin `deny_out` ni `egress_proxy` | nada |
| `Routes` | `deny_out` sin reglas de nombre de host | `blackhole`s de `deny_out \ allow_out` |
| `ProxyOnly` | nombres de host permitidos o `egress_proxy` | `blackhole` de todo lo directo; sólo sale el proxy |

El proxy lee el primer byte (`0x05` = SOCKS5, si no HTTP/1.x `CONNECT` o
forma absoluta), aplica la guardia **después** de resolver (loopback,
link-local e IMDS, multicast, las direcciones propias del guest y
`localhost`), marca contra la dirección comprobada sin volver a resolver y,
bajo deny-by-default, nunca convierte un nombre denegado en una consulta DNS
(T17). El DNS directo de uid ≥ 1000 no pasa por las rutas: los resolvedores
de la plataforma escuchan dentro del guest, pero bajo deny-all una regla
`ip rule` de puerto 53 los intercepta antes de que la tabla `local` los
resuelva (adenda de ADR-012, opción A, `m10-rayd-hardening`). En la
forma absoluta `http://` el proxy reescribe `Host` con la autoridad que
comprobó (RFC 9112 §3.2.2) y rechaza las líneas plegadas (obs-fold), así que
un nombre permitido no sirve de fachada para otro host virtual de la misma
IP; las direcciones IPv4-mapeadas (`::ffff:a.b.c.d`) e IPv4-compatibles
(`::a.b.c.d`) se juzgan y se marcan como su IPv4. **Residual**: `CONNECT` y
SOCKS5 llevan TLS que el proxy no inspecciona, así que el SNI y el `Host`
interior los pone el cliente y un nombre permitido en 443 puede servir de
fachada para otro nombre servido desde la misma dirección (CDN compartidas;
T17). Una vez arrancado, `rayd` exporta `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY` y `NO_PROXY` (y sus minúsculas) a cada proceso, PTY y kernel que
lance después, **debajo** de los `envs` del usuario; el sidecar no las
recibe. Un cliente que no honra el proxy falla cerrado en modo `ProxyOnly`.
`Health.egress_enforcement` publica `GUEST_ROUTES` o
`GUEST_ROUTES_AND_PROXY` sólo con la política verificada por `ip route get`.

### Kernel sidecar

Proceso Python 3.12 hijo de `rayd` (`/opt/rayito/sidecar`, paquete
`rayito_kernel_sidecar`, nunca publicado en PyPI), lanzado **como uid 1000**
con el mismo `PreExecPlan` que cualquier proceso del sandbox. No hay socket
Unix propio: `rayd` habla con él por **stdin/stdout con JSON lines**, un
protocolo que define y versiona `rayd` (`rayd-core::code::protocol`, v1; el
golden `kernel-sidecar/tests/fixtures/protocol_v1.jsonl` lo prueba desde los
dos lados). Si el sidecar muere, `rayd` lo relanza con backoff (0,5 s
doblando hasta 30 s), mata el grupo de procesos de los kernels huérfanos,
vacía el registro de contextos y reporta `kernel_ready=false` hasta que el
contexto por defecto vuelve a estar idle; tres timeouts consecutivos de op lo
matan (salvo cuando el lector está bloqueado en un cliente atascado, ver
backpressure).

Los `kernel_pid` que reporta el sidecar (`ready` y las respuestas de
crear, reiniciar o rotar un kernel) no son de fiar: cualquier proceso uid
1000 puede escribir en su tubería de stdout (`SECURITY.md` T12). `rayd` sólo
registra un pid que `/proc` confirma como hijo directo del sidecar en curso,
líder de su propio grupo y del mismo uid que el sidecar (`KernelProcesses`,
`rayd_core::code::kernel_process`; adaptador `ProcfsProcessTable`), lo fija
por su hora de arranque y lo vuelve a comprobar antes de cada `killpg`; un
pid que no pasa se descarta con `kernel_pid_rejected` y su grupo no se
señala nunca.

- Un `ipykernel` por contexto, arrancado con `jupyter_client.AsyncKernelManager(
  kernel_name="rayito", transport="ipc", ip="/run/rayito/k/<context_id>/k")`
  (directorio `0700` de uid 1000 que `rayd` prepara en cada arranque), como uid
  1000. Un `asyncio.Lock` por contexto: una ejecución a la vez por kernel; las
  demás esperan en cola y un `interrupt{execution_id}` cancela una encolada.
- Kernelspec `rayito` (`jupyter/kernels/rayito/kernel.json`, `interrupt_mode:
  signal`) generado en el `--socket-root` desde `--sidecar-root`, con
  `--config=<root>/ipython/ipython_kernel_config.py`: `NoColor`,
  `max_seq_length = 0`, `HistoryManager.enabled = False`,
  `capture_fd_output = True` y `exec_files` con los scripts de
  `ipython/startup/` (`0001_charts.py` formatter `e2b/chart`, `0002_data.py`
  `e2b/data` para `DataFrame`/`Series`, `0003_images.py` PNG/JPEG de PIL,
  `0004_warmup.py`). **No** fijar `MPLBACKEND`; `OPENBLAS_NUM_THREADS=1`.
- Pins en `kernel-sidecar/requirements.txt`, reproducibles desde
  `kernel-sidecar/requirements.in` con `uv pip compile --only-binary :all:
  --python-platform aarch64-manylinux_2_28 --python-version 3.12` (aarch64
  cp312 wheels, sin compiladores en la imagen): `ipykernel==7.3.0`,
  `jupyter_client==8.10.0`, `ipython==9.17.1`, `pyzmq==27.2.0`,
  `matplotlib==3.11.2`, `pandas==3.0.6`, `numpy==2.5.3`, `scipy==1.18.1`,
  `scikit-learn==1.9.1`, `pillow==12.3.0`, `pydantic==2.13.5` (lo importa
  `e2b_charts`).
- `e2b_charts` vendorizado (MIT, `_vendor/e2b_charts`) para que `chart` (JSON
  de matplotlib) y `data` (DataFrame, `orient=list`) se produzcan en el kernel
  y viajen como mime types.
- Warm-up antes de `/ready`, en el contexto por defecto: `numpy`, `pandas`,
  `matplotlib.pyplot`, `scipy.stats`, `sklearn.linear_model`, un PNG por
  `savefig`, un `chart_figure_to_chart`, `describe()` y `linalg.inv`, con el
  namespace limpio al terminar. Coste medido (`AWS_API_NOTES.md` §16 Q34):
  memoria del snapshot 925 MB (572 MB sin el stack), code install 1,29 GB,
  build 185–217 s; `warmup_ms` 9,3–9,7 s en la VM de build. Flag de build:
  el marcador `warmup_variant` = `slim` junto a los scripts (sólo dentro del
  zip de la variante slim, ver "Variantes de imagen") lo desactiva. Los
  formatters `e2b/chart` y `e2b/data` (`0001_charts.py`, `0002_data.py`)
  resuelven `Figure`, `DataFrame` y `Series` por nombre de módulo y clase en
  el `lookup` de IPython, sin importar matplotlib ni pandas al arrancar (un
  registro `for_type_by_name` no sirve: `select_figure_formats` del backend
  inline lo borra al primer `import matplotlib.pyplot`). **Coste/beneficio
  medido (M6, §16 Q42)**: como `/run` reinicia el kernel, el warm-up se
  ejecuta otra vez en cada `create()` y cuesta ≈ 2,2 s de `kernel_ready`
  (p50 5,20 s con él, 2,99 s sin él en `rayito-base-slim` 2.0) más 225 MB de
  snapshot (una diferencia de restore de ≈ 0,5 s en `agent_ready` que queda
  dentro de la resolución de la medida, $0,000 35 de lectura por
  lanzamiento); compra 0,65 s en la primera celda que importe pandas +
  matplotlib (0,14 s frente a 0,79 s). Decidir si se mantiene, se recorta o
  se traslada a `/validate` es un cambio de imagen (M6 Track A / seguimiento),
  no de este documento.
- `/run` **reinicia el kernel por defecto** (`restart_context{"default", envs
  del payload}`) en una task tras el 200: connection file nuevo (clave HMAC
  nueva), proceso nuevo (`random` y `numpy.random` se siembran de `os.urandom`
  al importar), los `envs` del sandbox, el warm-up otra vez desde la page cache.
  `kernel_ready` es `false` mientras dura. Medido: `restart_ms` 2 030–4 077 y
  `run-microvm → kernel_ready` 3,4–8,1 s (p50 6,2 s) en `rayito-base` 8.0
  (§16 Q35); el primer arranque de Python tras el restore lee 1,3 GB de
  site-packages del disco perezoso, por eso `/validate` hace el mismo reinicio
  para que Lambda prefetchee esas páginas (sin él: 45 s). `/resume` reseed
  `random`/`numpy.random` en cada kernel vivo con una celda silenciosa.
- Los `timeout_ms` del agente (interrupt y restart) corren en el reloj de
  ejecución ("Suspend / resume"): una celda en curso al suspender conserva
  tras el resume el plazo que le quedaba, aunque `CLOCK_MONOTONIC` avance
  durante la pausa (§16 Q19, Q41). Los deadlines gRPC del cliente siguen en
  el reloj del cliente.
- Todo pasa por un único stdio serializado (una línea por evento, tope 16 MiB
  en `rayd` y 15 MiB en el sidecar): dos contextos ejecutando a la vez
  comparten ese ancho de banda, y un `result` de más de 12 MiB en total se
  recorta a `rayito/omitted` por valor (los mayores primero).
- Mapeo Jupyter → proto: `execute_input` → `ExecutionStarted`; `stream` →
  `OutputChunk`; `display_data`/`execute_result` → `ExecutionResult`
  (`is_main_result` sólo para `execute_result`); `error` → `ExecutionError`
  (nunca terminal); `status: idle` → `ExecutionEnd`.
- Backpressure de extremo a extremo: `rayd` acota la cola de cada suscriptor
  de `Execute`/`Reattach` (256 eventos; el recorder nunca espera a un cliente:
  cola llena ⇒ `OutputTruncated` inmediato sólo para ese suscriptor, que puede
  volver con `Reattach(last_seq + 1)` gracias al ring) y el
  sidecar acota el buzón entre los sockets ZMQ y la celda (64 mensajes; los
  centinelas `died`/`abort` nunca esperan), así que un cliente lento frena al
  kernel (HWM de ZMQ) en vez de llenar la memoria del sidecar. Ningún evento
  supera una línea de 15 MiB (`result` de más de 12 MiB en total o una línea
  mayor sale como `rayito/omitted`) y los timeouts de ops que vencen mientras
  el lector está aparcado en un cliente atascado no cuentan para el
  kill-switch de tres timeouts.

---

## Capa 3 — Cliente SDK

Generado desde `.proto` con `buf` (`buf.gen.yaml`, plugins pinneados). No se
escribe transporte a mano.

Los dos shims de E2B, `rayito.e2b` (Python) y `rayito/e2b` (TypeScript, M9,
superficie de E2B 2.x), son capas de composición sobre los SDKs nativos: no
hablan con `rayd` ni con AWS por su cuenta, traducen nombres, valores por
defecto y errores, y lanzan `UnimplementedError` donde Lambda MicroVMs no
tiene primitiva. Desde M9 los SDKs también firman URLs de S3 con las
credenciales del llamante (boto3 en Python; `@aws-sdk/client-s3`,
`@aws-sdk/s3-request-presigner`, `@aws-sdk/s3-presigned-post` y
`@aws-sdk/lib-storage` en TypeScript, cargados bajo demanda) para
`upload_url`/`download_url` y los ficheros grandes (ADR-010); `rayd` nunca ve
esas credenciales.

### Python (`clients/python`, paquete `rayito`)

- Stack: `grpcio>=1.84,<2`, `protobuf>=7.36.1,<8` (el suelo es igual al patch
  del plugin `buf.build/protocolbuffers/python:v36.1`; se sube junto),
  `boto3>=1.43.82,<2`. `requires-python >= 3.11`, backend `uv_build`, src layout,
  `py.typed`. El gencode va a `src/rayito/v1/` (importable como `rayito.v1.*`).
- Layout: `_models.py` (dataclasses), `exceptions.py`, `_limits.py`,
  `_transport.py` (canales, `TokenStore`, mapeo de errores, `is_reconnectable`),
  `_aws.py` (boto3, testable con `Stubber`), `_sandbox_base.py` (constructores
  de request, mapeo evento → modelo, `ReconnectPoll`/`reconnect_failure`,
  `GateRetry` (reintento de una resuscripción rechazada por el phase gate,
  compartido por `Connect`, `Pty.Connect`, `WatchDir` y `Reattach`) e
  `imds_open_warning_due` (el aviso de IMDS espera la ventana de
  verificación de `rayd`), sin I/O), `_process_base.py`, `_filesystem_base.py`, `_code_base.py` y
  `_pty_base.py` (helpers puros por servicio: `validate_pty_size`,
  `build_pty_start_request`, `PtyMessages`), `sandbox_sync/{main,commands,filesystem,pty,code}.py`
  (`grpc`) y `sandbox_async/…` (`grpc.aio`, `AsyncSandbox`). Misma superficie
  sync y async; un canal nunca se comparte entre ambos. Las llamadas de plano de
  control en async van por `asyncio.to_thread` (sin `aiobotocore`).
  `e2b/` (M6): el shim de E2B por composición (`_compat.py` helpers puros y la
  tabla de kwargs, `_models.py` `SandboxInfo`/`SandboxMetrics`/`PtySize`/
  paginadores con la forma de E2B, `_sync.py`/`_async.py` wrappers sobre el
  nativo, `exceptions.py` con los nombres de `e2b.exceptions` más
  `UnimplementedError` y `RayitoCompatWarning`).
- Metadatos (M6): `create(metadata=)` viaja en el payload; `sbx.metadata` es el
  mapa del último `Health` (inmutable, definitivo desde el primer
  `agent_ready`); `Sandbox.get_info(id)` acuña un JWE y manda **un** `Health`
  por un canal dedicado sólo si el sandbox está `RUNNING`; `list(metadata=)`
  pagina `list-microvms` con `states=("RUNNING",)` y sondea cada item en
  orden (`get-microvm` + JWE + `Health`, O(n), un `Health` fallido es
  `SandboxException` con el id, nunca una lista incompleta), rechazando
  `states` distintos de `RUNNING` porque una sonda despertaría un sandbox
  suspendido.
- `with Sandbox.create() as sbx:` → `kill()` al salir. Sub-clientes
  `.commands`, `.files`, `.pty`; `run_code()` en la raíz.
- Vocabulario de timeouts (el de E2B): `request_timeout` (unarios, 60 s),
  `timeout` por operación (commands/pty 60 s, `run_code` 300 s, `watch_dir` 0 =
  ilimitado; mata o interrumpe la operación en el servidor) y `timeout` del
  sandbox (vida máxima, en `create`) y `reconnect_timeout` (60 s, cuánto
  espera un corte a que el agente vuelva).
- `Execution(results, logs: Logs(stdout, stderr), error, execution_count)`;
  `.text` es el `text` del resultado con `is_main_result`. `Result(text, html,
  markdown, svg, png, jpeg, pdf, latex, json, javascript, data, chart,
  is_main_result, extra)` con `formats()` y `_repr_*_`. `ExecutionError(name,
  value, traceback)`.
- `commands.run(cmd)` envía `ProcessConfig(cmd="/bin/bash", args=["-l","-c",cmd])`;
  deadline gRPC del stream = `timeout + 5 s` para que el `EndEvent` del servidor
  llegue primero. `CommandHandle`/`PtyHandle` comparten implementación
  (`pid`, `wait`, `kill`, `disconnect`, `send_stdin`, `resize`, `reconnects`,
  `last_seq`); `PtyHandle` itera `(None, None, bytes)` y acepta `on_data`.
  `sbx.pty.create(size=PtySize(cols, rows), envs, cwd, user, timeout,
  on_data)`, `.connect(pid, from_seq)`, `.send_input`, `.resize`, `.kill`;
  `sbx.pause(wait)`, `sbx.resume(wait)`, `sbx.get_health()`.

### TypeScript (`clients/typescript`, paquete `rayito`)

Misma API que Python en camelCase y milisegundos (`*Ms`, el vocabulario del
SDK JS de E2B), **sólo async** (Node no tiene cliente gRPC bloqueante), ESM +
CJS construidos con `tsdown`, Node >= 20, `pnpm`, Biome, vitest. Versión
`0.2.0` = la misma `MAJOR.MINOR` que el paquete Python y que `rayd` (lockstep
de `docs/RELEASING.md`; el 0.0.5 anterior fue la generación de M6, que
saltó a 0.2.0 con la release de M7).

- Stack: `@bufbuild/protobuf` ^2.15 (suelo = versión del plugin
  `buf.build/bufbuild/es:v2.15.0`, `target=ts`, `import_extension=js`),
  `@connectrpc/connect` ^2.2 + `@connectrpc/connect-node` ^2.2,
  `@aws-sdk/client-lambda-microvms` ^3.1133 + `@aws-sdk/client-sts` (sólo para
  resolver un template por nombre), `@smithy/node-http-handler`. Gencode
  commiteado en `src/gen/rayito/v1/*_pb.ts` (`make proto` regenera Python y
  TypeScript a la vez); ningún request/response se escribe a mano
  (`create(XxxSchema, …)`, oneofs como `{ case, value }`).
- Layout (`src/`): `index.ts` (exports + polyfill de `Symbol.asyncDispose`),
  `limits.ts` (GENERADO desde `limits.json`), `errors.ts`, `models.ts`,
  `charts.ts`, `payload.ts`, `logger.ts`, `version.ts`, `aws/control-plane.ts`
  (puerto `ControlPlane`, `PortSpec`, `LaunchRequest`, `TokenBucket`,
  `LambdaMicrovmsControlPlane`, `sharedControlPlane`, `translateAwsError`),
  `transport/{headers,tokens,transport,errors}.ts`, `sandbox/launch.ts`
  (validación de `create()`), `sandbox/readiness.ts` (`ReadinessPoll`,
  `ReconnectPoll`, `ReconnectBudget`, `reconnectFailure`), `sandbox/core.ts`
  (`SandboxCore`: transportes, clientes por servicio, `callUnary`/`openStream`,
  `waitUntilReady`, `recordHealth`, `reconnect`), `sandbox/sandbox.ts` (la
  clase pública `Sandbox`), `sandbox/{commands,filesystem,pty,code}.ts`
  (helpers puros + `Commands`/`CommandHandle`, `Filesystem`/`WatchHandle`,
  `Pty`/`PtyHandle`, `CodeClient`/`ExecutionBuilder`).
- Transporte: un `Http2SessionManager(baseUrl, { pingIntervalMs 30000,
  pingTimeoutMs 10000, pingIdleConnection, idleConnectionTimeoutMs 15 min })`
  explícito por transporte, pasado como `sessionManager` a
  `createGrpcTransport({ baseUrl: "https://<endpoint>:443", interceptors:
  [proxyAuth], readMaxBytes 64 MiB })`. HTTP/2 únicamente (la v2 no tiene
  opción `httpVersion`). Un transporte es un `Http2SessionManager` = una
  conexión: `Sandbox` abre como máximo dos (`unary`: `Health`, `Metrics`,
  unarios, `Start` en foreground, `Read`, `Write`, `Execute`, `Reattach`;
  `stream`, perezoso: `Start` en background, `Connect`, `Pty.Create/Connect`,
  `WatchDir`) y `close()` las aborta (`sessionManager.abort()`, el
  `channel.close()` de Python; sin eso vivirían 15 min con PINGs). `scheme:
  "http"` (h2c en claro) sólo se admite hacia loopback: es para el `rayd`
  falso; hacia otro host es `InvalidArgumentError`. No hay backoff de
  reconexión que acotar: tras un fallo, la siguiente petición abre una sesión
  nueva. El
  interceptor lee el JWE del `TokenStore` en cada llamada y pone
  `x-aws-proxy-auth`, `x-aws-proxy-port`, `x-aws-proxy-force-h2` y
  `x-access-token` en minúsculas, así rotar el token nunca toca el transporte;
  sin token para el puerto lanza `AuthenticationError` antes de enviar. Cada
  RPC lleva `timeoutMs` (entero: `grpc-timeout`), cada stream su
  `AbortController`.
- Errores de Connect (`transport/errors.ts`): una respuesta HTTP sin
  `grpc-status` es `ConnectError("HTTP <status>", codeFromHttpStatus)`: 403
  → `PermissionDenied "HTTP 403"` (`isProxyForbidden`: reacuñar y reintentar
  una vez), 429/502/503/504 → `Unavailable`. Clasificación del contrato de
  reconexión: **reconectable** = phase gate (`Unavailable` con `suspending` |
  `terminating`) o reset (`Unavailable` que no es puerta, `Aborted`,
  `Canceled` con `rawMessage` `http/2 stream closed…`, `Internal` con ese
  prefijo, `protocol error: missing status` o una causa Node `ERR_HTTP2_*` /
  `ECONNRESET` / `EPIPE`); **nunca** `DeadlineExceeded`, el kernel gate
  (`Unavailable kernel not ready: …`), un 403 del proxy ni un `Canceled` de
  nuestro propio `AbortSignal`. Para el sondeo de `Health` (arranque y
  reconexión) "aún no" = `Unavailable` | `DeadlineExceeded` | reset: los
  cortes que grpc-core funde en `UNAVAILABLE` no abortan el sondeo. Lo que un interceptor lanza vuelve envuelto en
  `ConnectError(Unknown)` con la causa: `translateRpcError` desenvuelve los
  errores propios. Tabla unaria = la de Python con los nombres de E2B
  (`SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`,
  `FileNotFoundError`, `SandboxNotFoundError`, `SandboxNotReadyError`,
  `SandboxStateError`, `SandboxLifetimeError`, `CommandExitError`,
  `RateLimitError`; fuera de la jerarquía `AuthenticationError`,
  `QuotaExceededError`, `CapacityError`), con `name` y prototipo fijados para
  que `instanceof` funcione en CJS.
- Límites: `limits.json` (raíz) → `scripts/gen_limits.py` → `_limits.py` y
  `limits.ts`; `make lint` corre `--check` y cada SDK tiene un test de deriva.
- Tests: `tests/unit` con un `rayd` falso servido por `connectNodeAdapter`
  sobre `node:http2` en loopback (mismos guiones que los fakes de Python,
  `suspend()`/`suspendResume()`, `forbidNext()` para el 403 del proxy, contador
  de sesiones) y un `FakeControlPlane`; `tests/e2e/m6.e2e.test.ts` contra
  AWS real con `RAYITO_E2E=1` + `RAYITO_TEMPLATE`.

### Gestión de tokens (JWE del proxy)

- TTL máximo **60 min** (CLI help y docs; el modelo sólo codifica `min=1`).
  Se acuña con `create_microvm_auth_token(expirationInMinutes=60,
  allowedPorts=[{"port": 8080}])` y se lee `resp["authToken"]["X-aws-proxy-auth"]`.
- `TokenRefresher` (hilo daemon en sync, task en async) renueva a los **45 min**;
  si falla reintenta cada 60 s. 50 min dejaría 10 min para una tormenta de
  `ThrottlingException` contra un TTL duro. Se acuña también en `connect()` y
  tras `resume()`; lo para `kill()`/`__exit__`/`__aexit__`. Los tokens por
  puerto de `get_host()` comparten el refresher.
- Inyección por RPC: `grpc.metadata_call_credentials(ProxyAuthPlugin)` compuesto
  con `grpc.ssl_channel_credentials()`; el plugin devuelve
  `[("x-aws-proxy-auth", jwe), ("x-aws-proxy-port", "8080"),
  ("x-aws-proxy-force-h2", "true"), ("x-access-token", secret)]` en cada
  llamada (unarias y streams). Rotar nunca reconstruye canales ni corta streams.
- **403 del proxy**: no lleva `grpc-status`, así que grpc-core lo presenta como
  `PERMISSION_DENIED` (401 → `UNAUTHENTICATED`, 429/502 → `UNAVAILABLE`). El SDK
  reacuña y reintenta **una** llamada unaria (nunca un stream) sólo cuando
  `debug_error_string()` contiene `Received http2 header with status: 403`; un
  `PERMISSION_DENIED` genuino de `rayd` (p. ej. `EACCES`) no se confunde.
- Un token acuñado antes de `pause()` sigue siendo válido tras `resume()`; la
  expiración es en reloj de pared, el refresher no se detiene en pausa.

### Pools de conexión

Cada MicroVM tiene su propio hostname, así que el sharding por origen compartido
de E2B no aplica. El límite oficial va en sentido contrario: **8 conexiones
concurrentes por MicroVM a 1 vCPU** (2 GB, el tamaño por defecto), 16 a 2 vCPU,
32 a 4 vCPU, no ajustable; ≈40 req/s publicado sólo para 4 vCPU.

- El SDK abre **como máximo dos canales HTTP/2 por `Sandbox`**: uno para
  unarios y otro para streams largos (PTY, watch, procesos background,
  `Execute`). Nunca una conexión por llamada.
- `rayd` sirve h2c en texto plano, así que el cliente **debe** enviar
  `x-aws-proxy-force-h2: true` en cada RPC. TLS + ALPN en `rayd` se descarta en
  M1–M5.
- Cliente: `grpc.keepalive_time_ms=30000`, `grpc.keepalive_permit_without_calls=1`,
  `grpc.max_receive_message_length=64 MiB`.
- Muchos ficheros pequeños van en **un** stream `Write` (multi-fichero) para no
  agotar el cap de req/s.

### Pool de suspendidos (SDK)

`SandboxPool` / `AsyncSandboxPool` (Python) y `SandboxPool` (TypeScript),
ADR-008, cambio `m7-suspended-pool`. **Qué corre dónde**: un hilo de relleno
(una `asyncio.Task` o una promesa con temporizadores `unref()`) dentro del
proceso del cliente y nada en AWS salvo las plazas aparcadas: ni Lambda, ni
tabla, ni servicio. Cada plaza se calienta con el `create()` normal (un
`secrets.token_bytes(32)` por plaza), se asienta con una celda trivial (`rayd`
retiene `Execute` mientras rota el kernel de `/run`, así la plaza nunca se
aparca a mitad de la rotación; Q56), se aparca con `pause(wait=True)` y se
cierra el handle: el pool guarda un `SlotRecord` (id, endpoint, token,
`started_at`, `maximum_duration_seconds`, política de idle, conectores,
región, estado `warming`/`ready`, `parked_at`) en un `PoolBackend` (memoria
por defecto; fichero JSON `rayito.pool/1` 0600 y atómico, de un solo proceso,
para tests y recuperación en el mismo host), nunca canales ni JWE. **Toma**:
la plaza `ready` que caduca antes con ≥ `min_remaining_seconds` de vida, el
registro borrado antes de tocar la red, `resume-microvm` explícito (0,38 s
medidos frente a 0,67 s del auto-resume), JWE y canal con el token de la plaza
y un sondeo de `Health` de 0,1 → 0,5 s (`TakePoll`), sin `get-microvm`;
fallback a `create()` sin plaza o con la plaza perdida (el pool es una
optimización de latencia, nunca un semáforo). **Cuotas**: todas las llamadas
pasan por el plano de control compartido del proceso, así que el pool y los
`create()` de la aplicación comparten los buckets de `RunMicrovm` 5,
`SuspendMicrovm` 2, `ResumeMicrovm` 5, `TerminateMicrovm` 10 y
`CreateMicrovmAuthToken` 50 TPS; backoff 1 → 60 s tras un calentamiento
fallido. **Sweeper**: cada `sweep_interval_seconds` recicla las plazas con
menos de `min_remaining_seconds` (con los defaults, cada ≈ 7 h, nunca al muro
de 8 h) y reconcilia con un `list-microvms` (terminada fuera → repuesta;
reanudada fuera → vuelta a aparcar). Medido (`test_m7_pool.py`, 2026-09-16):
`take()` → primera celda p50 0,77 / p95 0,90 s frente a `create()` p50 6,15 /
p95 6,49 s. Custodia del secreto: `SECURITY.md` T14.

### `get_host(port)`

Un MicroVM tiene un único hostname; el puerto se elige con `x-aws-proxy-port` y
el JWE debe incluirlo en `allowedPorts` (si no, 403). Por eso `get_host` devuelve
un `HostAccess(str)` cuyo valor es el hostname (`f"https://{sbx.get_host(3000)}"`
sigue funcionando) con `url`, `port`, `headers` (`x-aws-proxy-auth`,
`x-aws-proxy-port`; sin `force-h2`) y `client() -> httpx.Client`. El token de
ese puerto se acuña perezosamente y lo renueva el mismo refresher;
`Sandbox.create(allowed_ports=[3000, (8000, 8999)])` los preautoriza en el token
principal. `allPorts` **nunca** se usa por defecto: el puerto 9000 queda fuera.

### Readiness y normalización

- `create()`: `run_microvm(clientToken=uuid4 reutilizado en reintentos)` →
  `create_microvm_auth_token` → normalizar `endpoint` (`urlparse`: si trae
  esquema, `netloc`; si no, tal cual; el runbook de M0 guarda el `endpoint` crudo para confirmarlo) → target gRPC `f"{host}:443"` →
  sondear `Health` cada 0,25 s doblando hasta 2 s, tratando `UNAVAILABLE` y 502
  como "aún no", hasta `agent_ready` (y `kernel_ready` desde M4).
- Pasados `ready_timeout` (90 s) se llama a `get_microvm` **una vez** para
  obtener `state` y `stateReason`, se termina la VM (salvo `keep_on_failure`) y
  se lanza `SandboxNotReadyException`. `TERMINATING|TERMINATED` durante el
  sondeo es fatal de inmediato.
- **Nunca** se usa el `state` de `get-microvm` como señal de readiness.
  `is_running()` = `Health` responde. `microvmId` se valida sólo por longitud
  (1–256), no por prefijo (`mvm-` en docs, `ai-` en CloudTrail).

### Plano de control (boto3 / SDK JS v3)

Este cliente de `lambda-microvms` es el único plano de control: los
componentes opcionales en la cuenta del cliente (secretos, índice de
metadatos) son clientes AWS aparte, apagados por defecto (ADR-014).

- Cliente `lambda-microvms` con `Config(retries={"mode": "standard",
  "total_max_attempts": 5}, connect_timeout=5, read_timeout=60,
  user_agent_extra="rayito/<ver>")`. El SDK TypeScript usa
  `@aws-sdk/client-lambda-microvms` (`LambdaMicrovmsClient({ maxAttempts: 5,
  retryMode: "standard", NodeHttpHandler({ connectionTimeout: 5000,
  requestTimeout: 60000 }), customUserAgent: [["rayito", VERSION]] })`) detrás
  del mismo puerto `ControlPlane`, con los mismos buckets y el mismo mapeo de
  errores por `error.name`; `list-microvms` se pagina a mano con
  `maxResults 50` + `nextToken` (el paginador del SDK exige una instancia real
  del cliente y el adaptador acepta cualquier `{ send }`).
- **Token buckets por proceso** alineados con las cuotas publicadas:
  `RunMicrovm` 5 TPS, `ResumeMicrovm` 5, `SuspendMicrovm` 2, `TerminateMicrovm`
  10, `GetMicrovm` 100, `CreateMicrovmAuthToken` 50. Un `pause()` masivo es
  lento por diseño (2 TPS).
- `run_microvm` siempre con `clientToken` (idempotente: un reintento tras
  timeout no lanza dos VMs), `maximumDurationInSeconds` (default 3600) y, si se
  quiere auto-suspensión, el `idlePolicy` completo (los tres campos son
  obligatorios si el bloque está presente). `logging.cloudWatch.logGroup`
  siempre explícito (`/rayito/<template>`) o `logging.disabled`.
- `suspend/resume/terminate` están marcados `idempotent` en el modelo;
  `terminate` sobre una VM ya terminada devuelve éxito.
- Mapeo de errores **por nombre de excepción, nunca por status HTTP**:

| botocore | `rayito.exceptions` |
|---|---|
| `ResourceNotFoundException` | `SandboxNotFoundException` |
| `ValidationException` | `InvalidArgumentException` |
| `AccessDeniedException` | `AuthenticationException` |
| `ThrottlingException` | `RateLimitException(retry_after=retryAfterSeconds)` |
| `ConflictException` | `SandboxStateException` (`pause()` devuelve `False`) |
| `ServiceQuotaExceededException` (HTTP **402**) | `QuotaExceededException` |
| `InsufficientCapacityException` (botocore ≥ 1.43.82) | `CapacityException` |
| `InternalServerException` | `SandboxException` |

| gRPC / `StreamError.code` | excepción |
|---|---|
| `INVALID_ARGUMENT`, `unimplemented`, `invalid_argument` | `InvalidArgumentException` |
| `UNAUTHENTICATED`, `PERMISSION_DENIED`, `permission_denied` | `AuthenticationException` |
| `NOT_FOUND` / `not_found` | `FileNotFoundException` en `FilesystemService`, `NotFoundException` en el resto |
| `RESOURCE_EXHAUSTED` | `RateLimitException` |
| `DEADLINE_EXCEEDED`, `CANCELLED`, `deadline_exceeded` | `TimeoutException` |
| `suspending` (final de stream o `UNAVAILABLE`) | reconexión (contrato de "Suspend / resume"); `SandboxStateException` sólo si reconectar es imposible |
| `UNAVAILABLE` o reset a mitad de stream | reconexión; si falla: `TERMINATING|TERMINATED` → `SandboxNotFoundException`, `SUSPENDED` sin auto-resume → `SandboxStateException`, `reconnect_timeout` agotado → `SandboxException` |
| `OUT_OF_RANGE` en `Connect`/`Pty.Connect` | la re-suscripción vuelve a `from_seq=0` con warning; `NotFoundException` cuando lo pide el usuario |
| `Reattach` `NOT_FOUND` / `OUT_OF_RANGE` | `SandboxException` ("se perdió salida de la ejecución") |

- Jerarquía: `SandboxException(status_code, grpc_code, aws_code)` ←
  `TimeoutException`, `InvalidArgumentException`, `NotFoundException` (←
  `FileNotFoundException`, `SandboxNotFoundException`),
  `SandboxNotReadyException(state, state_reason)`, `SandboxStateException`,
  `SandboxLifetimeException`, `CommandExitException(SandboxException,
  CommandResult)`, `RateLimitException`. Fuera de la jerarquía, como en E2B:
  `AuthenticationException`, `QuotaExceededException`, `CapacityException`.
- Módulo único de límites `_limits.py` (validados en cliente antes de llamar a
  AWS): `MAX_DURATION_SECONDS=28800`, `MIN_DURATION_SECONDS=1`,
  `IDLE_MAX_IDLE_MIN_SECONDS=60`, `IDLE_SUSPENDED_MIN_SECONDS=0`,
  `TOKEN_TTL_MINUTES=60`, `TOKEN_REFRESH_AFTER_MINUTES=45`,
  `RUN_HOOK_PAYLOAD_MAX_CHARS=4096`, `CLIENT_TOKEN_MAX=128`, `LIST_MAX_RESULTS=50`,
  `NETWORK_CONNECTORS_MAX=10`, `DEFAULT_PORT=8080`,
  `HOOK_PATH_PREFIX="/aws/lambda-microvms/runtime/v1"`, `MICROVM_STATES` (6),
  `SUPPORTED_REGIONS` (10, sólo informativo: se descubren con
  `list-managed-microvm-images`).
- Metadatos por sandbox: los MicroVMs **no se pueden etiquetar** y
  `list-microvms` sólo filtra por `imageIdentifier`/`imageVersion`. Fuera de
  M1–M5 (SPEC §4).
- Credenciales dentro del MicroVM: el execution role se sirve por IMDSv2 en el
  guest y **cualquier código del sandbox puede leerlo**.
  `Sandbox.create(execution_role_arn=None)` por defecto; sin rol no hay logs de
  runtime en CloudWatch. Ver `SECURITY.md`.

---

## ADR-001 — gRPC en lugar de Connect-RPC

**Contexto.** E2B usa Connect-RPC en `envd` (Go). La recomendación inicial fue
replicarlo.

**Decisión.** gRPC puro vía `tonic`, servido en h2c (texto plano) detrás del
proxy de AWS, que termina TLS. El cliente envía `x-aws-proxy-force-h2: true`.

**Razón.** El agente es Rust y `connect-rs` no tiene la madurez de `connect-go`.
La ventaja principal de Connect (JSON, navegador) no aplica: los clientes son
Python y TypeScript del lado servidor, y Lambda MicroVMs soporta gRPC inbound
de forma nativa. tonic 0.14 por defecto no arrastra `ring`/`aws-lc`/`openssl`,
así que el binario estático queda sin crate TLS. El contrato `.proto` es
idéntico: migrar a Connect no requiere rediseñar la API. En TypeScript el
cliente sí es Connect-ES (`createGrpcTransport`), que habla gRPC estándar.

**Consecuencia.** No se puede llamar al agente con `curl` sin tooling.
Mitigación: `grpcurl` en desarrollo y `grpc-reflection` en builds de debug.
Q17 (trailers y streams largos a través del proxy) se confirma en M1 con el
primer binario tonic; todos los streams son server-stream, no bidi (ADR-005).

---

## ADR-002 — Kernel de Jupyter en sidecar Python

**Contexto.** `run_code` con resultados enriquecidos requiere hablar el
protocolo wire de Jupyter (ZeroMQ, 5 sockets, firma HMAC). En 2026 existe un
cliente Jupyter en Rust (`jupyter-zmq-client`), así que la premisa original
("varias semanas de trabajo en Rust") ya no es cierta.

**Decisión.** Se mantiene el sidecar Python con `jupyter_client` para M4. Un
proceso hijo de `rayd`, protocolo JSON lines por stdio definido por `rayd`.
Consolidar todo en Rust es una **opción de M6**, no un objetivo.

**Razón.** El trabajo que de verdad cuesta está del lado Python en cualquier
caso: formatters de IPython, extracción de gráficos (`e2b_charts`), warm-up,
supervisión y reinicio de kernels, reseed. El salto por stdio es irrelevante
frente a la latencia de ejecución. El `.proto` no cambia con el backend, así
que la decisión es reversible sin tocar clientes.

**Consecuencia.** Dos procesos que supervisar. `rayd` reinicia el sidecar si
muere, reporta `kernel_ready`/`kernel_state_lost` en `Health` y `/ready` no
devuelve 200 hasta que el contexto por defecto está idle.

---

## ADR-003 — Sin abstracción multi-cloud

**Decisión.** El SDK habla directamente con la API de Lambda MicroVMs.

**Razón.** Una interfaz `Provider` genérica desde el día uno obliga a diseñar
para el mínimo común denominador entre backends que aún no existen, y es la vía
más rápida a un proyecto que no termina nada. El contrato `.proto` del agente
ya es portable: si algún día hay backend de Firecracker propio, se reimplementa
la Capa 3 contra el mismo agente.

---

## ADR-004 — Token del agente vía `runHookPayload`

**Contexto.** El diseño original inyectaba el access token del agente "como
variable de entorno en `run-microvm`". `RunMicrovmRequest` no tiene ese
parámetro; `environmentVariables` sólo existe en `create-microvm-image`, es de
nivel imagen y queda horneado en el snapshot compartido por todos los MicroVMs.

**Decisión.** El SDK genera el secreto, envía **su sha256** dentro de
`runHookPayload` (JSON `{"v":1,"token_sha256":…}`), AWS lo entrega como body de
`POST /run`, `rayd` instala el hash una vez por arranque y exige
`x-access-token` en todo RPC salvo `Health`, comparando en tiempo constante.

**Razón.** Es el único canal per-VM de la API y el análogo directo del
`POST /init` + hash de `envd`. Sólo viaja el hash porque el payload puede quedar
en CloudTrail. `connect(sandbox_id)` necesita el secreto: se persiste junto al
`sandbox_id` o se lee de `RAYITO_ACCESS_TOKEN`; no hay plano de control que lo
devuelva.

**Consecuencia.** Límite de 4096 caracteres para hash + `envs` + `user` +
`workdir`; el SDK mide el payload serializado y falla con
`InvalidArgumentException` señalando `envs` por comando y `files.write` como
alternativas.

---

## ADR-005 — Backend de PTY: `nix::pty::openpty` + `tokio::process`, no `portable-pty`

**Contexto.** Se evaluaron (A) `portable-pty` 0.9 ejecutando el shell vía
`setpriv --reuid=1000 …` (lector bloqueante → `spawn_blocking`, duplica `nix`
0.28 junto a `nix` 0.31) y (B) `nix::pty::openpty` + `tokio::process::Command`
con `.uid(u).gid(g).pre_exec(setsid + ioctl(TIOCSCTTY))` y `resize` vía
`ioctl(TIOCSWINSZ)`.

**Decisión.** (B). ~80 líneas, cambio de uid nativo, una sola versión de `nix`,
lectura asíncrona.

**Consecuencia.** `PtyService` es server-stream + unarios (`Create` →
`PtyServerMessage`, `Connect(pid)`, `SendInput`, `Resize`, `Kill`), la misma
forma que `envd`: no depende de streams bidi a través del proxy y permite que la
PTY comparta el `CommandHandle` del SDK (en `grpcio` sync un bidi exige un
iterador bloqueante y un tipo de handle distinto). Sólo Linux: tests bajo WSL2/CI.

---

## ADR-006 — Hooks en el puerto dedicado 9000, fuera de `allowedPorts`

**Contexto.** Los hooks son POST HTTP/1.1 en `/aws/lambda-microvms/runtime/v1/*`
sobre un puerto que declara la imagen. Servirlos en el puerto gRPC exigiría
`accept_http1` + mezcla de rutas en tonic y expondría los hooks a cualquiera
con un JWE del puerto 8080.

**Decisión.** `rayd` sirve los hooks en un listener HTTP/1.1 (router
`axum`, servido con hyper por `hooks::serve`) en `0.0.0.0:9000`, separado
del gRPC h2c en `:8080`. El SDK nunca acuña tokens con
`allPorts` por defecto ni incluye 9000 en `allowedPorts`.

**Consecuencia.** Si M0 (fila "hooks alcanzables vía proxy" de la tabla de resultados medida en el spike de M0, historial de git) demuestra que un token `allPorts` alcanza el 9000
desde fuera, se documenta como riesgo T2 en `SECURITY.md`; la mitigación
principal sigue siendo no acuñar `allPorts` y aceptar `/run` una sola vez.
Esa mitigación acota el origen **externo** y sólo ése: el listener es
`0.0.0.0:9000` en el mismo netns que los procesos del sandbox, así que
cualquier proceso uid 1000 de dentro de la VM alcanza las seis rutas por
loopback sin token alguno —`/terminate` se lleva la VM y `/validate` reinicia
el contexto `default` del kernel—. Por eso `rayd` comprueba además el uid
del par (`sec-sandbox-isolation`, C-01): un `/terminate` o un `/validate`
cuya conexión pertenece a un uid del sandbox responde 200 `peer_refused` sin
hacer nada, un `/run` de un uid del sandbox responde `sandbox_origin` sin
consumir el `/run` del arranque (`sec-rayd-agent-hardening`), y un `/ready`
o un `/validate` tras el primer `/run` no hace nada
y se audita como anomalía. La auditoría `hook_audit` cubre los hooks de
runtime y, tras el `/run`, también esos dos hooks de build.

---

## ADR-007 — Sin `set_timeout`: la vida del sandbox es inmutable, tope 8 h

> **Sustituida por ADR-011** (`m9-server-timeout`).

**Contexto.** E2B expone `set_timeout()` para extender la vida de un sandbox.
La API de Lambda MicroVMs no tiene `UpdateMicrovm`: `maximumDurationInSeconds`
(1–28800, running **+** suspended) e `idlePolicy` se fijan en `run-microvm` y no
cambian. El tope de 8 h no es ajustable.

**Decisión.** No existe `set_timeout` en Rayito ni se emula con un temporizador
local (diferiría silenciosamente del TTL de servidor de E2B: peor que su
ausencia). `create(timeout=…)` fija la vida; `get_info()` expone `expires_at` y
`remaining_seconds`; `connect()` **no** extiende la vida.

**Consecuencia.** Es la diferencia más visible con E2B y va en el README. Un
agente que necesita más de 8 h crea otro sandbox y mueve su estado con
`reincarnate()` (ADR-009): los ficheros del `HOME` viajan por S3, las
variables del kernel y los procesos no.

## ADR-008 — Pool de MicroVMs: pool de suspendidos, no de VMs corriendo

**Contexto.** `SPEC.md` §4 dejó el pool como no-objetivo "sólo si M0/M6
demuestran que el cold start lo justifica" y `MILESTONES.md` M6 pidió
decidirlo con datos. El benchmark del 2026-09-16
(`docs/benchmarks/2026-09-cold-start.md`, regla D11 de
`openspec/changes/m6-benchmark-pool/design.md` fijada **antes** de medir:
`B20 < 8 s` y `R < 2 s` ⇒ sin pool; en otro caso pool, pre-calentado sólo si
además `R ≥ 2 s`) midió con `rayito-base` 10.0: `B20` (p95 de `run-microvm`
→ `kernel_ready` en una ráfaga de 20 `create()` por el SDK) = **8,57 s**
(p50 5,68 s), `R` (p95 de `resume()` → `Health` con generación nueva) =
**0,40 s** (p50 0,38 s), auto-resume p50 0,67 s, secuencial p50 5,20 / p95
6,07 s, primera celda 0,10 s. De los 8,57 s, ≈ 3 s son la cola del bucket de
5 TPS del propio SDK (sin bucket, 20 `run-microvm` simultáneos: p95 5,69 s y
0 `ThrottlingException`, §16 Q43); y sin el warm-up del kernel
(`rayito-base-slim` 2.0) el secuencial baja a 2,99 / 3,41 s. `B20` supera el
umbral por 0,57 s, menos que la incertidumbre de medida de ese valor (la
sonda del run resolvía ≈ 0,4–0,7 s, informe §1): la regla se aplicó tal cual,
con el margen dentro de la resolución.

**Decisión.** `B20 ≥ 8 s` ⇒ hay pool. Como `R < 2 s`, el pool es de
**MicroVMs suspendidos**, nunca de VMs `RUNNING` ($0,126/h cada una): el
SDK (lado cliente, sin servicio) crea N sandboxes por adelantado con
`IdlePolicy(auto_resume=True)`, los pausa nada más llegar a `kernel_ready`
y `Sandbox.create()` toma uno cuya primera petición lo reanuda en ≈ 0,7 s.
Coste aparcado = storage del snapshot suspendido (≈ 0,92 GB × $0,08/GB-mes
≈ $0,07 por plaza y mes) + una escritura por aparcado ($0,0038/GB) + una
lectura por toma ($0,00155/GB). La implementación es el cambio OpenSpec
`m7-suspended-pool`, fuera de v0.1 y de M6; hasta entonces `create()` sigue
siendo el camino único. El bucket de 5 TPS y el warm-up de la imagen son
palancas independientes (cuota publicada §11; Track A) que ese cambio debe
medir con ráfagas repetidas antes de tocarlas, no razones para saltarse la
regla.

**Consecuencias.** (1) Las 8 h de `maximumDurationInSeconds` cuentan el
tiempo suspendido: cada plaza se recicla antes de vencer (un lanzamiento por
plaza cada < 8 h) y una plaza aparcada más de `suspended_duration_seconds`
se termina sola. (2) El `runHookPayload` (hash del access token, `envs`) se
fija en `/run`: el dueño del pool acuña el token y quien toma la VM lo
hereda. Cerrado en `m7-suspended-pool` D4/D14: un secreto por plaza acuñado
por el pool, borrado del backend al tomar, sin rotación (imposible sin un
segundo `/run`, que `rayd` rechaza por diseño); `SECURITY.md` T14. (3) Por lo
mismo, `envs` son por
pool, no por sandbox. (4) Las plazas consumen la cuota de memoria de la
región (§11, 1024 GB en us-east-1) y aparecen en `list()` como `SUSPENDED`.
(5) `SPEC.md` §4 deja de listar el pool como no-objetivo genérico: sigue
descartado el pool pre-calentado.

## ADR-009 — Persistencia de `/home/user` por S3 desde `rayd` (root + IMDS), no desde el sandbox

**Contexto.** ADR-007 deja al agente que necesita más de 8 h sin más camino
que crear otro sandbox y mover sus ficheros por `files`: a 6,7 MB/s de bajada y
**0,65 MB/s** de subida por la ventana HTTP/2 del proxy (Q32), 1 GB cuesta
≈ 26 min de cliente por reencarnación, y `kill()` pierde todo.
`docs/research/2026-09-m7-oss-readiness.md` §3(g) compara tres salidas: (1)
un sync a S3 corrido por el usuario del sandbox, que expone el execution role
al código del usuario (T1); (2) un adaptador S3 **dentro de `rayd`**, que
corre como root y alcanza IMDS mientras `rayito-base-caps` mantiene IMDS
bloqueado para uid 1000–65535 (Q48), a cambio de compilar una pila TLS en el
binario musl; (3) EFS por un conector VPC, sin documentar para MicroVMs.

**Decisión.** Opción 2. `FilesystemService` gana `Checkpoint` y `Restore`
(sin servicio nuevo); `rayd` empaqueta el `HOME` del usuario (tar + gzip
nivel 1, lista de exclusión fija, lectura bajo la identidad del usuario) y lo
sube en partes de 8 MiB a `s3://<bucket>/<prefix>/<name>/home.tar.gz` más un
`manifest.json` escrito sólo después del archivo; el restore extrae sin salir
del `HOME`, sin dispositivos ni hard links, con los bits setuid/setgid/sticky
descartados y el sha256 verificado. Credenciales: **sólo** IMDSv2 como root
(`ImdsCredentialsProvider`, perfil `execution_role`); nada de entorno ni
ficheros. La escalera de compilación de `m7-s3-persistence` D6 (`aws-lc-rs` →
`ring` → opción 1) se resolvió en el **primer peldaño**: `aws-lc-sys` 0.45.0
compila sus fuentes C con `zig cc` (builder `cc`, sin cmake, bindings
pregenerados para `aarch64-unknown-linux-musl`) sin ningún ajuste; `cargo
deny check` pasa con la allowlist existente. El SDK expone
`S3Prefix(bucket, prefix, name, region)`, `create(persist=)` (exige
`execution_role_arn`, restaura si `name` ya tiene checkpoint),
`checkpoint_files()`, `restore_files()` y `reincarnate()` = checkpoint →
`create(persist=)` con las mismas opciones → `kill()` de la VM vieja; la
misma superficie en TypeScript. La política IAM del execution role es un
parámetro de `infra/iam.yaml` (`PersistenceBucket`/`PersistencePrefix`):
`s3:PutObject/GetObject/AbortMultipartUpload` sobre `<bucket>/<prefix>/*` y
`s3:ListBucket` acotado con `s3:prefix`, sin `DeleteObject`.

**Consecuencias.** (1) El binario lleva un cliente TLS (`rustls` +
`aws-lc-rs`) y el SDK de S3: **4 700 984 → 12 524 384 B** (+7,8 MB, ×2,66;
por debajo del ×3 que habría exigido `cargo bloat`), build limpio ARM64 de
106 s a 222 s en la máquina de desarrollo, 256 paquetes en `.dep-v0` (antes
145); Q42 midió que el tamaño del snapshot no gobierna el cold start y las
páginas del cliente S3 no se tocan hasta el primer `Checkpoint` (medido
2026-09-17, Q53: 50 MB suben en 1,50 s la primera vez y 1,40 s la segunda,
31–35 MB/s; el restore baja a 78 MB/s; `reincarnate()` 8,85 s de pared). (2) El rol de ejecución alcanza un prefijo de
S3; con `persist=` el operador lo sabe y `SECURITY.md` T15 lo registra, y
`rayd` acota cada sandbox a la base que su `create(persist=)` ligó en el
`runHookPayload` (C-07), así que un prefijo por inquilino basta aunque el rol
sea compartido; en
`rayito-base-caps` uid 1000 sigue sin IMDS. (3) Un restore que falla a mitad
deja el `HOME` parcial; la recuperación documentada es otra `create(persist=)`.
(4) `reincarnate()` es la respuesta honesta a lo que `set_timeout` no puede
dar: pasar de `max_lifetime` (ADR-011); sobreviven los ficheros, no la
memoria del kernel. (5) Fuera: EFS, URLs firmadas (M9 las entrega por otro
camino, con las credenciales del llamante y sin execution role: ADR-010),
checkpoints incrementales, snapshots con historia, checkpoints automáticos,
proveedores S3 compatibles (el endpoint no es configurable) e IMDSv1. (6) La
constitución (`openspec/project.md`) pasa de "sin crate TLS" a "sin TLS en
los listeners; el único cliente TLS es el adaptador S3" (M9 añade el segundo,
`HyperSignedHttp`, que reutiliza la misma pila TLS: ADR-010).

## ADR-010 — Transferencias S3 prefirmadas con las credenciales del llamante; rayd no guarda credenciales

**Contexto.** Las URLs de E2B (`upload_url`/`download_url`) llevan su
autenticación en la query string; el proxy del MicroVM sólo la lee de una
cabecera o de un subprotocolo WebSocket (`AWS_API_NOTES.md` §7), así que
ninguna URL puede llegar a `rayd` a través del endpoint. El camino de bytes
más rápido medido es VM ↔ S3 (Q59: 55,7–106,2 MB/s de subida y 84,2–99,0 MB/s
de bajada frente a 0,68 / 4,77 MB/s por el proxy).

**Decisión.** El SDK firma con las credenciales del llamante; `rayd` recibe
las URLs por petición (nunca en `envs`, argv, `runHookPayload` ni logs), valida
cada una contra el objeto nombrado (`transfer::url_policy`: https, puerto 443,
host virtual-hosted regional exacto del bucket, ruta igual a una clave ligada
al sandbox `…/<sandbox_id>/<up|down>/<id>`, SigV4, sin literales IP) y mueve
los bytes con un cliente HTTPS sin credenciales (puerto `SignedHttp`, adaptador
`HyperSignedHttp` con un resolvedor que descarta loopback, link-local e IMDS y
sin redirecciones). Subida = importación armada que sondea el `GET` y escribe
por el camino de `Write` (`FsIdentityGuard`, temporal + rename); descarga =
exportación de una foto del fichero abierto con `O_NOFOLLOW` como el usuario;
los ficheros grandes de `files.write`/`files.read` (desde
`S3Staging.threshold_bytes`, 8 MiB por defecto) van por la misma maquinaria.
La configuración es `S3Staging(bucket, prefix, region)` en `create`/`connect`
(o `RAYITO_TRANSFER_BUCKET`/`RAYITO_TRANSFER_PREFIX`/`RAYITO_TRANSFER_REGION`).

**Alternativas descartadas.** (a) `rayd` usando el execution role para S3 (el
camino de ADR-009): exige un rol en cada sandbox, reabre C-07 (el objeto no
quedaría ligado al sandbox) y T1 en la imagen por defecto; (b) un listener
`/files` HTTP en `rayd`: sólo alcanzable con la cabecera del JWE, así que no
entrega URLs sin cabeceras y añade una superficie de la clase de T2; (c) el
usuario del sandbox corriendo `curl` con la URL: la URL quedaría en argv y en
`/proc`, visible para el código del usuario, y la importación correría sin
política de tamaño, reserva de disco ni identidad.

**Consecuencias.** Hace falta un bucket de transferencias (sin él,
`upload_url`/`download_url` lanzan `UnimplementedError`); las URLs son
credenciales al portador (`SECURITY.md` T16); las subidas aterrizan de forma
asíncrona (barrera de lectura tras subida y `ticket.wait()`); los tickets son
de un solo uso; las descargas son una foto tomada al llamar; la caducidad
siempre está fijada (≤ 7 días y ≤ la vida de las credenciales del llamante);
el binario crece sólo en el código del adaptador, porque la pila TLS
(`rustls` + `aws-lc-rs`) ya estaba enlazada por ADR-009. Medido en
`rayito-base` 21.0 (filas Q71–Q75 de `AWS_API_NOTES.md`): dentro del VM
`rayd` exporta 50 MB en 1,2-1,6 s y 200 MB en 2,3 s (32-86 MB/s), importa
200 MB en 2,9 s (68 MB/s) y un fichero subido es visible entre 0,3 y 0,9 s
después del `200` del `PUT`; fuera del VM manda el enlace del desarrollador,
no el proxy del endpoint. `rayd` crece 1 230 744 B (+9,8 %) con todo M9.

## ADR-011 — Plazo lógico impuesto por rayd; el tope de la plataforma se elige en create()

Sustituye a ADR-007.

**Contexto.** ADR-007 hizo de `maximumDurationInSeconds` la vida del sandbox
porque no existe `UpdateMicrovm` (`AWS_API_NOTES.md` §1, §2, §11), así que
`set_timeout` era `UnimplementedError`. Q58 midió que cuando `rayd`, PID 1 de
la imagen, sale por su cuenta, el endpoint responde 502 en menos de 1 s y la
VM pasa a `TERMINATED` ≈ 15 s después sin llamada al plano de control. §15
(Q19, Q41): `CLOCK_MONOTONIC` avanza durante una suspensión y AWS corrige el
reloj de pared al reanudar, así que un plazo en el monotónico crudo cuenta el
tiempo suspendido, igual que el `end_at` de E2B y que el tope de la
plataforma.

**Decisión.**

- El `runHookPayload` gana un bloque `lifecycle` (`timeout_ms`, `on_timeout`,
  `auto_resume`); `rayd-core::sandbox_timeout` lleva el plazo lógico en el
  monotónico crudo y el vigilante `rayd-timeout` (un hilo fuera del runtime)
  lo hace cumplir aunque el cliente muera.
- `max_lifetime` = `maximumDurationInSeconds` (120–28800 s, por defecto
  `timeout + 60`) es el **tope**; `timeout` es el **plazo lógico**, que
  `set_timeout` fija con exactitud (puede acortarlo) y `connect(timeout=)`
  sólo alarga (`LifecycleService.SetTimeout`, `EXACT`/`AT_LEAST`). El plazo
  nunca pasa de `max_lifetime − 60 s` desde el arranque.
- `on_timeout='kill'`: `rayd` cierra los streams con `sandbox_timeout`, señala
  a cada grupo y sale con código 124 (sin IAM). `on_timeout='pause'`: el SDK
  con cliente vivo suspende en cuanto vence; sin cliente, la política de idle
  lo hace en `max_idle`; `auto_resume` sigue a E2B, con un plazo nuevo de
  `max(timeout, 300 s)` tras un auto-resume.
- Un `create()` nativo sin `max_lifetime` ni `on_timeout` se comporta
  exactamente como ADR-007: fase `UNMANAGED`, el tope es el `timeout`, el
  cable es idéntico.
- Pedir un ciclo de vida a una imagen anterior a M9 falla cerrado:
  `LifecycleUnsupportedException` (0.4.0: subclase de `UnimplementedError`,
  no de `InvalidArgumentException`; misma feature ausente que cualquier
  otra) y el VM se termina.

**Consecuencias.** Costes honestos, también en `e2b-compat.md` y
`concepts.md`: (1) el tope sigue contando el tiempo suspendido: 8 h como
mucho desde el arranque, mientras E2B guarda sandboxes pausados sin límite;
(2) en modo `pause` los procesos siguen corriendo entre el vencimiento y la
suspensión (≈ 3 s con cliente vivo, hasta `max_idle` sin él); (3) en modo
`pause` la política de idle puede suspender antes del plazo, así que un
trabajo desenganchado se congela tras `max_idle` sin tráfico; (4) tras una
salida en modo `kill` se facturan ≈ 15 s de 502 (Q58); (5) el plazo nunca
pasa de `max_lifetime − 60 s`; (6) en modo `kill` con idle, un sandbox
suspendido a través de su plazo que nadie reanuda sólo lo retira la
plataforma (`suspendedDurationSeconds` o el tope), al coste del snapshot
guardado; (7) una suspensión real de menos de 2 s que cruza el plazo no se
reconoce como congelación; (8) dejar sin CPU al hilo vigilante justo al
vencer imita esa congelación desde dentro del sandbox: le vale la única
gracia de 30 s de ese plazo (o, con `auto_resume`, la regla de 5 min una
vez), nunca más allá del tope; sólo `SetTimeout`, que exige el token, o la
propia regla de auto-resume devuelven la gracia al mover el plazo. El shim `rayito.e2b` exige una imagen M9. Más allá
de `max_lifetime` el único camino sigue siendo `reincarnate()` (ADR-009).
Los números de la salida con código 124 y de la pausa al vencer son las filas
Q63–Q65 de `AWS_API_NOTES.md`: la plataforma propaga el 124
(`Container Stopped with Exit Code: 124`) y la VM termina sola, así que el
fallback a 0 de `m9-server-timeout` D6 no se aplica.

## ADR-012 — Política de egress en el guest sobre rayito-base-caps; conectores de plataforma sólo por VPC del cliente

**Contexto.** `allow_internet_access=False` y `network=` de E2B no tienen
primitiva en `run-microvm`: omitir `egressNetworkConnectors` o pasar `[]`
hereda el conector de la versión de imagen y el VM sigue saliendo (Q44, Q60);
`NO_EGRESS` no existe (`ValidationException`, Q60). El único control fuera
del guest es un conector VPC del cliente con un security group deny-all
(`infra/egress-connector.yaml`, T8), que no filtra el DNS de Amazon. En
`rayito-base-caps` `rayd` tiene `CAP_NET_ADMIN` y ya instala una ruta de
política por uid para IMDS (Q48).

**Decisión.** El guest aplica la política con dos capas que comparten un
objeto de política en `rayd` (sección "Política de egress (M9)"): (1) rutas
del kernel en una tabla por `uidrange 1000-65535`; (2) un proxy local de
`rayd` (root) que decide las reglas por nombre de host y encadena al SOCKS5
del operador. Modos: `Unrestricted` (nada), `Routes` (`blackhole`s de
`deny_out \ allow_out`) y `ProxyOnly` (todo lo directo bloqueado; sólo sale el
proxy). Sólo corre donde `CapEff` tiene `CAP_NET_ADMIN`; en cualquier otra
imagen el SDK termina el VM y lanza `UnimplementedError` antes de devolver un
sandbox sin la política pedida (falla cerrado, nunca se aproxima).
`update_network` cambia la política en caliente (`NetworkService`).

**Consecuencias.** Las capas del guest caen ante un exploit del kernel del
guest o ante root dentro del guest (`RAYITO_ALLOW_ROOT`) y no afectan a los
uids exentos del agente de la plataforma (`SECURITY.md` T17). En modo proxy
los clientes que no honran `HTTPS_PROXY` fallan cerrados. El DNS de uid ≥ 1000
**no** queda bloqueado en `rayito-base-caps`: sus resolvedores escuchan dentro
del guest (QE1, fila Q66 de `AWS_API_NOTES.md`; ver la adenda siguiente).
UDP/QUIC no pasan por el proxy. Un cambio de política afecta a las conexiones
nuevas. La alternativa de plataforma (conector VPC del cliente con security
group deny-all) sigue siendo el único control fuera del guest, con su
salvedad de DNS.

### Adenda: DNS bajo deny-all en caps

**Contexto.** La regla de parada de `m9-egress-policy` D17 se disparó en QE1
(fila Q66, medida en `rayito-base-caps` con `allow_internet_access=False`):
`/etc/resolv.conf` lista dos resolvedores de la plataforma, uno de loopback
(`127.0.0.0/8`) y otro link-local (`169.254.0.0/16`), y **los dos escuchan
UDP 53 dentro del guest**. Una consulta de uid 1000 a esas direcciones se
enruta por la tabla `local` (regla de prioridad 0), que va antes que la
regla `uidrange` de la política (150/151), así que los `blackhole`s no la
alcanzan: bajo deny-all `getaddrinfo` como uid 1000 resuelve, mientras un
`sendto` directo a un resolvedor externo y toda conexión fuera del VM fallan
(`EINVAL`, ruta `blackhole`).

**Decisión (opción C).** En M9 la aplicación no cambia: bajo deny-all en caps
los nombres **pueden resolverse** a través de los resolvedores de la
plataforma dentro del guest, y **toda conexión fuera del VM sigue bloqueada**.
La aceptación de DNS pasa de "la resolución falla" a "resuelve o no, pero
ninguna dirección resuelta conecta". El modo proxy y las reglas por nombre de
host no cambian: el proxy decide por nombre y, bajo deny-by-default, nunca
resuelve un nombre denegado.

**Consecuencias.** Queda un canal residual de exfiltración por DNS: el código
de uid ≥ 1000 puede codificar datos en consultas que los resolvedores de la
plataforma reenvían hacia fuera. Es un canal estrecho y de sólo consultas,
pero existe (`SECURITY.md` T17). La aplicación en el guest sigue siendo de
mejor esfuerzo; el control duro sigue siendo el conector VPC del cliente, con
su propia salvedad (un security group no filtra el DNS de Amazon).

**Alternativas consideradas.** (A) Una regla `ip rule` con `uidrange
1000-65535`, `ipproto udp`/`ipproto tcp` y `dport 53` con acción `prohibit`
**antes** de la regla `local` de prioridad 0: exige mover la regla `local` a
otra prioridad, y un fallo a mitad del cambio (o una recuperación que no la
repone) deja el guest sin enrutamiento local (loopback, `rayd`, el proxy).
Queda **diferida al siguiente ciclo** como endurecimiento, con su propio
diseño de cambio atómico y rollback y su e2e (`MILESTONES.md`, M9, diferidos).
(B) Un `resolv.conf` por proceso con un espacio de nombres de montaje para
los procesos de uid ≥ 1000: complejo (cada proceso, PTY y kernel que lanza
`rayd`, más lo que ellos lancen) y no cierra el canal: los resolvedores del
guest siguen alcanzables por su dirección aunque no estén en `resolv.conf`;
descartada para M9.

**Reversible.** Sí: la opción A se añade después sin tocar el contrato
(`NetworkService`, `EgressEnforcement`) ni la semántica de la política;
cuando llegue, la aceptación vuelve a exigir que la resolución falle bajo
deny-all.

### Adenda M10: opción A implementada

La opción A descrita arriba está implementada (`m10-rayd-hardening`):
`NetworkManager` instala y desinstala el bloqueo de DNS exactamente cuando el
plan de rutas activo deniega todo el tráfico directo en cada familia
gestionada (`RoutePlan::denies_all`), en el mismo punto donde hoy aplica el
resto de la política (`/run` en modo deny-all, `UpdateNetwork` y la
recuperación de emergencia, que siempre termina en deny-all). El cambio
atómico y su rollback viven en `rayd_core::network::dns_guard` (puro,
`crates/rayd-core/src/network/dns_guard.rs`): primero se añade una segunda
regla `local` en la prioridad 1 (duplicado inofensivo), después se borra la
regla `local` original de la prioridad 0 (el enrutamiento local depende ya
sólo del duplicado, sin hueco) y sólo entonces se instalan las reglas
`uidrange 1000-65535 ipproto udp|tcp dport 53 prohibit` en la prioridad 0,
ya sola (dos reglas de la misma prioridad se desempatan por orden de
inserción, así que compartir la prioridad con la regla original del kernel
le habría perdido siempre a ella). Un fallo a mitad de la instalación
deshace exactamente los pasos que ya se aplicaron, en orden inverso
(`plan_dns_guard_rollback`), nunca deja el guest sin enrutamiento local. La
adaptación a `ip` vive en `crates/rayd/src/adapters/egress_routes.rs`
(`EgressRoutes::execute_dns_guard`), idempotente en ambos sentidos. E2e en
`crates/rayd/tests/m9_egress.rs`: bajo deny-all real en una VM Lima con
`unshare --net`, una consulta DNS de uid 1000 (`ip route get ... ipproto udp
dport 53`) deja de resolver, mientras el resto del tráfico de loopback y de
otros puertos no se toca; al volver a una política sin deny-all las reglas
se retiran y el DNS vuelve a resolver. `SECURITY.md` T17 actualizado: el
residual ya no es "el DNS de uid ≥ 1000 no se bloquea bajo deny-all", queda
sólo el marco general de aplicación de mejor esfuerzo en el guest. Pendiente
de aceptación en AWS real: republicar `rayito-base-caps` con el `rayd`
final y repetir `test_m9_egress.py`/`m9-egress.e2e.test.ts` sobre la imagen
republicada.

## ADR-013 — Kernels Deno sobre TCP de loopback con HMAC

**Contexto.** `javascript` y `typescript` llegan en M9 con el kernel Jupyter de
Deno 2.9.7 (Q61: `ijavascript` no instala en al2023 ARM64 sin compilador;
Deno es un único binario con su propio ZeroMQ). El kernel de Deno sólo abre
sockets TCP (`Deno.listen({ hostname, port })`): no sabe abrir endpoints
`ipc`, así que el diseño M4 de "un ipykernel por contexto sobre `ipc` bajo
`/run/rayito/k`" no es posible para Deno.

**Decisión.** Los contextos Deno usan `transport="tcp"` e `ip="127.0.0.1"`,
con los puertos que elige `jupyter_client`. El connection file (y su clave
HMAC) sigue en `/run/rayito/k/<context_id>/kernel.json` (directorio 0700 de
uid 1000). Python y bash siguen en `ipc`. El sidecar lo decide por lenguaje
(`KernelLanguage.transport`, `kernel_endpoint()`); `rayd` no toca los sockets
de los kernels.

**Consecuencias.** Cualquier proceso local puede abrir los cinco puertos.
shell/control/stdin exigen la clave HMAC, que Deno verifica; el socket PUB de
iopub no exige clave para suscribirse, así que un proceso local puede leer las
salidas de las celdas Deno. Todo proceso que alcanza 127.0.0.1 ya corre dentro
del mismo sandbox: uid 1000 (el mismo uid que posee el kernel y puede leer su
connection file, T12), `rayd` como root, o el agente de la plataforma (uids
991–994). El proxy de AWS alcanza listeners de loopback (Q18), pero sólo con un
JWE cuyo `allowedPorts` contenga ese puerto efímero; habla HTTP, no ZMTP, y el
SDK nunca acuña `allPorts` por defecto (T2/T3). El residual se acepta y queda
en `SECURITY.md` T12. Deno corre con todos los permisos (`allow_all`), el mismo
poder de uid 1000 que el kernel de Python.

**Reversible.** Si una versión futura de Deno abre `ipc`, basta con cambiar
`KernelLanguage.transport`; ni el cable ni los SDKs cambian.

## ADR-014 — Componentes opcionales en la cuenta del cliente

**Contexto.** `SPEC.md` §4 excluye explícitamente un "servicio de plano de
control" y "un almacén del lado cliente". Varias funciones de E2B que los
usuarios piden (sandboxes pausados consultables por metadatos, inyección y
CRUD de secretos, trazas OpenTelemetry del SDK) sólo son alcanzables con
estado o llamadas AWS opcionales que van más allá de `create/connect/kill`
directos contra la API de Lambda MicroVMs; E2B las resuelve con su propio
plano de control hospedado (Postgres, ClickHouse, Redis, proxies de host).
La investigación de E2B fuera de alcance
(`docs/research/2026-10-e2b-out-of-scope.md` §2, §5, §6, §8.1) mide el
esfuerzo, el coste y las alternativas de cada una y propone distinguir entre
un servicio hospedado por Rayito y componentes opcionales en la cuenta del
propio cliente.

**Decisión.** Se aprueban las siete reglas siguientes:

1. **Nunca un servicio hospedado por Rayito.** Rayito no opera, ni operará,
   ninguna pieza de infraestructura por cuenta de sus usuarios: sin servidor
   propio, sin base de datos propia, sin endpoint que reciba tráfico de
   clientes. Todo lo que no sea el SDK corre en la cuenta AWS del propio
   usuario.
2. **Componentes opcionales en la cuenta del cliente: permitidos.** Se
   entregan como plantillas CloudFormation independientes bajo `infra/`, una
   por función, que el usuario despliega explícitamente en su propia cuenta y
   región. En esta campaña (M11–M14): `infra/secrets-access.yaml` (rol y
   permisos mínimos de Secrets Manager, M13a) e `infra/metadata-index.yaml`
   (tabla DynamoDB del índice, M14). Un stack único que agrupe varias piezas
   (`infra/rayito-plane.yaml`, propuesto en la investigación como "un solo
   plano opcional") queda como destino futuro y no se crea en esta campaña:
   cada función tiene hoy su propia plantilla, más simple de auditar y de
   desplegar por separado.
3. **Tres invariantes**, obligatorios para cualquier componente opcional
   presente o futuro: (a) `create()`, `connect()` y `kill()` funcionan
   exactamente igual sin ellos desplegados; (b) ninguno es fuente de verdad
   del estado de un sandbox — manda siempre `list-microvms`, nunca una copia
   en una tabla o un índice; (c) ningún dato sale de la cuenta del cliente.
4. **Regla de coste.** Toda función que consuma cuota o dinero de AWS está
   **apagada por defecto** y sólo se activa con una **opción explícita** del
   SDK — un kwarg con nombre en Python, una propiedad con nombre en
   TypeScript — nunca con una variable de entorno, un fichero de
   configuración ni un setter global. Con el SDK usado sin ninguna de esas
   opciones, Rayito crea **cero recursos AWS y hace cero llamadas AWS extra**
   respecto a 0.4.0. Esta regla no admite excepciones: ni una comprobación de
   salud en segundo plano, ni una carga perezosa "por si acaso", ni una
   detección automática de credenciales para un servicio opcional.
5. **Convención de opt-in única**, fijada en detalle en
   `docs/site/docs/optional-features.md` y en los dos helpers de convención
   (`clients/python/src/rayito/_optional.py`,
   `clients/typescript/src/optional.ts`): un kwarg nombrado por función (más,
   como mucho, un kwarg con el objeto de configuración del servicio), objetos
   de configuración inmutables y reutilizables que crean su cliente AWS de
   forma perezosa, y un bloque de docstring/TSDoc titulado "Coste y
   activación" en cada opción de coste.
6. **Fase 1 de secretos: el valor es visible para el código del sandbox.**
   La inyección nativa de secretos (M13a) entrega el valor como variable de
   entorno del proceso que lo pide, igual que Modal, Northflank o Runloop
   directo (investigación §2.2); no hay, todavía, un gateway que medie el uso
   de la credencial sin exponer su valor a uid 1000 (la opción 4 de la
   investigación §2.3–2.4, un plano de credenciales en loopback, queda para
   un milestone posterior). Esto se documenta explícitamente en cada API de
   inyección: no inyectar credenciales de larga vida en código no confiable;
   preferir tokens de corta vida y mínimo privilegio.
7. **Volúmenes sobre S3: aprobados para un milestone posterior.** La
   investigación (§1) mide un camino técnico viable; queda fuera de
   M11–M14 y no se abre ningún trabajo de este ciclo en esa dirección.

**Consecuencias.** Las filas de la tabla de paridad
(`docs/site/docs/e2b-parity.md`) que dependen de estas funciones sólo cambian
de estado cuando el grupo que las entrega las implementa de verdad contra AWS
real, nunca por adelantado: `adr-optin` (este ADR) no mueve ninguna fila.
`docs/site/docs/optional-features.md` (A3) nace con todas sus filas en
"planificado (Mxx)" por la misma razón. Aumenta la superficie de seguridad
del proyecto (nuevas amenazas documentadas por cada grupo en `SECURITY.md`
según entrega) y su superficie de soporte (una plantilla CloudFormation más
por función optativa).

**Alternativas descartadas.** (a) Un ADR por función (cuatro ADR-014 en
conflicto, sin una regla común de coste ni de opt-in): fragmenta la decisión
de "apagado por defecto" en cuatro sitios que podrían divergir. (b) Un
servicio SaaS operado por Rayito para alguna de estas funciones: rompe la
tesis del producto (el código del cliente nunca sale de su cuenta). (c) No
hacer nada: deja fuera funciones que los usuarios de E2B ya usan hoy, sin
ninguna ganancia de simplicidad frente a la regla de coste explícita.

**Reversible.** Cada componente opcional vive en su propia plantilla
CloudFormation y su propio flag de opción; retirar uno no afecta a los demás
ni al SDK sin opciones.

## ADR-015 — `ConfigureSandbox` y los participantes del ciclo de vida (M15, 0.6)

**Contexto.** Las ocho funciones de 0.6 (montajes S3, volúmenes EFS, eventos
y webhooks, exportación OTLP, pasarela de secretos, dominio propio, tamaños
y templates) necesitan, cada una, configurar algo dentro del guest después
de `/run` (un montaje, un sink de eventos, un exportador de métricas, un
listener de loopback) sin añadir un RPC por función ni acoplar `rayd` a
ellas por adelantado.

**Decisión.** Un único servicio gRPC, `ConfigureService` (`Configure`,
`ConfigureStatus`), con una sección opcional por función dentro de
`ConfigureRequest`: una sección ausente se deja tal cual, una presente
(incluso vacía) sustituye el estado anterior de esa función. El orden de
aplicación es fijo (`rayd_core::configure::APPLY_ORDER`): eventos →
telemetría → pasarela → montajes S3 → volúmenes EFS (los eventos primero
para que las secciones siguientes ya puedan generarlos; los montajes al
final porque pueden tardar más en asentarse, `SECTION_CODE_PENDING`). Sólo
se acepta en fase `RUNNING`/`RESUMED` (`FAILED_PRECONDITION not_running`
en cualquier otra). El lado del agente es un `FeatureSet` de seis
`ConfigurableFeature<Cfg, Status>` (`rayd::features`), cada uno
`Unsupported` hasta que su propia función construye un adaptador real;
`Health.features` (`AgentFeatures`, `features.proto`) expone qué build
soporta qué, para que el SDK distinga "agente anterior a 0.6" (mensaje
ausente, presencia de proto3) de "0.6, pero esta función sigue en stub".

Las funciones que necesitan participar en `/suspend` y `/ready` implementan
`LifecycleParticipant` (`rayd::lifecycle::participants`): `on_suspend(share)`
corre **concurrentemente** con el `syncfs` por sistema de ficheros existente
(`rayd_core::suspend_sync::SuspendShares`, reutilizando `SuspendBudget` sin
tocar su contrato), nunca en serie después; `ready_gate()` combina su
veredicto con la decisión existente de `/ready` sin reemplazarla. Sin
ningún participante registrado (el caso de 0.6 foundations: todos los slots
son `Unsupported`), ambos hooks se comportan exactamente como en 0.5.x.

`main` construye **un único** `Arc<FeatureSet>` por proceso y lo comparte:
`grpc::router_with_features` se lo da a `ConfigureService` (aplica las
secciones) y a `Health` (`FeatureSet::agent_features`, derivado de
`supported()` de cada slot, así una función que gana un adaptador real
enciende su propio flag sin tocar `health.rs`), y `FeatureSet::participants()`
del mismo conjunto va a `HookServices.participants`. Nada de estado de
función vive en un singleton de proceso: el harness de los tests de
integración construye su propio `FeatureSet` por test igual que `main`.
`/suspend`, `/resume` y `/terminate` ejecutan los participantes
(`on_suspend`/`on_resume`/`on_terminate`) sólo ante una transición aceptada
(`Transition::changed`), todos a través de un único bucle
(`hooks::run_concurrently`), cada uno con su propio tope: su cuota de
`SuspendShares` en `/suspend`, `PARTICIPANT_RESUME_TIMEOUT` (2 s, en
paralelo con la sonda de kernels) en `/resume` y
`PARTICIPANT_TERMINATE_TIMEOUT` (1 s, antes de `schedule_shutdown`) en
`/terminate`. Un participante colgado cuesta su tope y nada más.

**Consecuencias.** `ConfigureSandbox` nunca se llama con las siete opciones
0.6 en `None`/`undefined`: el SDK pre-valida con `plan_features`/
`planFeatures` antes de `run-microvm` y no construye ningún `ConfigureRequest`
hasta que una función real lo necesite. El orden de aplicación fijo es una
decisión del ADR, no configurable: una sección no puede pedir ejecutarse
antes que las que la preceden en la lista.

**Reversible.** `ConfigureService` es aditivo (nuevo campo 16 en
`HealthResponse`, nuevo servicio gRPC); un agente 0.5.x sigue funcionando
sin cambios y el SDK lo detecta por la ausencia de `features`.

## ADR-016 — Convenio `OptionalStack` para infraestructura opcional (M15, 0.6)

**Contexto.** ADR-014 ya establece que la infraestructura opcional vive en
la cuenta del cliente, desplegada a mano con `aws cloudformation deploy`
contra las plantillas de `infra/`. Con ocho funciones nuevas añadiendo cada
una su propia plantilla, repetir ese flujo manual por función (parámetros,
capacidades, artefactos Lambda, espera de estado) sin una convención común
dispersaría la lógica y haría más difícil auditar qué crea cada una y
cuánto cuesta.

**Decisión.** Un puerto `StackProvisioner` (`describe`, `create`, `update`,
`delete`, `wait`, `putArtifact`, `failureReason`) con un adaptador real
sobre CloudFormation (`AWS_API_NOTES.md` §21) y un servicio `OptionalStacks`
(`components`, `deploy`, `status`, `destroy`) que nunca se invoca
implícitamente: ningún `create()`, listado o getter del SDK lo llama, sólo
una llamada explícita (o `rayito stack`). Cada componente es una
`StackComponent` estática (nombre, parámetros, coste estructurado,
capacidades IAM) en un catálogo (`_stacks/_registry.py` /
`stacks/registry.ts`); `scripts/gen_stack_assets.py` renderiza, de forma
determinista y por componente, la plantilla y el artefacto Lambda (si lo
hay) desde `infra/<component>.yaml` hacia ambos SDKs, así ninguno necesita
el repositorio presente en tiempo de ejecución. Dos componentes existentes
(`metadata-index` de M14, `secrets-access` de M13a) migran a este convenio
en M15 foundations sin cambiar su plantilla; los otros siete son stubs
(`supported: false`) hasta que su propia función los complete.

**Consecuencias.** `rayito stack list|deploy|status|destroy` sustituye el
`aws cloudformation deploy` manual documentado hasta ahora en
`infra/README.md` para estos dos componentes (`infra/README.md` sigue
documentando el despliegue manual como alternativa). `deploy` imprime
siempre el bloque de coste del componente y pide confirmación salvo
`--yes`. CAPABILITY_AUTO_EXPAND nunca se usa (ningún componente usa macros
ni SAM).

**Alternativas descartadas.** Un único stack que agrupe todas las funciones
(`infra/rayito-plane.yaml`, ya descartado por ADR-014 regla 2): acoplaría el
ciclo de vida de componentes sin relación entre sí. Generar las plantillas
en tiempo de ejecución en vez de en tiempo de build: perdería la
determinismo que `scripts/gen_stack_assets.py --check` verifica en CI.

**Reversible.** Cada componente es independiente; retirar uno no afecta a
los demás ni al SDK sin usarlo.

## ADR-017 — s3-mounts (M15, 0.6)

`mounts=` monta uno o más buckets S3 (o un prefijo suyo) en el guest con
`mount-s3` (Mountpoint for Amazon S3), sólo sobre `rayito-base-caps` (o una
variante derivada por tamaño): el execution role del sandbox necesita
IMDS, que `rayito-base` no concede (ADR-012, `_role_policy.require_caps_for`; el nombre de la imagen sólo se interpreta si es un `rayito-<variante>[-<talla>]` oficial, el resto lo decide `Health.features`).

**Dominio** (`rayd_core::s3_mount`, puro): `S3Mount{mount_path, bucket,
prefix, read_only, allow_overwrite, allow_delete}`, `MountErrorClass`
(`network`/`iam_denied`/`not_found`/`not_allowed`/`helper_missing`/`timeout`,
espejo exacto de `S3MountState.error_class` del proto y de `MountException
.code`/`MountError.code` en ambos SDKs), `MountPhase`
(`Pending`/`Mounted`/`Failed`) y `validate_mounts` (rechaza un bucket fuera
del allowlist de la imagen o una ruta repetida en la misma petición, antes
de tocar nada). `RAYITO_ALLOWED_MOUNT_BUCKETS` es configuración de imagen
(`rayito image publish --env`), nunca un interruptor de activación por
sandbox (ADR-014 regla 4): vacía o ausente deniega todo.

**Puertos** (`rayd_core::s3_mount::ports`): `FuseDevice` (abre `/dev/fuse`
y hace el `mount(2)` del ABI de FUSE del kernel — `fd`, `rootmode`,
`user_id=1000`, `group_id=1000` — sobre `mount_path`) y `FuseDaemon`
(lanza, comprueba y mata el proceso `mount-s3` bound a ese descriptor).

**Adaptadores** (`rayd::adapters`): `LinuxFuseDevice` hace el `mount(2)`
crudo con `libc` (sin depender del feature `mount` de `nix`, que el
workspace no tiene) y **nunca sigue un enlace simbólico**: `rayd` es root y
uid 1000 es dueño de `/home/user`, así que recorre la ruta desde `/`
componente a componente con `openat(O_PATH|O_DIRECTORY|O_NOFOLLOW)`
(creando lo que falte con `mkdirat`), rechaza cualquier enlace con
`invalid_path` y monta sobre `/proc/self/fd/<dirfd>`; el desmontaje usa
`UMOUNT_NOFOLLOW` sobre el descriptor del padre, en cada relanzamiento
igual que en el primer montaje. `TokioMountS3Daemon` lanza
`mount-s3 --foreground <bucket> /dev/fd/3 [--prefix p] [--read-only |
--allow-overwrite --allow-delete]` como el usuario dedicado `rayito-mount`
(uid/gid 990, creado en `image/Dockerfile`), con el entorno reconstruido
desde cero (sólo `AWS_REGION`/`PATH`: nunca una credencial en argv ni en
entorno, SEC-3) y reapea su propio hijo con `Child::wait()` en una tarea
dedicada; lo lanza con `ChildRegistry::spawn`, así que el reaper de
huérfanos del PID 1 (`rayd-orphan-reaper`) nunca le roba ese estado de
salida: nunca hay dos sitios esperando el mismo pid. uid 990 está **por debajo** de
`MIN_UNPRIVILEGED_ID` (1000), así que el blackhole de IMDS de M6
(`uidrange 1000-65535`) no lo alcanza: `mount-s3` resuelve las credenciales
del execution role por su **propio** acceso a IMDS, en su propio proceso,
sin que `rayd` las toque nunca.

**Slot** (`rayd::features::s3_mounts`): un `S3MountsFeature` real, cuyo
`supported()` exige `CAP_SYS_ADMIN` en el conjunto efectivo de `rayd`
(sólo `rayito-base-caps`: el binario y el usuario `rayito-mount` van en las
cuatro variantes porque hay un único `Dockerfile`); `Health.features` y
`root_egress` se derivan de cada slot (`FeatureSet::agent_features`/
`root_egress`, `ConfigurableFeature::root_egress_class`). `apply()`
responde `SECTION_CODE_PENDING` y monta en segundo plano; el SDK sondea
`ConfigureStatus` hasta que cada montaje está `mounted` (15 s como mucho)
y, si no, lanza `MountException`/`MountError` y termina el sandbox. `apply()` valida todo el
`S3MountsConfig` (allowlist + rutas duplicadas) antes de montar o
desmontar nada — una sección inválida no toca un solo montaje existente
— desmonta lo que ya no está en la lista deseada, monta lo nuevo o lo que
cambió de spec, y dos `Configure` seguidas con el mismo contenido son un
no-op (ni un `/dev/fuse` nuevo ni un `mount-s3` relanzado).
`ConfigurableFeature::participant()` devuelve un `LifecycleParticipant`
cuya `demand().max` es `Duration::ZERO` (el `syncfs` acotado ya cubre el
montaje FUSE, §7.2: "`/suspend` no añade ningún paso propio") y cuyo
`on_resume` lanza, en paralelo, una sonda `stat` de 1 s por montaje (dentro
del tope `PARTICIPANT_RESUME_TIMEOUT` de 2 s que `hooks` pone a todo el
`on_resume`), relanzando el daemon si está muerto o no responde.

**IAM** (`infra/s3-mounts.yaml`, `OptionalStack`): la política gestionada
`RayitoS3MountAccess` (pide `CAPABILITY_IAM`) concede `ListBucket` acotado
por un `s3:prefix` condicional (parámetro `Prefixes`, coma-separado) y
`GetObject` (más `PutObject`/`DeleteObject`/`AbortMultipartUpload` con
`ReadOnly=false`) sobre ARNs de objeto que llevan esos mismos prefijos
(hasta 4 por pila): la contención por prefijo no depende sólo de
`mount-s3 --prefix` dentro de un guest que ejecuta código no confiable.

**SEC-3 (residual aceptado, no un fallo)**: uid 1000 puede leer el
`cmdline` del proceso `mount-s3` (mismo `/proc` que cualquier otro proceso
del guest), no su `environ` (`EACCES`: el daemon es uid 990,
`AWS_API_NOTES.md` Q103); el bucket y el prefijo están declarados **no
secretos** (igual que la `metadata` de T4), así que esto no es una fuga —
nunca hay una credencial en argv ni en entorno, documentado en T20
(`SECURITY.md`). El daemon recibe `--allow-other`: sin él Mountpoint sólo
atiende a su propio uid y uid 1000 recibe `EACCES` (Q101).

**Sin API en el shim de E2B**: E2B no tiene un equivalente a `mounts=`
(fila 111 de `e2b-parity.md`, divergente desde 0.6.0).

## ADR-018 — efs-volumes (M15, 0.6, experimental)

**Contexto.** E2B's beta `Volume` gives several sandboxes a live-shared
POSIX directory; Rayito only has `persist=` (an S3 checkpoint/restore copy
that never shares data between two running sandboxes). The feasibility
study (`docs/research/2026-10-efs-persistence.md`) found the design viable
*conditioned on* an AWS measurement campaign (EFS-1..EFS-20) that decides
three stop criteria before any real mounting code is written: NFSv4.1
compiled into the guest kernel (EFS-1, already answered: it is), a
Rayito-owned VPC connector that reaches an EFS mount target (EFS-3), and
`efs-utils` mounting with TLS+IAM+access-point and no `systemd` (EFS-8).

**Decisión.** Split the feature in two. This change ships everything that
does not depend on the campaign's answer: the pure domain and port
(`rayd_core::volume`, `VolumeMounter`), `rayd`'s only adapter
(`UnavailableEfsMounter`, always `Unsupported`; `features::efs_volumes`
holds it behind the port, so `Health.features.efs_volumes` is its
`support()` and the section answers `UNSUPPORTED` until a real mounter
lands), a real `VolumeStore` CRUD over EFS access points (caller's
credentials, never the execution role), the `Sandbox.create(volumes=)`
surface that validates eagerly (shape, mount path, `base-caps` variant)
and always raises `UnimplementedError` naming the pending campaign, the
`efs-volumes` `OptionalStack` component (`infra/efs-volumes.yaml`: file
system, mount targets, the two NFS security groups, a dedicated egress
connector), and the E2B shim's `Volume`/`AsyncVolume` (real CRUD once
`E2B(volume_store=...)` configures one; content operations stay
`UnimplementedError`, since there is still no data plane outside a
MicroVM). A real `VolumeMounter` adapter — `rayd` actually running
`mount -t efs -o tls,iam,accesspoint=...` as root, the watchdog, the
`/suspend`/`/resume` handling — waits for the measurement campaign's own
change, once EFS-2/EFS-3/EFS-8 clear it.

**Consecuencias.** `volumes=`/`VolumeStore` are off by default: no
`EfsVolume` constructed and no `VolumeStore` method called means no `efs`
client and no `ConfigureSandbox` call, so the zero-cost golden trace is
unaffected. `VolumeStore`'s CRUD is real and useful today (an operator can
pre-provision volumes) even though mounting one into a sandbox is not;
this is documented as experimental, never silently approximated (project
rule: no undocumented divergence). Access to a volume is isolated by
execution role, not by sandbox (same limitation as S3 persistence's T15):
any sandbox with that role's credentials can mount any access point the
role is scoped to. Threat T21 (`SECURITY.md`) covers the mount path.

**Existing VPC (`EfsVolumes`).** Most accounts cannot create a VPC (an
organization SCP denied `ec2:CreateVpc` in the test account, Q124), so the
stack deploys into an existing one: `SubnetIds` (1–3, one mount target per
AZ) and only new resources — never the VPC, its subnets, routes, NACLs or
rules on existing security groups. `EfsVolumes.check()` is a pure
evaluation over read-only `ec2:Describe*` facts (a `NetworkInspector` port,
also behind `rayito doctor --efs-vpc-id`), and `deploy()` refuses on any
`FAIL`. The file system stays `DeletionPolicy: Retain`;
`destroy(delete_file_system=True)`/`delete_file_system(id)` delete it
explicitly, only when it carries the template's literal
`rayito=efs-volumes` tag. The connector's only egress is NFS to the mount
targets: internet through the VPC would depend on the VPC's own NAT and a
connector that allows it (EFS-4 measures combining with `INTERNET_EGRESS`).

**Adenda 2026-10-04: adaptador real tras la aceptación (Q126–Q134).** Every
stop criterion passed, so `UnavailableEfsMounter` is replaced by
`EfsUtilsMounter` (`mount -t efs -o tls,iam,accesspoint[,mounttargetip][,ro]`
as root), still gated by support detection (`CAP_SYS_ADMIN`, `nfs4`,
`mount`, `/usr/sbin/mount.efs`, `/usr/sbin/efs-proxy`): no published image
installs `amazon-efs-utils` yet, so shipped images keep answering
`UNSUPPORTED` and the SDK keeps `volumes=` in `UnimplementedError` until
an image layer and the `create()` wiring land (both landed: addendum (b)
below). What the measurements
changed:

- *`efs-proxy` outlives `umount`* (Q128). The adapter serializes mounts,
  attributes the root `efs-proxy` processes that appear during the helper
  run (pid + start time, `rayd_core::volume::proxy`) and stops them on
  unmount and `/terminate`. The helper goes through `ChildRegistry`;
  the proxy is re-parented to PID 1 and its zombie is left to the orphan
  reaper (rayd never `waitpid`s it).
- *Symlinked mountpoints*. The helper mounts on a root-only staging
  directory and the result is bind-mounted through the `O_NOFOLLOW` walk
  `mounts=` already uses (`adapters::mountpoint`, now shared).
- *A pause past the credentials' expiry breaks the volume* (Q129). Each
  mount records the execution-role lease expiry; `/resume` remounts when it
  has passed (or is inside `REFRESH_MARGIN`), otherwise probes with a
  bounded `stat` child and remounts on failure. Remounts are tasks: the
  hook waits 1.5 s at most and `ConfigureStatus` reports `REMOUNTING` →
  `MOUNTED`/`DEGRADED`, like an S3 mount relaunch.
- *Unflushed writes + unreachable mount target lose the VM* (Q130). The
  slot is a `/suspend` participant: one bounded `syncfs` per volume inside
  its `SuspendShares` allocation, `DEGRADED`/`flush_timeout` when it does
  not finish. The loss itself has no guest-side mitigation and is
  documented.
- *uid 1000 reaches `efs-proxy`'s loopback port* (Q133) and the guest
  kernel has no `owner` match (Q48), so read-only is enforced in IAM:
  `ReadOnlyAccessPointArns` adds an explicit `Deny` on `ClientWrite`. A
  guest-side `ip rule … dport <port> prohibit` ahead of `local` (the M10
  DNS-guard mechanism) is a measured-later hardening, not a control we
  rely on (T21).
- *One egress connector per MicroVM* (Q131). The SDKs reject `volumes=`
  without exactly one own connector in `egress=` (never `INTERNET_EGRESS`)
  before `run-microvm`; internet for such a sandbox must come through the
  customer's VPC (NAT or transit gateway plus a connector that allows it).

**Adenda 2026-10-04 (b): `create(volumes=)` real e imagen opcional.**
`volumes=` leaves `UnimplementedError` (OpenSpec `m15-efs-volumes`
design D11–D14). The SDKs validate before launching (1–4 volumes, paths,
caps, one own connector, `execution_role_arn`), resolve each missing mount
target IP with one `DescribeMountTargets` per file system (caller
credentials, first `available` by `AvailabilityZoneId`) in a generic
pre-launch step (`prepare_features`/`prepareFeatures`), and send the
`efs_volumes` section in the single post-ready `Configure`; because `rayd`
mounts inside that call, a `SlowApplySection` stretches the call deadline
to 4 × 15 s + 5 s. A failed mount terminates the sandbox
(`VolumeMountException`/`VolumeMountError` with a closed `code`) and
`reincarnate()` replays the section. `amazon-efs-utils` ships only in the
opt-in image `rayito-base-caps-efs` (`rayito image publish --with-efs`,
a zip marker read by a conditional Dockerfile layer that also re-links
`/usr/bin/python3` to 3.12), so the default images and their
`Health.features.efs_volumes = false` do not change; `rayd` passes
`AWS_REGION` to the helper, so the image bakes no region. The E2B shim's
`volume_mounts` launches with `E2B(volume_connector_arn=)` as its only
egress connector.

## ADR-019 — sizes-catalog (M15, 0.6)

**Contexto.** `create-microvm-image` fija la memoria del guest con
`resources[0].minimumMemoryInMiB` por *versión de imagen*, no por
lanzamiento: no hay una API de tipo "RunMicrovm con este tamaño". Medido
(AWS_API_NOTES.md §24, Q87): sólo 512/1024/2048/4096/8192 MiB construyen;
256, 3072, 10240 y 16384 dan `ValidationException` síncrona sin crear
nada. Medido también (RES-2/Q88, confirma el punto suelto de Q61/Q68) que
el guest ve memoria/512 vCPU y hasta ~4x la memoria nominal en los cinco
tamaños del catálogo (512→1 vCPU/1989 MiB, 1024→2/3998, 2048→4/8016,
4096→8/16052, 8192→16/32123; disco raíz ext4 8,3 GB hasta 2048, 16,7 GB a
4096, 33,6 GB a 8192). SPEC.md §4 decía "no hay, ni se promete, un
resolvedor de tamaño por sandbox" (ADR-019 lo reemplaza; D1 deja la
redacción exacta a decisión del mantenedor).

**Decisión (opción A de §4 de la investigación).** Un catálogo cerrado de
cinco tamaños (`SUPPORTED_MEMORY_MIB`/`SIZE_NAMES`:
`512mb`/`1gb`/`2gb`/`4gb`/`8gb`), resuelto enteramente en el SDK, sin RPC ni
sección de `ConfigureSandbox`: `size=`/`size` no es un ajuste del guest en
marcha, es qué imagen lanzar. `resolveSize` (`_sizing.py`/`sizing.ts`)
redondea siempre hacia arriba al primer valor publicado que cubra lo
pedido (nunca por debajo) y avisa con `RayitoCompatWarning`/
`RayitoCompatWarning` cuando no hay coincidencia exacta; por encima de
`MAX_SUPPORTED_MEMORY_MIB` (8192) es `InvalidArgumentException` antes de
cualquier llamada a AWS. El nombre de imagen sigue la convención
`<variant>[-<size>]` con el sufijo siempre al final (`apply_size_suffix`/
`applySizeSuffix`): el baseline (2048 MiB) nunca lleva sufijo, así que
`rayito image publish` sin `--sizes` sigue publicando exactamente lo
mismo que en 0.5.x. `rayito image publish --sizes 512mb,4gb` publica,
desde el mismo artefacto, una imagen adicional por tamaño
(`cli/_publish.py`: `sized_settings`/`publish_sizes`), horneando
`RAYITO_BASELINE_MEMORY_MIB` en `environmentVariables` (configuración de
imagen, nunca un interruptor de activación, ADR-014 regla 4) y sometiendo
los `create`/`update-microvm-image` en oleadas de a lo sumo
`MAX_CONCURRENT_IMAGE_BUILDS_Q83` (10) construcciones simultáneas antes de
esperar a que ninguna se asiente. `ConventionCatalog`
(`_size_catalog.py`/`sizing/catalog.ts`) hace, sólo cuando `size=`/`size`
se usó, una única llamada gratuita a `GetMicrovmImageVersion` por versión
de imagen (cacheada por `(imageArn, imageVersion)` y por proceso) para
confirmar `resources[0].minimumMemoryInMiB` y rellenar
`SandboxInfo.baselineMemoryMib`/`baselineCpu` en `get_info()`/`getInfo()`;
`cpu_count`/`memory_mb` (`cpuCount`/`memoryMb`) siguen siendo lo que el
guest reporta de verdad vía `Health`, nunca un valor derivado. `baselineCpu`
es la proporción memoria/512 que RES-2/Q88 midió exactamente para los
cinco tamaños del catálogo (no una extrapolación).
`infra/sizes-guard.yaml` (`RayitoRunAllowedSizes`, componente
`sizes-guard` de `OptionalStack`) es un guardarraíles de coste opcional:
una política IAM que limita `lambda:RunMicrovm` a los ARN de imagen que el
operador liste explícitamente, para que nadie lance, por accidente, un
tamaño más caro que el publicado. `create(pool=, size=)` /
`create({ pool, size })` es `InvalidArgumentException`/`InvalidArgumentError`:
una plaza del pool ya salió de una imagen fija.

**Consecuencias.** Sin `size=`/`size` (su valor por defecto) no hay ningún
`GetMicrovmImageVersion`, ningún ajuste al nombre de la plantilla y el
comportamiento es exactamente el de 0.5.x (golden test de M15
foundations). `Template.build(memory_mb=)` (m15-templates) consume
`resolveSize` sólo como lector, sin duplicar el catálogo.

**Alternativas descartadas.** Resolver el tamaño en el agente (`rayd`)
leyendo `minimumMemoryInMiB` de su propia `Health`: no sirve para decidir
*qué imagen lanzar*, que es una decisión de antes de `run-microvm`.
Permitir cualquier entero de memoria en `size=`: el catálogo de
`create-microvm-image` ya es cerrado (Q87), así que aceptar cualquier
valor sólo trasladaría el error de validación de cliente a AWS, más tarde
y con una imagen a medio construir.

**Reversible.** El catálogo es datos puros (`limits.json`); una imagen sin
sufijo sigue siendo válida siempre. Borrar la pila `sizes-guard` no borra
ninguna imagen ni versión.

## ADR-020 — events-webhooks (M15, 0.6)

`rayd` emite una línea por evento de ciclo de vida (`created`, `paused`,
`resumed`, `killed`) en su propio stdout: `rayito.event.v1 <b64url(json)>
<b64url(mac)>`. La clave (`k_sbx`) la deriva el SDK —
`HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` — y la empuja una
vez por `ConfigureSandbox` (`LifecycleEventsConfig.sandbox_key`); `rayd`
nunca ve `stack_key`, así que un sandbox comprometido sólo revela su propia
clave. `created` se emite la primera vez que la sección trae una clave;
`paused`/`resumed` en `/suspend`/`/resume` y `killed{reason: request}` en
`/terminate`, a través del participante de la función (ADR-015: una vez por
transición aceptada). `paused` y `killed` esperan a que la línea salga
(`LifecycleEventSink::flush`: una barrera en la misma cola que la tarea de
drenaje reconoce tras escribir y vaciar stdout), acotado por la cuota de
`/suspend` (≤ `SUSPEND_SHARE_MAX`, 1 s) y por
`PARTICIPANT_TERMINATE_TIMEOUT`: sin eso el VM podía congelarse, o el
proceso salir, con la línea aún en cola. Si la fuente aleatoria falla, el
evento se descarta y se cuenta (`random_unavailable`) en vez de emitir un
`event_id` repetido. El estado vive en el `FeatureSet` único del proceso
(ADR-015), nunca en un singleton.

Una suscripción de CloudWatch Logs reenvía cada línea a un Lambda
*forwarder*, que re-deriva `k_sbx` del secreto del stack, comprueba que el
*log stream* de origen termina en el `sandbox_id` de la línea
(`YYYY/MM/DD[<versión>]<microvmId>`, Q106) y escribe
el evento, de forma idempotente, en una tabla DynamoDB (TTL 7 días). Sólo un
evento nuevo mueve la fila `STATE#` del sandbox, y sólo hacia delante
(escritura condicional sobre `last_seen_ms`); `killed` la deja como lápida
con TTL, así que una línea tardía nunca reabre un sandbox. Un *deliverer*
(disparado por DynamoDB Streams) entrega el evento a cada webhook suscrito a
ese tipo, firmado al estilo E2B (`e2b-signature` = base64 sin relleno de
`sha256(secreto + payload)`), con un guardián SSRF (resuelve DNS, rechaza
loopback/privada/link-local/CGNAT, conecta a la dirección ya comprobada —
nunca una segunda resolución), hasta 3 intentos sólo ante 5xx o error de
transporte y una lectura de respuesta acotada a 64 KiB. Cada par
`(evento, webhook)` se reclama (`attempting`) antes del primer intento y se
cierra como `delivered` o `failed`; sólo `delivered` se salta en una
reentrega, así que un fallo o un timeout nunca pierde una entrega. Cada
intento cabe en el tiempo que le queda a la invocación; si se agota, el
registro pendiente vuelve como `batchItemFailures` y el stream reintenta
desde ahí (lo que agota sus reintentos va a una cola SQS). Un *reconciler*
(cada `ReconcilerIntervalMinutes`, 5 por defecto, mínimo 2) compara
`ListMicrovms` con los sandboxes que la tabla aún considera abiertos y
sintetiza `killed{unknown}` con la generación y la imagen de su último
evento — sólo `unknown`: distinguir `timeout` exige conocer la duración
máxima de cada sandbox, que esta iteración no rastrea. Su cliente
`lambda-microvms` sale de una sesión botocore propia cuyo `data_path` es el
modelo que `scripts/gen_stack_assets.py` inyecta en el zip desde
`docs/aws-api/service-2.json` (decisión 8).

**Envío desde `create()`:** `plan_features`/`planFeatures` valida
`events=` antes de `run-microvm` (un `LifecycleEvents`/`AsyncLifecycleEvents`
y un `logging` que llegue a CloudWatch) y lo deja en `FeaturePlan.events`;
la sección necesita `sandbox_id`/`image_arn`/`image_version`, así que
`planned_sections`/`plannedSections` añade un
`LifecycleEventsSectionFactory` en cuanto `run-microvm` los da, y el mismo
`_apply_configure_sections`/`#applyConfigureSections` que `mounts=`/
`gateways=`/`telemetry=` la resuelve (un `GetSecretValue` de la clave del
stack por instancia de `LifecycleEvents`, nunca por la `SecretCache` de
`secrets=`: no es un secreto del usuario), exige `Health.features.
lifecycle_events` y la manda en el único `Configure`. Cualquier fallo (pila
sin desplegar, agente anterior a 0.6.0, sección `INVALID`) termina el
sandbox salvo `keep_on_failure`. `AsyncLifecycleEvents` aporta su
`LifecycleEvents` interno: la resolución corre en un hilo también en
`AsyncSandbox.create()`. `reincarnate()` la repite como al resto de
secciones (m15-reincarnate-configure-replay: `LaunchOptions.features`
reenvía `mounts=`/`events=`/`telemetry=`/`gateways=` al `create()` del
sucesor, que deriva `k_sbx` del nuevo `sandbox_id`), y `pool=`/`take()` la
rechazan como el resto de opciones 0.6.

## ADR-021 — rayd-otlp (M15, 0.6)

**Contexto.** `get_metrics_history()` (M9) sólo sirve el anillo de 5 s de
`MetricsHistory` dentro del propio proceso del llamante; para verlo junto al
resto de la infraestructura en un dashboard, alguien tendría que montar su
propio reenvío. La investigación (`docs/research/2026-10-e2b-out-of-scope.md`
§6) evaluó cinco opciones; B1 ("`rayd` → CloudWatch OTLP firmado") es la
única que exporta sin montar un plano propio, y el crítico de la
investigación añadió B1' (el mismo diseño con un token al portador) porque
B1 por sí sola exige el execution role dentro del guest, legible por uid
1000 en `rayito-base` sin caps (T1).

**Decisión.** `rayd` reutiliza el muestreador de 5 s existente para exportar
7 gauges (research §7.5) como un `ExportMetricsServiceRequest` OTLP/HTTP
protobuf + gzip a `monitoring.<región>.amazonaws.com/v1/metrics`, con
`TelemetryAuth::ExecutionRole` (SigV4 sobre las credenciales IMDS
compartidas, `adapters::credential_broker::ImdsCredentialBroker`, ADR-012:
exige `rayito-base-caps`) o `TelemetryAuth::Bearer` (un token empujado por
`ConfigureSandbox`, guardado sólo en memoria del agente, funciona en
`rayito-base`). La cola entre el muestreador y la red es acotada
(`rayd_core::telemetry::Batcher`, research §6.5) con backoff con jitter por
sandbox, para no sincronizar reintentos tras un `/resume` masivo contra la
cuota de 500 TPS de la cuenta. El exportador implementa
`LifecycleParticipant` (ADR-015): su `/suspend` es un intento de vaciado
acotado a 2 s, nunca bloqueante; `ready_gate()` usa el valor por defecto
(nunca retrasa `/ready`). `Health.features.telemetry_export` (y
`root_egress` con `CloudwatchOtlp`) sólo son `true` cuando el agente conoce
`AWS_REGION`: sin región no hay endpoint que construir ni firma posible, así
que el slot se queda `Unsupported`, igual que un build anterior a 0.6.0.
El firmante SigV4 (HMAC-SHA256 sobre `sha2`, verificado contra el vector de
RFC 4231) vive en `rayd` en vez de añadir una dependencia nueva (§5 del plan
de M15: esta función puede añadir protos vendidos, no crates); el
`ExportMetricsServiceRequest` se construye sobre un subconjunto vendido y
mínimo de los tipos de OpenTelemetry (`crates/rayito-proto/vendor/opentelemetry/`,
Apache-2.0, ver `/NOTICE`), con los mismos números de campo que el esquema
real, nunca el `.proto` completo.

La propagación W3C `traceparent` del lado del SDK (research Q92: sólo
`traceparent`/`tracestate`, nunca `grpc-trace-bin` ni `baggage`) tiene su
seam (`CallMetadataProvider` en `_transport.py`/`transport/`) y su
implementación (`TraceparentProvider`/`_telemetry_export/_propagation.py`),
pero **no está conectada todavía al canal gRPC real**: hacerlo bien exige
reordenar cuándo se construye el canal de autenticación frente a cuándo se
conoce la instrumentación OTel en varios puntos de un fichero compartido
entre las siete funciones 0.6 (`sandbox_sync/main.py` y su espejo). Es un
seguimiento razonado y no bloqueante, registrado en
`openspec/changes/archive/2026-10-03-m15-rayd-otlp/design.md`.

**Consecuencias.** Sin `telemetry=`/`telemetry`, `rayd` no abre ninguna
conexión nueva y el SDK no envía ninguna sección de `ConfigureSandbox`
(prueba de coste cero dedicada). Con `OtlpAuth.execution_role()`, la
política IAM mínima de CloudWatch no puede acotarse por namespace
(investigado): un sandbox malicioso con esa opción puede escribir métricas
arbitrarias, no sólo las 7 de `rayito` (documentado en T23 y en el bloque
"Coste y activación" de la función). `sbx.get_telemetry_status()` (Python) /
`sbx.getTelemetryStatus()` (TypeScript) es una llamada explícita a
`ConfigureStatus`, nunca parte de `get_health()`/`getHealth()`, para que el
camino sin `telemetry=` nunca pague un RPC de más.

**Reversible.** Aditivo: un campo de `ConfigureRequest`/`ConfigureResponse`
(`telemetry_export`), un slot más de `FeatureSet` y una política IAM
opcional (`rayito stack destroy otlp-export` la retira sin afectar a nada
más). Un agente 0.5.x o un 0.6.0 sin este slot implementado se comporta
exactamente igual que antes de este cambio.

## ADR-022 — templates (M15, 0.6)

**Contexto.** E2B's `Template()` builder (filas 56, 81, 89, 112 de la
investigación) es la función con más valor para quien migra un agente ya
escrito contra E2B: un DSL fluido que hoy en Rayito sólo tiene como
análogo escribir un `Dockerfile` a mano y correr `rayito image publish`.
TPL-1 (Q83) y TPL-2 (Q84) de la campaña de medición resolvieron las dos
preguntas de diseño abiertas antes de empezar: el grupo de logs de la
imagen recibe la salida completa de BuildKit (`#N [k/n] RUN …`, códigos de
salida) tanto en un build correcto como en uno fallido, así que el SDK
puede explicar un fallo sin CodeBuild ni streaming en vivo; y
`codeArtifact.uri` sólo acepta `s3://`, nunca una referencia a ECR, así que
`from_base_image()` siempre compone sobre un zip ya existente en S3, nunca
sobre una imagen externa.

**Decisión.** Opción A de la investigación §3: un compilador enteramente en
el cliente, sin plano de control propio. `Template`/`AsyncTemplate`
(`rayito._templates`/`templates/`) son un builder inmutable que compila a
las cinco instrucciones de cable que el propio E2B usa (`COPY`, `ENV`,
`RUN`, `WORKDIR`, `USER`). `Template.build()` resuelve la versión `ACTIVE`
de la imagen base nombrada por `from_base_image()` (la única fuente
soportada: inyectar `rayd` y sus hooks en una imagen externa no tiene
camino soportado, así que `from_image`/`from_template`/`from_dockerfile`/
`from_gcp_registry` lanzan `UnimplementedError`), descarga su
`codeArtifact` completo, inserta la capa nueva justo antes de la última
`CMD`/`ENTRYPOINT` de ese Dockerfile y cierra con `USER root` más esa
misma instrucción repetida (así `rayd` sigue siendo PID 1), reempaqueta un
zip determinista con **todas** las entradas del zip base más las nuevas
(el Dockerfile base puede necesitar cualquiera de sus propios ficheros), lo
sube por hash de contenido y llama a `create`/`update-microvm-image`,
reutilizando una versión ya `SUCCESSFUL`+`ACTIVE` con la misma
configuración en vez de reconstruir salvo `force=True`. Un build que no
termina `SUCCESSFUL`+`ACTIVE` se explica releyendo el grupo de logs de la
imagen (`BuildException(step, command, exit_code, log_tail)`) o, si
`stateReason` nombra un `ready_cmd` con 4xx/5xx (TPL-5/Q85),
`BuildException(reason="ready_client_error"|"ready_server_error")`. Un
guardia local (`MAX_CONCURRENT_BUILDS = 10`, Q83) rechaza un undécimo
build concurrente en el mismo proceso antes de llamar a AWS.
`setStartCmd()`/`set_start_cmd()` hornea `/etc/rayito/template.json`
(`rayito.template/1`, dominio compartido en `rayd_core::template`). En
`rayd`, el slot `features::template_start` lo lee una vez al arrancar
(`adapters::fs_template_spec`; ausente o inválido = arranque de 0.5.x) y,
si existe, aporta un `LifecycleParticipant`: en `on_boot`, antes de
que el servidor de hooks conteste el `/ready` del build (así el snapshot
ya lleva el proceso en marcha), lanza `start_cmd` como proceso gestionado por `ProcessManager` y sondea
`ready_cmd` con `/bin/sh -c` (`adapters::shell_ready_probe`); su
`ready_gate` mantiene `/ready` en 503 hasta que `ready_cmd` sale con 0 y
responde 500 al agotarse el plazo, que AWS trata como fallo definitivo
(Q85; `rayd_core::template::ready_decision`). `Retry` sigue siendo 503.

El núcleo de build de imágenes (envío de `create`/`update-microvm-image`,
gate de tres estados, reuso por configuración con la normalización Q52,
subida por hash) vive en `rayito/_images.py` y `src/images/gateway.ts`,
compartido con `rayito image publish`. La imagen compuesta hereda toda la
configuración de la versión base (`additionalOsCapabilities` incluida).
Los ficheros de contexto viajan bajo `__rayito_context/` en el zip, así
que nunca sustituyen una entrada del zip base (T26).

**Consecuencias.** Divergencias documentadas: sin caché de capas entre
builds (0.6 no tiene una; `skip_cache()` equivale a `force=True`); sólo
ARM64; sin streaming en vivo de los pasos del build; el etiquetado de E2B
(`assign_tags`/`remove_tags`/`get_tags`/`alias_exists`) no tiene análogo
porque `create`/`update-microvm-image` etiqueta la imagen entera, no una
versión. `Template.build()` necesita una política IAM separada de la de
lanzar sandboxes (`RayitoTemplateBuilder`, `infra/templates.yaml`, un
componente `OptionalStack`), para que un agente que crea sandboxes no
pueda también publicar imágenes; esa política acota las acciones de
imagen a `microvm-image:*` de la cuenta y deniega crear o actualizar las
imágenes base publicadas. Desviación del plan: la pila no crea el bucket
de artefactos cifrado con expiración (crear un bucket convertiría una
pila de $0 sólo-IAM en una con datos que borrar al destruirla); el bucket
es del cliente, y la guía y la pila le piden una regla de ciclo de vida
sobre `rayito/templates/`.

**Riesgo residual antes del `/run` (`sec-rayd-agent-hardening`, RAYD-08).**
El `start_cmd` está en el snapshot, así que en cada sandbox lanzado desde
la plantilla corre **antes** de que llegue el `/run` de la plataforma, y
`/run` se acepta una sola vez por arranque. Un `start_cmd` malicioso o
comprometido podía ganar esa carrera: con un payload malformado dejaba la
VM sin token, y con uno bien formado instalaba su propio digest (con su
`network`, `envs`, `metadata` y `lifecycle`) y el `/run` genuino quedaba
como `already_ran`. `rayd` rechaza ahora un `/run` cuyo socket pertenece a
un uid del sandbox sin consumir el del arranque (`HookOrigin`, ADR-006), lo
que cubre al `start_cmd` del usuario por defecto (uid 1000) y a cualquier
otro uid ≥ 1000. Sigue abierto: un `start_cmd` que corra como root
(`RAYITO_ALLOW_ROOT`), y congelar los procesos del `start_cmd` hasta el
`/run` (`SIGSTOP` del grupo antes del snapshot), que no protege de un
proceso que se haya separado con `setsid` y necesita validarse en AWS real.

**Reversible.** Aditivo: ningún `Sandbox.create()` existente cambia de
comportamiento, y nada se importa ni se construye hasta que se llama a
`Template.build()`/`build_in_background()`.

## ADR-023 — secrets-gateway (M15, 0.6)

**Contexto.** `secrets=` entrega un valor como variable de entorno del
proceso que lo pide: útil, pero visible a ese proceso y a cualquier cosa
que lance. Un agente que llama a una sola API externa (Anthropic, OpenAI,
el propio backend del cliente) no necesita que su código *tenga* la
credencial, sólo que las peticiones que hace la *lleven*. T24 nombra el
riesgo: un gateway mal diseñado sería un confuso-diputado (SSRF hacia
cualquier host, no sólo el declarado) o filtraría el valor por los mismos
canales que `secrets=` (environ, cmdline, logs).

**Decisión.** `gateways=`/`gateways` declara, por nombre, un conjunto de
rutas: cada una fija un único `upstream` (`https://host`, sin ruta ni
query), una allowlist de `(método, ruta)` (exacta o con sufijo `/*`), un
límite de peticiones por minuto y qué cabeceras inyectar (el *nombre* de
un secreto ya existente, nunca el valor, hasta que `GatewaySection.fill`
lo resuelve con la misma `SecretCache` que usa `secrets=`, justo antes de
cada `ConfigureSandbox`). `rayd` abre un listener de loopback por ruta
(`rayd::secret_gateway::listener::GatewayRuntime`), que decide
(`rayd_core::secret_gateway::decision`, cubo de tokens entero y
determinista) antes de tocar la red: una petición fuera de la allowlist o
por encima del límite nunca llega al upstream. Antes de la allowlist, la
ruta de la petición pasa una lista de permitidos
(`rayd_core::secret_gateway::route::path_is_safe`,
`sec-rayd-agent-hardening`): sólo caracteres RFC 3986 de ruta salvo `;`,
ningún segmento vacío salvo el último, y cada segmento, decodificado una
sola vez, UTF-8 válido sin bytes de control, sin `/`, `\`, `%` ni `;` y
distinto de `.` y `..`. `rayd` reenvía la ruta tal cual, así que eso es lo
que impide que un upstream que normaliza `..;` o decodifica dos veces
(`%252e`) la lleve fuera de una regla `/*`; las rutas de `allow` deben
cumplir la misma regla (`invalid_allow_path`, y los SDKs lo comprueban
antes de llamar). Lo que sí llega tiene las
cabeceras que el guest pudo haber puesto para esos mismos nombres
eliminadas primero (`header_template::must_drop`) y las vaultadas
inyectadas después, así que el código del sandbox no puede suplantar su
propio secreto. La respuesta vuelve con su estado y su cuerpo intactos;
de sus cabeceras se eliminan las que llevan un nombre inyectado o un valor
vaultado (`header_template::must_drop_from_response`), pero el cuerpo no se
inspecciona: un `upstream` permitido que refleje las cabeceras de la
petición entrega el secreto, y por eso `allow` nunca debe listar un
endpoint así (T24). El valor en sí vive sólo en memoria de
`rayd` (`SecretValue`, `Zeroizing`, sin `Debug`/`Display`/`serde`),
expuesto una única vez, al construir la cabecera saliente. El upstream se
alcanza por el cliente HTTPS compartido (`GatewayUpstream`, raíz de
confianza del SO, `FilteringResolver` sobre
`rayd_core::transfer::is_forbidden_address`): un `upstream` no puede
resolver nunca a loopback, link-local o IMDS, cerrando el vector de
confuso-diputado hacia dentro de la propia VM. Tanto la petición como la
respuesta se transmiten en flujo, sin bufferizar ningún cuerpo entero, para
que SSE y una subida troceada atraviesen la pasarela sin cambios.

Una `SecretGatewayConfig` presente sustituye el estado entero del feature
(nunca un diff): toda ruta ausente de la llamada se para, toda ruta
presente se (re)arranca. No hay `LifecycleParticipant`: una conexión en
vuelo cuando `/suspend` congela la VM es, para el propio cliente HTTP del
sandbox, una conexión cortada como cualquier otra — ya sabe reintentar.

**Consecuencias.** Sin `gateways=`/`gateways`, `rayd` no abre ningún
socket de loopback para esta función y el SDK no construye ningún cliente
`secretsmanager` nuevo ni manda ningún `Configure` (ADR-014 regla 4). El
tráfico hacia el upstream fijo sale como root (el proceso `rayd`, no uid
1000): es la única excepción de egress declarada que abre esta función
(`RootEgressClass::SecretGatewayUpstream`, `Health.features.root_egress`),
documentada en T24 en vez de escondida.

**Reversible.** Aditivo: un nuevo slot `ConfigurableFeature` y una nueva
sección de `ConfigureRequest`; ningún agente ni SDK anteriores a este
cambio ven comportamiento distinto.

## ADR-024 — custom-domain (M15, 0.6)

**Contexto.** E2B da un hostname público estable por sandbox
(`allow_public_traffic`, `get_host()`). Rayito 0.5.x no tiene equivalente:
todo request necesita las cabeceras `x-aws-proxy-auth`/`x-aws-proxy-port`
que ya lleva `HostAccess`, lo que es correcto para un cliente programático
pero inviable para un navegador, un webhook o un `<iframe>`
(`e2b-parity.md` fila 15, "imposible en la plataforma").

**Decisión.** `CustomDomain` (`OptionalStack` componente `custom-domain`,
ADR-016) despliega una distribución CloudFront con alias comodín
(`*.<PublicDomain>`), una CloudFront Function de enrutado en runtime
`cloudfront-js-2.0` asociada en `viewer-request` y un `KeyValueStore`
asociado a esa Function. El hostname de una ruta es
`{puerto}-{alias}.<PublicDomain>` (`alias` el `sandbox_id`); cada ruta
ocupa dos claves del KVS, `j:<label>` (el JWE vigente de
`create-microvm-auth-token`, 823 B medidos) y `m:<label>` (JSON compacto:
`endpoint`, sha256 del `traffic_token` o cadena vacía, expiración), porque
ambos no caben en el límite de 1 KiB por valor del KVS. La Function borra
toda cabecera `x-aws-proxy-*` que traiga el viewer, comprueba el
`traffic_token` en tiempo constante cuando la ruta lo exige, y llama a
`cf.updateRequestOrigin({domainName: meta.e, customHeaders: {...}})` — el
origen real nunca está declarado de antemano en la plantilla, se elige por
petición; una ruta sin entrada en el KVS, o cuyo `m.x` (expiración) ya
pasó, recibe 404 directo de la Function (una ruta caducada no se
distingue desde fuera de una que nunca existió, ni siquiera revela que
exigía `traffic_token`), independientemente de que el JWE en sí también
caduque del lado del proxy de AWS.
`CustomDomain.register()`/`unregister()`/`refresh()` son las únicas
escrituras al KVS (SDK), nunca la Function. Aunque `cloudfront-
keyvaluestore` declara `signatureVersion: v4` en su `service-2.json`, su
`endpoint-rule-set-1.json` fija `authSchemes: sigv4a` y es la resolución de
endpoint quien manda: el plano de datos **sí necesita SigV4A** (verificado
offline contra botocore 1.43.103 con credenciales ficticias y un hook
`before-send`). `awscrt` (extra `rayito[custom-domain]`) y
`@aws-sdk/signature-v4a` (peer opcional) son dependencias reales de esta
función; sin ellas cada llamada falla con un mensaje propio en vez del
error crudo del SDK.

**Consecuencias.** `custom-domain` es la única función de M15 sin ningún
componente en `rayd`: todo vive en la distribución y en el SDK. La
integración con `Sandbox.create(domain=)`/`get_host()`/`expose()`/
`unexpose()` queda fuera de `m15-custom-domain`: la arquitectura esperaba
que foundations pre-añadiera un `HostResolver` y los miembros delegados
`expose()`/`unexpose()` (§1(g)), pero `v06-foundations` no llegó a
hacerlo, y `sandbox_{sync,async}/main.py` es de foundations en lo que toca
a kwargs, delegaciones y exports (§5). `domain=` sigue lanzando
`UnimplementedError` hasta que esa integración se construya como
seguimiento propio — el mismo criterio que usó `v06-foundations` para no
activar el reaper de zombies a medias. Mientras tanto, `CustomDomain` es
utilizable de forma independiente: desplegar la pila y llamar a
`register()`/`unregister()` a mano con el JWE y el `endpoint` que el
código llamante ya tiene. DOM-2 (HTTP/1.1 por `updateRequestOrigin`),
DOM-3 (WebSocket), DOM-5 (latencia de propagación del KVS), DOM-7
(keep-alive tras caducar el JWE) y DOM-8 (auto-resume por el dominio)
quedan sin medir hasta D3 (dominio y certificado ACM del mantenedor) y la
etapa de aceptación AWS.

**Alternativas descartadas.** Lambda@Edge en vez de una CloudFront
Function: correría en `origin-request` (sólo en cache miss, y aquí nada es
cacheable) y añadiría una Lambda por petición donde una Function basta y
es más barata. Guardar el JWE y los metadatos en una única clave del KVS:
no cabe bajo el límite de 1 KiB junto con los metadatos (D1 de
`design.md` del cambio). Asumir que el servicio firma con SigV4 llano sólo
porque `service-2.json` declara `signatureVersion: v4`: la resolución de
endpoint del servicio exige SigV4A de verdad (D2 del mismo documento,
corregido tras verificarlo offline).

**Reversible.** `destroy()` borra la distribución, la Function y el KVS;
ninguna ruta sobrevive. Mientras `domain=` siga sin cablear, no tocar
`CustomDomain` dejaría el SDK exactamente como en 0.5.x/`v06-foundations`.

## ADR-025 — ai-agent (agente de IA dentro del sandbox)

**Contexto.** El spike de agentes (`docs/research/2026-10-agent-spike.md`)
corrió OpenCode 1.18.34 y un grafo de deepagents dentro de un sandbox
`rayito-base-caps`, con Bedrock alcanzado por una `SecretGateway`
(ADR-023) y el egress cerrado: la credencial del modelo nunca entra en el
guest. Rehacer esa receta a mano tiene aristas que el spike midió: OpenCode
reintenta un 5xx sin fin, sin `--title` hace una llamada oculta a otro
modelo y, con `--attach`, su código de salida no refleja `session.error`.
En este repositorio "agente" ya significa `rayd`, así que esta función se
llama "agente de IA" y sus capacidades OpenSpec llevan el prefijo
`ai-agent-`.

**Decisión.** `sbx.agent` ejecuta un agente de IA dentro del sandbox con
las capas hexagonales de siempre:

- **Dominio puro** (`rayito/_agent/_domain.py`, `_events.py`;
  `src/agent/domain.ts`, `events.ts`): `AgentSpec`, `AgentModel`,
  `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `AgentLimits`,
  la unión de eventos discriminada por `type` y `TokenUsage`. Ningún tipo
  tiene campo de clave ni de cabeceras: el modelo y cada MCP remoto nombran
  una entrada de `sbx.gateways`, y `provider` es una clave reservada de
  `raw_config`, así que el runtime no puede apuntar a un upstream fuera de
  la pasarela. Los permisos sólo admiten `allow`/`deny` (la ejecución es
  desatendida) y niegan por defecto `question`, `webfetch` y `websearch`.
- **Fallos cerrados**: `AgentException`/`AgentError` con un `reason` de
  una lista cerrada (`model_error`, `runtime_error`, `runtime_missing`,
  `runtime_version_mismatch`, `protocol_error`, `timeout`, `max_steps`,
  `token_budget`, `output_limit`, `aborted`, `busy`) y un mensaje de una
  tabla fija; la única parte variable es un `detail_code` con forma de
  nombre de clase (`APIError`). El texto del proveedor, el prompt y el
  contenido nunca llegan a un mensaje, un log ni un span.
- **Límites impuestos por el SDK**, con valores por defecto que acotan el
  coste (`limits.json`): 50 pasos, 600 s, 16 MiB de salida y 1 000 000 de
  tokens. El `steps` de OpenCode es blando, así que el SDK aborta al
  empezar el paso `max_steps + 1`; el presupuesto de tokens se comprueba
  tras cada paso y puede pasarse en uno.
- **Pasarelas ya hechas** (`bedrock_gateway`, `anthropic_gateway`,
  `openai_compatible_gateway`): `allow` cubre sólo las rutas de los modelos
  elegidos, con el id codificado como lo mandan los clientes (`%3A`), que
  es lo que `rayd` compara en crudo.
- **Puerto `AgentRuntime`** (puro: configuración, script, traducción de
  líneas a eventos, cierre y aborto) con un adaptador por runtime
  (OpenCode; deepagents en `ai-agent-deepagents`). Una ejecución por
  sandbox a la vez (`flock`), `--title` siempre, prompt por stdin, nunca
  en argv, y `Done` sólo con salida 0 y sin evento `error`.
- **Telemetría** sólo con el `tracer_provider` opt-in existente: un span
  `rayito.agent.run` con atributos de una lista permitida.

**Consecuencias.** Sin llamar a `sbx.agent`, nada cambia: tocar la
propiedad no hace ningún RPC y construir un `AgentSpec` o una pasarela no
llama a AWS. Una ejecución usa `commands.run` y `files.write` existentes;
`rayd` y el `.proto` no cambian. Los permisos no son una frontera de
seguridad (SECURITY.md T29): la frontera es la MicroVM con el egress
cerrado. El coste del modelo domina el de la VM (7-20 veces en el spike).

**Reversible.** Aditivo: un paquete nuevo en cada SDK y una propiedad nueva
en `Sandbox`; ni el shim de E2B ni ningún agente anterior ven
comportamiento distinto.

**Nota (`ai-agent-providers`).** `ModelProvider` suma `openai`
(Responses API, también xAI), `google` (Gemini API) y `azure` (Azure
OpenAI v1), y hay nueve presets más con clave estática:
`openai_gateway`, `gemini_gateway`, `azure_openai_gateway`,
`openrouter_gateway`, `groq_gateway`, `mistral_gateway`,
`deepseek_gateway`, `xai_gateway` y `litellm_gateway`. Un valor nuevo de
`ModelProvider` sólo existe si el runtime necesita otro paquete cliente:
OpenRouter, Groq, Mistral, DeepSeek y LiteLLM siguen siendo
`openai-compatible` con su `base_path`. Sólo Bedrock y Gemini llevan el
modelo en la ruta; en las APIs al estilo de OpenAI la pasarela no puede
limitar el modelo y el tope de gasto es el del proveedor (SECURITY.md
T29). Los valores viven en `testdata/agent/provider-catalogue.json`, que
leen los tests de los dos SDKs. deepagents con `azure` usa
`AzureChatOpenAI` con la Responses API, que manda la clave en `api-key` y
no en `Authorization` (`ai-agent-providers`, tareas §6). Las
suscripciones de consumo (plan de ChatGPT, Claude Pro/Max, Copilot,
SuperGrok) se rechazan o aplazan; los motivos están en
`docs/site/docs/guias/agente-proveedores.md`. Sin cambio en `rayd` ni en el
`.proto`.

## ADR-026 — Panel de gestión opcional en la cuenta del cliente

**Contexto.** `SPEC.md` §4 listaba "Billing, dashboard o cualquier superficie
de producto SaaS" como no-objetivo. Los usuarios que operan flotas de
MicroVMs echan en falta una vista de lo que ya existe en su cuenta:
sandboxes vivos, imágenes y versiones, cuotas reales de Lambda MicroVMs
(memoria por región, TPS), coste y eventos. `rayito sandbox list|info|kill`,
`rayito doctor` y las pilas `metadata-index` y `events-webhooks` ya aportan
los datos, pero no una superficie única. La frase literal del no-objetivo
excluía un panel aunque fuera del propio usuario; lo que se quiere seguir
excluyendo es un producto hospedado por Rayito. ADR-014 ya separa ambas cosas
para el estado y las pilas opcionales; este ADR lo extiende a una interfaz.

**Decisión.** Se permite un panel de gestión **opcional** con estas reglas:

1. **Sólo en la cuenta del cliente.** El panel corre en la máquina del
   usuario (fase 1, `rayito dashboard` en `127.0.0.1`) o en una pila
   `OptionalStack` desplegada en su cuenta (fase 2, componente `dashboard`,
   ADR-016). Rayito no opera ningún servidor, base de datos ni endpoint de
   panel, y ninguna telemetría del panel sale de la cuenta.
2. **Sin facturación propia.** El panel no cobra, no mide uso para cobrar ni
   gestiona planes. El coste que muestra viene de Cost Explorer del propio
   usuario (facturado, con retraso de 24 h y caché) o es una estimación
   rotulada como tal (horas activas por precio del tamaño); nunca se presenta
   la estimación como factura.
3. **Apagado por defecto.** Sin invocar `rayito dashboard` o desplegar la
   pila no hay ninguna llamada ni recurso (regla de funciones con coste,
   ADR-014). Un test de cero llamadas lo comprueba en cada SDK. La caja
   "Coste y activación", la fila de `optional-features.md` y
   `clients/typescript/scripts/cost-declarations/dashboard.json` son obligatorias. Las llamadas a Cost
   Explorer ($0,01 por petición) sólo ocurren con el coste activado y con
   caché; en la fase 2 es un parámetro de la pila apagado por defecto y
   `ce:GetCostAndUsage` sólo se concede con él activo.
4. **Sólo lectura por defecto.** Las acciones (matar, suspender, reanudar)
   exigen una opción explícita (`--allow-actions`, o el rol de acciones de la
   pila), confirmación en la UI y una cola que respete los TPS de la
   plataforma (suspender, 2 TPS). El panel muestra sólo el campo `metadata` de
   un sandbox; el JWE de la sonda se queda en memoria y nunca se serializa.
   No muestra ni registra jamás JWE, tokens, `envs`, `runHookPayload` ni
   valores de secretos (la URL de inicio impresa en la fase 1 es la única
   excepción deliberada, con token de un solo uso). Pedir un aumento de cuota
   es un enlace a la consola, no una acción del panel.
5. **Una sola API y una sola SPA.** Los puertos del núcleo son
   `SandboxInventory`, `ImageCatalog`, `UsageCost`, `Quotas`,
   `EventsHistory` y `Actions`, con un adaptador por API de AWS e imports
   perezosos (`_optional.py`, `loadOptionalPeer`). El dominio es puro. La API
   JSON versionada (`/api/v1/...`) y la SPA sin paso de build (módulos ES,
   componentes web, tokens de la web de docs, claro y oscuro) son las mismas
   en la fase 1 y la fase 2; el servidor local y la Lambda son dos cáscaras
   de entrada sobre los mismos servicios.
6. **Paridad de SDK en los servicios.** `Quotas` y `UsageCost` se ofrecen en
   Python (sync y async) y TypeScript con la misma superficie. El servidor
   del panel vive en la CLI de Python (el ADR-027 sustituye esta frase: la CLI
   de TypeScript lo servirá también).
7. **Seguridad.** Fase 1: loopback, comprobación de `Host`/`Origin` y
   defensa de CSRF como `cli/_proxy.py`, más un token de inicio de un solo
   uso que el servidor cambia por una cookie `HttpOnly` `SameSite=Strict` y
   una URL limpia. Fase 2: una Function URL sólo admite `AWS_IAM` o `NONE` y
   un navegador no firma con SigV4, así que la variante por defecto es
   `NONE` con validación del JWT de Cognito en el manejador (emisor,
   audiencia, caducidad, firma JWKS, `token_use`) antes de cualquier ruta o
   llamada a AWS, y la shell estática como única ruta sin autenticar; `AWS_IAM`
   con identity pool y SigV4 es la variante opcional (`AuthMode=aws-iam`: dos
   Function URL, la de la API con `AWS_IAM` y la de la página sin permisos,
   con el token de Cognito en `X-Rayito-Token`; implementada y probada con
   dobles, a verificar en la cuenta de pruebas). Una función tiene un solo rol: corre con un rol de
   sólo lectura y asume por `sts:AssumeRole` el rol de acciones tras
   comprobar el grupo del llamante. Sin CORS (la SPA se sirve desde el mismo
   origen, así que la Function URL no lo habilita), `Origin` propio en cada
   `POST`, token en `Authorization` y cabeceras CSP, `frame-ancestors 'none'` y
   `Referrer-Policy` en toda respuesta. Una fila de auditoría en DynamoDB por
   acción, escrita antes de ejecutarla (si no se puede escribir, la acción no
   corre), porque CloudTrail sólo vería la identidad de la Lambda. El tema de
   usuario entra por un parámetro de la pila (`ThemeJson`) o de SSM y pasa por
   los mismos validadores que `--theme`.

**Fases.** 0: este ADR y el cambio OpenSpec `dashboard`. 1: `rayito
dashboard` local con inventario, imágenes, cuotas, coste y acciones. 2: pila
`dashboard` con la SPA y la API servidas desde una Lambda con Function URL
(CloudFormation); CloudFront más S3 queda como variante opcional documentada
como no probada donde las políticas de la organización lo impidan. 3
(opcional): dashboard de CloudWatch generado (`rayito dashboard cloudwatch` y la
pila `dashboard-cloudwatch`), vista multi-cuenta del panel local por roles de
lectura asumidos (`--account`, plantilla `dashboard-read-role` en la cuenta
destino) y alertas de cuota (pila `quota-alerts`: Lambda, EventBridge y SNS).
Cada una apagada por defecto y con su propia declaración de coste.

**Consecuencias.** El no-objetivo de `SPEC.md` §4 pasa a excluir sólo el
billing y un panel hospedado por Rayito. Hay más superficie que mantener y
asegurar: cada fase pasa revisión de `SECURITY.md` antes de publicarse.
`list-microvms` sigue siendo la fuente de verdad del estado; sin
`metadata-index` la metadata cuesta O(n) sondas y el panel lo dice. La
utilización de las cuotas de MicroVMs en Service Quotas y los TPS de las
operaciones de lista no están documentados: se miden en AWS real antes de
cerrar cada fase.

**Alternativas descartadas.** Un panel hospedado por Rayito: contradice
ADR-014 regla 1. Sólo un dashboard de CloudWatch: no hay métricas nativas de
MicroVMs ni acciones. Un panel sin API versionada (SPA acoplada a la CLI):
obligaría a rehacerlo en la fase 2. Amplify Hosting: añade una pieza sin
ventaja sobre S3 y Lambda.

**Reversible.** `rayito stack destroy dashboard` borra la pila; el comando
local no deja estado. Sin ellos el SDK queda exactamente como antes.

## ADR-027 — Herramientas de línea de órdenes en los dos SDKs

**Contexto.** La CLI `rayito`, el servidor MCP (`rayito-mcp`) y el servidor
local del panel viven sólo en el paquete Python, detrás de los extras `cli` y
`mcp`. `cli.md` y la checklist de paridad declaraban una CLI en TypeScript
"fuera de alcance", y el punto 6 de ADR-026 repetía que la CLI de Python
"sigue siendo la única CLI". Quien trabaja en Node tiene que instalar Python
sólo para desplegar una pila, listar sandboxes o abrir el panel, y la
paridad de SDK que persigue el proyecto se detiene en la biblioteca. E2B ya
resuelve esto con un paquete aparte (`@e2b/cli`, ejecutable `e2b`).

**Decisión.**

1. **TypeScript entrega los tres ejecutables.** El paquete npm `rayito`
   añade `bin` con `rayito` (CLI) y `rayito-mcp` (servidor MCP); el panel
   local es la orden `rayito dashboard`. Son entradas ESM nuevas del
   empaquetador con shebang. `exports` no cambia (`.`, `./e2b` y
   `./package.json`), `sideEffects: false` se mantiene y ningún ejecutable se
   importa desde `index`: la biblioteca no se rompe ni crece.
2. **Misma superficie, un solo contrato.** La CLI de TypeScript reproduce
   rutas, opciones, tipos, valores por defecto, `--json`, códigos de salida y
   traducción de errores de la de Python. El contrato es
   `testdata/cli/surface.json`, un golden que genera y comprueba un test de
   pytest sobre el árbol de `typer`; vitest comparará con él (fase 1) el árbol de
   comandos de TypeScript, y `scripts/check_sdk_parity.py --cli` lista lo que
   falta con la fase que lo entrega. Mientras dure la transición el informe
   no hace fallar el CI; cuando cierre la última fase se convierte en
   comprobación. Lo mismo para MCP con `testdata/mcp/tools.json`.
3. **Sin dependencias para la CLI.** Se usa `node:util parseArgs` con un
   árbol declarativo propio (`defineCommand`). La CLI es una cáscara sobre
   servicios ya públicos del SDK; la lógica nueva (doctor, publish, prune,
   proxy, panel) se escribe como dominio puro más adaptador, con los
   valores por defecto de las constantes del SDK y los límites compartidos en
   `limits.json`.
4. **MCP con el SDK oficial como peer opcional.** `@modelcontextprotocol/server`
   (v2) y `zod` son peers opcionales cargados con `loadOptionalPeer`, el
   equivalente del extra `mcp`. Se descarta `@modelcontextprotocol/sdk` 1.x
   por su cadena de dependencias (express, hono, jose, ajv…) y un JSON-RPC
   propio por stdio por el coste de seguir a mano el protocolo en una
   superficie expuesta. Mismas seis herramientas, anotaciones, variables
   `RAYITO_MCP_*`, lease de un sandbox por proceso y log sólo por stderr y sin
   payloads. Si `--http` exige un segundo peer, será opcional y se carga sólo
   con esa opción.
5. **Panel: una SPA, un contrato, dos cáscaras locales.** La copia canónica de
   la SPA sigue en el paquete Python (la necesitan el wheel y la Lambda del
   componente `dashboard`); el build de TypeScript la copia a `dist/` sin
   versionarla, y `pack-check` compara el tarball con la fuente byte a byte.
   El contrato `/api/v1` es el esquema JSON ya existente más casos y vectores
   compartidos (`api-v1-cases.json`, `guard-vectors.json`,
   `security-headers.json`, `theme-vectors.json`) que ejecutan los dos SDKs.
   La seguridad de ADR-026 punto 7 no se relaja: sólo `127.0.0.1`, `Host` y
   `Origin` comprobados, token de inicio de un solo uso cambiado por cookie
   `HttpOnly` `SameSite=Strict`, CSRF, CSP sin `unsafe-inline`, solo lectura
   por defecto y sin registrar peticiones, cabeceras ni token. La Lambda del
   panel sigue siendo Python mientras dure la transición.
6. **Funciones con coste apagadas.** Nada de esto cambia ADR-014: sin
   invocar la orden no hay llamadas ni recursos, el coste facturado del panel
   sigue apagado por defecto y el test de cero llamadas vale en los dos SDKs.
7. **Una instalación global gana a la otra.** Si conviven la CLI de Python y
   la de TypeScript, manda la primera del `PATH`; `rayito --version` dice qué
   runtime responde (`node` o `python`) y la documentación lo avisa.
8. **Sin API síncrona en TypeScript (D8).** TypeScript sigue siendo sólo
   asíncrono (`await Sandbox.create()`); no se ofrece una variante bloqueante,
   ni en la biblioteca ni como apoyo de la CLI. Las opciones estudiadas:
   `Atomics.wait` con un worker (estilo synckit) bloquea el bucle de eventos,
   exige argumentos clonables (sin callbacks `onStdout`, streams, PTY,
   `AbortSignal` ni handles vivos), necesita un registro de proxies entre hilos,
   no sirve en navegador ni en el hilo principal de Electron y duplica
   superficie y tests; `child_process.execFileSync` lanza un proceso por llamada
   (cientos de milisegundos), no reutiliza gRPC ni la caché de tokens, no
   permite streaming y hace pasar secretos por argumentos o entorno. El SDK de
   JavaScript de E2B tampoco la tiene: sólo Python ofrece sync y async. La
   regla `ASYNC_PREFIX` de `check_sdk_parity.py` ya lo recoge, y la
   referencia de TypeScript lo documenta con un ejemplo de `await` de primer
   nivel.

**Fases.** Un PR apilado por fase y una sola aceptación en AWS al final:
0, este ADR, el cambio OpenSpec `typescript-cli-tools`, el golden y el
informe de paridad; 1, núcleo de la CLI y las órdenes más usadas; 2, `doctor`;
3, pilas, templates, eventos y dominio; 4, terminal interactiva y
`sandbox proxy`; 5, `image publish|prune|zip`; 6, servidor MCP; 7, núcleo del
panel; 8, `rayito dashboard` local; 9, documentación y CHANGELOG.

**Consecuencias.** Hay que mantener dos implementaciones de la CLI, del MCP y
de la seguridad del panel, y la deriva se acota con el golden de superficie y
los vectores compartidos en `testdata/`. `SECURITY.md` se revisa antes de las
fases 4 y 8. El guest y `rayd` siguen probándose una vez; los ejecutables de
TypeScript se prueban en los dos SDKs y con e2e propios. El punto 6 de
ADR-026 queda sustituido (la CLI de Python deja de ser la única); el resto de
ADR-026 sigue en vigor. `cli.md` y la checklist de paridad dejan de declarar la
CLI de TypeScript fuera de alcance.

**Alternativas descartadas.** Un paquete npm aparte para la CLI (`@e2b/cli`):
duplica versionado y publicación y rompe `npx rayito`. Dejar la CLI sólo en Python: obliga a instalar Python a quien sólo usa Node.
Una dependencia de parseo de argumentos (commander, yargs): suma cadena de
suministro para lo que cubre `parseArgs`.

**Reversible.** Quitar `bin` y las entradas del empaquetador devuelve el
paquete a ser sólo biblioteca; la CLI, el MCP y el panel de Python no
cambian.

## ADR-028 — agent-runtimes-byo: trae tu propio agente y despliégalo

**Contexto.** ADR-025 dejó `sbx.agent` con un puerto `AgentRuntime` y dos
adaptadores (OpenCode y deepagents), pero el resto está cerrado:
`AgentTemplate` sólo acepta esos dos nombres y tiene su instalación escrita
a mano, un agente sólo corre como tarea (`sbx.agent.run`) desde un proceso
que tiene el handle del sandbox, y quien ya tiene un agente en Strands
Agents (SDK o harness), Hermes Agent, LangGraph o su propio código no tiene
cómo construirlo, configurarlo y servirlo sin rehacer la receta. Los tres
estudios previos (Strands, Hermes y el resto de frameworks) coinciden en
tres cosas: cada framework se puede apuntar a una URL base con una clave de
marcador, así que la pasarela de secretos (ADR-023) sirve para todos; el
contrato HTTP de Bedrock AgentCore (`GET /ping`, `POST /invocations`, JSON o
SSE) ya lo hablan Strands y `bedrock-agentcore`, y es el único documentado
que vale igual en una MicroVM, en Docker y en AgentCore; y todos traen
valores por defecto peligrosos (telemetría hacia fuera, herramientas que
ejecutan código del modelo sin aislar, memoria persistente, autoactualización)
que hay que apagar.

**Decisión.**

1. **Un puerto, más runtimes.** `AgentRuntime` (ADR-025) no cambia de firma.
   Se añaden dos puertos opcionales que un adaptador puede implementar:
   `InstallableRuntime` (`install_steps(target)`, `manifest_entry()`: lo que
   `AgentTemplate` instala y hornea en el manifiesto) y `ServableRuntime`
   (`host_config(...)`: cómo lo sirve el host del agente). Un objeto que sólo
   implemente `AgentRuntime` sigue valiendo para `sbx.agent.run`. El registro
   suma `strands`, `strands-harness`, `hermes` y `langgraph` (alias de
   `DeepAgents` con `entrypoint` obligatorio); `custom` no tiene entrada por
   nombre porque necesita configuración (`CustomRuntime(...)`). OpenCode y
   deepagents se reexpresan con esos puertos sin cambiar su comportamiento,
   sus comandos ni sus vectores.
2. **`AgentTemplate` deja de tener recetas a mano.** Compone los
   `install_steps()` de cada runtime (objeto o nombre). Los valores por
   defecto (`("opencode", "deepagents")`), el Dockerfile y el manifiesto de
   esos dos no cambian; el manifiesto `rayito.agent-template/1` gana claves
   aditivas (`runtimes`, `host_sha256`). Todo lo que se descarga va fijado:
   binarios con sha256, venvs con `--require-hashes` desde ficheros de
   paquete, Hermes por commit verificado con `git rev-parse` (no tiene
   paquete en PyPI), Node por tarball con sha256. Las versiones viven en
   `limits.json`.
3. **Un host del agente dentro de la VM.** `deepagents_runner.py` se
   generaliza en `agent_host` (biblioteca estándar, sin `rayito`, de root y
   0755 bajo `/opt/agents/rayito/`). Habla el protocolo JSONL v1 (ADR-025)
   por dos transportes: stdio (tarea, lo que hoy hace el runner) y HTTP
   (servicio). Sus drivers: `deepagents`/`langgraph`, `strands` (SDK y
   harness, en proceso), `node-callable` (un puente Node con un adaptador por
   framework: Strands, LangGraph.js y deepagents-js, AI SDK de Vercel y una
   función propia; `strands-ts` es su nombre antiguo), `hermes` (su API server
   en loopback, con clave aleatoria por arranque), `opencode` (traductor del
   JSONL de `opencode run`), `callable` (función `módulo:atributo`),
   `command` (proceso que habla v1 o texto plano) y `http-app` (servidor
   propio del usuario detrás del host: con el protocolo `json` también
   reenvía sus demás rutas). El host supervisa las apps de larga vida (las
   relanza con espera creciente y declara `app_crashloop` agotado el
   presupuesto) y puede poner una cola FIFO acotada delante de
   `max_concurrency`. La ruta `deepagents_runner.py` se
   mantiene como envoltorio, así que una imagen nueva sirve a un SDK
   anterior y al revés. Los límites de `AgentLimits` los aplica el host
   cuando no hay SDK en el bucle (servicio), con la misma semántica y los
   mismos vectores que el `LimitTracker` del SDK.
4. **Un contrato de servicio canónico: `rayito.agent-service/1`.** El de
   AgentCore: `GET /ping` (`Healthy`/`HealthyBusy`) y `POST /invocations` con
   `{"prompt", "session_id"?, "model"?, "reasoning"?}`; con
   `Accept: text/event-stream` responde SSE con un evento v1 por mensaje, y
   si no, un JSON con `text`, `session_id`, `usage` y `failed`. En una
   MicroVM escucha en `AGENT_SERVICE_PORT` (el 8080 y el 9000 son de
   `rayd`); en un contenedor exportado, en el 8080 de AgentCore. Una
   invocación a la vez por defecto (`HealthyBusy` y 429 por encima).
5. **`AgentProject`: un objeto, un fichero.** Un value object que compone
   lo que ya existe (`AgentSpec`, pasarelas, `AgentLimits`, opciones de red)
   con el runtime, el código del usuario (`source`) y las opciones de
   servicio. Se construye en código o se carga de `rayito-agent.toml` (o
   `.json`, sin dependencias) con esquema `rayito.agent-project/1`: claves
   desconocidas rechazadas con su ruta, y ningún campo admite el valor de un
   secreto, sólo su nombre. Operaciones: `build()` (la imagen, vía
   `AgentTemplate`), `run()` (una tarea en un sandbox efímero), `deploy()`
   (una MicroVM que sirve el contrato y devuelve un `AgentDeployment` con
   `url`, `headers`, `invoke()`, `stream()`, `status()` y `stop()`) y
   `export()` (un contexto de build Docker o AgentCore, sin llamar a AWS).
   Python síncrono y asíncrono (`AsyncAgentProject`, `AsyncAgentDeployment`)
   y TypeScript con la misma superficie; la CLI de los dos SDKs suma
   `rayito agent init|build|run|deploy|export|invoke|status|stop`.
6. **Credenciales.** Dentro de Rayito, igual que ADR-025: el runtime recibe
   la URL de la pasarela y `MODEL_CREDENTIAL_PLACEHOLDER`, nunca la clave;
   Bedrock va con clave de API por la pasarela, no con SigV4 ni con IMDS.
   Una combinación runtime/proveedor que no se pueda cablear así falla con
   `InvalidArgumentException` antes de cualquier RPC (matriz en
   `testdata/agent/provider-catalogue.json`). En `export()` no hay `rayd` ni
   pasarela: el contenedor lee la credencial de la plataforma (variable de
   entorno estándar del proveedor o la cadena de credenciales de AWS) y la
   documentación dice que esa garantía pasa a ser de la plataforma.
7. **Valores por defecto seguros.** Telemetría de los frameworks apagada
   (sin `OTEL_TRACES_EXPORTER`, sin trazas a terceros); en el harness de
   Strands, `programmatic_tool_caller` y la búsqueda web apagados y memoria y
   sesiones bajo `AGENT_STATE_DIR`; Hermes con `HERMES_HOME` propio,
   `HERMES_IGNORE_USER_CONFIG=1` y su API sólo en loopback; sin
   autoactualización; egress cerrado (`allow_internet_access=False`) salvo
   que el proyecto lo abra. Los permisos siguen sin ser frontera (T29).
8. **Coste.** `build()`, `run()` y `deploy()` cuestan lo que el
   `Template.build` y el `Sandbox.create` que hacen por debajo, más el
   modelo; nada se llama sin invocarlas, y `export()`, `init` y cargar un
   fichero no llaman a AWS. Caja "Coste y activación" en `AgentProject` y
   en `deploy()`, y un test de cero llamadas en cada SDK.

**Consecuencias.** Sin usar `AgentProject` ni los runtimes nuevos nada
cambia: `sbx.agent.run`, `AgentTemplate()` y sus vectores quedan igual.
`rayd` y el `.proto` no cambian. El traductor de OpenCode existe ahora tres
veces (Python, TypeScript y el host) y las tres se prueban contra los mismos
vectores (`testdata/agent/opencode-*`); es el precio de servir OpenCode sin
SDK en el bucle. Un despliegue en una MicroVM vive como mucho 8 h (ADR-007)
y sus cabeceras del proxy caducan a los 60 min: `AgentDeployment.connect()`
las vuelve a acuñar desde cualquier proceso, pero rotarlo antes de las 8 h
es cosa del llamante. Las imágenes con Hermes o Node pesan más y su primer
arranque es más lento; se mide en AWS antes de cerrar.

**Alternativas descartadas.** Un `Sandbox` de Strands que ejecute sólo las
herramientas en Rayito y el agente fuera: útil y se documenta como ejemplo,
pero no despliega el agente. Usar el servidor propio de cada framework
(`langgraph-api`, `opencode serve`, `hermes gateway`) como contrato público:
cinco contratos, y `langgraph-api` es Elastic-2.0 con clave de licencia.
Desplegar en AgentCore, ECS o Lambda llamando a sus APIs: no están en
`AWS_API_NOTES.md` y el usuario ya tiene sus herramientas; `export()` deja
el artefacto listo. YAML como formato: necesita dependencia en los dos SDKs;
TOML va con `tomllib` en Python y un peer opcional en TypeScript, y JSON
sin nada.

**Reversible.** Aditivo: módulos nuevos en cada SDK, entradas nuevas en el
registro y en `limits.json`, y claves nuevas en el manifiesto. Quitar los
runtimes nuevos devuelve `AgentTemplate` a sus dos runtimes sin tocar
imágenes ya construidas.

### Addendum a ADR-028 — agent-runtimes-dx: lo que un usuario externo encontró

**Contexto.** Un usuario desplegó un grafo de LangGraph que ya tenía (un
agente ReAct detrás de FastAPI) con los runtimes de 0.14 y midió el camino.
Funcionó, pero nueve cosas costaron tiempo o escondieron información: el
ejemplo documentado de `HttpApp` no corre (la imagen sólo trae `python3`), la
app no corre desde su directorio de código, un arranque fallido sólo dice una
clase de excepción y `deploy()` destruye la VM con la causa dentro, el
andamiaje y la tabla de claves no mencionan `[runtime.app]`, un grafo de
LangGraph a secas no produce salida, un `deploy()` sin `access_token` deja un
token que nadie puede leer y `AgentDeployment` no pausa, el uso de tokens de
una app es cero sin decir que es desconocido, el protocolo por defecto
esconde las rutas propias de la app y los preparativos de una sola vez están
repartidos.

**Decisión.** Se arreglan sin romper nada y antes de publicar 0.14.0: (1) el
host arranca la app y el comando con el venv de `requirements` primero en el
`PATH`, el código copiado en el `PYTHONPATH` y, en una `HttpApp`, como
directorio de trabajo, y `python` sin ninguno en el `PATH` es el intérprete
del host; (2) el marcador de fallo lleva el código y la cola saneada de la
salida de error (sin valores de entorno ni credenciales, acotada a 2 KiB) y
`deploy(keep_on_failure=True)` conserva el sandbox, con un token conocido;
(3) `AgentDeployment` expone `access_token` y suma `pause()` y `resume()`;
(4) `usage_reported` dice si el uso es real, sin cambiar el tipo de `usage`;
(5) el traductor de `langgraph` lee también el nodo `agent` de
`create_react_agent`; (6) el protocolo por defecto no cambia, y `raw` se
explica, se avisa en `rayito agent deploy` y se escribe en el andamiaje
`custom`; (7) `rayito image publish --base-image-version latest` resuelve la
versión de la base gestionada y la imprime. Detalle y alternativas en
`openspec/changes/agent-runtimes-dx/design.md`.

**Consecuencias.** Una imagen construida antes necesita reconstruirse para
tener el host nuevo; el SDK es compatible hacia los dos lados (un marcador de
sólo un código y una respuesta sin `usage_reported` se leen como antes). La
redacción del diagnóstico es heurística: por eso está acotada, el host no la
registra y el SDK la vuelve a acotar.

**Alternativas descartadas.** Cambiar el protocolo por defecto a `raw` o
detectarlo: quitaría `invoke()` a los proyectos que usan `agentcore` con rutas
propias. Hacer `usage` opcional: rompe `result.usage.total` en todo llamador de
0.13. Traducir por tipo de mensaje en vez de por nodo: cambiaría los eventos de
deepagents (su middleware añade `ToolMessage`).

**Reversible.** Aditivo: opciones nuevas del host, un campo con valor por
defecto y métodos nuevos; ningún nombre público cambia ni se retira.

## ADR-029 — agent-zero-config: despliega el agente que ya tienes con una orden

**Contexto.** ADR-028 y su addendum dejaron que un agente existente se
despliegue, pero hay que escribir el fichero de proyecto a mano (runtime,
punto de entrada, receta de instalación, opciones de servicio), hacer cuatro
preparativos de una vez (imagen base, bucket, secreto del modelo, token) y,
para pasar de una MicroVM a varias, escribir uno su propio enrutado de
sesiones. El objetivo de 0.14 es que un proyecto existente, en cualquiera de
los frameworks que los runtimes ya sirven, se despliegue con una llamada o una
orden y sin tocar su código, y que todo lo detectado se pueda sustituir con
una opción tipada.

**Decisión.**

1. **Detección pura.** `AgentProject.detect()` (y `deploy_agent`,
   `deployAgent`, `rayito agent deploy` por debajo) deciden idioma,
   framework, punto de entrada, rutas de una app HTTP, protocolo, plan de
   instalación, versiones de Python y Node y proveedor leyendo ficheros del
   proyecto: nunca importa, ejecuta ni evalúa código del usuario, nunca lee
   valores de `.env*`, y no llama a AWS. Es un servicio de dominio sobre un
   puerto `ProjectTree`; el adaptador es el sistema de ficheros. Las reglas
   (nombres de manifiestos, dependencias por framework, patrones, candidatos,
   prioridad) son datos en `agent-detection.json`, generados a los dos SDKs
   con `--check` como `gen_limits.py`, y los dos SDKs pasan los mismos
   vectores (`testdata/agent-detection/`). No se usa `ast` de Python, porque
   un usuario de TypeScript también despliega agentes de Python y al revés.
2. **Orden de precedencia.** Fichero de proyecto, después opción o
   argumento, después detección, después valor por defecto con nombre. Si hay
   dudas se falla con `ambiguous` o `not_detected` y el mensaje nombra la
   opción que lo resuelve: una conjetura nunca es silenciosa. Si hay un objeto
   del framework y una app HTTP, gana el driver del framework (trae
   `invoke()`, eventos y uso) y la app queda en `alternatives`.
3. **Cuatro objetos de valor, sin renombres.** `ServeOptions` suma cola
   acotada y su tope de sesiones por VM queda limitado por el tope de
   conexiones del tamaño; `ScalingOptions` (`min_vms`, `max_vms`, `warm_vms`,
   `sessions` compartidas o dedicadas, `scale_in_after_seconds`);
   `AgentProject` suma `env` (sin secretos), `network`, región y versiones; y
   `AgentDeployOptions` para el despliegue de una llamada. Todos los valores por
   defecto y límites son constantes de `limits.json`.
4. **Frameworks sin conocimiento en el dominio.** El dominio no importa
   ningún framework. El conocimiento vive en los datos de detección y en un
   driver del host por framework; un driver Node genérico con un adaptador
   por framework de JavaScript cubre LangGraph.js, deepagents-js, Vercel AI
   SDK y Strands TypeScript (`strands-ts` se mantiene como alias). El
   contrato mínimo para cualquier otro es una app HTTP, una función o una
   orden. Los paquetes de Bedrock de JavaScript (`@ai-sdk/amazon-bedrock`,
   `@langchain/aws`, `@strands-agents/sdk`) leen `AWS_BEARER_TOKEN_BEDROCK` y
   `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` (comprobado leyendo los paquetes
   publicados el 2026-10-16), así que llegan a la pasarela sin cambiar el
   agente.
5. **Protocolo `json`.** Para una app HTTP: el host sirve `/ping` y
   `/invocations`, traduce la invocación a un POST en la ruta propia de la
   app y reenvía el resto sin tocarlo, de modo que `invoke()` funciona y las
   rutas de la app siguen accesibles. `HttpApp` explícito conserva
   `agentcore` como valor por defecto; sólo la detección elige `json`.
6. **Flota enrutada en el cliente, sin servidor propio.** `AgentFleet` reparte
   sesiones entre MicroVMs. Con más de una VM el id de sesión es un
   identificador enrutable `rs1.<sandbox>.<id>` que apunta a su VM sin
   consulta. Los miembros son las MicroVMs no terminadas de la imagen del
   proyecto, porque una MicroVM no se puede etiquetar. Las VMs en espera son
   el `Pool` existente (con un `access_token` opcional), la reducción usa el
   `set_timeout` y la autoterminación de `rayd` que ya existen, y la cola
   (primero en el host, luego en el cliente con `429 Retry-After`) acaba en
   `CapacityException`. Un índice para ids elegidos por el llamante en modo
   dedicado es opcional y reutiliza la pila `metadata-index`.
7. **El host supervisa la app.** Reinicia con espera exponencial y
   presupuesto acotado; pasado el presupuesto, `/ping` falla y el marcador
   lleva `app_crashloop`, y la flota sustituye la VM.
8. **Valores por defecto de AWS sin crear nada a escondidas.** Región, imagen
   base, bucket, secreto del modelo y token se resuelven en orden fijo
   (argumento, fichero, variable de entorno, descubrimiento, error en
   español antes de crear nada). El bucket nunca se crea, la imagen base se
   publica sólo con confirmación y el secreto sólo con `--create-secret`. El
   agente sigue sin ver nunca la clave del modelo (ADR-023 y ADR-025).
9. **Coste.** `deploy_agent`, `deployAgent`, `rayito agent deploy` y
   `AgentFleet` llevan la caja "Coste y activación". `detect`, `load` y
   `--dry-run` no llaman a AWS ni cargan sus peers, con un test en cada SDK.
10. **Sin cambios en Rust.** El "agente" de `crates/` es `rayd`; la
    supervisión, el enrutado de sesiones, la salud y la inactividad las cubren
    el host, el cliente y funciones que `rayd` ya tiene. Arrancar el host
    dentro de la instantánea de la imagen se descarta para 0.14 y queda como
    candidato de 0.15. Se reabre si una prueba en AWS muestra que el host no
    sobrevive a suspender y reanudar, o que el tope de conexiones exige
    admisión en `rayd`.

**Consecuencias.** Todo es aditivo: `AgentProject`, `deploy()`, `sbx.agent.run`,
`AgentTemplate`, un `rayito-agent.toml` de antes y el nombre `strands-ts`
siguen igual. Una imagen construida antes necesita reconstruirse para tener el
host nuevo. La detección es heurística y por eso nunca adivina: prefiere un
error que nombra la opción. Cambiar `memory_mib` produce una versión nueva de
imagen (el tamaño se fija en la imagen). El tope de conexiones por debajo de
2 GB no está publicado; el valor del límite es conservador y se mide en AWS.

**Alternativas descartadas.** Importar el código del usuario para
inspeccionarlo: ejecutaría código ajeno en el portátil del desarrollador.
`ast` de Python para detectar: no sirve a TypeScript con el mismo resultado.
Un servidor de sesiones propio o una tabla obligatoria: rompe "sin servidor
propio" y añade coste; el identificador enrutable no necesita ninguno. Cambiar
el protocolo por defecto de `HttpApp` a `json`: rompería a quien usa
`agentcore` con `invoke()`. Un carril Rust para arrancar el host dentro de la
instantánea: exige cambiar el contrato de `rayd` para ganar menos de lo que ya
mide la reanudación.

**Reversible.** Aditivo: módulos, claves del fichero y constantes nuevos;
quitar `deploy_agent` y `AgentFleet` deja el 0.14 anterior intacto. Detalle en
`openspec/changes/agent-zero-config/design.md`.

## ADR-030 — rayito setup: preparar la cuenta en una orden

**Contexto.** Preparar una cuenta y una región para Rayito son seis pasos a
mano (`configurar-aws.md`): credenciales, un bucket, la pila `rayito-m0-iam` con
`infra/iam.yaml`, la descarga de `rayito-image.zip` y `SHA256SUMS` de la release,
`rayito image publish` y `rayito doctor`. Cada paso es una ocasión de equivocar
un parámetro (el bucket en otra región, una pila sin `BuildRoleArn`, una imagen
de otra versión de `rayd`), y quien llama a `Sandbox.create()` antes de acabar
recibe un `ResourceNotFoundException` sin pista de qué falta. 0.14 ya tiene
casi todas las piezas: el bucket de `cdk bootstrap` que reutiliza
`rayito agent deploy`, `rayito image publish` idempotente, `OptionalStacks` y
`rayito doctor`. Esta decisión las pone detrás de una orden idempotente, como
`cdk bootstrap` prepara un entorno para CDK. Lo que no existe y no se añade:
ningún camino de código del SDK que llame a `CreateBucket` (`AWS_API_NOTES.md`);
la enmienda del 2026-10-19 (al final de este ADR) hace que `setup` cree el
bucket cuando no hay ninguno, pero con CloudFormation, no con llamadas de S3.

**Decisión.**

1. **Un servicio privado `AccountSetup` con un plan puro, detrás de puertos.**
   `rayito setup` es una orden de la CLI sobre un servicio privado, como
   `rayito doctor`; no se exporta nada de `rayito/__init__.py` ni de
   `src/index.ts`, porque una API pública (síncrona y asíncrona, shim, docs) no
   la pidió nadie. El dominio (`classify_stack`, `bucket_candidates`,
   `decide_bucket`, `plan_bucket_creation`, `classify_image`, `plan_setup`, `parse_checksums`,
   `verify_checksum`) no importa `boto3`, `@aws-sdk/*`, `typer`, HTTP ni
   sistema de ficheros; el servicio sólo llama a puertos (pila de IAM, pila
   del bucket, bucket, imagen base, publicador, descarga de la release) y los adaptadores
   traducen los fallos a `AccountSetupException` / `AccountSetupError`, con un
   `code` cerrado. Python y TypeScript tienen las mismas capas y el mismo
   comportamiento.
2. **Tres recursos, cinco acciones, un plan.** El plan son siempre tres pasos,
   en este orden: `bucket` (`reuse`, `wait` y, desde la enmienda, `create`), `iamStack` (`create`, `update`,
   `reuse`, `wait`) y `baseImage` (`publish`, `reuse`, `wait`). Está `ready`
   cuando los tres son `reuse` y pide confirmación si algún paso es `create`,
   `update` o `publish`; un `wait` no cuesta nada nuevo. Primero lee (un
   `DescribeStacks`, un `HeadBucket`, `GetMicrovmImage` y
   `ListMicrovmImageVersions`) y cada escritura se decide de una observación
   de esa misma ejecución: repetir tras un éxito observa tres `reuse` y no
   escribe nada, y una cuenta preparada a mano se detecta como completa. La
   pila se clasifica por su estado: ausente, `create`; `CREATE_COMPLETE`,
   `UPDATE_COMPLETE` o `UPDATE_ROLLBACK_COMPLETE` con `BuildRoleArn`, `reuse`
   (o `update` si su `ArtifactBucket` difiere, enviando sólo ese parámetro y
   conservando el resto con `UsePreviousValue`); sin `BuildRoleArn`,
   `stack_incomplete`; `DELETE_IN_PROGRESS`, `ROLLBACK_COMPLETE` y `*_FAILED`,
   `stack_blocked`; otro `*_IN_PROGRESS`, `wait`. La imagen: ausente, `publish`;
   con una versión `PENDING` o `IN_PROGRESS`, `wait`; con una versión
   `SUCCESSFUL` y `ACTIVE`, `reuse`; sólo fallidas o inactivas, `publish`. Una
   base lanzable es `reuse` sea cual sea el `rayd` con que se construyó:
   compararla con la release exigiría descargar el zip en una detección de
   sólo lectura, y `rayito doctor --launch` y `rayito image publish` ya cubren
   esa diferencia.
3. **La pila de IAM pasa por `OptionalStacks`, no por el catálogo.**
   `scripts/gen_stack_assets.py` empaqueta `infra/iam.yaml` en los dos SDKs. Un
   `StackComponent` `iam` declara sus siete parámetros (`ArtifactBucket`
   obligatorio, el resto con el valor por defecto de la plantilla) y
   `CAPABILITY_NAMED_IAM`, un valor del enum `Capability` de CloudFormation
   verificado sin conexión en botocore 1.43.103 (`AWS_API_NOTES.md` §21). No
   entra en `_registry.COMPONENTS`: `rayito stack list` muestra funciones
   opcionales con coste, y esta pila es un prerrequisito con su nombre
   histórico `rayito-m0-iam`.
4. **Reutilizar un bucket y, sólo si no hay ninguno, crearlo** (redactado de
   nuevo en la enmienda del 2026-10-19; el texto original prohibía crear un
   bucket). Dos fases. *Reutilización*: los candidatos, en orden, son
   `--bucket` o `RAYITO_BUCKET` (si se da uno explícito es el único), el
   `ArtifactBucket` de la pila de IAM y el bucket de `cdk bootstrap`
   (`cdk-<calificador>-assets-<cuenta>-<región>`, calificador `hnb659fds`). Cada
   uno se comprueba con un `HeadBucket` con tres resultados: existe (en otra
   región, `bucket_region`), no existe, o 403 (`bucket_unavailable`, salvo para
   el de `cdk bootstrap`, que se salta porque Rayito no lo necesita). Una
   cuenta con bucket nunca sale de esta fase: no hay lecturas ni pilas nuevas.
   *Creación*: si ninguno sirve, el destino es el nombre explícito (si no
   existía) o `rayito-artifacts-<cuenta>-<región>`, y `setup` despliega la
   pila `rayito-m0-artifacts` con `infra/artifact-bucket.yaml` (un
   `AWS::S3::Bucket` y su política) pasando sólo `BucketName`. Antes mira esa
   pila: usable, la reutiliza; en curso, espera; bloqueada, `stack_blocked`;
   gestiona otro bucket que el pedido, `artifact_stack_conflict`. Un bucket
   que ya existe con el nombre por defecto y no es de esa pila nunca se adopta
   (`bucket_taken`): el nombre se deduce del número de cuenta, así que otra
   cuenta podría haberlo creado antes y recibir nuestros zips; si es tuyo, lo
   pasas con `--bucket`. Como el bucket elegido se escribe en la pila de IAM, la
   siguiente ejecución lo encuentra aunque no esté `RAYITO_BUCKET`.
5. **El artefacto de la release, comprobado contra `SHA256SUMS`.** Sin
   `--artifact`, descarga por HTTPS `SHA256SUMS` y `rayito-image.zip` de la
   release `rayd-v<versión del SDK>` a un directorio temporal que la CLI borra
   (Python con `urllib.request`, TypeScript con `fetch`; plazo de 600 s por
   asset) y compara el sha256 del zip con su línea (`checksum_missing`,
   `checksum_mismatch`: no se publica nada). Un estado HTTP distinto de 2xx,
   un plazo agotado o un fallo de red son `release_download`. Qué prueba y qué
   no: el hash detecta un zip truncado, corrupto o equivocado, **no
   autenticidad**: `SHA256SUMS` sale de la misma release que el zip. La firma
   la comprueba `cosign verify-blob` (`verify.md`); quien la quiera verifica el
   zip y lo pasa con `--artifact`. La documentación y `SECURITY.md` (T10) lo
   dicen sin rodeos.
6. **Orden de aplicación, idempotencia y reanudación.** Región admitida (antes
   de cualquier llamada), plan, impresión del plan y del coste, `--dry-run`
   termina ahí, confirmación si hace falta (`--yes`, o terminal; si no,
   `needs_confirmation`), y se aplica: la pila (`create`/`update` más espera, o
   `wait`), la salida `BuildRoleArn` (`stack_incomplete` si falta) y la imagen
   (esperar el build en curso, o publicar: descargar y verificar, subir por
   contenido, reutilizar una versión idéntica o crear/actualizar la imagen y
   esperar a que sea lanzable). La clave de S3 es por contenido
   (`rayito/images/rayd-<sha12>.zip`), así que una subida cortada se repite y
   una terminada se salta. Una ejecución cortada deja la pila o una versión en
   curso, y la siguiente planifica `wait` y nunca lanza un segundo build. Un
   `wait` que acaba en fallo termina con `image_build_failed` y no escala a un
   `publish` dentro de la misma ejecución. La versión de la base gestionada es
   siempre `latest`. Al final ejecuta `rayito doctor` (sin `--launch`, que
   cuesta un sandbox que nadie pidió) y publica `RAYITO_TEMPLATE` y
   `RAYITO_BUCKET`.
7. **Códigos de salida y errores.** `0`: éxito, `--dry-run`, nada que hacer con
   el doctor limpio. `2`: uso o entorno (`unsupported_region`,
   `bucket_unavailable`, `bucket_region`, `bucket_taken`,
   `artifact_stack_conflict`, `artifact_missing`,
   `artifact_variant`, `needs_confirmation`, y los de siempre sin región,
   perfil o credenciales). `1`: `stack_blocked`, `stack_incomplete`,
   `release_download`, `checksum_missing`, `checksum_mismatch`,
   `image_build_failed`, un «no» en la confirmación, un `FAIL` del doctor y un
   error de AWS sin traducir. `AccountSetupException` no hereda de
   `SandboxException` (nada aquí es atribuible a un sandbox), es privada y su
   mensaje nunca lleva una URL, un enlace prefirmado, un token ni el mensaje
   crudo de AWS. Con `--json` sale igualmente un único documento con
   `error: {code, message}` y `exitCode`, más la línea `rayito: <mensaje>` por
   stderr.
8. **Los errores de una cuenta sin preparar apuntan a `rayito setup`.** Una
   frase (`ACCOUNT_SETUP_HINT`) se añade como `"<mensaje> (<pista>)"` al
   `ResourceNotFoundException` de `RunMicrovm` (misma clase, mismo
   `status_code`, `aws_code` y causa saneada: cubre `Sandbox.create()`, el pool
   y los shims de E2B) y a los errores «no hay bucket» y «la imagen base no
   existe» de `deploy_agent`/`deployAgent`. No se añade ninguna llamada para
   averiguar si la cuenta está preparada.
9. **El coste sólo cita cifras ya publicadas.** Imagen: «≈ $0,04 por semana por
   versión publicada (mínimo una semana)» (`cost.md`); pila de IAM: «gratis» y
   zip en el bucket: «céntimos» (`configurar-aws.md`); build: «≈ 10 min». No es
   una función opcional (no crea nada que un sandbox necesite en ejecución más
   allá de lo que el tutorial ya hacía crear), así que no tiene fila en
   `optional-features.md`; su referencia en la CLI y el tutorial llevan la caja
   «Coste y activación». El contrato compartido de las dos CLI (opciones,
   ayuda, salida humana, documento JSON, constantes, errores) vive en
   `testdata/account-setup/` y los dos SDK lo comparan en sus tests.

**Consecuencias.** Una orden prepara una cuenta, tenga o no bucket (ver la
enmienda). `rayito setup`
nunca cambia una cuenta preparada a mano que funciona ni sube `rayito-base` a un
`rayd` más nuevo: las dos cosas siguen siendo explícitas (`rayito image
publish`). Quien prefiere artefactos verificados con cosign conserva ese camino
con `--artifact`. El SDK distribuye ahora `infra/iam.yaml`; un cambio en
`infra/` llega a los usuarios por `gen_stack_assets.py --check`, que ya es una
puerta. Riesgos: una SCP de la organización puede denegar `iam:CreateRole` con
nombres fijos o `cloudformation:CreateStack` con `CAPABILITY_NAMED_IAM` (la
aceptación lo comprueba antes, sólo lectura, con la simulación IAM de `rayito
doctor`); detrás de un proxy corporativo `urllib` respeta `HTTPS_PROXY` pero el
`fetch` de Node no, y `--artifact` es el camino documentado; durante la
aceptación de un PR de release la release `rayd-v<versión nueva>` aún no existe
y la prueba usa `--artifact`; los roles se llaman `rayito-m0-*-<región>`, así
que dos regiones no chocan, pero una pila a medio borrar puede dejar un nombre
ocupado, y `stack_blocked` manda borrar la pila primero.

**Alternativas descartadas.** Crear el bucket si falta: descartada en la
redacción original y aprobada después por el mantenedor (enmienda). Hacer de la pila de IAM un componente del catálogo
(`rayito stack deploy iam`): el catálogo es para funciones opcionales con coste,
su nombre por defecto sería `rayito-iam` y no `rayito-m0-iam`, y habría dos
formas de desplegar la misma pila. Un `UpdateStack` siempre, para llevar las
pilas antiguas a la plantilla empaquetada: convierte una ejecución de sólo
lectura sobre una cuenta preparada en una escritura que pide permisos de IAM
que quien la preparó a mano quizá no tiene, y cambiaría políticas sin pedirlo.
Verificar el bundle de cosign dentro del SDK (sigstore-python, sigstore-js): dos
dependencias pesadas y una ruta de actualización de la raíz de confianza para
una orden; `--artifact` deja abierto el camino de cosign. Embeber el sha256 del
zip en el SDK: las ruedas y el paquete npm se construyen en jobs que no dependen
de `rayd-build`, así que el hash no se conoce al construirlos; exigiría cambiar
el pipeline de release. Ejecutar `rayito doctor --launch` al final: cuesta un
sandbox (≈ $0,002) que el usuario no pidió; el mensaje final lo sugiere. Una API
pública `AccountSetup`: especulativa.

**Reversible.** Aditivo: una orden, un paquete privado en cada SDK, dos
plantillas empaquetadas, una frase añadida a tres mensajes y el contrato de
pruebas; quitar `rayito setup` deja el 0.14 anterior intacto y el camino a mano
sigue documentado. Detalle en `openspec/changes/account-setup/design.md`.

**Enmienda 2026-10-19: `rayito setup` crea el bucket de artefactos cuando no
hay ninguno.** El mantenedor decidió que una sola orden debe dejar la cuenta
lista, como `cdk bootstrap`; esto sustituye al punto 4 original (que
prohibía crear un bucket) y a la alternativa descartada «crear el bucket si falta».

- *Cuándo.* Sólo si ni `--bucket`, ni `RAYITO_BUCKET`, ni el bucket de la pila
  de IAM, ni el de `cdk bootstrap` sirven (fases de reutilización y de creación
  del punto 4). `rayito agent deploy` no cambia: reutiliza, no crea.
- *Cómo.* Con la pila `rayito-m0-artifacts` y `infra/artifact-bucket.yaml`
  (nombre por defecto `rayito-artifacts-<cuenta>-<región>`), no con llamadas de
  S3: el SDK sigue sin llamar a `CreateBucket`; lo hace CloudFormation con las
  credenciales de quien ejecuta `setup`. Un `AWS::S3::Bucket` con SSE-S3
  (`AES256`), bloqueo de todo acceso público, ACL desactivadas
  (`BucketOwnerEnforced`) y los objetos de `rayito/images/` caducando a los 7
  días (un zip sólo se lee mientras se construye la versión de la imagen, y
  `rayito image publish` lo vuelve a subir si falta); un `AWS::S3::BucketPolicy`
  que niega todo acceso sin TLS. `DeletionPolicy` y `UpdateReplacePolicy` son
  `Retain`: borrar la pila conserva el bucket. Una plantilla propia y no un
  componente del catálogo (`rayito stack list`), igual que la de IAM.
- *Alternativas descartadas.* Llamadas S3 directas (`CreateBucket`,
  `PutBucketEncryption`, `PutPublicAccessBlock`, `PutBucketOwnershipControls`,
  `PutBucketPolicy`, `PutBucketLifecycleConfiguration`): seis APIs de escritura
  nuevas en los dos SDK y un fallo a medias deja un bucket a medio configurar
  sin registro de lo aplicado; la pila es ese registro. El bucket dentro de la
  pila de IAM: mezcla un recurso de datos que se conserva con roles que se
  borran, y haría derivar toda `rayito-m0-iam` desplegada a mano. Adoptar el
  bucket con el nombre por defecto sin pila: ver `bucket_taken` arriba. SSE-KMS:
  una clave cuesta y obliga a dar `kms:*` al rol de build. Versionado: las
  claves son por contenido y las versiones no vigentes seguirían costando.
  `DeletionPolicy: Delete`: CloudFormation no borra un bucket con objetos.
- *Consecuencias.* `setup` necesita permisos de administrador o de bootstrap una
  vez, como `cdk bootstrap`: CloudFormation, IAM y, cuando crea el bucket, S3;
  la política `CallerPolicy` del tutorial no alcanza. Un bucket creado por
  `setup` sigue ahí al borrar la pila: se vacía y se borra a mano. Errores
  nuevos: `bucket_taken` y `artifact_stack_conflict` (código de salida 2);
  `no_bucket` desaparece y `bucket_unavailable` queda sólo para el 403.
- *Riesgo.* Una SCP de la organización puede denegar `s3:CreateBucket` o
  `s3:PutBucketPolicy`; la aceptación lo simula antes de escribir nada.
- *Etiquetas de pila.* `rayito setup --tag K=V` (repetible) pasa las etiquetas
  como `Tags` de `CreateStack`/`UpdateStack` de `rayito-m0-artifacts` y
  `rayito-m0-iam`; CloudFormation las propaga a los recursos. Una pila
  reutilizada no se reetiqueta y la imagen no las recibe. Existe porque una SCP
  que exige etiquetas deniega `s3:CreateBucket` sin ellas (D11).
- Detalle (D2, D4, D10, D11) en `openspec/changes/account-setup/design.md`.

## ADR-031 — agentcore-deploy: despliega en Amazon Bedrock AgentCore Runtime

**Contexto.** 0.14 despliega un proyecto de agente existente en una flota de
Lambda MicroVMs con una llamada (`deploy_agent`, `deployAgent`, `rayito agent
deploy`, ADR-029) y ya sabe exportar el mismo proyecto como un contexto de
contenedor listo para AgentCore (`export(target="agentcore")`: Dockerfile
`linux/arm64`, puerto 8080, `/ping` y `/invocations`, cabecera de sesión de
AgentCore). Pasar de ese contexto a un Amazon Bedrock AgentCore Runtime en
marcha sigue siendo manual: construir una imagen arm64 (Docker con buildx, o
CodeBuild a mano), crear un repositorio de ECR, escribir un rol de ejecución,
crear el runtime, acordarse de activar MMDSv2 (obligatorio desde el
2026-06-30: sin él toda invocación falla) y escribir código de invocación con
SigV4 e ids de sesión de al menos 33 caracteres. Los equipos que estandarizan
en AgentCore quieren la experiencia sin configuración de 0.14 con AgentCore
como destino, desde el mismo proyecto, con la misma API de `invoke` y sin
Docker local. Esta decisión añade ese destino sin tocar el de MicroVMs, que
sigue siendo el predeterminado.

**Decisión.**

1. **Un destino nuevo, opcional, en los puntos de entrada que ya existen
   (D1).** `deploy_agent`, `deploy_agent_async` y `plan_deploy` (Python) y
   `deployAgent` y `planDeploy` (TypeScript) aceptan `target` (`microvm` por
   defecto, o `agentcore`) y `agentcore` con `AgentCoreOptions`; la CLI,
   `--target microvm|agentcore` en `rayito agent deploy`, `invoke`, `status` y
   `stop`, más las órdenes nuevas `rayito agent list` y `rayito agent delete`
   (sólo `--target agentcore`). Con `microvm` corre el código de 0.14 sin
   cambios, con los mismos tipos de retorno, salida y llamadas a AWS; sin
   `target="agentcore"` no se crea ni se carga ningún cliente de AgentCore,
   CodeBuild ni ECR, y un test en cada SDK lo comprueba. Detrás hay un
   servicio de aplicación privado sobre puertos con un dominio puro (nombres,
   validación, tabla de opciones, ids de sesión, etiqueta de imagen, ARNs de
   Bedrock y de secretos, plan y decisiones de aplicación) y adaptadores que
   traducen cada error de AWS por su código a `AgentCoreException` /
   `AgentCoreError` (un `code` cerrado y un `exit_code` / `exitCode`),
   `RateLimitException`, `QuotaExceededException` o `AgentException`.
   `AgentCoreException` es pública y no hereda de `SandboxException`: nada de
   esto es atribuible a un sandbox (mismo razonamiento que
   `AccountSetupException`, ADR-030).
2. **Construcción: una imagen de contenedor que construye CodeBuild, no el
   despliegue directo de código (D2).** El despliegue directo (`codeConfiguration`)
   corre un único runtime de lenguaje gestionado por agente, y el host de
   cero configuración necesita más: cada framework de TypeScript corre como el
   host de Python más un puente de Node, y OpenCode y Hermes instalan
   binarios e intérpretes fijados. Además habría que armar un zip para arm64 en
   la máquina del desarrollador (las ruedas nativas de macOS o x86 serían
   erróneas) y se saltaría la exportación que 0.14 ya prueba byte a byte en los
   dos SDK. La imagen reutiliza esa exportación sin cambios: la imagen que
   escribe `rayito agent export --target agentcore` y la que corre `deploy` son
   la misma. CodeBuild (`ARM_CONTAINER`, `BUILD_GENERAL1_SMALL`) construye
   nativamente en Graviton en las 21 regiones donde AgentCore tiene microVMs,
   sin QEMU ni Docker local; la guía de AgentCore recomienda CodeBuild para
   builds arm64 y un build cuesta unos $0,02-0,03 pasado el nivel gratuito. La
   imagen base es la fijada por digest de la exportación; CodeBuild la baja del
   espejo de ECR Public de las imágenes oficiales de Docker, con el mismo
   digest, mediante el argumento de build `AGENT_BASE_IMAGE`, porque Docker Hub
   limita las descargas anónimas desde las direcciones compartidas de
   CodeBuild.
3. **La infraestructura de cada agente es una pila de CloudFormation; el
   runtime, la API (D3).** `infra/agentcore-runtime.yaml` se despliega como
   `rayito-agentcore-<nombre>` por `OptionalStacks.deploy` con un
   `StackComponent` que **no** está en el catálogo de `rayito stack` (como las
   pilas de IAM y del bucket de `rayito setup`, ADR-030): repositorio de ECR
   (etiquetas inmutables, análisis al subir, conserva las últimas 5 imágenes,
   se vacía al borrar la pila), proyecto de CodeBuild, su grupo de logs (14
   días), el rol de build y el rol del runtime, con `CAPABILITY_IAM` y las tres
   etiquetas fijas más las de `--tag`. El runtime se crea con
   `CreateAgentRuntime` / `UpdateAgentRuntime` y no como recurso de la pila:
   la imagen debe existir antes que el runtime y el repositorio antes que la
   imagen (una pila con el runtime pediría dos actualizaciones por despliegue),
   y la API permite esperar a `READY`, informar de la versión, exigir MMDSv2 y
   actualizar de forma idempotente por nombre con un `clientToken`.
4. **Nombres y propiedad (D4).** El nombre del agente es el del template
   (`[image].template`) o del proyecto, validado con
   `^[a-z0-9]+(?:[_-][a-z0-9]+)*$` y de hasta 41 caracteres, para que quepan
   todos los derivados: runtime `rayito_<nombre>` (con `_` en vez de `-`),
   pila `rayito-agentcore-<nombre>`, repositorio `rayito/agentcore/<nombre>`,
   proyecto de CodeBuild `rayito-agentcore-<nombre>` y clave de origen
   `rayito/images/agentcore/<nombre>/<etiqueta>.zip`, dentro del prefijo
   `rayito/images/` que el bucket de `rayito setup` caduca a los 7 días. Un
   runtime es de Rayito sólo si tiene ese nombre **y** la marca
   `rayito.agentcore/1` en la descripción: con el nombre y sin la marca es
   `name_conflict` y nunca se toca.
5. **El plan no llama a AWS; las decisiones se toman al aplicar, de
   observaciones frescas (D5).** `plan_deploy`, `planDeploy` y `--dry-run` no
   hacen ninguna llamada ni cargan ningún cliente: validan lo que se puede
   validar sin red, resuelven el bucket sólo de fuentes explícitas (argumento,
   `[deploy].bucket`, `RAYITO_BUCKET`, o descubrimiento al aplicar) y muestran
   tres pasos `planned` (pila, imagen, runtime), la sección de seguridad, los
   avisos y el coste. Al aplicar, tras confirmar, se observa y se decide en
   orden: identidad y bucket (un bucket de otra región es `bucket_region`:
   CodeBuild sólo lee código de su región); pila (ausente `create`, usable
   `update` o `reuse` si CloudFormation no tiene cambios, en curso `wait`,
   bloqueada `stack_blocked`, sin las seis salidas `stack_failed`, con la
   clasificación `classify_stack` que `rayito setup` ya usa y que pasa a un
   módulo compartido); imagen (la etiqueta existe, `reuse`; si no, `build`:
   subir el zip, `StartBuild`, sondear, borrar el zip; más de 2 GB,
   `image_too_large`); runtime (ninguno, `create` + espera + `UpdateAgentRuntime`
   con MMDSv2 + espera; ajeno, `name_conflict`; en curso, `wait`; `CREATE_FAILED`,
   `DELETING` o `DELETE_FAILED`, `runtime_blocked`; `UPDATE_FAILED`, `update`;
   `READY`, `reuse` si los ocho campos comparados coinciden, si no `update`).
6. **Idempotencia (D6).** La etiqueta de la imagen es el contenido: `src-` más
   los 16 primeros caracteres hexadecimales del sha256 del árbol de la
   exportación, con un encuadre fijo, así que el mismo proyecto da la misma
   etiqueta en los dos SDK. El `clientToken` es `rayito-` más 32 caracteres del
   sha256 de la petición canónica y `StartBuild.idempotencyToken` es la
   etiqueta: un reintento nunca crea un duplicado ni un segundo build. Redesplegar
   sin cambios hace un `UpdateStack` sin efecto, ningún build y ninguna
   escritura del runtime, y comunica `reuse` tres veces; un proyecto cambiado
   actualiza el mismo runtime. Un despliegue interrumpido se reanuda.
7. **Invocación y sesiones: la misma API que `AgentFleet` (D7).**
   `AgentCoreFleet.invoke(prompt, session_id=None, model=None, reasoning=False)`
   y `stream(...)` devuelven el mismo `AgentResult` y los mismos eventos que
   `AgentFleet`; por debajo, `InvokeAgentRuntime` con SigV4, `accept`
   `application/json` o `text/event-stream` y plazos de lectura de 900 s y 3600 s
   (cuotas de AgentCore). El id de sesión va sólo en la cabecera
   (`runtimeSessionId`): sin id, `rayito-` y 32 caracteres de un uuid4; un id del
   llamante de 33 a 100 caracteres válidos se usa tal cual; cualquier otro no
   vacío se convierte en `rayito-` más 32 caracteres del sha256 de su UTF-8, de
   modo que el mismo id del llamante llega siempre a la misma sesión. El
   resultado devuelve el id realmente usado y pasarlo continúa la conversación,
   como el manejador enrutable de una flota de MicroVMs.
8. **MMDSv2 siempre activo (D3, D9).** `CreateAgentRuntime` no admite
   `metadataConfiguration`, así que tras crear y llegar a `READY` se manda un
   `UpdateAgentRuntime` con la configuración completa y `requireMMDSV2=true`, y
   todo `UpdateAgentRuntime` lo incluye. Es obligatorio desde el 2026-06-30.
9. **La imagen: la exportación de 0.14 más dos adiciones sólo para AgentCore
   (D9).** `render_export(project, target="agentcore")` añade `ARG
   AGENT_BASE_IMAGE=<referencia fijada>` antes de `FROM` y un `FROM
   --platform=linux/arm64 ${AGENT_BASE_IMAGE}`; para un framework de la lista
   de adaptadores por entorno (hoy `vercel-ai`, cuyo cliente de Bedrock no trae
   proveedor de credenciales de AWS) un venv de botocore fijado por hash y un
   ayudante que el host ejecuta antes de cada invocación, de modo que el puente
   de Node recibe las tres variables de credenciales del rol del runtime sólo
   mientras dura la invocación; y una sección del README sobre AgentCore, MMDSv2
   y la diferencia de seguridad. El destino `docker` no cambia ni un byte. Las
   variables de entorno del runtime son las del proyecto más `AWS_REGION` y
   `AWS_DEFAULT_REGION` con la región del despliegue; nunca secretos.
10. **Las opciones de flota que no aplican no se aproximan en silencio (D10).**
    La tabla `optionsTable` del contrato es normativa. Una opción de MicroVMs
    pasada **en la llamada** que no tiene sentido en AgentCore es
    `option_not_supported` antes de cualquier llamada a AWS (`min_vms`,
    `max_vms`, `warm_vms`, `scale_in_after_seconds`, `--memory`, `--port`,
    `--timeout`, `--idle`, `--secret`, `--create-secret`, `--token-file`,
    `--keep-on-failure`, las de la imagen base, `session_index`,
    `pool_backend`, `warm_timeout_seconds`). El mismo valor que viene del
    **fichero de proyecto** (compartido entre destinos) es un aviso en el plan
    y se ignora: `[scaling]`, `memory_mib`, `[serve].port|timeout|idle`,
    `[deploy].base_image_version`. Aplican `max_concurrency`, `queue_max`,
    `queue_timeout_seconds`, `bucket`, `build_image`, `build_timeout_seconds`,
    `on_step`, `on_build_logs` y `env`; `sessions` aplica como `dedicated`. Una
    política de salida o de red del proyecto es `network_policy_unsupported`:
    el modo PUBLIC no filtra la salida y aplicar media política sería un
    riesgo silencioso. Un proveedor de modelo que no sea `bedrock` es
    `provider_unsupported`: sin pasarela, su clave quedaría en claro en la
    configuración del runtime.
11. **La diferencia de seguridad se dice en todas partes (D11).** En MicroVMs
    el agente nunca ve la credencial del modelo (ADR-023, ADR-025). En
    AgentCore no hay pasarela de secretos de Rayito: el agente obtiene las
    credenciales del rol del runtime por el servicio de metadatos de la microVM,
    y cualquier código que corra en la sesión puede leerlas. La compensación es
    el mínimo privilegio: el rol sólo puede invocar en Bedrock los ARNs de los
    modelos resueltos (un id de perfil de inferencia da el ARN del perfil en la
    región del modelo y el modelo base en cualquier región; un perfil `global.`
    suma además el ARN del modelo base sin región) y sólo lee los secretos de
    `--readable-secret`; nada más allá de lo básico documentado (extraer su
    imagen de ECR, escribir en su grupo de logs, X-Ray, métricas en el espacio
    `bedrock-agentcore` y tokens de identidad de carga de trabajo sin
    `GetWorkloadAccessTokenForUserId`). El plan, el README exportado, la guía,
    `security.md` y `SECURITY.md` lo dicen con las mismas palabras.
12. **Sin CDK por ahora (D12; sustituida por ADR-032).** Un constructo de
    CDK de Rayito sería un tercer paquete (jsii, su propio pipeline de
    release y de versiones, publicado en npm y PyPI) que envolvería el
    `aws_bedrockagentcore.Runtime` de `aws-cdk-lib`, que ya existe y admite
    una imagen de ECR o un asset local. Es grande e independiente de la CLI
    y de los SDK, y va a un cambio posterior; nada de éste lo bloquea: la
    plantilla, el contexto exportado y las reglas de sesión son
    reutilizables.
13. **Coste y activación (D13).** Sólo `target="agentcore"` (o `--target
    agentcore`) crea algo. El plan muestra las líneas de coste del contrato
    (precios de lista de la plataforma V1, CodeBuild por minuto con el nivel
    gratuito, almacenamiento de ECR, ingesta de logs; el modelo lo cobra su
    proveedor). Las docstrings y el TSDoc de `deploy_agent` / `deployAgent`,
    `AgentCoreFleet` y `AgentCoreOptions` llevan el bloque «Coste y
    activación»; la guía, la caja y una fila de `optional-features.md`, igual.
14. **Orden de aplicación, borrado y errores.** Validar (sin AWS), plan,
    imprimir plan, seguridad, avisos y coste; `--dry-run` termina ahí;
    confirmación (`--yes` o terminal; si no, `needs_confirmation`); identidad
    y bucket; pila; imagen; runtime. `delete` lista el runtime (comprueba que
    sea de Rayito), lo borra y espera, borra la pila (el repositorio se vacía
    solo) y borra el grupo de logs `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`;
    sin nada que borrar, «nada que borrar» y salida 0; los zips de origen no
    sobreviven a su build. Códigos de salida: `2` para uso y entorno, `1` para
    fallos del lado de AWS; la lista cerrada de `code` está en
    `testdata/agentcore-deploy/contract.json`. Con `--json` sale un único
    documento con `error: {code, message}` y `exitCode`, más la línea
    `rayito: <mensaje>` por stderr, como `rayito setup`. Los mensajes van en
    español y nunca llevan un ARN, una URL, un token ni el texto crudo de AWS.

**Consecuencias.** Una orden lleva un proyecto de 0.14 a AgentCore y el mismo
proyecto sigue desplegándose en MicroVMs sin cambios; el contexto exportado y la
imagen desplegada son el mismo artefacto. Rayito llama ahora a cuatro servicios
de AWS más (plano de control y de datos de AgentCore, CodeBuild y ECR) y a
`DeleteLogGroup` de CloudWatch Logs, todos detrás de puertos y documentados en
`AWS_API_NOTES.md`. El Dockerfile de la exportación `agentcore` cambia (argumento
de imagen base, README); el de `docker` no. Quien usa AgentCore pierde la
garantía de la pasarela y los documentos lo dicen; el rol es de mínimo
privilegio. `rayito agent delete` y `rayito agent list` sólo existen para
AgentCore: una flota de MicroVMs se para con `rayito agent stop`. Riesgos: ECR
informa del tamaño comprimido de la imagen y, si AgentCore aplica sus 2 GB a la
imagen descomprimida, un agente grande fallaría al crear el runtime y no en la
comprobación previa (la aceptación lo mide); no está comprobado sin red que
OpenCode lea las credenciales del rol (si no puede, `opencode` entra en la lista
de adaptadores por entorno, un cambio de datos); una cuenta nueva puede tener
una cuota de un solo build arm simultáneo en CodeBuild (`build_quota` lo dice);
una SCP que exija etiquetas al actualizar un runtime existente no está cubierta
(las etiquetas se mandan al crear y no se actualizan después, no hay
`TagResource`); el espejo de ECR Public podría ir por detrás de un digest y el
digest fijado hace que el build falle de forma visible en vez de derivar.

**Alternativas descartadas.** Despliegue directo de código: un runtime de
lenguaje por agente, dependencias arm64 armadas en la máquina del desarrollador
y una segunda vía de empaquetado que se aparta de la exportación probada. Docker
local o buildx: lo excluye el requisito, además de ser lento y frágil bajo QEMU
en x86. El starter toolkit o la CLI de AgentCore como dependencia: una tercera
herramienta con sus ficheros de configuración y políticas de IAM «pensadas para
desarrollo», con las que Rayito perdería el contrato compartido y el rol de
mínimo privilegio. El runtime como recurso de CloudFormation: ver el punto 3. Una
clave de origen fija `source.zip`: dos despliegues concurrentes de versiones
distintas se pisarían; las claves por contenido borradas tras el build no
cuestan nada. Aproximar las opciones de MicroVMs (`max_vms` a la cuota de
sesiones, `--idle` al tiempo de inactividad): descartado, son semánticas
distintas sin avisar; los equivalentes de AgentCore son opciones explícitas. Pasar
la clave de un modelo que no es de Bedrock como variable de entorno del
runtime: la leería `GetAgentRuntime` y el propio agente; AgentCore Identity
podría guardarla, pero es otra función. Exigir MMDSv2 sólo cuando falle una
invocación: desde el 2026-06-30 un runtime sin él no se puede invocar.

**Reversible.** Aditivo: un destino opcional en los puntos de entrada, un
paquete privado en cada SDK, una plantilla empaquetada, dos argumentos y una
variable en la exportación `agentcore` y el contrato de pruebas; quitar el
destino deja 0.14 intacto y la exportación manual sigue documentada. Detalle
(D1 a D13), fuentes oficiales con fecha de consulta y el contrato en
`openspec/changes/agentcore-deploy/design.md`,
`testdata/agentcore-deploy/contract.json` y `AWS_API_NOTES.md` («Despliegue en
AgentCore Runtime»).

## ADR-032 — agentcore-cdk: constructos CDK `RayitoAgent` y `RayitoMicrovmImage`

**Contexto.** 0.15 despliega un agente en AgentCore con una orden (ADR-031) y
prepara la cuenta con `rayito setup` (ADR-030), pero ni el runtime ni su
imagen ni la imagen de MicroVM viven en CloudFormation: no salen en un
`cdk diff`, `cdk destroy` no los borra y no se pueden referenciar desde el
resto de la aplicación. ADR-031 D12 dejó el constructo para después como un
tercer paquete jsii. Desde entonces AWS ha pasado la L2 de AgentCore a
`aws-cdk-lib` estable (2.255.0) y ha publicado `CfnMicrovmImage` (2.261.0).
Verificado de solo lectura el 2026-10-20 (`AWS_API_NOTES.md` Q181-Q184): el
recurso `AWS::BedrockAgentCore::Runtime` no tiene ninguna propiedad de
metadatos ni de MMDS; `AWS::Lambda::MicrovmImage` exige toda la configuración
de la imagen (salvo `Tags`), su `Name` sólo se fija al crear y su manejador
de actualización puede borrar versiones; no existe recurso para un MicroVM
en marcha.

**Decisión.** Sustituye a ADR-031 D12.

1. **Dos constructos nativos dentro de los paquetes de siempre (D1).**
   `rayito/cdk` (subruta de npm, `aws-cdk-lib ^2.261.0` y `constructs
   ^10.5.0` como peers opcionales) y `rayito.cdk` (PyPI, extra `cdk`), con
   la misma versión, etiquetas y trabajos de publicación que los SDK. La
   paridad la dan el contrato `testdata/agentcore-cdk/` (plantillas
   normalizadas que los dos SDK reproducen) y `check_sdk_parity.py`. Sin
   jsii: fijaría TypeScript 6, añadiría un tercer flujo de release y haría
   que Python ejecutara el SDK de TypeScript bajo Node. Nunca el paquete
   alpha; recursos L1 (`CfnRuntime`, `CfnMicrovmImage`) para controlar cada
   propiedad.
2. **`RayitoAgent` despliega el mismo agente que la CLI (D3).** Las props son
   las de `AgentCoreOptions`; el plan es el de los SDK (`plan_agentcore`,
   `planAgentCoreSync` sobre un puerto `ProjectReader` síncrono). Una pila
   anidada incluye `infra/agentcore-runtime.yaml` con los mismos parámetros
   (IAM idéntico por construcción), el `source_zip` va como asset, un
   recurso personalizado construye la imagen con el proyecto de CodeBuild de
   la plantilla (se salta si la etiqueta ya existe) y `CfnRuntime` crea el
   runtime. **Por defecto CodeBuild (D4)**, sin Docker local;
   `AgentImageBuild.localDocker()` es opcional.
3. **MMDSv2 con un recurso personalizado (D5).** Como CloudFormation no
   puede fijarlo, `Custom::RayitoRuntimeMmds` manda `UpdateAgentRuntime` con
   la configuración leída y `requireMMDSV2=true` cuando falta, y vuelve a
   correr con cada versión nueva del runtime. El manejador es un solo
   fichero, `infra/agentcore-cdk-handler.py`, empaquetado por
   `gen_stack_assets.py` con el modelo de botocore de
   `bedrock-agentcore-control` (el boto3 del runtime de Lambda puede no
   conocer `metadataConfiguration`), así que el asset tiene el mismo hash en
   los dos SDK.
4. **Propiedad `+cdk` (D6).** La descripción de un runtime de CDK lleva
   `rayito.agentcore/1+cdk` y cada recurso la etiqueta
   `rayito:managed-by=rayito-cdk`. Los SDK y las CLI lo listan, lo muestran y
   lo invocan como siempre, pero `rayito agent deploy` y `rayito agent
   delete` lo rechazan con `name_conflict` y remiten a `cdk deploy` y `cdk
   destroy`; `rayito image publish` rechaza una imagen con esa etiqueta
   (`GetMicrovmImage` devuelve `tags`, no la descripción). Ningún recurso de
   hoy lleva la marca: nada de lo que funciona cambia.
5. **`RayitoMicrovmImage` (D9).** Publica una imagen de Rayito desde un
   `rayito-image.zip` comprobado (un fichero con sha256 opcional, o la
   descarga de la release verificada con `SHA256SUMS` antes de sintetizar y
   reutilizada sin red después), con la configuración exacta de `rayito
   image publish` (`desired_configuration` pasado a nombres de
   CloudFormation), sus tamaños, capacidades del SO y lista de buckets de
   montaje. Crea su propio rol de build, sólo con lectura del objeto del
   asset, y un grupo de logs por imagen. `baseImageVersion` es obligatoria y
   el nombre `rayito-base*` queda reservado para `rayito setup` y `rayito
   image publish`. Los MicroVMs los sigue lanzando el SDK
   (`Sandbox.create(template=…)`). Las imágenes de agente con runtimes
   (`AgentTemplate`) quedan fuera: leen AWS al construir y una flota sólo
   existe cuando el SDK arranca MicroVMs (D10).
6. **La síntesis no llama a AWS (D7).** Nada de `fromLookup` ni clientes; la
   región del stack debe ser concreta (`region_unresolved` si no) y la cuenta
   puede ser un token.
7. **Cero impacto sin el extra (D8).** `import rayito`, las CLI, MCP y E2B
   nunca cargan `aws_cdk`, `jsii`, `constructs` ni `aws-cdk-lib`; lo prueban
   un test de aislamiento, `check_wheel.py` y `pack-check.mjs`.
8. **Coste y activación (D14).** Sintetizar es gratis. `cdk deploy` crea
   recursos que cuestan: minutos de CodeBuild, ECR, builds y snapshots de la
   imagen de MicroVM (uno por tamaño), logs a 14 días, sondeo de Lambda y
   Step Functions y el uso de AgentCore. Trazas, VPC y entrega de logs siguen
   apagadas. Los assets del bootstrap de CDK duran hasta `cdk gc`. Cada
   docstring y TSDoc lleva el bloque «Coste y activación» y
   `optional-features.md` una fila.

**Consecuencias.** `cdk deploy` y `rayito agent deploy` producen el mismo
agente desde el mismo proyecto, y una imagen de Rayito puede vivir en una
pila de CDK junto al resto de una aplicación. Hay dos dependencias opcionales
más y una pila anidada que depende de `infra/agentcore-runtime.yaml` (un test
de igualdad de IAM y las plantillas del contrato lo vigilan). Riesgos: si una
actualización de CloudFormation devuelve `requireMMDSV2` a falso (Q185) el
recurso personalizado lo repara en el mismo despliegue, pero la detección de
deriva puede señalarlo; si CloudFormation conserva todas las versiones de la
imagen (Q186) el almacenamiento de snapshots crece y `rayito image prune` es
la respuesta documentada; un build atascado bloquea la pila hasta el tiempo
total del proveedor (40 min por defecto); `aws-cdk-lib` pesa unos 139 MB y la
síntesis en Python necesita Node; un bootstrap de CDK con permisos
restringidos necesita `CreateMicrovmImage` sobre `*`,
`lambda:PassNetworkConnector` y `iam:PassRole` (Q114).

**Alternativas descartadas.** Un paquete jsii (punto 1). La L2 `Runtime` con
`fromAsset`: Docker local con arm64 y un rol distinto del de la CLI.
`@cdklabs/deploy-time-build`: una dependencia de terceros para un build que la
plantilla ya tiene. Una sola marca compartida: la CLI podría actualizar o
borrar un runtime que es de CloudFormation. Dejar MicroVM fuera de 0.15: lo
descartó el mantenedor, y el alcance se limita al recurso de imagen. Una marca
en la descripción de la imagen: `GetMicrovmImage` no la devuelve. Resolver
`latest` al sintetizar: pediría una llamada a AWS.

**Reversible.** Aditivo: un módulo opcional en cada SDK, un manejador
empaquetado, una marca nueva que sólo llevan recursos creados por CDK y el
contrato de pruebas; quitar los constructos deja 0.15 intacto. Detalle (D1 a
D14), hechos verificados y tareas en `openspec/changes/agentcore-cdk/`,
`testdata/agentcore-cdk/contract.json` y `AWS_API_NOTES.md` («Constructos
CDK»).
