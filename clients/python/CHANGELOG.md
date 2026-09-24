# Changelog

Todos los cambios notables del paquete `rayito` (SDK Python). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

## [0.3.0] - 2026-09-24


Rayito 0.3.0 (M9, paridad con E2B 2.x). Notas completas en
`docs/RELEASE_NOTES_0.3.0.md`; todo lo de M9 exige una imagen publicada con
el `rayd` de M9.

### Cambios que rompen

- **`rayito.e2b` pasa de E2B 1.x a 2.x y exige una imagen M9**: `create()`
  siempre pide un plazo lógico a `rayd` y, contra una imagen anterior,
  termina el VM y lanza `UnimplementedError("lifecycle")`. `timeout` deja de
  ser la vida inmutable del MicroVM (ahora es el plazo lógico; el tope es
  `max_lifetime`). Detalle en "Changed (shim)".
- `rayito.e2b`: `sbx.pause()` devuelve `bool` (antes el id del sandbox);
  `files.watch_dir(path, on_event)` y `pty.create(size, on_data)` toman el
  callback por nombre (el segundo posicional es `user`, como en E2B 2.x).
- `rayito.e2b`: `set_timeout`, `upload_url`/`download_url`,
  `get_metrics(start=, end=)`, `list(next_token=)` y
  `allow_internet_access=False` ya no lanzan `UnimplementedError`, y
  `mcp=`, `network=` y `lifecycle=` ya no son `TypeError`: el código que
  capturaba esos errores para degradar cambia de camino.

### Added

- **Transferencias por S3 prefirmado** (`m9-file-transfer`, ADR-010):
  `S3Staging(bucket, prefix="rayito-transfer", region=None,
  max_expires_in=86400, threshold_bytes=8 MiB, multipart_threshold_bytes=5 GiB)`
  (o `RAYITO_TRANSFER_BUCKET`/`RAYITO_TRANSFER_PREFIX`/`RAYITO_TRANSFER_REGION`;
  sin bucket por defecto), `create(transfer=)`, `connect(transfer=)` y
  `sbx.transfer`. `files.upload_url(path, user=, expires_in=3600,
  max_bytes=, form=)` devuelve un `UploadTicket` (un `str` con la URL, más
  `headers`, `fields`, `expires_at`, `wait()`, `status()`, `cancel()`; de un
  solo uso, con la importación ya armada en el sandbox) y
  `files.download_url(path, user=, expires_in=3600, filename=)` un
  `DownloadLink` (`str` con `size`, `sha256` y `expires_at`; una foto del
  fichero al llamar, multiparte desde `multipart_threshold_bytes`).
  `Sandbox.upload_url/download_url(path, user, use_signature_expiration)`
  con la forma de E2B. El SDK firma con tus credenciales (SigV4, host
  virtual regional, vida `min(expires_in, max_expires_in, 604800)`); `rayd`
  no guarda ninguna. `repr` nunca muestra la URL y `pickle` la rechaza.
  Nuevos `TransferStatus`, `AsyncUploadTicket`, `TransferException(code,
  reason)`, `FileUploadException` y `UnimplementedError(feature, reason,
  doc=None)` nativo (sin staging: "configura transfer=S3Staging(...) o
  RAYITO_TRANSFER_BUCKET"; con una imagen anterior: "actualiza la imagen").
- **Ficheros grandes por S3**: con `transfer`, `files.write`/`write_files`
  suben lo que mide al menos `threshold_bytes` (y todo stream binario no
  buscable) directamente a S3 y `rayd` lo importa verificando su sha256;
  `files.read` exporta y descarga verificando el sha256. Sin `transfer`,
  el camino gRPC no cambia. `request_timeout` (o `60 s + 1 s por MB`)
  acota también la pata S3 y `stream_idle_timeout` rige entre trozos de la
  descarga en `format="bytes"`/`"text"`: una subida o descarga colgada
  falla con `TimeoutException` sin esperar a los reintentos de botocore.
  `create(pool=..., transfer=...)` firma con la sesión boto3 del pool.
- `files.read(gzip=, stream_idle_timeout=)`, `files.write`/`write_files(gzip=,
  metadata=, use_octet_stream=)` y `EntryInfo.metadata` (mapa de sólo
  lectura, claves en minúsculas). `gzip` y `metadata` en una escritura
  exigen una imagen M9 (`UnimplementedError` antes de mover bytes);
  `use_octet_stream` se acepta sin efecto. Nuevos códigos de `StreamError`:
  `resource_exhausted` (`DiskFullException` o `RateLimitException`) y
  `failed_precondition` (`InvalidArgumentException`).

- **Plazo del servidor** (`m9-server-timeout`, ADR-011; exige una imagen M9):
  `create(max_lifetime=, on_timeout="kill" | "pause")`. Con cualquiera de los
  dos, `timeout` es un plazo lógico que `rayd` hace cumplir aunque el cliente
  muera y `max_lifetime` (120–28 800 s, por defecto `timeout + 60`) es
  `maximumDurationInSeconds`. `set_timeout(timeout)` (instancia y clase, con
  el access token) lo fija con `SetTimeout` EXACT (puede acortarlo; más allá
  del tope, `InvalidArgumentException`) y `connect(timeout=)` (instancia y
  clase) lo alarga con `AT_LEAST`. `on_timeout="pause"` suspende al vencer y
  `idle.auto_resume` reanuda con un plazo nuevo de `max(timeout, 300 s)`.
  Nuevos `SandboxLifecycle` (en `get_health().lifecycle` y
  `get_info().lifecycle`) y `LifecycleUnsupportedException` (una imagen
  anterior a M9 con un ciclo de vida pedido: el VM se termina salvo
  `keep_on_failure`). Sin `max_lifetime` ni `on_timeout` el cable y la
  semántica son los de siempre. `TimeoutException` también cuando una llamada
  choca con el plazo (`sandbox_timeout`).
- **Historial de métricas y listado reanudable**
  (`m9-sandbox-observability`): `get_metrics_history(start=, end=,
  max_points=)` (instancia y clase con el access token; una muestra cada 5 s,
  anillo de 8 h, hueco mientras está suspendido; `UnimplementedError` en una
  imagen anterior, como las transferencias y el plazo, con el
  `UNIMPLEMENTED` de gRPC en `__cause__`),
  `SandboxMetrics.mem_cache_bytes`, `SandboxHealth.cpu_count` y
  `memory_total_bytes`, `SandboxInfo.agent_version`/`cpu_count`/`memory_mb`
  en `get_info()`. `Sandbox.paginate(limit=, next_token=, order=,
  started_after=, states=, metadata=)` → `SandboxListPaginator`
  (`next_items()`, `has_next`, `next_token` opaco) y `list(order=,
  started_after=)`.
- **Política de egress** (`m9-egress-policy`, ADR-012): `create(network=,
  allow_internet_access=)`, `update_network()` (instancia y clase),
  `get_network()`, `NetworkPolicy`, `NetworkOptions`, `EgressProxy`,
  `NetworkState`, `EgressEnforcement`, `ALL_TRAFFIC` y
  `SandboxHealth.egress_enforcement`. Se aplica en el guest de
  `rayito-base-caps`; en cualquier otra imagen el SDK termina el VM (también
  con `keep_on_failure`) y lanza `UnimplementedError`.
- **Kernels JavaScript y TypeScript** (`m9-deno-kernels`): `language`
  acepta `typescript` (alias `ts`) además de `javascript` (`js`), servidos
  por el kernel de Deno de `rayito-base-poly`.
- **Git** (`m9-e2b-v2-surface`): `sbx.git` (`Git`/`AsyncGit`) con la API git
  de E2B sobre `commands.run` (`clone`, `status`, `commit`, `push`, `pull`,
  `dangerously_authenticate`, ...), `GitStatus`, `GitBranches`,
  `GitFileStatus`, `GitAuthException` y `GitUpstreamException`; las
  credenciales nunca se registran y se redactan de la salida.
- **CLI** (`rayito[cli]`): `rayito sandbox create [--detach --token-file]`,
  `connect`, `exec -- CMD` y `metrics [--follow]`, con terminal interactiva
  (POSIX y consola de Windows) y el token sólo por fichero o
  `RAYITO_ACCESS_TOKEN`, nunca por argv.
- `create()`/`connect()` aceptan `logger=` (los logs de ese sandbox van al
  `logging.Logger` dado), `is_running(request_timeout=)` y
  `CommandHandle.wait(on_pty=, on_stdout=, on_stderr=)`.
- **Shim `rayito.e2b` 2.x** (`m9-e2b-v2-surface`, contrato `e2b` 2.51.0 y
  `e2b-code-interpreter` 2.10.0): `set_timeout`, `connect(timeout)` y la
  instancia `sbx.connect()`, `lifecycle`, `beta_create(auto_pause=True)`,
  `upload_url`/`download_url`, `get_metrics(start, end)` y la forma de clase
  con el access token, `list(limit, next_token, order)` con
  `SandboxQuery.state/started_after/template`, `allow_internet_access=False`
  y `network` (en `rayito-base-caps`), `update_network`, `git`,
  `run_code(language="typescript")`, `E2B`, `ConnectionConfig`,
  `sbx.connection_config`, `proxy=`, `headers=`, `retries=`, `logger=`,
  `traffic_access_token`, `envd_api_url` y las excepciones
  `FileNotFoundException`, `SandboxNotFoundException`,
  `ServiceBusyException`, `FileUploadException`, `BuildException`.
  `UnimplementedError` explícito (nunca `TypeError` ni `AttributeError`) para
  `fork`, snapshots, `pause(keep_memory=False)`, `network.rules`,
  `mask_request_host`, `allow_public_traffic=True`, `mcp`, `iam`, volúmenes,
  secretos y templates.

### Changed (shim)

- **`rayito.e2b` exige una imagen M9**: `create()` siempre pide un plazo
  lógico; contra una imagen anterior termina el VM y lanza
  `UnimplementedError("lifecycle")`. `timeout` es ahora el plazo lógico y
  `maximumDurationInSeconds` es `max_lifetime` (por defecto
  `max(3600, min(timeout + 60, 28800))`).
- `rayito.e2b.Sandbox.set_timeout` deja de lanzar `UnimplementedError` (lo
  que decía la entrada de 0.2.0): mueve el plazo lógico hasta
  `max_lifetime`.
- `sbx.pause()` devuelve `bool` (antes el id); `files.watch_dir` y
  `pty.create` toman el callback por nombre (el segundo posicional es
  `user`); `proxy=` se honra; `NotEnoughSpaceException` es
  `DiskFullException` y se lanza; `rayito.e2b.UnimplementedError` es la
  misma clase que `rayito.UnimplementedError`.

### Fixed

- **Auto-resume tras una pausa por el plazo con el cliente vivo**: en modo
  `on_timeout="pause"` con `auto_resume`, si este cliente suspendió el
  sandbox al vencer y la suspensión real duró menos de 2 s (el vigilante de
  `rayd` no la reconoce como congelación), el sandbox volvía `expired` y la
  siguiente llamada fallaba con `sandbox_timeout`. Ahora el SDK aplica la
  regla de E2B (`max(timeout, 300 s)`, acotada al tope menos 5 s) con un
  `SetTimeout` en el primer `sandbox_timeout` tras reanudar, una vez por
  suspensión. La marca de un solo uso sólo se consume cuando la
  `resume_generation` ya avanzó y el `SetTimeout` respondió (un
  `sandbox_timeout` que llega antes de la congelación ya no la gasta), y los
  callers concurrentes comparten una sola reapertura (un lock en `Sandbox`,
  una tarea compartida en `AsyncSandbox`): un único `SetTimeout` y todos
  reintentan.
- `rayito doctor`: la tabla de compatibilidad gana la fila `0.3` (`rayd`
  mínimo `0.3.0`); sin ella la comprobación `compatibility` daba `FAIL` con
  el SDK 0.3.0.
- Las URLs y la pata S3 de las transferencias se firman con la sesión del
  plano de control (`LambdaMicrovmsControlPlane.session`) cuando no se pasa
  `session=`, también en un sandbox tomado de un pool (`pool.session`); antes
  caían a la cadena por defecto de boto3 y podían firmar con otro principal.
- `get_info()` devuelve los hechos del guest (`agent_version`, `cpu_count`,
  `memory_mb`) en el valor que retorna sin guardarlos en `sbx.info`.
- `UnimplementedError` de una política de egress en una imagen sin
  `CAP_NET_ADMIN` nombra `allow_internet_access=False` sólo cuando ese flag,
  y no `network=`, es lo que pide la política (la misma regla que TS).
- CLI: al salir de `rayito sandbox connect`/`create` el hilo que lee la
  terminal local se detiene (antes podía seguir consumiendo la entrada y, en
  macOS, bloquear el cierre del descriptor de la PTY).
- `Result.extra` se lee del proto con las claves ordenadas: el orden de un
  mapa protobuf no está garantizado.
- La readiness de `create()`, `connect()` y `resume()` exige además un
  `Health` con `sandbox_id`: el proxy deja pasar `Health` antes de que
  `rayd` reciba `/run`, y ese `Health` (kernel del snapshot sin rotar) daba
  por listo un sandbox cuya rotación de `/run` ponía `kernel_ready=false`
  justo después (AWS_API_NOTES.md Q78, visto en la regresión de M9 con `create()` en 2,35 s).

### Security

- `repr()` de `LaunchPlan` ya no muestra el access token ni el de
  `LaunchRequest` el `runHookPayload` (que lleva los `envs` de `create()`):
  ambos campos quedan fuera de `repr` (`field(repr=False)`).
- **`JsonFilePoolBackend` escribe por un temporal exclusivo**
  (auditoría interna H-01…H-06, fila H-03): el fichero del pool se escribía
  por `<path>.tmp`, un nombre fijo que otro usuario del sistema podía crear
  o apuntar con un enlace antes que el SDK; ahora se crea con
  `tempfile.mkstemp()` (`O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`) y `fchmod`
  sobre el descriptor antes de escribir nada, y se renombra al destino. Sin
  esto la promesa de `0600` de T14 no era cierta.
- **`rayito-mcp --http` pasa siempre `TransportSecuritySettings`** (fila
  H-05): el SDK `mcp` sólo auto-activa la protección anti-DNS-rebinding para
  tres cadenas de host exactas, así que `--host 127.0.0.2` servía sin mirar
  `Host` ni `Origin`. Ahora el servidor exige que ambos sean el `host:puerto`
  con el que arrancó, y un `--host` comodín (`0.0.0.0`, `::`) se rechaza con
  error de uso (exit 2).
- **Aviso único cuando `RAYITO_ACCESS_TOKEN` se hereda del entorno** (fila
  C-08): `create()` también lee la variable, así que exportarla convierte el
  secreto por sandbox en uno de toda la flota del proceso. El SDK lo avisa
  una vez por proceso (`logger.warning`, nunca el valor).
- **Los errores de AWS ya no encadenan el `ClientError` crudo**: botocore no
  cuelga la petición firmada de un `ClientError`, pero lo que AWS devuelve
  repite la firma: un `SignatureDoesNotMatch` de S3 trae `AWSAccessKeyId` y
  `CanonicalRequest` (con el valor de `x-amz-security-token`) en
  `response["Error"]`, y un `InvalidSignatureException`/`SignatureDoesNotMatch`
  de lambda-microvms o STS mete la cadena canónica en el mensaje, que acababa
  en el mensaje de la excepción traducida y, por `raise ... from exc`, en
  cualquier traceback (también el `logger.debug(..., exc_info=True)` de la
  reconexión). Ahora el plano de control y las transferencias por S3 lanzan
  fuera del `except` y desde un `AwsErrorSummary`
  (`rayito._aws_sanitize.sanitize_aws_error`) con sólo `name`, `code`, el
  mensaje redactado, `status_code`, `request_id`, `extended_request_id` y
  `attempts` (S3 sin mensaje: un `EndpointConnectionError` nombra el
  bucket); ni `__cause__` ni `__context__` guardan el error de botocore. El
  mensaje traducido pierde la cadena canónica, las cabeceras de firma, los
  parámetros `X-Amz-*` y los ids de clave. `status_code`, `aws_code`,
  `retry_after` y `quota_code` no cambian.

## [0.2.0] - 2026-09-17

Segunda release: los siete cambios de M7 (`MILESTONES.md`), aceptados
contra AWS real sobre `rayito-base` con `rayd` 0.2.0. Los SDKs y `rayd`
suben `MAJOR.MINOR` en lockstep; `rayito doctor` exige `agent_version` ≥
0.2.0 para el SDK 0.2 (`docs/site/docs/limits.md`).

### Added

- **Persistencia del `HOME` en S3** (`m7-s3-persistence`, ADR-009):
  `S3Prefix(bucket, prefix="rayito", name=None, region=None)`,
  `Sandbox.create(persist=, persist_timeout=600)` (exige
  `execution_role_arn`; con `name` restaura el checkpoint existente en
  `sbx.last_restore`, sin `name` lo fija al `sandbox_id`),
  `connect(persist=)`, `sbx.persist`, `checkpoint_files(target=, exclude=,
  timeout=, on_progress=)` → `CheckpointResult`, `restore_files(source=,
  timeout=, on_progress=)` → `RestoreResult` (`NotFoundException` sin
  checkpoint) y `reincarnate(exclude=, persist_timeout=)` (checkpoint →
  `create(persist=)` con las mismas opciones → `kill()`; el viejo sigue vivo
  si el `create` falla). Nuevos `CheckpointProgress`, `RestoreProgress`,
  `LaunchOptions` y `PersistenceException(code)` (`permission_denied`,
  `internal`, `failed_precondition`, `unimplemented`, `interrupted`,
  `resource_exhausted`); `create(pool=)` rechaza `persist=`. Misma
  superficie en `AsyncSandbox`. `rayito.e2b.Sandbox.set_timeout` sigue
  siendo `UnimplementedError` y su mensaje nombra `reincarnate()`.
- `run_code(code, language=...)` y `create_code_context(language=...)` aceptan
  `python`, `bash` y `javascript` (alias `js`, sin distinguir mayúsculas;
  `m7-poly-kernels`): `language` selecciona el contexto por defecto de ese
  kernel (`default-bash`), que el agente crea en la primera celda, y es
  excluyente con `context` (`InvalidArgumentException`). El kernel `bash`
  sólo lo trae la variante de imagen `rayito-base-poly`; `javascript` es un
  nombre reservado que hoy ninguna imagen trae (`AWS_API_NOTES.md` Q57). Un
  kernel que la imagen no trae es `InvalidArgumentException` con `grpc_code`
  `UNIMPLEMENTED` y `rayito-base-poly` en el mensaje; `envs` por ejecución
  sólo en contextos Python. El shim `rayito.e2b` reenvía `bash`,
  `javascript` y `js` al core y mantiene `UnimplementedError` para `r`,
  `java` y el resto (`docs/site/docs/kernels.md`).
- **Pool de sandboxes suspendidos** (`SandboxPool`, `AsyncSandboxPool`,
  `PoolConfig`, `PoolStats`, `PoolSlotInfo`, `PoolBackend`,
  `InMemoryPoolBackend`, `JsonFilePoolBackend`, `PoolClosedException`;
  ADR-008, `m7-suspended-pool`): N MicroVMs calentados con `create()`,
  asentados con una celda trivial, aparcados con `pause(wait=True)` y
  entregados por `take(wait=...)` con `resume_microvm` explícito y el token
  de la plaza (sin `get_microvm` en la toma); fallback a `create()` sin
  plaza; relleno en un hilo del proceso con backoff 1 s → 60 s por los
  token buckets compartidos; reciclado antes del muro de 8 h y
  reconciliación con `list_microvms`; recuperación desde el backend JSON
  (`rayito.pool/1`, modo `0600`, el mismo fichero que lee TypeScript);
  custodia del secreto por plaza hasta `take()` (`SECURITY.md` T14);
  `Sandbox.create(pool=...)` / `AsyncSandbox.create(pool=...)` como azúcar
  que rechaza todo kwarg de lanzamiento. Documentación en
  `docs/site/docs/pool.md`.
- Servidor MCP (`rayito.mcp`, extra `rayito[mcp]`, script `rayito-mcp`):
  seis herramientas (`run_code`, `run_command`, `read_file`, `write_file`,
  `list_files`, `list_sandboxes`), stdio y streamable HTTP, un sandbox por
  proceso (creado en la primera llamada, suspendido por AWS al quedar ocioso,
  terminado al cerrar el servidor); configuración sólo por entorno
  (`RAYITO_TEMPLATE`, `RAYITO_MCP_TIMEOUT_SECONDS`, `RAYITO_MCP_IDLE_SECONDS`,
  `RAYITO_MCP_LOG_LEVEL`). Documentación en `docs/site/docs/mcp.md`.
- **CLI `rayito`** como extra `rayito[cli]` (`typer`) con el entry point
  `rayito` y `python -m rayito.cli`: `rayito image publish|list|prune|zip`,
  `rayito sandbox list|kill|info|logs` (stream de CloudWatch
  `YYYY/MM/DD[<versión>]<id>` resuelto desde `get-microvm`) y
  `rayito doctor` (diez comprobaciones `OK|WARN|FAIL|SKIP`: credenciales y
  región, imágenes gestionadas, Service Quotas frente a los defaults,
  `iam:SimulatePrincipalPolicy` orientativa, bucket, gate de tres estados de
  la imagen, MicroVMs `RUNNING`, token, `Health` del agente y la tabla de
  compatibilidad SDK ↔ `rayd` de `rayito.cli._compat` (la versión de imagen,
  contador de builds por imagen y cuenta, sólo se informa); `--launch`
  crea y mata un sandbox de 300 s; `--json` en todos los comandos). Ninguna
  salida contiene un JWE, un access token ni un `runHookPayload`.
- `probe_health` / `probe_health_async` (un `Health` sin access token sobre
  un canal dedicado) como base de `probe_metadata`; superficie pública sin
  cambios.

### Fixed

- `rayito image zip` / `copy_sidecar.py`: la lista de exclusión se evalúa
  antes de `is_file()`, así que un symlink de Linux dentro de un `.venv`
  excluido (creado por una sesión Docker) ya no rompe la copia en Windows
  (`WinError 1920`).

### Changed

- `scripts/publish_image.py`, `image_prune.py`, `image_zip.py` y
  `copy_sidecar.py` son shims de `rayito.cli` (`_publish`, `_prune`,
  `_artifact`) con los mismos argumentos; `publish` exige `--bucket` o
  `RAYITO_BUCKET` (la biblioteca ya no trae el bucket del mantenedor) y el
  `Makefile` lo pasa con la variable `BUCKET` corriendo los dos shims de
  AWS con `uv run --project clients/python`.

- Licencia MIT → Apache-2.0 (PEP 639: `license = "Apache-2.0"`,
  `license-files = ["LICENSE", "NOTICE"]`, sin clasificador `License ::`;
  la wheel lleva `License-Expression: Apache-2.0` y `NOTICE` en
  `.dist-info/licenses/`, comprobado por `scripts/check_wheel.py`).

## [0.1.0] - 2026-09-16

Primera versión publicable. Reúne la superficie aceptada contra AWS real en
los hitos M1-M6 (`MILESTONES.md`).

### Added

- **Ciclo de vida (M1)**: `Sandbox.create` (`run-microvm` + token del proxy +
  sondeo de `Health` hasta `agent_ready` y `kernel_ready`), `connect`, `kill`,
  `list`, `get_info`, `is_running`, `get_host(port)` con las cabeceras del
  proxy, `get_health`; `IdlePolicy` (auto-suspensión a los 300 s por
  defecto); `timeout` como vida máxima del MicroVM (tope 8 h).
- **Comandos (M2)**: `sbx.commands.run` en foreground y background,
  `on_stdout`/`on_stderr`, `stdin`, `list`, `kill`, `connect(pid, from_seq)`,
  `send_stdin`, `close_stdin`; `CommandHandle` con `wait`, iteración y
  reconexión; `get_metrics()` (instantánea procfs).
- **Ficheros (M3)**: `sbx.files.read` (`text`/`bytes`/`stream`), `write`,
  `write_files` en un solo stream, `list(depth)`, `exists`, `get_info`,
  `remove`, `rename`, `make_dir`, `watch_dir` con `WatchHandle`.
- **Código (M4)**: `sbx.run_code` sobre kernels Jupyter con estado,
  `Execution` con `results` (mime bundles y charts de E2B), `logs`, `error`
  como dato; contextos (`create_code_context`, `list_code_contexts`,
  `remove_code_context`, `restart_code_context`).
- **PTY y suspend/resume (M5)**: `sbx.pty` (`create`, `connect`,
  `send_input`, `resize`, `kill`; `PtyHandle`), `pause()`/`resume()` con
  procesos, PTYs, watches y kernels vivos al otro lado, el contrato de
  reconexión (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`, `Reattach`) y
  `reconnect_timeout`.
- **Metadatos por sandbox (M6)**: `Sandbox.create(metadata=...)` viaja en el
  `runHookPayload` (4096 caracteres junto a `envs`) y `rayd` lo devuelve en
  `Health`: `sbx.metadata`, `SandboxHealth.metadata`, `SandboxInfo.metadata`
  (`None` = no leído del agente), `Sandbox.get_info(sandbox_id)` con la sonda
  de `Health` sobre sandboxes `RUNNING` y `Sandbox.list(metadata=...)`, un
  filtro en cliente **O(n)** (un `get-microvm` + un JWE + un `Health` por
  sandbox `RUNNING`) que nunca despierta un sandbox suspendido.
- **Shim de E2B (M6)**: `rayito.e2b` con `Sandbox`, `AsyncSandbox` y los
  nombres del SDK de E2B 1.x (`Execution`, `Result`, `CommandHandle`,
  `SandboxInfo`, `SandboxQuery`, `SandboxPaginator`, `PtySize(rows, cols)`,
  las excepciones de `e2b.exceptions`, los charts...). Mapea `template`,
  `timeout` (300 s), `metadata`, `envs`, `request_timeout` y `sandbox_id`;
  ignora con `RayitoCompatWarning` `api_key`, `domain`, `debug`, `proxy` y
  `secure=False`; lanza `UnimplementedError` para `set_timeout`,
  `upload_url`/`download_url`, rangos de `get_metrics`, `connection_config`,
  kernels no Python, `list(next_token=)`, `beta_create(auto_pause=...)`,
  templates/`fork` y `allow_internet_access=False` (medido: un MicroVM sin
  conector de egress hereda el de la imagen y sigue saliendo a internet,
  `AWS_API_NOTES.md` Q44).
- `AsyncSandbox` con la misma superficie que `Sandbox` sobre `grpc.aio`,
  incluidos metadatos y el shim.
- Empaquetado: clasificadores, URLs, `py.typed`, wheel comprobada
  (`scripts/check_wheel.py`, `twine check`), workflow `release.yml` con
  Trusted Publishing de PyPI y sitio de documentación (`docs/site`).

### Requisitos de imagen

- `run_code` necesita `rayito-base` ≥ 7.0 (sidecar de kernels); `pty` y la
  reconexión tras `pause()` necesitan ≥ 10.0 (M5); los **metadatos** necesitan
  la imagen de M6 (un `rayd` que devuelva `HealthResponse.metadata`; la
  aceptación de 0.1.0 corrió sobre `rayito-base-e2b` 1.0, construida del mismo
  árbol que la `rayito-base` de M6). Sobre una imagen anterior el SDK funciona
  y `metadata` se lee vacío.

### Limitaciones conocidas

- La vida de un sandbox no se puede extender (`set_timeout` de E2B es
  `UnimplementedError`: no existe `UpdateMicrovm`); tope 8 h running +
  suspended.
- Ancho de banda del endpoint de un MicroVM de 2 GB: ≈ 0,65 MB/s de escritura
  y ≈ 6,71 MB/s de lectura (medidos); 8 conexiones concurrentes.
- `list(metadata=)` es O(n) sobre los sandboxes `RUNNING` y pospone la
  auto-suspensión de cada uno una ventana de idle.
- Sólo kernels Python; sin URLs firmadas; sin historial de métricas.
- Las URLs del proyecto apuntan a `https://github.com/alejandro-cedeno-10/rayito` hasta
  que el repositorio sea público (placeholder documentado).

### Publicación

Pasos manuales, fuera de CI, antes del primer tag (pasos canónicos en
`docs/RELEASING.md`):

1. Registrar el *Trusted Publisher* en PyPI: proyecto `rayito`, repositorio
   `alejandro-cedeno-10/rayito`, workflow `release.yml`, environment `pypi`.
2. Crear el environment `pypi` en GitHub (Settings → Environments).
3. `git tag python-v0.1.0 && git push origin python-v0.1.0`: el job `build`
   construye y comprueba la wheel (contenido y `twine check`) y verifica que
   el tag coincide con `pyproject.toml`; el job `publish` sube a PyPI sin
   token. `workflow_dispatch` sólo ensaya el `build`.

## [0.0.1] - [0.0.5]

Builds internos de los hitos M1-M5, nunca publicados.

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.2.0...HEAD
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.1.0...python-v0.2.0
[0.1.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/python-v0.1.0
