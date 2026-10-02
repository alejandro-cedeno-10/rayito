# Changelog

Todos los cambios notables del agente `rayd` (`crates/rayd`, con
`crates/rayd-core` y `crates/rayito-proto`, que comparten versión por
`[workspace.package]`). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/). `rayd` no se publica en crates.io: se
distribuye como binario estático `aarch64-unknown-linux-musl` dentro de la
imagen `rayito-base` y como asset de la GitHub Release del tag `rayd-v*`.

## [Unreleased]

### Added

- **`ConfigureService` y los seis slots de función 0.6** (`v06-foundations`,
  M15 foundations, ADR-015): el servicio gRPC `Configure`/`ConfigureStatus`,
  el orden de aplicación fijo (eventos → telemetría → pasarela → montajes
  S3 → volúmenes EFS), `Health.features` (`AgentFeatures`, campo 16,
  `configure=true` y el resto `false` hasta que su propia función
  construye un adaptador real) y `LifecycleParticipant` para que una
  función futura participe en `/suspend`/`/ready` sin cambiar su contrato.
  El reaper de zombies huérfanos de PID 1 (`rayd_core::orphans`,
  `ChildRegistry`, `OrphanReaper`, Q80) queda implementado y probado, sin
  activarse todavía en `main.rs` (ver MILESTONES.md M15). El registro de
  `.proto` pasa a descubrirse por glob (`crates/rayito-proto/build.rs`).
  Sin ninguna sección de `ConfigureSandbox`, el comportamiento es
  idéntico al de 0.5.x.
- **Un único `FeatureSet` por proceso y participantes acotados**
  (`v06-foundations` §15, ADR-015): `main` comparte un `Arc<FeatureSet>`
  entre `ConfigureService`, `Health.features` (derivado de `supported()` de
  cada slot) y los hooks (`grpc::router_with_features`).
  `/suspend`, `/resume` y `/terminate` llaman a `on_suspend`/`on_resume`/
  `on_terminate` de cada participante sólo ante una transición aceptada,
  cada uno con su propio tope (`PARTICIPANT_RESUME_TIMEOUT`,
  `PARTICIPANT_TERMINATE_TIMEOUT`). Sin participantes, idéntico a 0.5.x.
<!-- m15-s3-mounts -->
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
- **Eventos de ciclo de vida firmados** (`m15-events-webhooks`, ADR-020):
  `rayd` emite `created`/`paused`/`resumed`/`killed{reason: request}` como
  una línea `rayito.event.v1 <b64url(json)> <b64url(hmac-sha256)>` en su
  propio stdout, sólo cuando `ConfigureSandbox` trae una
  `LifecycleEventsConfig` con clave (la deriva y empuja el SDK; `rayd`
  nunca ve el secreto del stack). Cola acotada y no bloqueante; `paused` y
  `killed` esperan a que la línea salga a stdout (barrera `flush` en la
  misma cola) dentro de la cuota de `/suspend` y del tope de `/terminate`.
  Si la fuente aleatoria falla, el evento se descarta y se cuenta
  (`random_unavailable`). El estado vive en el `FeatureSet` único del
  proceso. Sin `events=`, cero líneas y cero coste (el participante de esta
  función comprueba su propio estado antes de hacer nada, y no espera
  ningún `flush` si no encoló nada).
<!-- m15-events-webhooks -->
<!-- m15-rayd-otlp -->
<!-- m15-templates -->
<!-- m15-secrets-gateway -->
<!-- m15-custom-domain -->

## [0.5.1] - 2026-10-01

Sin cambios en el agente: versión en paso con los SDK 0.5.1. Una imagen con
`rayd` 0.5.0 sigue siendo compatible.

## [0.5.0] - 2026-10-01

### Fixed

- `/suspend` ya no puede colgarse en `sync(2)`: el paso 4 del checklist
  llamaba a `sync(2)` sin plazo, y un montaje de red o FUSE colgado (o un
  disco muy lento) lo dejaba en estado `D`; un `/suspend` que no responde a
  tiempo hace que AWS termine el `MicroVM`. Ahora se hace un `syncfs(2)` por
  sistema de ficheros (sacados de `/proc/self/mountinfo`: escribibles, uno
  por dispositivo, sin pseudo-sistemas), cada uno en un hilo desechable, y
  el hook espera como mucho el plazo de `SuspendBudget` (5 s por defecto,
  acotado a que gracia + quiesce + sync quepan en la mitad del presupuesto
  del hook). Lo que no termine sigue fuera del camino del 200; su sistema de
  ficheros se salta en los siguientes `/suspend` mientras siga colgado, y el
  plazo vencido se registra sólo con contadores (nunca rutas). La regla vive
  en `rayd_core::suspend_sync`; las syscalls, en `adapters::bounded_sync`.

## [0.4.0] - 2026-09-30

### Cambios que rompen

- `LifecycleService.SetTimeout` con `timeout_ms=0`: la regla del timeout es
  sólo del dominio, que juzga primero la fase. Un sandbox sin plazo responde
  `FAILED_PRECONDITION lifecycle_unmanaged` (antes `INVALID_ARGUMENT
  "timeout_ms must be positive"`) y uno terminando `FAILED_PRECONDITION
  sandbox_timeout`; con plazo activo, 0 ms da el mismo `INVALID_ARGUMENT`
  que 1-999 ms. Migración: ninguna para los SDK (validan >= 1 s antes del
  RPC); un cliente gRPC propio debe tratar `lifecycle_unmanaged` como antes
  trataba el `INVALID_ARGUMENT`.
- Los mensajes de error del agente (status gRPC, `StreamError.message` y
  motivo del `/run` rechazado) pasan al español; `timeout below 1 s` es ahora
  `el timeout debe ser de al menos 1 s`. Los tokens que parsean los SDK
  (`suspending`, `terminating`, `sandbox_timeout`, `lifecycle_unmanaged`,
  `timeout beyond cap; cap_unix_ms=`, `kernel not ready`, `disk_reserve`,
  `disk_full`, `metadata_unsupported`, `metadata_too_large`, los
  `<reason>:` de las transferencias, `egress_update_failed`,
  `egress_verify_failed`) no cambian y viven en `rayd_core::wire_tokens`.
  Migración: quien compare mensajes por texto debe usar el código gRPC o
  esos tokens.

### Changed

- `RegistryError::Full`/`Unknown` se convierten en `TransferError`
  (`From`) y toman su texto de él en lugar de repetirlo; el cierre por
  suspensión de un stream de proceso o PTY es un solo constructor de
  dominio (`ProcessEnd::suspending`).

## [0.3.3] - 2026-09-29

### Changed

- Plazo del sandbox (refactor interno, sin cambio de comportamiento): el
  veredicto de congelación de ADR-011 (`is_thaw`/`was_frozen`) pasa a
  `rayd-core` (`sandbox_timeout::freeze`) como funciones puras con tests de
  tabla, y el hilo del plazo sólo guarda los instantes; una sola conversión
  de `SystemTime` a milisegundos Unix (`clock::unix_millis`, reexportada
  desde `metrics`) sustituye la copia privada del hilo del plazo.
- Egress (refactor interno, sin cambio de comportamiento): los casos de uso
  de egress de `/run` y `/resume` pasan del adaptador de hooks a
  `NetworkManager` (`on_run`/`on_resume`); el estado instalado (política,
  plan, slot, guardia DNS) vive en `rayd-core` (`Installation`) y el
  veredicto de verificación se decide con comprobaciones puras
  (`VerifyFailure`); una sola clasificación de direcciones especiales
  (`SpecialAddress`) sirve a la guardia SSRF de transferencias, a las dos
  guardias del proxy y a la sonda; el mapeo de `NetworkError` a código gRPC
  es exhaustivo (`status_class`); la tabla y prioridad de IMDS son
  constantes tipadas con su invariante de orden; `ConnectFailure` y sus
  respuestas HTTP/SOCKS pasan a `rayd-core`.
- Transferencias (refactor interno, sin cambio de comportamiento): toda
  mutación del registro pasa por un único punto que refresca el contador de
  la barrera de lectura tras subida; importación y exportación comparten el
  cierre de la tarea (`TaskEnding`); el presupuesto de reintentos de una
  petición a S3 vive en `rayd-core` (`RequestRetries`); el plan de una
  exportación lleva la URL de cada parte y un `UploadPart` correcto lleva
  siempre su `ETag` (`PutOutcome::PartStored`).

### Security

- Imagen: las dos capas `pip install -r` de `image/Dockerfile` (el stack
  científico principal y la variante `poly`) llevan ahora `--require-hashes
  --no-deps --only-binary=:all:`. `kernel-sidecar/requirements.txt` y
  `requirements-poly.txt` se regeneraron con `uv pip compile
  --generate-hashes` sin cambiar ninguna versión (diff de `nombre==versión`
  vacío); un fichero añadido a una release ya publicada, o un sdist que
  compilase en la VM de build, se rechazan en vez de instalarse en
  silencio. Cierra C-12 (`docs/SECURITY_AUDIT.md`); gate nuevo en
  `scripts/check_pins.py` (puerta 5). Sin cambio de comportamiento en
  tiempo de ejecución.

### Fixed

- CI: el job x86_64 (`check`) vuelve a correr la suite completa del
  workspace, incluidas las suites de integración de `rayd`. El runner no se
  congelaba por `rayd`: `a_stalled_second_subscriber_is_truncated_alone`
  (`tests/m5_pty.rs`) buscaba el final de 3 MB de salida reescaneando todo
  el buffer en cada trozo (cuadrático, ~30 s en debug en x86 frente a un
  plazo de 30 s) y, al vencer, imprimía el buffer entero en el panic: una
  línea de ~2.9 MB en el log que dejaba mudo al runner (ni `timeout` ni la
  cancelación respondían). La búsqueda ahora es lineal (1-2 s) y los panics
  muestran sólo la longitud y los últimos 512 bytes. Investigación en
  [`docs/research/2026-10-ci-x86-freeze.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-ci-x86-freeze.md).

## [0.3.2] - 2026-09-29

### Fixed

- Imagen: `e2b/data` convierte los valores ausentes (`None`, `NaN`, `NaT`,
  `pd.NA`) en `null`. Un `DataFrame` con un `NaT` junto a otras columnas
  hacía fallar la celda entera (`NaTType does not support strftime`), y con
  pandas 3 un texto ausente salía como `'nan'`.

- Con la política de egress en deny-all (imagen `rayito-base-caps`), el DNS
  (puerto 53, UDP y TCP) queda bloqueado para uid >= 1000 con reglas
  `ip rule` instaladas de forma atómica y con rollback (egress, opción A de
  ADR-012): antes los nombres se resolvían aunque toda conexión fallara.
- `/run` marca la rotación del kernel de forma síncrona, así que una sonda de
  readiness durante la restauración ya no ve el contexto anterior como listo.
- Los hijos de `rayd` (procesos, shell PTY, sidecar) restauran todas las
  señales a `SIG_DFL` y vacían la máscara antes de `exec`: un `SIGHUP`
  ignorado por el padre ya no impide que el job en primer plano muera con la
  shell.
- `ci.yml` (job `build`) y `release.yml` (job `rayd`) compilan `rayd` con el
  mismo `--remap-path-prefix` que `make build` (`REMAP_CONFIG` del
  Makefile): el binario publicado ya no incrusta las rutas del runner
  (`/home/runner/...`) de `CARGO_HOME` ni del directorio de compilación. Un
  paso nuevo en ambos jobs falla el build si `strings` encuentra
  `/home/runner` en el binario.

## [0.3.1] - 2026-09-28

Versión de mantenimiento, sin cambios en el contrato gRPC.

### Changed

- Dependencias de Cargo actualizadas (versiones menores y de parche).
- Los tests generan sus certificados TLS con `rcgen`: el repositorio ya no
  contiene claves privadas de prueba.
- Tests de descriptores de la PTY sin la carrera de bash (`$(...)`) que los
  hacía intermitentes.

## [0.3.0] - 2026-09-24

`rayd` 0.3.0 (M9). Todos los cambios del contrato (`proto/rayito/v1/`) son
aditivos y compatibles con `buf breaking` (FILE): un SDK 0.2 sigue hablando
con este agente. Los SDK 0.3 necesitan este agente para las features de M9.

### Added

- **`FilesystemService`: transferencias por URLs prefirmadas**
  (`m9-file-transfer`, ADR-010): `StartImport`, `StartExport`,
  `GetTransfer`, `WatchTransfer` y `CancelTransfer`; `rayd` mueve bytes con
  un cliente HTTPS sin credenciales (`HyperSignedHttp`) sólo hacia URLs cuyo
  host y ruta son los del objeto nombrado (`transfer::url_policy`), con un
  resolvedor que descarta loopback, link-local e IMDS; barrera de lectura
  tras subida; `Write`/`Read` con gzip opcional (`rayito-compress: gzip`) y
  metadatos por fichero como xattrs `user.rayito.*`.
- **`LifecycleService.SetTimeout`** y `HealthResponse.lifecycle` (campo 12)
  (`m9-server-timeout`, ADR-011): el plazo lógico del bloque `lifecycle` del
  `runHookPayload`, vigilado por el hilo `rayd-timeout`; al vencer en modo
  `kill`, streams cerrados con `sandbox_timeout`, `SIGTERM`/`SIGKILL` a
  todos los grupos y salida con código 124; la capa `timeout_gate` responde
  `FAILED_PRECONDITION sandbox_timeout` a todo RPC salvo `Health` y
  `SetTimeout`.
- **`HealthService.MetricsHistory`**, `MetricsResponse.mem_cache_bytes` y
  `HealthResponse.cpu_count`/`memory_total_bytes` (campos 14 y 15)
  (`m9-sandbox-observability`): muestreo procfs cada 5 s sólo en
  `running`/`resumed`, anillo de 5 760 muestras.
- **`NetworkService`** (`UpdateNetwork`, `GetNetwork`) y
  `HealthResponse.egress_enforcement` (campo 13) (`m9-egress-policy`,
  ADR-012): política de egress en el guest con `CAP_NET_ADMIN` (tablas de
  rutas por `uidrange 1000-65535`, cambio atómico, verificación), proxy local
  HTTP CONNECT/SOCKS5 en `127.0.0.1` con guardia contra IMDS y las
  direcciones propias del guest, cadena al SOCKS5 del operador, deny-all en
  `/run` con `network.enforce` y re-verificación en `/resume`.
- `language = "typescript"` en `CodeService` (`m9-deno-kernels`): el
  catálogo del agente conoce los cuatro nombres; en una imagen sin el kernel,
  `UNIMPLEMENTED` nombrando `rayito-base-poly`.

### Fixed

- **Un `Write` en un directorio observado llega como `WRITE`**
  (`m9-e2b-v2-surface`): el rename del temporal `.rayito-tmp-*` sobre el
  destino se emparejaba como un `RENAME` sin `entry`, y el código portado
  de E2B que espera `WRITE` tras `files.write` (el ejemplo de
  docs.e2b.dev) no veía nada. `WatchTranslator` empareja las dos mitades
  del rename por su cookie de inotify (`RawWatchEvent.cookie`) y emite un
  único `WRITE` del destino, con `entry` si se pidió `include_entry`. Los
  `mv`/`files.rename` corrientes siguen siendo `RENAME`.
- **El plazo en milisegundos ya no oscila**: `deadline_unix_ms` y
  `cap_unix_ms` (en `Health.lifecycle` y en el detalle de `SetTimeout` más
  allá del tope) se calculaban redondeando por separado el reloj de pared y
  el monotónico, y dos lecturas seguidas podían diferir en 1 ms. Ahora la
  suma se hace en nanosegundos y se redondea una sola vez hacia abajo
  (también antes de la época).
- **`Health` no dice `agent_ready` mientras se instala el deny-all de
  `/run`** (`egress_settling`): la plataforma deja pasar `Health` antes del
  200 de `/run`, y con `network.enforce` el SDK podía dar por listo un
  sandbox cuya política aún no estaba aplicada (el primer `ip` de un guest
  recién restaurado tarda segundos). `agent_ready` espera a que la política
  quede publicada, verificada o no; una imagen sin `CAP_NET_ADMIN` queda
  lista tras `/run` como siempre.
- **La salida por el plazo no cuenta como reinicio del sidecar**: el sidecar
  y los kernels parados para la salida con código 124 (o cualquier apagado)
  no suben `sidecar_restarts` ni se registran como caída.
- **Egress en un guest recién creado**: `ip route flush|show table <T>`
  responde "FIB table does not exist" (salida 2) si la tabla de la política
  nunca se creó; ahora se trata como una tabla vacía en vez de como un fallo
  que hacía fracasar la instalación de la primera política. `ip` nunca se registra: su stderr sólo se
  lee para reconocer esa frase.
- **Una sola gracia de reanudación por plazo, nunca más allá del tope**
  (`m9-server-timeout`, revisión independiente): el hilo vigilante toma
  cualquier hueco ≥ 2 s entre dos ticks por una congelación, y dejarlo sin
  CPU desde dentro del sandbox lo imita; seguido de un `/suspend` +
  `/resume` forjado, la gracia de 30 s podía reabrirse una y otra vez y, en
  modo `kill`, pasar del tope. Ahora cada gracia acaba como mucho en el
  tope (ninguna se abre en él o después) y un plazo abre una sola; sólo un
  plazo que se mueve (`SetTimeout`, que exige el token, o la regla de
  auto-resume de 5 min, que sigue igual) vuelve a tenerla.
- **El proxy de egress reescribe `Host` en la forma absoluta `http://`**
  (`m9-egress-policy`): conservaba el `Host` del cliente, así que un nombre
  permitido podía servir de fachada para otro host virtual de la misma IP.
  Ahora el `Host` es la autoridad comprobada (RFC 9112 §3.2.2) y las líneas
  plegadas (obs-fold) se rechazan con `400`. `CONNECT` y SOCKS5 no
  inspeccionan TLS: el SNI sigue siendo del cliente (residual en T17).
- **Las direcciones IPv4-compatibles (`::a.b.c.d`) se juzgan como IPv4** en
  la guardia y la política del proxy (`canonical_ip`, con
  `Ipv6Addr::to_ipv4` como la política de URLs de transferencia): antes
  sólo `::ffff:a.b.c.d`, y `::169.254.169.254` o `::127.0.0.1` pasaban la
  guardia. `::` y `::1` siguen siendo IPv6.
- **`Health` ya no puede quedarse sin `agent_ready` para siempre**: si la
  tarea del deny-all de `/run` entraba en pánico, `egress_settling` no se
  limpiaba nunca; ahora lo limpia un guardián al terminar la tarea de
  cualquier modo. Además cada invocación de `ip` tiene un tope de 5 s
  (`IP_COMMAND_TIMEOUT`, por encima de los 2,76 s medidos para el primer
  `ip` tras un restore): pasado, el hijo se mata y se recoge y la llamada
  falla como `timed out`, en vez de retener el lock del gestor de egress.

### Dependencies

- **`zlib-rs` aparece en `Cargo.lock` y en el SBOM, pero no se enlaza**
  (`m9-file-transfer`, ADR-010): la feature `gzip` de `tonic` 0.14.6 pide
  `flate2` con sus features por defecto, y la feature débil
  `runtime_detection` (`zlib-rs?/std`) basta para que Cargo anote la
  dependencia opcional `zlib-rs` en el lockfile aunque nadie la active. El
  workspace ya fija `flate2` con `default-features = false` y
  `rust_backend`, pero las features son aditivas y no pueden quitar el
  `default` que pide `tonic`, así que el lock no cambia. El binario enlaza
  sólo `miniz_oxide` (`cargo tree -i zlib-rs` no encuentra la crate en el
  grafo resuelto).

### Security

- **`user=` sólo acepta cuentas sin privilegio** (auditoría interna, fila
  C-05): `UserPolicy::authorize_identity` era una lista negra de uid 0, así
  que un alias de uid 0, una cuenta de sistema con uid < 1000 —fuera del
  `uidrange 1000-65535` con el que M6 agujerea IMDS— o cualquier miembro del
  grupo `root` pasaban la puerta. Ahora la comprobación es positiva
  (`uid >= 1000 && gid >= 1000` y sin el grupo 0) y devuelve
  `PrivilegedAccount`. La puerta duplicada de `persistence` desaparece: ese
  camino llama a la misma política con el opt-in de root retirado
  (`UserPolicy::without_root`).
- **Ningún proceso de usuario hereda descriptores más allá de 0/1/2**:
  `openpty` devuelve master y slave heredables y `rayd` sólo los marcaba
  close-on-exec unas instrucciones después; un spawn concurrente (otro PTY,
  un proceso o el sidecar) que hiciera `fork` en esa ventana entregaba el
  master o el slave de otra terminal a la shell o al proceso del usuario
  (visto en la CI aarch64 de GitHub: la shell listaba `0 1 142 145 2 255
  3`). Ahora el `PreExecPlan` común a procesos, shells PTY y sidecar marca
  close-on-exec todo descriptor >= 3 en el hijo justo antes de `exec`
  (`close_range(3, ~0U, CLOSE_RANGE_CLOEXEC)`, con un bucle acotado de
  `fcntl(F_SETFD)` hasta el `NOFILE` blando, máximo 4096, si el kernel es
  anterior a 5.11), después de que `std` haya colocado stdio en 0/1/2. Cubre
  también los descriptores que `rayd` hereda de su padre. El harness de
  tests deja de sellarlos por su cuenta.

## [0.2.0] - 2026-09-17

Agente de M7, aceptado contra AWS real (`MILESTONES.md`).

### Added

- `FilesystemService.Checkpoint` y `Restore` (`m7-s3-persistence`, ADR-009):
  tar.gz del `HOME` del usuario (lista de exclusión fija, `exclude` ≤ 64,
  lectura bajo `FsIdentityGuard`) subido a `s3://<bucket>/<key_prefix>/`
  (`home.tar.gz` en partes de 8 MiB + `manifest.json` v1) y restaurado sin
  salir del `HOME` (sólo regular/dir/symlink, modo `0o777`, sha256
  verificado), con las credenciales IMDSv2 del execution role como root
  (`--persistence-credentials imds|default`, `imds` por defecto), una
  operación por sandbox (`FAILED_PRECONDITION`), `progress` ≤ 1/s,
  `KeepAlive` cada 30 s y cierre `suspending` en `/suspend` con abort del
  multipart. Nuevo módulo `rayd-core::persistence`, adaptadores
  `S3ObjectStore` (`aws-sdk-s3` 1.148.0 + `aws-config` 1.12.0, `rustls` +
  `aws-lc-rs` compilados con `zig cc`) y `TarHomeArchiver` (`tar` 0.4.46 +
  `flate2` 1.1.10 `rust_backend`). Binario ARM64 musl auditable:
  4 700 984 → 12 524 384 B; build limpio 106 → 222 s.
- `ExecuteRequest.language` (`optional string`, campo 5; `m7-poly-kernels`):
  `python`, `bash` o `javascript` seleccionan el contexto por defecto de ese
  kernel (`default`, `default-<language>`), que `rayd` crea perezosamente en
  la primera celda bajo un lock por lenguaje (nunca antes de `/run`; cuenta
  para el tope de 8; `DestroyContext` permitido y recreación en la siguiente
  celda). `CreateContextRequest.language` acepta los tres nombres. Hoy sólo
  `rayito-base-poly` trae el kernel `bash`; `javascript` es un nombre
  reservado que ninguna imagen trae (`AWS_API_NOTES.md` Q57). Nuevos
  estados: `INVALID_ARGUMENT` para `language` junto a `context_id` y para
  `envs` por ejecución en un contexto no Python; `UNIMPLEMENTED` (mensaje
  con `rayito-base-poly`) para un lenguaje conocido que la imagen no trae,
  según `ready.languages` del sidecar.
- Protocolo del sidecar (v1, aditivo): `language` en `create_context`,
  `languages` en `ready` (ausente = sólo Python) y `skipped` en la respuesta
  de `reseed` (contextos no Python, logueado como cuarto contador).
  `ListContexts` informa el lenguaje real de cada contexto.

### Changed

- Licencia MIT → Apache-2.0 (`license = "Apache-2.0"` en
  `[workspace.package]`, heredada por los tres crates; `LICENSE` junto al
  crate).

### Compatibilidad

- `Health.agent_version` es `0.2.0`. Los SDKs 0.2.0 (Python y TypeScript)
  exigen `agent_version` ≥ 0.2.0 (`Checkpoint`/`Restore` y
  `ExecuteRequest.language`): `rayito doctor` lo evalúa con la tabla de
  `docs/site/docs/limits.md`; sobre un `rayd` 0.1.0 `persist=` responde
  `UNIMPLEMENTED` y `language=` se ignora. El número de versión de imagen
  sigue siendo un contador de builds por cuenta (la aceptación de M7 corrió
  sobre las versiones anotadas en `MILESTONES.md`).

## [0.1.0] - 2026-09-16

Primer agente completo, aceptado contra AWS real en M1-M6 (`MILESTONES.md`).
Un proceso por MicroVM, como root, estático musl, con gRPC h2c (`tonic`) en
`:8080` y los hooks de Lambda (`axum`, HTTP/1.1) en `:9000`, nunca en
`allowedPorts`.

### Added

- **M1 — arranque y readiness**: `HealthService.Health` (el único RPC sin
  `x-access-token`: `agent_ready`, `kernel_ready`, `agent_version`,
  `uptime`, `sandbox_id`) y `Metrics`; hooks `/ready`, `/validate`, `/run`,
  `/terminate` bajo `/aws/lambda-microvms/runtime/v1/`; el secreto del
  sandbox llega como hash en `runHookPayload` y se compara en tiempo
  constante (`zeroize`).
- **M2 — procesos**: `ProcessService` (`Start`, `Connect(from_seq)`,
  `SendInput`, `CloseStdin`, `SendSignal`, `List`) con usuario por defecto
  uid 1000, entorno construido desde cero, `setrlimit`, grupos de procesos
  + `killpg`, canales de salida acotados con `output_truncated`, máximo 256
  procesos/PTYs; `HealthService.Metrics` desde procfs.
- **M3 — ficheros**: `FilesystemService` (`Read` en stream, `Write` en
  stream, `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir`) con
  lista de denegación sobre la ruta canónica, `..` rechazado,
  `setfsuid`/`setfsgid` por operación, `O_NOFOLLOW`, escrituras a temporal
  con `fchown`.
- **M4 — código**: `CodeService` (`CreateContext`, `Execute`, `Reattach`,
  `ListContexts`, `DestroyContext`, `RestartContext`) sobre el kernel
  sidecar Python (hijo de `rayd`, JSON lines por stdio; ADR-002): reinicio
  del sidecar con backoff, `/run` reinicia el kernel por defecto (clave HMAC
  y semillas nuevas por sandbox), máximo 8 kernels, `result` > 12 MiB
  recortado.
- **M5 — PTY y suspend/resume**: `PtyService` (`Create`, `Connect`,
  `SendInput`, `Resize`, `Kill`) con `openpty` + `setsid` + `TIOCSCTTY` como
  uid 1000 (ADR-005); hooks `/suspend` (cierra streams con `suspending`,
  `quiesce` + `sync`, no destruye nada) y `/resume` (`resume_generation`,
  sonda de kernels, reseed de `random`/`numpy.random`,
  `kernel_state_lost`); deadlines que excluyen el tiempo suspendido
  (`clock_offset_ms`).
- **M6 — endurecimiento**: bloqueo de IMDS para uid 1000-65535 en la
  variante `rayito-base-caps` (`Health.imds_blocked`), auditoría de hooks y
  watchdog de `/suspend` estancado (`Health.hook_anomalies`), `RLIMIT_CPU`
  opcional por proceso (`limits.cpu_seconds` del payload), presupuesto de
  salida de 128 MiB por sandbox, reserva de disco de 256 MiB
  (`disk_full`), `metadata` del payload devuelto en `Health`.

### Compatibilidad

- `Health.agent_version` es `0.1.0` (el `CARGO_PKG_VERSION` del crate). Los
  SDKs 0.1.0 (Python) y 0.0.5 (TypeScript) exigen una imagen `rayito-base`
  **≥ 10.0** (la primera con este `rayd` y el sidecar de M5); la
  superficie de M6 (`imds_blocked`, `hook_anomalies`, `metadata`,
  `cpu_seconds`, `disk_full`) requiere la imagen construida de este tag (la
  aceptación corrió sobre `rayito-base` 16.0 y `rayito-base-caps` 6.0).
- Sin cambio de imagen `agent_version` no cambia: las versiones de imagen
  son números de build opacos de AWS, anotados en `MILESTONES.md`. Esos
  números (10.0, 16.0, 6.0) son los de la cuenta del mantenedor: el contador
  es por imagen y por cuenta, y el criterio de compatibilidad que aplica
  `rayito doctor` es sólo `agent_version`.

## [0.0.1] - [0.0.5]

Builds internos de los hitos M1-M5, publicados sólo como versiones de imagen
de la cuenta de desarrollo.

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.2.0...HEAD
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.1.0...rayd-v0.2.0
[0.1.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/rayd-v0.1.0
