# Changelog

Todos los cambios notables del agente `rayd` (`crates/rayd`, con
`crates/rayd-core` y `crates/rayito-proto`, que comparten versión por
`[workspace.package]`). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/). `rayd` no se publica en crates.io: se
distribuye como binario estático `aarch64-unknown-linux-musl` dentro de la
imagen `rayito-base` y como asset de la GitHub Release del tag `rayd-v*`.

## [Unreleased]

## [0.7.0] - 2026-10-05

### Added

- **Volúmenes EFS** (`m15-efs-volumes`, ADR-018, experimental):
  `rayd_core::volume` (`VolumeSpec`/`VolumePlan`/`MountState`/`VolumeError`,
  la atribución de `efs-proxy` y la decisión de `/resume`, todo puro) y el
  adaptador real `EfsUtilsMounter` (`mount -t efs -o tls,iam,accesspoint`,
  medido en AWS real, `AWS_API_NOTES.md` §16 Q128), activo sólo en una
  imagen con `amazon-efs-utils` y `CAP_SYS_ADMIN` (en las demás
  `Health.features.efs_volumes` es `false` y la sección responde
  `UNSUPPORTED`). Monta sobre un directorio de root y enlaza el resultado a
  la ruta pedida sin seguir enlaces simbólicos (recorrido compartido con
  `mounts=`, `adapters::mountpoint`); termina el `efs-proxy` de cada volumen
  al desmontar y en `/terminate` (`umount` no lo para, Q128); en `/resume`
  remonta el volumen cuya pausa cruzó la caducidad de las credenciales del
  túnel o cuya sonda falla (en segundo plano si no cabe en el presupuesto,
  con el estado en `ConfigureStatus`; Q129); en `/suspend` vacía cada
  volumen con plazo y lo marca `degraded`/`flush_timeout` si no termina
  (Q130). `proto/rayito/v1/efs_volumes.proto` documenta las clases nuevas
  (`invalid_path`, `credentials_expired`, `flush_timeout`, `stale`,
  `unreachable`, `gone`).

### Changed

- `EfsUtilsMounter` pasa `AWS_REGION` (la región del MicroVM) al entorno de
  `mount -t efs`: `amazon-efs-utils` 3.1.3 la lee antes que su
  `efs-utils.conf`, así que una imagen sin región horneada monta en
  cualquier región. Sin `AWS_REGION`, el helper sigue con sus propios
  respaldos.
- `image/Dockerfile`: capa condicional de
  `amazon-efs-utils-3.1.3-1.amzn2023` (sólo con el marcador `efs_variant` de
  `--with-efs`), que rehace el enlace de `/usr/bin/python3` a 3.12;
  `scripts/check_pins.py` clava su NEVRA y ve los `dnf install` dentro de
  `if …; then`.

### Security

- **Las operaciones de ficheros ya no vuelven a resolver la ruta que la
  lista de denegación comprobó** (`sec-rayd-agent-hardening`, RAYD-01):
  cada RPC de `FilesystemService` (y la exportación e importación por S3)
  abre el directorio padre componente a componente sin seguir enlaces
  simbólicos y actúa sobre ese descriptor, así que un componente que el
  código del sandbox cambie entre la comprobación y el uso se rechaza con
  la denegación de la política en vez de seguirse; un directorio en `proc`,
  `sysfs` o `devpts` se rechaza llegue por donde llegue. El borrado
  recursivo y las escrituras atómicas trabajan también por descriptor.
- **La pasarela de secretos valida la ruta con una lista de permitidos**
  (RAYD-02): en vez de rechazar unas cuantas codificaciones conocidas,
  `rayd` sólo deja pasar caracteres de ruta de RFC 3986 salvo `;`, y cada
  segmento decodificado una sola vez debe ser UTF-8 válido, sin `/`, `\`,
  `%`, `;` ni bytes de control y distinto de `.`/`..`. Cierra el uso de la
  credencial inyectada fuera de `allow` a través de un upstream que quite
  parámetros `;` o decodifique dos veces. Una ruta de `allow` que ninguna
  petición pueda cumplir se rechaza en `Configure` (`invalid_allow_path`).
- **Los lanzadores internos ya no heredan el entorno de `rayd`** (RAYD-04):
  la sonda de disponibilidad de los montajes S3 (uid 1000) arranca con un
  entorno con sólo `PATH`, un binario absoluto, el sellado de descriptores
  y el restablecimiento de señales que ya tenían los procesos del usuario;
  `mount-s3` gana el mismo sellado por encima de su descriptor FUSE.
- **Un `/run` desde un proceso del sandbox ya no consume el `/run` del
  arranque** (RAYD-08): `rayd` lee el uid dueño del socket del llamante y
  rechaza (200 `sandbox_origin`, contado en `hook_anomalies`) un `/run`
  de un uid del sandbox, así que el `start_cmd` de una plantilla, que
  corre antes de que llegue el `/run` de la plataforma, no puede instalar
  su propio token ni dejar el sandbox sin él. Pendiente de aceptación en
  AWS real.
- **Documentado el alcance de la reescritura de `Host` del proxy de
  egress** (RAYD-06): sólo cubre la forma absoluta `http://`; un túnel
  `CONNECT`/SOCKS5 a un nombre permitido en el 80 comparte el riesgo
  residual de IPs compartidas que ya tenía el 443 (`SECURITY.md` T17).
- **Los puertos de `rayd` tienen un tope de conexiones**: el gRPC (8080)
  atiende como mucho 256 conexiones a la vez y los hooks (9000) 32; las
  demás esperan en el backlog del kernel sin ocupar un descriptor, así que
  un proceso del sandbox ya no puede agotar los 1024 descriptores de
  `rayd` abriendo conexiones ociosas. Los hooks cortan una cabecera que no
  llega en 10 s y cierran la conexión tras cada respuesta, y un `accept`
  que falla por falta de descriptores espera 100 ms en vez de reintentar
  en bucle (`SECURITY.md` T7). Pendiente de aceptación en AWS real.
- **`rayd` comprueba los pids de kernel que le reporta el sidecar antes de
  señalarlos**: un proceso del sandbox puede escribir en la tubería del
  protocolo del sidecar, así que `rayd` (root) sólo registra un kernel que
  `/proc` confirma como hijo del sidecar en curso, líder de su grupo y del
  mismo usuario, lo fija por su hora de arranque y lo vuelve a comprobar
  antes de cada `killpg`, que nunca va a los grupos 0 y 1 ni al suyo
  propio. Un pid que no pasa se descarta (`kernel_pid_rejected`)
  (`SECURITY.md` T12).
- **La puerta de identidad tiene techo**: un proceso, PTY, operación de
  ficheros o kernel sólo corre con uid y gid entre 1000 y 65535, el mismo
  rango que cubren el bloqueo de IMDS y las reglas de egress y DNS; una
  cuenta por encima de 65535 en una imagen propia se rechaza como cuenta
  con privilegios (`SECURITY.md` T1).
- **Los hooks de ciclo de vida comprueban quién abrió la conexión**
  (`sec-sandbox-isolation`, C-01): `rayd` busca el extremo cliente de cada
  conexión al puerto de hooks en las tablas de sockets del kernel. Un
  `/terminate` o un `/validate` que llega desde un proceso del sandbox
  (uid 1000-65535) responde 200 `peer_refused` sin hacer nada y cuenta en
  `hook_anomalies`; un `/suspend` o un `/resume` desde el sandbox se sigue
  aceptando pero también cuenta. Ningún hook responde nunca un no-2xx por
  esta comprobación.
- **`/validate` y `/ready` ya no actúan tras el `/run`** (C-02, C-03): un
  `/validate` posterior al `/run` responde 200 `validate_skipped` sin
  reiniciar el contexto `default` del kernel ni ejecutar la celda de
  validación, y tanto él como un `/ready` tardío quedan en `hook_audit` como
  anomalía. Las llamadas del build, antes del `/run`, no cambian.
  Además, el camino de ejecución propio del build (el que se salta el
  `stream_gate`) se niega por sí solo fuera de la fase de build, aunque se
  llegue a él sin pasar por el hook.
- **`Checkpoint` y `Restore` quedan dentro del ámbito que liga el `/run`**
  (C-07): si el `runHookPayload` trae el bloque `persist` (bucket y base del
  prefijo, lo manda el SDK desde `create(persist=)`), `rayd` responde
  `PERMISSION_DENIED`, antes de tocar S3, a cualquier destino de otro bucket
  o fuera de esa base. Un prefijo por inquilino separa así inquilinos que
  comparten execution role. Sin el bloque nada cambia.
- **La pasarela de secretos no devuelve la credencial en las cabeceras de la
  respuesta**: se eliminan las cabeceras de la respuesta del `upstream` que
  llevan el nombre de una cabecera inyectada o contienen un valor vaultado.
  El cuerpo sigue llegando sin cambios, así que la documentación (T24 y la
  página de la pasarela) avisa de no permitir endpoints que reflejen las
  cabeceras de la petición.
- **Los assets firmados de `rayd` se construyen sin credenciales y no se
  pueden reemplazar desde la release.** `release.yml` parte el job `rayd` en
  `rayd-build` (sólo lectura), `rayd-sign` (token OIDC, sin checkout ni
  herramientas de build, firma lo que `sha256sum -c` confirma que salió del
  build) y `rayd-upload` (environment `release` con aprobación del
  mantenedor, sin `--clobber`). Ningún job de la release restaura ya una
  caché de Actions: zig se descarga contra un sha256 fijado y las
  herramientas de cargo se compilan en una raíz nueva en cada release.
  Publicar exige que el commit del tag esté en `main`.
- **La receta de verificación liga la firma a la versión que instalas**:
  `cosign verify-blob --certificate-identity
  "…/release.yml@refs/tags/rayd-v${RAYD_VERSION}"` en vez de una regexp que
  aceptaba la firma de cualquier tag `rayd-v*` (`docs/site/docs/verify.md`).
- **Firmar `rayd` espera la aprobación del mantenedor.** `rayd-sign` corre en
  el environment `release` y sólo cuando el run publica: sin aprobación no
  hay token OIDC ni certificado de Sigstore con la identidad
  `release.yml@refs/tags/rayd-v<versión>`, y un ensayo (`dry_run`) ya no
  firma. Antes, un ensayo lanzado desde un tag `rayd-v*` firmaba con la
  identidad exacta que verifican los usuarios sin pasar por ningún revisor.
  `verify.md` dice ahora qué prueba la firma y qué no.
- **La receta recomendada de `rayito-image.zip` verifica antes de
  publicar**: descarga `SHA256SUMS`, comprueba el bundle con
  `cosign verify-blob` y la suma, y sólo después llama a
  `rayito image publish` (`docs/site/docs/primeros-pasos/configurar-aws.md`).
  Ya no es un paso opcional: un asset de una release se podría reemplazar
  con un token del repositorio.
- **CI sin cachés donde hay credenciales y con herramientas fijadas**: los
  jobs del sitio de documentación y del e2e ya no restauran cachés de
  Actions; uv se instala fijado por versión y sha256 en todos los workflows
  y nunca re-bloquea un `uv.lock` desfasado; pip-audit y cfn-lint salen de
  requisitos con `--hash` en vez de `uvx`.

## [0.6.1] - 2026-10-04

### Fixed

- **`rayd` (PID 1) recoge los zombis huérfanos** (`rayd-orphan-reaper`,
  Q80): un demonio con doble `fork` (o `mount-s3` en modo daemon) ya no deja
  procesos `<defunct>` colgados de `rayd` durante toda la vida del sandbox.
  Todo hijo de `rayd` (procesos, PTYs, el sidecar de kernels, `mount-s3`,
  sondas internas) se lanza por un único `ChildRegistry` por proceso que lo
  registra con su pid y su hora de arranque antes de que ninguna pasada de
  recogida pueda verlo; el reaper, en cada `SIGCHLD` y cada 5 s, sólo hace
  `waitpid` sobre los zombis reasignados a `rayd` que no son suyos, así que
  los códigos de salida de comandos, PTYs, kernels y `mount-s3` nunca se
  pierden. Sólo se activa como PID 1 (o *child subreaper*). `clippy.toml`
  prohíbe lanzar procesos fuera de `ChildRegistry::spawn`.

## [0.6.0] - 2026-10-03

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
- **`s3_mounts` feature slot (`m15-s3-mounts`, ADR-017)**: `rayd_core::s3_mount`
  (`S3Mount`, `MountErrorClass` incl. `InvalidPath`,
  `validate_mounts`/`parse_allowed_buckets`, ports `FuseDevice`/`FuseDaemon`)
  and `rayd_core::mount_path` (the absolute/canonical/allowed-roots/
  no-overlap/max-count check, shared with a future `efs_volumes`, re-run
  here before anything else so a non-SDK or buggy client can never steer
  `mount(2)` outside `/mnt/`/`/home/user/`). The Linux adapters
  (`adapters::fuse_device`: the mountpoint is walked from `/` one
  component at a time with `O_PATH|O_DIRECTORY|O_NOFOLLOW` (missing ones
  created with `mkdirat`), any symlink is `invalid_path`, and `mount(2)`/
  `umount2(MNT_DETACH|UMOUNT_NOFOLLOW)` only ever see `/proc/self/fd/<n>`,
  so uid 1000 can never redirect a root mount onto a system directory by
  swapping its own folder for a symlink; `mount(2)` with `allow_other` plus a real
  `probe_ready` (a bounded, killable `stat` subprocess as the guest uid);
  `adapters::mount_s3`: `mount-s3 --allow-other --uid 1000 --gid 1000` (without
  `--allow-other` Mountpoint answers only its own uid: every guest access
  was `EACCES`, AWS_API_NOTES.md Q101) run as the
  dedicated `rayito-mount` user, uid 990, with a from-scratch environment
  — no credential ever in argv or env, SEC-3 — its pid registered in the
  shared `ChildRegistry`, its exit classified from its status and a
  stderr ring buffer drained for the daemon's whole life, so a chatty
  daemon never blocks on a full pipe, and classified from its *last*
  bytes, never logged) and a real
  `features::s3_mounts::S3MountsFeature`: `apply()` reports
  `SECTION_CODE_PENDING` immediately and settles each mount to
  `Mounted`/`Failed` in the background; a background watcher relaunches a
  dead daemon with a fresh FUSE attach and backoff, each relaunch as its
  own task and never on top of an attempt still `Pending`; `/resume`'s probe
  forces the same relaunch for a daemon that survived the snapshot but
  whose FUSE connection did not. `supported()`/`Health.features.s3_mounts`
  require `CAP_SYS_ADMIN` in rayd's effective set (only `rayito-base-caps`
  has it; the binary, `/dev/fuse` and the `rayito-mount` user alone ship in
  every variant) instead of a literal `true`. `Health.features.root_egress`
  is now derived generically from `FeatureSet` too (a new
  `ConfigurableFeature::root_egress_class()`, reported only for a
  `supported()` slot), so later features add nothing to `grpc/health.rs`. `s3_mounts.proto` now carries real fields (`S3Mount`,
  `S3MountState`, `S3MountPhase`). With no `S3MountsConfig` section sent,
  behaviour is unchanged from 0.5.x: no `/dev/fuse` open, no `mount-s3`
  spawn.
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
<!-- m15-events-webhooks -->
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
<!-- m15-rayd-otlp -->
- **Exportador OTLP/HTTP a CloudWatch** (`m15-rayd-otlp`, ADR-021):
  `rayd` exporta 7 gauges de CPU, memoria y disco cada `interval_s`
  (15-300 s) sobre el `telemetry_export` de `ConfigureSandbox`, firmado con
  SigV4 sobre el execution role o con una API key de CloudWatch Metrics
  empujada por el SDK; cola acotada con backoff y jitter por sandbox
  (`rayd_core::telemetry::Batcher`), participante de `/suspend` con un
  vaciado de hasta 2 s que nunca pierde puntos aunque el hook lo corte, y
  `/resume` (su `on_resume`, bajo el tope por participante de los hooks)
  rehace el pool de conexiones sin esperar envíos previos. Firma SigV4 con
  `aws-sigv4` (ya en `Cargo.lock`), corrigiendo el reloj del guest con la
  cabecera `Date` de AWS; persistencia y funciones 0.6 comparten un único
  proveedor de credenciales IMDS. Lee `traceparent` entrante y lo registra
  como `trace_id`/`span_id`. Un subconjunto vendido y mínimo de los tipos de
  OpenTelemetry (Apache-2.0, `crates/rayito-proto/vendor/opentelemetry/`).
  `Health.features.telemetry_export` (y `root_egress`) sólo son `true` con
  `AWS_REGION` conocido. Sin `telemetry=`, `rayd` no abre ninguna conexión
  nueva: comportamiento idéntico a 0.5.x.
<!-- m15-templates -->
- **Templates declarativos** (`m15-templates`, ADR-022):
  `rayd_core::template` (`StartSpec`, `ReadyPoll`, `ready_decision`,
  `rayito.template/1`) y el slot `template_start`: al arrancar, `rayd` lee
  `/etc/rayito/template.json` (`adapters/fs_template_spec.rs`; ausente o
  inválido = arranque de 0.5.x) y, si existe, antes del `/ready` del build lanza
  `start_cmd` como proceso gestionado (visible en `commands.list`) y
  sondea `ready_cmd` con `/bin/sh -c` (`adapters/shell_ready_probe.rs`);
  `/ready` responde 503 hasta que `ready_cmd` sale con 0 y falla al
  agotarse su plazo. `Health.features.template_start` pasa a `true`.
<!-- m15-secrets-gateway -->
- **Pasarela de secretos en loopback** (`m15-secrets-gateway`, M15, ADR-023):
  el slot `secret_gateway` deja de ser `Unsupported`. Un listener `axum`
  por ruta de `SecretGatewayConfig` (`rayd::secret_gateway`), con
  allowlist de método/ruta y límite de peticiones por minuto (cubo de
  tokens entero y determinista, `rayd_core::secret_gateway`) antes de
  reenviar al `upstream` fijo de la ruta por un cliente HTTPS compartido
  (`GatewayUpstream`: raíz de confianza del SO, `FilteringResolver` para
  que un `upstream` nunca resuelva a loopback/link-local/IMDS, cuerpos en
  flujo sin bufferizar). Las cabeceras del guest con el mismo nombre que
  una vaultada se eliminan antes de inyectar el valor real (T24): el
  código del sandbox nunca puede leer ni suplantar su propio secreto. Una
  ruta de petición con segmentos `.`/`..` (también codificados), un `/` o
  `\` codificado, una barra invertida o un segmento vacío se rechaza (403)
  antes de la allowlist. Los nombres de cabecera se validan (token RFC
  9110, únicos sin distinguir mayúsculas, nunca `host`/`content-length`/
  hop-by-hop: `invalid_header_name`/`duplicate_header_name`), igual que el
  nombre de ruta (`invalid_route_name`) y el `upstream`
  (`invalid_upstream_host`). Un `Configure` que repite el nombre de una
  ruta conserva su listener y su puerto y sólo cambia su estado (la
  siguiente petición, también por una conexión keep-alive abierta, ya lo
  ve); una ruta retirada cierra sus conexiones keep-alive. Sólo un
  `CONNECT_TIMEOUT` (10 s) o `RESPONSE_HEAD_TIMEOUT` (600 s) es
  `upstream_timeout`; cualquier otro fallo tras conectar es
  `upstream_error`. `Health.features.secret_gateway` sale del `FeatureSet`
  construido: si el slot degrada a `Unsupported` (sin raíz de confianza),
  no se anuncia. Sin ningún `Configure` con `secret_gateway`, `rayd` no
  abre ningún socket de loopback para esta función.
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

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.7.0...HEAD
[0.7.0]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.6.1...rayd-v0.7.0
[0.6.1]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.6.0...rayd-v0.6.1
[0.6.0]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.5.1...rayd-v0.6.0
[0.5.1]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.5.0...rayd-v0.5.1
[0.5.0]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.4.0...rayd-v0.5.0
[0.4.0]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.3.3...rayd-v0.4.0
[0.3.3]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.3.2...rayd-v0.3.3
[0.3.2]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.3.1...rayd-v0.3.2
[0.3.1]: https://github.com/alejandro-cedeno-10/rayito/compare/rayd-v0.3.0...rayd-v0.3.1
[0.3.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/rayd-v0.3.0
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.2.0.md
[0.1.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.1.0.md
