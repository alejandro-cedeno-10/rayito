# Changelog

Todos los cambios notables del paquete `rayito` (SDK Python). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Added

- **Dominio propio** (`m15-custom-domain`, ADR-024, experimental y apagado
  por defecto): `CustomDomain`/`AsyncCustomDomain` despliegan una
  distribución CloudFront con alias comodín, una CloudFront Function de
  enrutado (`cloudfront-js-2.0`) y un KeyValueStore (`infra/
  custom-domain.yaml`, `rayito domain deploy|status|destroy`); `register`/
  `unregister`/`refresh` gestionan las rutas `{puerto}-{alias}.<tu
  dominio>` sin que la Function necesite confiar en nada que el viewer
  mande. `register()` exige `traffic_token` salvo `public=True` explícito
  — nunca hay una ruta pública por omisión — y reintenta/limpia sus dos
  escrituras encadenadas al KVS ante una carrera de `ETag`. Nuevo extra
  `rayito[custom-domain]` (`awscrt`): el plano de datos de
  `cloudfront-keyvaluestore` exige SigV4A pese a declarar
  `signatureVersion: v4` en su modelo. Sin instanciar `CustomDomain` no hay
  ningún cliente `cloudfront-keyvaluestore` ni `cloudformation`.
  `Sandbox.create(domain=)` sigue lanzando `UnimplementedError`, con un
  mensaje que ya nombra ese seguimiento en vez de `m15-custom-domain`: la
  integración con `get_host()`/`expose()` queda para un cambio posterior
  (ver `ARCHITECTURE.md` ADR-024). La CloudFront Function trata una ruta
  cuyo TTL (`register(ttl_seconds=)`/`refresh()`) ya pasó como si nunca
  hubiera existido (404), no sólo cuando caduca el JWE en sí (T25).
  `deploy()`/`destroy()` usan un timeout propio
  (`CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`, 30 min) en vez del genérico de
  `OptionalStacks`, porque CloudFront tarda bastante más en deshabilitar y
  borrar una distribución; `refresh()` ya no deshace una escritura previa
  si la segunda falla (sólo `register()` lo hace, al tratarse de claves
  nuevas). DOM-2/3/5/7/8/14 pendientes de la aceptación contra AWS real.

### Fixed

- **`reincarnate()` reaplica todas las secciones de `ConfigureSandbox`**
  (`m15-reincarnate-configure-replay`, sync y async): hasta ahora sólo
  reenviaba `gateways=` y un sucesor perdía `mounts=`, `events=` y
  `telemetry=`. `create()` guarda sus opciones 0.6 en
  `LaunchOptions.features` (`relaunch_features`: todas menos `size`, que ya
  va en el ARN de la imagen) y `reincarnate()` las reenvía campo a campo
  (`feature_kwargs`) al `create()` del sucesor, que las aplica por el mismo
  camino en su único `Configure`: `events=` deriva `k_sbx` del nuevo
  `sandbox_id`, `mounts=` espera otra vez a `mounted` y `telemetry=` usa la
  imagen y la memoria del sucesor. `LaunchOptions.gateways` pasa a
  `LaunchOptions.features.gateways` (atributo interno).

## [0.6.0] - 2026-10-03

### Added

- **Convenio `OptionalStack` y `ConfigureSandbox`** (`v06-foundations`,
  M15 foundations, ADR-015/ADR-016, opcional y apagado por defecto): el
  servicio `OptionalStacks` (`deploy`/`status`/`destroy`/`components`) y la
  CLI `rayito stack` sobre un catálogo de nueve componentes
  (`metadata-index` y `secrets-access`, migrados sin cambios de
  comportamiento; los otros siete son stubs hasta su propia función); las
  siete opciones 0.6 de `Sandbox.create()` (`mounts`, `volumes`, `size`,
  `events`, `telemetry`, `gateways`, `domain`) existen ya en la firma y
  lanzan `UnimplementedError` nombrando el cambio que las trae mientras
  sigan siendo un stub, antes de `run-microvm`; en cuanto una deja de
  serlo (`mounts=`, ver más abajo), `create()`/`take()` ejecutan sus
  `configure_sections` justo tras el primer `Health` (`_configure_base`:
  `require_capabilities`, `build_configure_request`,
  `check_configure_response`), dentro del mismo camino que ya termina el
  sandbox ante cualquier fallo anterior a `agent_ready`. Fila de
  compatibilidad "0.6" en `rayito.cli._compat.COMPATIBILITY`. Sin ninguna
  opción nueva, el comportamiento es byte a byte el de 0.5.x (traza de oro en
  `tests/unit/fixtures/zero_cost_0_5_trace.json`).
<!-- m15-s3-mounts -->
- **`mounts=` (`m15-s3-mounts`, ADR-017, experimental, apagado por
  defecto)**: `Sandbox.create(mounts=)`/`AsyncSandbox.create(mounts=)`
  monta uno o más buckets S3 (`S3Mount(bucket=, prefix=, read_only=True,
  allow_overwrite=False, allow_delete=False)`, exportado desde `rayito`)
  en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`
  (`require_caps_for` lo exige antes de `run-microvm` cuando la imagen se
  nombra directamente; sobre un ARN opaco la decisión se difiere a
  `Health.features` tras `/run`, que termina el sandbox si falta la
  capacidad). `create()` no vuelve hasta que cada montaje está montado:
  sondea `ConfigureStatus` (como mucho 15 s) y, si uno falla o no se
  asienta, termina el sandbox y lanza `MountException`. `sbx.mounts`
  (propiedad) da el estado en vivo de cada montaje (`"pending"`/`"mounted"`/`"failed"`, `ConfigureStatus` en cada
  lectura); una sección rechazada o un montaje fallido lanza
  `MountException` con un `code` cerrado (`network`, `iam_denied`,
  `not_found`, `not_allowed`, `invalid_path`, `helper_missing`,
  `timeout`). `rayd` lanza `mount-s3` como el usuario dedicado
  `rayito-mount` (uid 990) con credenciales resueltas por su propio
  acceso a IMDS, nunca en argv ni en entorno; un daemon caído se
  relanza solo, con backoff. Política IAM `RayitoS3MountAccess` del
  componente `OptionalStack` `s3-mounts` (`infra/s3-mounts.yaml`,
  desplegable con `rayito stack deploy s3-mounts --param
  BucketName=... --param Prefixes=...`, pide `CAPABILITY_IAM`), acotada
  al bucket y a sus prefijos (hasta 4) también para leer, escribir y
  borrar objetos. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS`
  de la imagen (`rayito image publish --env`, de `m15-sizes-catalog`).
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
- **Catálogo de tamaños, aceptación en AWS real** (`m15-sizes-catalog`):
  `rayito image publish --env`/`--sizes` vuelve a reutilizar una versión
  ya construida con la misma configuración: `list-microvm-image-versions`
  no devuelve `environmentVariables` (Q118), así que antes toda imagen con
  variables (cada imagen con sufijo de tamaño) se reconstruía en cada
  publicación; ahora se confirman con `GetMicrovmImageVersion` (gratuita),
  sólo cuando la publicación lleva variables. `rayito image sizes` acepta
  `--image-name`, como `publish`, para listar una familia publicada con
  nombre propio.
- **Catálogo de tamaños** (`m15-sizes-catalog`, ADR-019, opcional y
  apagado por defecto): `Sandbox.create(size="4gb")`/`SizeRequest(memory_mib=...)`
  resuelve, enteramente en cliente y sin ningún RPC, al primer tamaño del
  catálogo cerrado (512mb/1gb/2gb/4gb/8gb, Q87) que cubra lo pedido
  (redondea siempre hacia arriba, avisa con `RayitoCompatWarning` si no
  encaja exacto) y antepone el sufijo de imagen (`rayito-base-4gb`) antes
  de resolver el ARN; `size=` con un template dado por ARN, o por encima
  del máximo publicado, es `InvalidArgumentException` antes de cualquier
  llamada a AWS. `rayito image publish --sizes 512mb,4gb` publica, desde
  el mismo artefacto, una imagen adicional por tamaño (en oleadas de hasta
  10 construcciones simultáneas), horneando `RAYITO_BASELINE_MEMORY_MIB`
  en la imagen (nunca un interruptor de activación); `--env KEY=VALUE`
  añade cualquier otra variable de imagen. `get_info()` confirma, con una
  única llamada cacheada a `GetMicrovmImageVersion` por versión de imagen,
  `SandboxInfo.baseline_memory_mib` y `baseline_cpu` (vCPU medido
  exactamente para los cinco tamaños del catálogo); `cpu_count`/`memory_mb`
  siguen siendo lo que el guest reporta de verdad. El shim `rayito.e2b`
  reporta ese mismo baseline en `SandboxInfo.cpu_count`/`memory_mb` cuando
  `size=` se usó (como E2B reporta lo declarado por el template), la vista
  real del guest si no. `rayito image sizes [--variant]` lista, por
  tamaño del catálogo cerrado, qué imagen ya se publicó (`list-microvm-
  images`) y si comparte artefacto con el baseline (`sameArtifact`, una
  `GetMicrovmImageVersion` sin cuota propia por imagen ya publicada, nunca
  lanza ningún sandbox). Guardarraíles de coste opcional
  `rayito stack deploy sizes-guard` (`RayitoRunAllowedSizes`: un Deny de
  `lambda:RunMicrovm` fuera de los ARN de imagen permitidos, efectivo
  aunque la identidad ya tenga el `microvm-image:*` de la `CallerPolicy`
  estándar). Sin `size=`, el comportamiento sigue siendo exactamente el de
  0.5.x.
<!-- m15-events-webhooks -->
- **`LifecycleEvents`/`AsyncLifecycleEvents`** (`m15-events-webhooks`,
  opcional y apagado por defecto): despliega `infra/events-webhooks.yaml`
  (`deploy`/`status`/`destroy`, componente `events-webhooks` de
  `OptionalStacks`), registra webhooks compatibles con E2B
  (`register_webhook`/`list_webhooks`/`delete_webhook`) y lee el historial
  (`get_events`, 1–100 filas, filtrado por tipo en DynamoDB). CLI
  `rayito events deploy|status|destroy|list` y `rayito events webhook
  add|list|remove`, con el mismo bloque de coste y confirmación que
  `rayito stack`. Los errores de AWS llegan como `WebhookException` con sólo
  el código (`aws_code`). `events=` en `Sandbox.create()`/
  `AsyncSandbox.create()` valida el tipo y que `logging` llegue a CloudWatch
  antes de lanzar y, tras `run-microvm`, manda la clave del sandbox
  (`k_sbx`, derivada de la clave del stack y el `sandbox_id`; un
  `GetSecretValue` por instancia de `LifecycleEvents`) en el mismo
  `ConfigureSandbox` que `mounts=`/`gateways=`/`telemetry=`
  (`LifecycleEventsSectionFactory`, ADR-020); sin la pila desplegada, sin
  `lifecycle_events` en el agente o con la sección rechazada, termina el
  sandbox (salvo `keep_on_failure`). Sin `events=`, ni un cliente
  DynamoDB/Secrets Manager nuevo ni una llamada `ConfigureSandbox`. `rayito events deploy --tag K=V` (repetible) para
  cuentas cuya organización exige etiquetas al crear recursos. Aceptado en
  AWS real (`AWS_API_NOTES.md` Q105–Q108): el forwarder exige que el log
  stream termine en `]<sandbox_id>` (formato medido) y el forwarder y el
  reconciliador dejan una línea JSON por invocación con lo que aceptaron,
  rechazaron (por motivo) o sintetizaron.
<!-- m15-rayd-otlp -->
- **`telemetry=`: exportación OTLP de `rayd` a CloudWatch** (`m15-rayd-otlp`,
  ADR-021, opcional y apagado por defecto): `TelemetryExport`/`OtlpAuth`
  nuevos (`OtlpAuth.execution_role()`, exige `rayito-base-caps`;
  `OtlpAuth.bearer(secret_name=...)`, experimental, funciona en
  `rayito-base`; el nombre se resuelve bajo `rayito/` por la misma
  `SecretCache` que `secrets=`). Se envía como una sección de
  `ConfigureSandbox` tras `/run`; una imagen cuyo `rayd` no exporta termina
  el sandbox (salvo `keep_on_failure=True`) y lanza `UnimplementedError`.
  Con `tracer_provider=`, cada RPC del handle lleva además el `traceparent`
  del span de esa misma llamada hacia `rayd` (lo calcula un interceptor
  del canal en el hilo o la tarea que llama, no el `AuthMetadataPlugin`,
  que grpc ejecuta en un hilo suyo sin el contexto OpenTelemetry del
  llamante); sin él, ninguna cabecera nueva. El token de
  `OtlpAuth.bearer(...)` es una API key de CloudWatch Metrics.
  `sbx.get_telemetry_status()` (síncrono y `AsyncSandbox`) lee
  `ConfigureStatus`, como una llamada explícita aparte de `get_health()`.
  Sin `telemetry=`, ningún comportamiento cambia frente a 0.5.x (traza de
  oro sin tocar).
<!-- m15-templates -->
- **Templates declarativos** (`m15-templates`, ADR-022, opcional y apagado
  por defecto): `Template`/`AsyncTemplate` compilan un DSL (igual al
  `Template` de E2B v2) a un Dockerfile y un zip deterministas sobre una
  imagen `rayito-base`/`rayito-base-caps` ya publicada;
  `Template.build()`/`build_in_background()`/`get_build_status()`/
  `exists()` suben el artefacto por hash de contenido y llaman a
  `create`/`update-microvm-image`, reutilizando una versión idéntica en
  vez de reconstruir. Un build fallido se explica con `BuildException`
  (`step`/`command`/`exit_code`/`log_tail` del log de BuildKit, o
  `reason="ready_client_error"|"ready_server_error"` si falló el
  `ready_cmd`), sin repetir nada. `from_image`/`from_template`/
  `from_dockerfile`/`from_gcp_registry`/`apt_install` lanzan
  `UnimplementedError` (documentados en ADR-022). El shim
  `rayito.e2b.Template`/`AsyncTemplate` ya construye de verdad, con la
  firma de E2B (`alias`, `skip_cache`, `memory_mb` redondeado con
  `RayitoCompatWarning`, `cpu_count` con aviso) y `E2B(bucket=...)`;
  `TemplateException`/`BuildException` del shim pasan a ser las clases
  nativas. `set_start_cmd()` necesita una imagen base con `rayd` 0.6. La
  imagen compuesta hereda la configuración de la base
  (`additionalOsCapabilities` incluida); `skip_cache()` equivale a
  `force=True`; la cuota de builds de AWS llega como
  `reason="build_quota"`. El núcleo de build de imágenes pasa a
  `rayito._images`, compartido con `rayito image publish` (sin cambio de
  comportamiento). Sin llamar a `Template.build()`, el SDK no crea ningún
  cliente nuevo. `infra/templates.yaml` (`rayito stack deploy templates`):
  sólo la política IAM `RayitoTemplateBuilder`, $0 en reposo, que no puede
    sobrescribir las imágenes base publicadas. Aceptación en AWS real: `pip_install()` compila
  a `python3 -m pip install --no-cache-dir --break-system-packages`
  (rayito-base no tiene `pip` en el `PATH`); la versión gestionada que se
  hereda se envía como `1`, no como el eco `1.0` que `create-microvm-image`
  rechaza; la política concede `CreateMicrovmImage` sobre `*` (AWS no la
  autoriza por ARN) y `lambda:PassNetworkConnector` sobre los conectores
  gestionados, y su `Deny` cubre `UpdateMicrovmImage` sobre las bases
  (`AWS_API_NOTES.md` Q114-Q116).
<!-- m15-secrets-gateway -->
- **`gateways=` — pasarela de secretos en loopback** (`m15-secrets-gateway`,
  M15, ADR-023, opcional y apagado por defecto): `Sandbox.create(gateways=
  {"nombre": SecretGateway(upstream=..., headers=..., allow=..., ...)})`
  abre, dentro del agente, un listener de loopback por ruta que reenvía
  sólo lo que su `allow` cubre, dentro de su límite de peticiones por
  minuto, inyectando cada cabecera vaultada (resuelta de Secrets Manager
  con la misma `SecretCache` que `secrets=`, nunca antes de `Configure`) y
  eliminando primero cualquier cabecera del mismo nombre que el sandbox
  intente poner. `sbx.gateways["nombre"].url` da la URL de loopback;
  `refresh()`/`arefresh()` rotan el secreto sin recrear el sandbox (releen
  Secrets Manager aunque la `SecretCache` no haya vencido, conservan el
  puerto y lanzan si `rayd` rechaza la sección). También
  `SandboxPool.take(gateways=)`/`AsyncSandboxPool.take(gateways=)`.
  Cualquier fallo al configurarla tras `run-microvm` (imagen anterior a
  0.6.0, flag ausente, secreto que falta, sección rechazada) termina el VM
  salvo `keep_on_failure`. Sin `gateways=`, ningún cliente `secretsmanager`
  nuevo se construye y no se manda ningún `ConfigureSandbox`.
<!-- m15-custom-domain -->

### Fixed

- `rayito stack deploy events-webhooks --artifact-bucket B` (y
  `OptionalStacks.deploy("events-webhooks", artifact_bucket=B)`) ya no exige
  repetir `--param ArtifactBucket=B`: `StackArtifact.bucket_parameter_key`
  pasa a la plantilla el mismo bucket al que se sube el código, y un valor
  distinto es `InvalidArgumentException` antes de subir nada (aceptación 0.6
  en AWS real).

## [0.5.1] - 2026-10-01

### Fixed

- Los mensajes de error y los docstrings del SDK nombran la versión de la
  imagen o del SDK que hace falta (por ejemplo, "necesita una imagen 0.3.0 o
  posterior"), en lugar del hito interno (M9, M12…).

### Documentation

- Sitio de documentación reorganizado por tareas: primeros pasos, guías por
  función con ejemplos en Python y TypeScript, funciones opcionales con su
  coste, migración desde E2B y referencia (Python, TypeScript, CLI, errores
  y variables de entorno). Los ejemplos se comprueban en CI.

## [0.5.0] - 2026-10-01

### Added

- **Spans OpenTelemetry del lado del SDK** (`m13-otel-sdk`, opcional y
  apagado por defecto, ADR-014): `tracer_provider=` en `Sandbox.create()`
  (también con `pool=`), `connect()` (ambas formas) y las variantes de clase
  `kill`/`pause`/`resume`; instrumenta `commands.run`, `run_code`, `files.*`
  y `kill`/`pause`/`resume` de instancia con spans `rayito.*` de
  `SpanKind.CLIENT`. Sin `tracer_provider=` no se importa `opentelemetry`
  (extra `otel`, `pip install rayito[otel]`) ni se crea ningún span
  (`NOOP.span()` devuelve siempre el mismo `nullcontext`; sólo queda el
  diccionario de atributos trivial de cada llamada). Lista cerrada de atributos (nunca texto de comandos, código,
  rutas, `envs`, secretos ni metadata); un error del span lleva el nombre de
  la clase de la excepción, nunca su mensaje. $0 de AWS; el coste (si lo
  hay) es el del backend de exportación del llamante. Fuera de alcance:
  propagación `traceparent`/`tracestate` hacia `rayd`, telemetría del
  sandbox y el shim de E2B.

- **Índice de metadatos sobre DynamoDB** (`m14-metadata-index`, opcional y
  apagado por defecto, ADR-014): `DynamoDbIndex(table_name, region=,
  session=, on_write_failure="terminate", ttl_margin_seconds=3600)` y la
  opción `index=` en `Sandbox.create()`, `Sandbox.list()`,
  `Sandbox.paginate()` (y sus gemelos asíncronos) y `PoolConfig`. `create()`
  escribe una fila inmutable (`PutItem` condicional) tras `run-microvm`; con
  `metadata=` e `index=`, `list()`/`paginate()` unen `list-microvms` con
  `BatchGetItem` por página y filtran también sandboxes `SUSPENDED` sin
  ninguna sonda de `Health`, token ni `get-microvm`. Nuevas excepciones
  `SandboxIndexException` e `IndexWriteException`. Coste: ~1 WRU por sandbox
  creado y 0,5 RRU por candidato listado (DynamoDB on-demand); la tabla la
  despliegas tú con `infra/metadata-index.yaml`. `reincarnate()` conserva
  `index=`: el sucesor escribe su propia fila.
- **Shim de E2B**: `Sandbox.list(query=SandboxQuery(metadata=...,
  state=[PAUSED]), index=...)` y `E2B(index=...)` filtran sandboxes en pausa
  por metadatos con el índice.
- **CLI**: `rayito sandbox list --metadata K=V --state suspended
  --index-table TABLA` (y `--state` sola); `--index-table` sin `--metadata`
  es un error de uso (salida 2); sin `--index-table`, sin cambios.
- **Secretos sobre AWS Secrets Manager** (`m13-secrets`, opcional y
  apagado por defecto, ADR-014): `SecretStore(region=, session=,
  prefix="rayito/", kms_key_id=)` con `create`/`update`/`get_info`/
  `exists`/`list`/`destroy` (versión entera en `ClientRequestToken`,
  metadatos en `Description`, borrado sin ventana de recuperación, reintento
  acotado de `create` tras un borrado reciente) y `SecretCache(ttl_seconds=300)`
  (clave por región, sesión, secreto y versión; una sola lectura en vuelo por
  clave; los aciertos no llaman a AWS; `refresh()`/`invalidate()`; nunca
  muestra valores). Coste: $0,40/secreto-mes hasta `destroy` + $0,05/10 000
  llamadas.
- **`secrets=` / `secret_cache=`** en `Sandbox.create()` (también con
  `pool=`), `connect()`, `SandboxPool.take()` y, por llamada, en
  `commands.run`, `pty.create`, `run_code` (contextos Python) y
  `create_code_context`, síncrono y asíncrono: entregan el valor como
  variable de entorno por los `envs` que ya viajan a `rayd` (sin RPC nueva,
  vale con imágenes 0.4.0), nunca en el `runHookPayload`, `metadata`, logs ni
  errores. Se resuelven antes de `run-microvm`. La primera vez avisan con
  `RayitoCompatWarning`: el valor es visible para el código del sandbox.
- **Shim de E2B**: `Secret`/`AsyncSecret`, `SecretInfo`, `SecretPaginator`/
  `AsyncSecretPaginator`, `SecretException`/`SecretNotFoundException` sobre
  `SecretStore`, con los nombres de `e2b` 2.51.0; `E2B(...).Secret` usa su
  región y su sesión. `fill()` devuelve el placeholder, que nada resuelve;
  `iam_token` sigue en `UnimplementedError`.

### Changed

- El motivo de `UnimplementedError` de `list(state=PAUSED, query.metadata)`
  en el shim de E2B nombra ahora la opción `index=DynamoDbIndex(...)`.
- `RayitoCompatWarning` vive ahora en `rayito.exceptions` (y se re-exporta
  desde `rayito.e2b.exceptions`: es la misma clase).
- `rayito.e2b.Secret`/`AsyncSecret` y `E2B(...).Secret` ya no lanzan
  `UnimplementedError`: llaman a Secrets Manager cuando se usan.

- **`rayito sandbox proxy ID --port N`** (`m12-sizes-proxy`): expone un
  puerto del guest en `http://<bind>:<local-port>` (`--local-port`, por
  defecto igual a `--port`; `--bind`, por defecto `127.0.0.1`;
  `--allow-remote` para escuchar fuera de loopback) sin coste de AWS más
  allá de `GetMicrovm` + `CreateMicrovmAuthToken` (gratuitos). Reutiliza el
  `TokenRefresher`/`TokenStore` del SDK (mismo refresco a los 45 min);
  siempre acuña `PortSpec.single(N)`, nunca `allPorts`; rechaza el puerto
  9000 (hooks, ADR-006) antes de llamar a AWS. Quita cualquier cabecera
  `x-aws-proxy-*` del cliente, fija `Host`/`X-aws-proxy-auth`/
  `X-aws-proxy-port` y fuerza `Connection: close` salvo en peticiones de
  upgrade (`Upgrade` + `Connection: upgrade`; el paso de WebSocket está
  implementado, no medido contra AWS). Sin JWE vigente o sin conexión al
  sandbox responde `502` (motivo en stderr, sin JWE ni ruta); lectura de
  cabecera y conexión al sandbox con tiempo máximo de 30 s. Nunca registra el JWE, las cabeceras, los cuerpos ni las rutas.
  El parseo de la cabecera del cliente es HTTP/1.1 estricto: rechaza (`400`
  y cierra) cualquier CR/LF suelto fuera de un `\r\n` (contrabando de
  cabeceras que podía colar un `x-aws-proxy-*` falso), `obs-fold` y nombres
  fuera de los `tchar` de RFC 9110. `--port` y `--local-port` se validan y
  el socket local se reserva **antes** de `GetMicrovm`/
  `CreateMicrovmAuthToken`, así que un puerto inválido u ocupado falla
  limpio (mensaje con el puerto, salida 1, sin traceback) sin gastar
  ninguna llamada. Sin dependencias nuevas.

### Fixed

- `SecretStore.destroy()` (y `Secret.destroy`/`AsyncSecret.destroy` del shim
  de E2B) devolvía `True` para un nombre que nunca existió, porque AWS
  acepta el `DeleteSecret` forzado de un nombre inexistente sin
  `ResourceNotFoundException` (aceptación de 0.5.0 en AWS real). Ahora hace
  `DescribeSecret` antes y devuelve `False` sin borrar si no existe (o ya
  estaba programado para borrarse, o lo borró otro a la vez), como documenta
  y como E2B. Usa `secretsmanager:DescribeSecret`, ya incluido en
  `RayitoSecretsAdmin`.
- `SecretStore.create()` sobre un nombre recién borrado agotaba su
  presupuesto de reintentos de 30 s: AWS liberó el nombre tras 19–28 s y el
  backoff no llegaba. El presupuesto es ahora de 60 s
  (`CREATE_RETRY_BUDGET_SECONDS`) con ±25 % de jitter.
- El e2e de secretos (SEC-10) ponía el logger raíz a DEBUG y capturaba los
  cuerpos de botocore con el valor del secreto; ahora captura sólo el logger
  `rayito`. `docs/site/docs/secrets.md` avisa de que el DEBUG de
  botocore/urllib3 imprime los valores (Rayito nunca lo hace) y de que
  `list()` es eventualmente consistente (~3–5 s).

## [0.4.0] - 2026-09-30

### Fixed

- `rayito doctor` tiene la fila `0.4` de la tabla de compatibilidad (SDK 0.4
  exige `rayd` 0.4.0); sin ella la comprobación `compatibility` daba FAIL.

- `files.read()` de un fichero grande enrutado por S3: si el plazo
  (`request_timeout`/`stream_idle_timeout`) vencía justo cuando el
  `get_object` seguía en vuelo, el SDK seguía lanzando ese `get_object` tras
  una cancelación ya vista, aunque el resultado tardío se descartaba y el
  llamante siempre recibía `TimeoutException`. `ObjectFetch.run` comprueba
  ahora la cancelación antes de abrir el cuerpo, así que no sale ningún
  `get_object` de más una vez vista la cancelación.

### Cambios que rompen

- `LifecycleUnsupportedException` pasa a ser subclase de `UnimplementedError`
  (ya no de `InvalidArgumentException`/`SandboxException`), y el kernel
  ausente en `run_code`/`create_code_context` (y cualquier RPC que el agente
  no implemente, vía la tabla unaria genérica de `_transport.py`) deja de ser
  `InvalidArgumentException`/`SandboxException`: ahora es `UnimplementedError`
  en los tres casos, un único tipo para "esta feature no está disponible en
  este sandbox". Migración: cambia `except InvalidArgumentException`/`except
  SandboxException` alrededor de esas llamadas por `except
  rayito.UnimplementedError` (o `except LifecycleUnsupportedException`, que
  ahora es subclase suya); `MetricsHistoryUnavailable` sigue siendo
  `UnimplementedError` para quien la captura, pero su `__cause__` cambia: ya
  no es la `RpcError` de gRPC directamente, sino el `UnimplementedError`
  genérico de la tabla unaria (cuya propia `__cause__` sí es la `RpcError`;
  `__cause__.__cause__` para llegar a ella). `rayito.e2b` no cambia de
  superficie: seguía lanzando `UnimplementedError` en los tres casos con el
  mismo `feature`, `reason` y `doc`; sólo se simplifica su traducción
  interna. La pista "publica una imagen con una versión actual de rayd" de
  `unimplemented_rpc_error` sólo sale para un RPC de verdad ausente: un
  kernel ausente en `run_code`/`create_code_context` ya nombra
  `rayito-base-poly` en el propio detalle de `rayd`, así que ya no repite un
  consejo que no aplica (la imagen ya está actualizada).

### Changed

- `sbx.kill()`, `sbx.pause()` y `sbx.connect()` (`rayito.e2b`, llamadas de
  instancia) avisan ahora con `RayitoCompatWarning` cuando se les pasa
  `headers=`, `proxy=` o `retries=` (y `sbx.kill()`/`sbx.pause()` también con
  `request_timeout=`), en vez de perderlos en silencio: esta llamada opera
  sobre el canal y el plano ya construidos de este sandbox y no puede
  reconstruirlos; usa `Sandbox.<kill|pause|connect>(sandbox_id, ...)` si
  necesitas aplicarlos.

## [0.3.3] - 2026-09-29

### Security

- `.github/workflows/release.yml`: el job `python` se parte en `python-build`
  (sin `environment` ni `id-token`, el único que hace checkout y corre `uv
  build`) y `python-publish` (con el token OIDC de PyPI, sin checkout y sin
  `uv`, que verifica con `sha256sum -c` que el `dist/` descargado es el que
  produjo `python-build` antes de publicar) — cierra C-10/H-02 residuo
  (`docs/SECURITY_AUDIT.md`). Sin cambio de comportamiento en lo publicado
  ni en el Trusted Publisher de PyPI.

### Changed

- Shim E2B: `native_call_kwargs` (`rayito.e2b._compat`) ya no lee
  `ConnectionConfig.current_integration()` ni construye un plano de
  control compartido (`shared_control_plane`) — la construcción con AWS
  se mueve al nuevo `bind_control_plane` (`rayito.e2b._connection`), el
  único punto de I/O; `native_call_kwargs` recibe `integration` como
  parámetro y sólo describe, en `NativeCall.plane_settings`, si hace
  falta un plano nuevo. `snapshot_config` ya no escribe el atributo
  privado de `ConnectionConfig` desde fuera de la clase; ahora pasa por
  el nuevo `ConnectionConfig._snapshot`. Sin cambio de comportamiento
  observable ni de ninguna firma pública (`ConnectionConfig`, `Sandbox`,
  `AsyncSandbox`, `E2B`); ver `openspec/changes/m10-python-shim`.

## [0.3.2] - 2026-09-29

### Fixed

- Reconexión tras un reset de stream del proxy de AWS (`RST_STREAM` antes
  del plazo real): grpcio lo presenta como `CANCELLED "Stream removed"`
  (`AWS_API_NOTES.md` #33), que `is_stream_reset` no clasificaba como corte
  reconectable (sólo miraba `UNAVAILABLE`/`INTERNAL`), así que
  `commands.connect`/`pty.connect`, `watch_dir` y el reenganche de
  `run_code` fallaban en vez de reconectar. Ahora `CANCELLED` con esa marca
  (u otra de `STREAM_RESET_MARKERS`) reconecta igual que un `Socket closed`;
  un `CANCELLED` sin la marca (el propio `call.cancel()`, p. ej.
  `CommandHandle.disconnect()`) nunca reconecta, y el chequeo del handle
  desconectado sigue yendo antes que la clasificación, así que un corte que
  llegara disfrazado de reset justo después de `disconnect()` tampoco
  reconecta (`_transport.py`).
- `rayito-mcp` y `python -m rayito.mcp` sin el extra `rayito[mcp]`: antes
  fallaban con un `ModuleNotFoundError` sin capturar (traza completa) porque
  `rayito.mcp/__init__.py` importaba todo el paquete de forma ansiosa, así
  que cualquier import de `rayito.mcp` (incluido `rayito.mcp.__main__`)
  disparaba el fallo antes de que su propio `try`/`except` pudiera
  atraparlo. Ahora `rayito.mcp` resuelve sus cuatro nombres con
  `__getattr__` perezoso (PEP 562) y `rayito.mcp.__main__` imprime un aviso
  de una línea con el comando de instalación y sale con 2, sin traza, igual
  que la CLI `rayito` con `rayito[cli]`.

### Changed

- `tests/e2e/test_m3_filesystem.py`: `UPLOAD_BUDGET_SECONDS` (20 s fijos
  para 8 MB) fallaba en uplinks lentos (0,25-0,44 MB/s medidos). El
  presupuesto ahora se deriva de una línea base medida en el propio run
  (una escritura de prueba de 300 KB) escalada a los 8 MB con un margen de
  3x, acotada entre 5 s y 120 s: el test comprueba una regresión de
  throughput, no la red de quien lo corre.

## [0.3.1] - 2026-09-28

Versión de mantenimiento, sin cambios de API; se publica para mantener las
versiones de los tres componentes sincronizadas.

### Changed

- `boto3`/`botocore` 1.43.103 en el lockfile de desarrollo y rango de
  `uv-build` ampliado a `<0.13`.

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
