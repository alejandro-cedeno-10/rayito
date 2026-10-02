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
| `:9000` | HTTP/1.1 (`axum`) | sólo Lambda (hooks de ciclo de vida) | `POST /aws/lambda-microvms/runtime/v1/<hook>` |

El puerto 9000 **nunca entra en `allowedPorts` de ningún token** (ADR-006).

### Servicios

Ver `proto/rayito/v1/`. Resumen:

| Servicio | RPCs | Notas de implementación |
|---|---|---|
| `HealthService` | `Health`, `Metrics`, `MetricsHistory` (M9) | `Health` es el único RPC sin `x-access-token`: sonda de readiness/liveness del SDK (`get-microvm` es eventualmente consistente). Expone `resume_generation`, `clock_offset_ms`, `kernel_state_lost`, `sandbox_id` y, desde M6, `imds_blocked` (campo 9: ruta de IMDS instalada **y** verificada), `hook_anomalies` (campo 10) y `metadata` (campo 11, el mapa del `runHookPayload`, vacío antes de `/run`); M9: `lifecycle` (campo 12, el plazo lógico de ADR-011: fase, `deadline`, `cap`, `extensions`), `egress_enforcement` (13, ADR-012), `cpu_count` y `memory_total_bytes` (14 y 15, la vista del guest, que la carga de trabajo ya lee de `/proc`). `MetricsHistory` exige `x-access-token` y sirve el anillo que el muestreador de métricas (`lifecycle/metrics_sampler.rs`) llena cada 5 s **sólo** en `running`/`resumed` (5 760 muestras = 8 h; sin red, así que no cuenta como actividad para la política de idle; una suspensión deja un hueco), con rango inclusivo y reducción a `max_points` |
| `ProcessService` | `Start` (server-stream), `Connect(pid, from_seq)`, `SendInput`, `CloseStdin`, `SendSignal`, `List` | `tokio::process::Command` + `process_group(0)`; `timeout_ms` impuesto por el agente sobre el **reloj de ejecución** (monotónico menos el tiempo suspendido, M5: un `timeout=60` pausado a los 10 s conserva 50 s tras el resume; SIGTERM al grupo, SIGKILL 5 s después, `EndEvent{status:"timeout"}`); `stdin=false` ⇒ `/dev/null`; ring de 1 MiB por pid para `from_seq`; canal acotado por suscriptor (64 eventos × 32 KiB) con backpressure real: si sigue lleno 30 s el suscriptor se desengancha con `EndEvent{status:"output_truncated"}` y el proceso sigue; máx. 8 suscriptores por pid; `StartEvent`+`EndEvent` retenidos 30 s tras salir (máx. 256 entradas terminadas) |
| `FilesystemService` | `Read` (stream 256 KiB), `Write` (client-stream multi-fichero), `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir` (server-stream) | `std::fs` bloqueante bajo `spawn_blocking` con identidad de fichero por hilo (`setfsuid`/`setfsgid` del usuario, nunca root; `tokio::fs` no puede fijar la identidad de un hilo entre awaits); lista de denegación sobre la ruta **canónica** (`/proc`, `/sys`, `/dev`, `/etc`, `/usr`, `/run/rayito`, `/opt/rayito`, el binario de `rayd`; `/root` y los `HOME` ajenos quedan cubiertos por los permisos POSIX bajo la identidad del usuario, …) ⇒ `PERMISSION_DENIED`, `..` ⇒ `INVALID_ARGUMENT`, rutas relativas al `HOME` del usuario; `Read` con `O_NOFOLLOW` y sólo ficheros regulares; escritura a temporal `.rayito-tmp-*` en el mismo directorio + fsync + fchmod/fchown + rename, padres creados, temporal borrado en cualquier fallo o cancelación; chunks ≤ 1 MiB; `ListDir` nunca sigue symlinks y corta a 10 000 entradas (`RESOURCE_EXHAUSTED`); `WatchDir` con `notify` (inotify, un watch por directorio, sin seguir symlinks), primer mensaje `WatchStarted` sólo con el watch instalado, sin debounce, cola de 1024 eventos con `RESOURCE_EXHAUSTED` al desbordar, máx. 64 watches por sandbox, `KeepAlive` cada 50 s; los mensajes de error nunca incluyen la ruta. M7 (ADR-009): `Checkpoint(CheckpointRequest) → stream CheckpointEvent` y `Restore(RestoreRequest) → stream RestoreEvent`: tar.gz del `HOME` del usuario (lista de exclusión fija + `exclude` ≤ 64, symlinks tal cual, especiales y no legibles contados en `skipped`, leído bajo `FsIdentityGuard`) subido/bajado por `rayd` como root a `s3://<bucket>/<key_prefix>/home.tar.gz` + `manifest.json` con las credenciales IMDSv2 del execution role; un checkpoint o restore a la vez (`FAILED_PRECONDITION`), `NOT_FOUND` sin manifest, `PERMISSION_DENIED` sin rol o `user=root`, `started → progress* → done | error`, `KeepAlive` cada 30 s, `/suspend` cierra con `suspending` y aborta el multipart. M9 (ADR-010): `StartImport`, `StartExport`, `GetTransfer`, `WatchTransfer` (server-stream) y `CancelTransfer` mueven bytes entre el fichero y **URLs de S3 prefirmadas por el SDK** con las credenciales del llamante (`rayd` no guarda ninguna): la política de URL (`transfer::url_policy`: https, puerto 443, host virtual-hosted regional exacto del bucket, ruta = la clave ligada al sandbox, SigV4, sin literales IP) se aplica antes de cualquier E/S, el adaptador `HyperSignedHttp` resuelve con un filtro que descarta loopback, link-local e IMDS y no sigue redirecciones; la importación sondea el `GET` (1 s los primeros 600 s, luego 5 s) y escribe por el mismo `WriteSink` que `Write`; la exportación hace `pread` sobre un descriptor abierto con `O_NOFOLLOW` como el usuario; 16 transferencias activas, 2 en curso, 64 terminadas retenidas 30 min; `GetTransfer("")` = `NOT_FOUND` es la sonda de capacidad del SDK; barrera de lectura tras subida (`Read`, `Stat`, `ListDir`, `Process.Start`, `Execute` y `Pty.Create` esperan hasta 2 s a una importación armada). `Write`/`Read` aceptan además `grpc-encoding: gzip` sólo si el cliente lo pide (cabecera `rayito-compress`), y `Write` y `Stat`/`ListDir` llevan `metadata` por fichero como xattrs `user.rayito.*` (≤ 64 claves, ≤ 4 000 B) aplicados sobre el descriptor |
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
| `/validate` | build, en una VM nueva desde el snapshot | build role | 1–3600 s | ninguno | reinicia el kernel por defecto (el mismo `restart_context` que hará `/run`) y ejecuta una celda real con pandas + matplotlib; responde 503 (`validating`) hasta terminar y 200 (`validated`/`validate_failed`), para que Lambda prefetchee esas páginas. Sin el reinicio aquí la rotación en `/run` costaba 45 s (`AWS_API_NOTES.md` §16 Q35); con él, 2–4 s |
| `/run` | arranque desde el snapshot; **el tráfico externo sólo llega tras el 200** | execution role | 1–60 s | `{"microvmId": "...", "runHookPayload": "..."}` | parsea el envelope, instala `sandbox_id`, el hash del token, `metadata` y `limits.cpu_seconds` (M6), responde 200; se acepta **una vez por arranque** (los siguientes devuelven 200 `already_ran`, se auditan como anomalía y no tocan nada). Tras el 200: rotación del kernel y, si el bloqueo de IMDS está instalado, la verificación `imds_probe` (≤ 10 s). Si falla o expira, el MicroVM pasa a `TERMINATING` sin haber estado `RUNNING` (`stateReason`). M9: si el payload trae el bloque `lifecycle` instala el plazo lógico (ADR-011) y despierta a su vigilante; si trae `network.enforce` y hay `CAP_NET_ADMIN`, **antes** del 200 arranca el proxy local e instala y verifica el deny-all de egress en 1,5 s (ADR-012); en cualquier caso responde 200, y mientras ese deny-all no se asienta `Health` dice `agent_ready=false` (medido: `Health` llega durante `/run`, `AWS_API_NOTES.md` §16 fila 66); la tarea lo asienta al terminar de cualquier modo, también si entra en pánico, y cada `ip` que lanza muere a los 5 s, así que un `ip` colgado no deja `Health` sin listo para siempre |
| `/suspend` | antes del checkpoint | execution role | 1–60 s | ninguno | checklist de la sección "Suspend / resume" (M9: también recoloca en espera las transferencias en curso; sigue respondiendo **siempre** 200); desde M6 se audita (`hook_audit`; los repetidos son `unchanged` y no cuentan; ninguna transición se rechaza) y arma el watchdog de suspensión estancada (20 s sin salto de `CLOCK_MONOTONIC` ⇒ `stale_suspend_recovered`: puerta reabierta, sin nueva generación, +1 anomalía) |
| `/resume` | tras restaurar; la VM sigue `SUSPENDED` hasta el 200 | execution role | 1–60 s | ninguno | ídem; el reseed es advisory (el sidecar responde en el acto `reseeded`/`deferred`/`failed`, su timeout nunca cuenta para el kill switch) y se recomprueba la ruta de IMDS (`imds_rule_missing` si desapareció). M9: llama a `timeout_resumed` con el veredicto del vigilante del plazo (sólo un salto de `CLOCK_MONOTONIC` ≥ 2 s abre la gracia de 30 s o aplica la regla de auto-resume; una sola gracia por plazo y nunca más allá del tope), re-verifica la política de egress en ≤ 3 s y despierta los sondeos de las importaciones armadas |
| `/terminate` | antes de liberar recursos | execution role | 1–60 s | ninguno | ACK 200; nada que persistir |

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
`CMD` de la imagen) y `/validate` (reinicia el contexto `default`) son los dos
que faltaban en T2, y `/ready` y `/validate` son hooks de build que nunca
llegan a `audit()`, así que un `/validate` forjado no deja línea `hook_audit`
ni sube `hook_anomalies`. Autenticar `/terminate` y `/validate` por el uid del
par queda pendiente. Ninguna transición se
rechaza ni se limita: `rayd` no distingue un hook forjado del genuino que
llega justo detrás, y rechazar el genuino dejaría un checkpoint real sin
preparar (revisión de M6; `SECURITY.md` T2).

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

**`rayd` (adaptadores)**:

- `grpc/`: un módulo por servicio; convierte tipos de `rayito-proto` ↔ dominio,
  añade el interceptor de `x-access-token` y los `KeepAlive`.
- `hooks/`: router `axum` con las seis rutas; sólo parsea HTTP y delega en
  `rayd-core::hooks`.
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
- `main.rs`: parseo de flags, wiring, arranque de los dos listeners, `anyhow`
  sólo aquí.

Reglas: `unwrap_used`/`expect_used` denegados en el workspace (permitidos en
tests vía `clippy.toml`); los tests de dominio corren en Windows con puertos
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

**Decisión.** `rayd` sirve los hooks en un listener `axum` HTTP/1.1 en
`0.0.0.0:9000`, separado del gRPC h2c en `:8080`. El SDK nunca acuña tokens con
`allPorts` por defecto ni incluye 9000 en `allowedPorts`.

**Consecuencia.** Si M0 (fila "hooks alcanzables vía proxy" de la tabla de resultados medida en el spike de M0, historial de git) demuestra que un token `allPorts` alcanza el 9000
desde fuera, se documenta como riesgo T2 en `SECURITY.md`; la mitigación
principal sigue siendo no acuñar `allPorts` y aceptar `/run` una sola vez.
Esa mitigación acota el origen **externo** y sólo ése: el listener es
`0.0.0.0:9000` en el mismo netns que los procesos del sandbox, así que
cualquier proceso uid 1000 de dentro de la VM alcanza las seis rutas por
loopback sin token alguno —`/terminate` se lleva la VM y `/validate` reinicia
el contexto `default` del kernel—, y la auditoría cubre sólo los hooks de
runtime (`/ready` y `/validate` nunca pasan por `audit()`). Autenticar
`/terminate` y `/validate` por el uid del par queda pendiente.

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
S3; con `persist=` el operador lo sabe y `SECURITY.md` T15 lo registra; en
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
IMDS, que `rayito-base` no concede (ADR-012, `_role_policy.require_caps_for`).

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
dedicada (independiente de `adapters::{child_registry,orphan_reaper}`,
reservados a los procesos que pasan por `ProcessSpawner`): nunca hay dos
sitios esperando el mismo pid. uid 990 está **por debajo** de
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

Pendiente: lo completa `m15-efs-volumes`, tras la campaña de medición
EFS-1..EFS-20 (`docs/research/2026-10-efs-persistence.md`).

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

**Hueco de integración conocido:** `Sandbox.create(events=...)` valida la
opción (un `LifecycleEvents` y un `logging` que llegue a CloudWatch) y
después sigue lanzando `UnimplementedError`. La sección necesita
`sandbox_id`/`image_arn`/`image_version`, que sólo existen tras
`run-microvm`, y todavía no entra en `FeaturePlan.configure_sections` (el
envío tras el primer `Health` ya existe: `_apply_configure_sections`/
`#applyConfigureSections`, de `m15-s3-mounts`/`m15-secrets-gateway`).
Aceptar `events=` sin enviarla dejaría al usuario pagando la pila sin
recibir ningún evento. `LifecycleEvents._build_section` / `buildSection`
construyen la sección real y están probadas; conectarlas a ese envío es
fontanería pendiente. Hasta entonces la pila, los webhooks y
`get_events` funcionan, pero ningún sandbox emite eventos.

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
seguimiento razonado y no bloqueante (mismo patrón que el reaper de zombis
huérfanos de M15 foundations), registrado en
`openspec/changes/m15-rayd-otlp/design.md`.

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
por encima del límite nunca llega al upstream. Lo que sí llega tiene las
cabeceras que el guest pudo haber puesto para esos mismos nombres
eliminadas primero (`header_template::must_drop`) y las vaultadas
inyectadas después, así que el código del sandbox no puede ni suplantar ni
leer de vuelta su propio secreto. El valor en sí vive sólo en memoria de
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

Pendiente: lo completa `m15-custom-domain` (dominio propio sobre CloudFront,
JWE de enrutado en un KeyValueStore firmado con SigV4A; necesita D3).
