# Changelog

Todos los cambios notables del paquete `rayito` (SDK Python). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Security

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
