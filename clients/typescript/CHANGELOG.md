# Changelog

Todos los cambios notables del paquete `rayito` (SDK TypeScript). El formato
sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [Unreleased]

## [0.7.1] - 2026-10-06

### Changed

- Sin cambios en el SDK TypeScript: sube a 0.7.1 por las versiones
  enlazadas, con `rayd` 0.7.1 (avisos de licencia de terceros en el zip, la
  imagen y la release).

## [0.7.0] - 2026-10-05

### Added

- **Dominio propio** (`m15-custom-domain`, ADR-024, **experimental**,
  apagado por defecto): la clase `CustomDomain` despliega una distribución
  CloudFront, una CloudFront Function de enrutado (`cloudfront-js-2.0`) y
  un KeyValueStore (`infra/custom-domain.yaml`), y `register`/`unregister`/
  `refresh`/`hostFor` gestionan las rutas `{puerto}-{alias}.<tu-dominio>`
  (hostname HTTPS normal, sin las cabeceras `x-aws-proxy-*`).
  `register()` exige `trafficToken` salvo `public: true` explícito.
  `deploy({ alternateDomainNames })` sustituye el alias comodín por
  hostnames exactos. Nuevos peers opcionales
  `@aws-sdk/client-cloudfront-keyvaluestore` y `@aws-sdk/signature-v4a` (el
  plano de datos del KeyValueStore firma con SigV4A); sin instanciar
  `CustomDomain` no se importa ninguno. `domain` en `Sandbox.create()` sigue
  lanzando `UnimplementedError`: la integración con `getHost()`/`expose()`
  llega en un cambio posterior. **Experimental** porque no se ha verificado
  de punta a punta en AWS real: la Function compila y enruta en el runtime
  real (`TestFunction`), y la pila crea el KeyValueStore y la Function, pero
  la cuenta de pruebas deniega `cloudfront:CreateDistribution` por una SCP,
  así que nunca se ha servido tráfico por una distribución desplegada con
  esta plantilla (`AWS_API_NOTES.md` Q121, Q140, Q141). La API puede cambiar
  en una minor.
- `rayito-base-caps-efs` (y sus sufijos de tamaño) cuenta como variante caps
  para `mounts`, `volumes` y `telemetry` (`requireCapsFor`, `CAPS_VARIANTS`,
  `EFS_CAPS_VARIANT`).
- **Volúmenes EFS** (`m15-efs-volumes`, ADR-018, **experimental**, apagado
  por defecto): `VolumeStore` (CRUD real de access points EFS:
  `CreateAccessPointCommand`/`DescribeAccessPointsCommand`/
  `DeleteAccessPointCommand`, con `@aws-sdk/client-efs` como peer opcional
  cargado sólo en el primer uso) y `Sandbox.create({ volumes })`, que
  valida la petición (tipos, rutas, variante `caps` y un único conector
  propio en `egress`: sin él, con `INTERNET_EGRESS` o con dos conectores es
  `InvalidArgumentError`, porque un MicroVM sólo admite un conector de
  egress, `AWS_API_NOTES.md` §16 Q131), como mucho 4 volúmenes y
  `executionRoleArn` antes de cualquier llamada a AWS; resuelve la IP de
  mount target que falte con un `DescribeMountTargetsCommand` por sistema de
  ficheros antes de `run-microvm`, manda la sección `efs_volumes` en el
  único `ConfigureSandbox` de `create()` (con plazo de 65 s), sólo vuelve
  con todos los volúmenes `mounted` y, si uno falla, termina el sandbox
  (salvo `keepOnFailure`) y lanza `VolumeMountError` (`code`); sobre una
  imagen sin `amazon-efs-utils`, `UnimplementedError`. `reincarnate()` la
  vuelve a mandar y `sbx.volumes()` da el estado en vivo. `EfsVolume`
  (valida `mountTargetIp` como IPv4), `VolumeStatus` y los errores
  `VolumeError`/`VolumeMountError`/`VolumeNotFoundError`/`VolumePathNotFoundError`.
  El shim de E2B (`Volume`) hace CRUD real sobre
  `new E2B({ volumeStore })` (`volumeId` es el nombre del volumen, el mismo
  que reciben `connect`/`getInfo`/`destroy`); sus operaciones de contenido
  (`UnimplementedError("volume.content")`) siguen sin plano de datos, y
  `Sandbox.create({ volumeMounts })` monta con
  `new E2B({ volumeStore, volumeConnectorArn })`: ese sandbox sale sólo por
  el conector del volumen y `allowInternetAccess: true` explícito es
  `InvalidArgumentError`. Componente
  `rayito stack {deploy,status,destroy} efs-volumes`
  (`infra/efs-volumes.yaml`: sistema de ficheros EFS cifrado, un mount
  target por subred de `SubnetIds`, grupos de seguridad NFS nuevos y
  conector de egress dedicado, sólo dentro de una VPC que ya existe).
  `EfsVolumes`: `check({ vpcId, subnetIds })` comprueba la VPC sin crear
  nada (sólo `Describe*` de EC2 con el peer opcional `@aws-sdk/client-ec2`;
  cuenta las subredes con ruta por defecto a un NAT y a otra puerta, como un
  transit gateway),
  `deploy()` se niega si algún hallazgo es `FAIL`, `volumeStore()` da un
  `VolumeStore` sobre la pila, y `destroy({ deleteFileSystem: true })`/
  `deleteFileSystem(id)` borran el sistema de ficheros conservado (sólo uno
  con la etiqueta `rayito=efs-volumes`). `deploy({ readOnlyAccessPointArns })`
  (`ReadOnlyAccessPointArns`) le deniega `ClientWrite` a los access points de
  sólo lectura: la opción `ro` del montaje no basta, porque el usuario del
  sandbox alcanza el puerto local de `efs-proxy` (Q133). El hallazgo
  `internet-egress` de `check()` explica que un sandbox con volumen sólo
  tiene internet por la VPC.
  `list`/`get` son eventualmente consistentes, como `DescribeAccessPoints`
  (medido en AWS real, `AWS_API_NOTES.md` §16 Q125: hasta 11 s en listar un
  access point nuevo y 8 s en dejar de listar uno borrado): `create` de un
  nombre que ya existe reintenta `get` hasta 30 s y `destroy` de un access
  point que el listado aún mostraba pero ya no existe devuelve `false`.

### Security

- **Eventos de ciclo de vida (`events-webhooks`): una línea maliciosa ya no
  tira los eventos legítimos de su lote.** El forwarder verifica el MAC con
  la clave del sandbox que nombra el *log stream* **antes** de leer el
  contenido, parsea de forma estricta y nunca falla la invocación por una
  línea: la cuenta (`malformed_line`, `mac_invalid`, `stale_event`,
  `internal_error`) y sigue. Si falla una escritura en la tabla, el resto
  del lote se escribe igual y el lote va a la nueva cola
  `ForwarderFailuresQueue` (destino `OnFailure` de su invocación asíncrona)
  en vez de perderse. El nombre del stream deja de presentarse como prueba
  de identidad: sólo el MAC autentica (`SECURITY.md` T22).
- **Eventos: sólo lo que emite `rayd`, y reciente.** Tras el MAC, el evento
  debe ser del mismo sandbox que el stream, con un `event_id` de 32 hex
  (nunca un id del reconciliador), `kill_reason` sólo en `killed` y sólo
  `request`, un ARN de `microvm-image` y un `occurred_at_ms` de menos de
  24 h (y no más de 5 min en el futuro): una línea capturada y repetida
  cuando su fila de deduplicación ya caducó se rechaza. La deduplicación de
  entregas pasa a `DELIVERY#<sandbox_id>#<event_id>`, así que un sandbox no
  puede marcar como entregado el evento de otro (las entregas en curso al
  actualizar la pila pueden repetirse una vez).
- **Webhooks: uno roto o lento ya no bloquea a los demás.** El deliverer
  trata una URL que no puede parsear como un fallo permanente de ese
  webhook (`invalid_url`), aísla cualquier error inesperado a su webhook
  (`internal_error`) y el plazo de cada intento cubre la resolución DNS y
  la respuesta entera. `registerWebhook` rechaza con
  `InvalidArgumentError` una URL que el deliverer no podría alcanzar (puerto
  fuera de 1-65535, host inválido o no codificable en IDNA, espacios o
  barras invertidas), con el mismo criterio en los dos SDKs.
- **Código de las pilas opcionales: nunca se fía de un objeto que ya
  exista.** `OptionalStacks.deploy()` sube el zip a
  `rayito/stacks/<componente>/<sha256>.zip` (dentro del espacio `rayito/`
  que protegen las plantillas de IAM, ya no en la raíz del bucket), manda
  `ExpectedBucketOwner` con la cuenta del llamante y, si el objeto ya
  existe, compara su contenido antes de reutilizarlo (si no coincide, lo
  sobrescribe con `ChecksumSHA256` y avisa por el logger).
- **`infra/iam.yaml`: el lanzador ya no puede publicar imágenes.** Nuevas
  políticas `SandboxLauncherPolicy` (sólo lanzar y manejar sandboxes: la
  del rol de un servicio en producción) e `ImagePublisherPolicy` (sólo
  publicar); `CallerPolicy` sigue igual, como su unión. El `Deny` del
  execution role sobre los artefactos de imagen existe ya siempre, no sólo
  con `PersistenceBucket`.
- **`infra/templates.yaml`: `RayitoTemplateBuilder` sólo lee `rayito/*`**
  del bucket de artefactos o de la imagen base, nunca el bucket entero
  (que puede guardar los `HOME` persistidos y las transferencias).
- **Secretos de firma de webhooks fuera de `secrets=`.**
  `RayitoSecretsReader` niega `rayito/webhooks/*` y el SDK se niega a leer
  esos secretos por `secrets`/`SecretCache` (`InvalidArgumentError`).
  `SecretPrefix` de `infra/secrets-access.yaml` debe terminar en `/`.
- **Mínimo privilegio en `events-webhooks`**: cada Lambda escribe sólo sus
  tipos de fila (`dynamodb:LeadingKeys`) y sólo en su propio log group (la
  pila crea uno por función y lo borra con ella), y el rol del Scheduler
  exige `aws:SourceAccount`.
- **Eventos: el código del sandbox ya no puede inundar la pila con
  `paused`/`resumed`.** Esos dos eventos los emite `rayd` cuando corren sus
  hooks de suspensión y reanudación, que el código sin privilegios del
  sandbox puede invocar. El forwarder sólo admite ahora eventos que hagan
  avanzar el ciclo de vida de su sandbox (nada tras `killed`, ningún
  `created` repetido, ninguna posición repetida o anterior) y limita
  `paused`/`resumed` con un cubo por sandbox (20 seguidos, después uno cada
  30 s); lo rechazado se cuenta como `invalid_transition` o `rate_limited`
  y nunca se guarda ni se entrega. El reconciliador consulta un índice
  disperso de sandboxes abiertos en vez de escanear la tabla entera, y el
  deliverer sólo se invoca por eventos nuevos. `paused`/`resumed` quedan
  documentados como orientativos (`SECURITY.md` T22). Al actualizar la
  pila, un sandbox que ya estaba abierto entra en el índice con su
  siguiente evento.
- **Webhooks: segunda firma con marca de tiempo.** Cada entrega lleva
  además `rayito-signature: t=<segundos>,v1=<HMAC-SHA256>` sobre
  `"<t>.<webhook_id>."` y el cuerpo; la guía de eventos explica cómo
  verificarla con una ventana de 5 minutos. `e2b-signature` no cambia.
- **Webhooks: un secreto rotado deja de usarse en 5 minutos.** El deliverer
  guardaba cada secreto en caché mientras viviera su contenedor; ahora lo
  relee cada 5 minutos y, si el receptor responde `401`/`403`, lo relee en
  el acto y reintenta una vez con el nuevo.
- **Políticas del llamante por tarea en `events-webhooks`.** La pila emite
  `EventsLauncherPolicy` (`events=`: sólo la clave del stack),
  `EventsReaderPolicy` (`getEvents`: sólo filas de eventos; ya no puede
  registrar un webhook ni leer la clave del stack) y
  `EventsWebhookAdminPolicy` (`registerWebhook`/listar/borrar: sólo filas `WEBHOOK`).
  La guía de eventos tiene la tabla de qué llamada necesita cuál y avisa de
  que los webhooks son de toda la pila.
- **`sizes-guard`: `RayitoRunAllowedSizes` niega también publicar
  imágenes.** Sin ello, una identidad con la `CallerPolicy` estándar podía
  reconstruir una imagen permitida a un tamaño mayor y lanzarla sin salir
  de la lista. La política ya no dice que valga "sin importar lo demás": una
  versión antigua y mayor bajo un nombre permitido sigue siendo lanzable con
  `imageVersion` (`SECURITY.md` T27).
- **`SECURITY.md`: filas T20, T23, T26 y T27**, que los cambios de M15
  planearon y nunca se aplicaron (`infra/otlp-export.yaml` ya citaba T23).
- `infra/ci-oidc-role.yaml` y `infra/README.md` piden una cuenta dedicada a
  e2e (o imágenes de test propias): el rol puede acuñar tokens y terminar
  cualquier sandbox de las imágenes de `TestImageArns`.
- **`SecretGateway` rechaza una ruta de `allow` que `rayd` nunca dejaría
  pasar** (`sec-rayd-agent-hardening`): las rutas de `allow` siguen ahora
  la misma lista de permitidos que `rayd` aplica a cada petición
  (caracteres de ruta de RFC 3986 salvo `;`, sin segmentos vacíos salvo el
  último, y cada segmento decodificado una sola vez UTF-8 válido, sin `/`,
  `\`, `%`, `;` ni bytes de control y distinto de `.`/`..`), y una que no
  la cumpla lanza `InvalidArgumentError` antes de cualquier llamada,
  con los mismos vectores compartidos que `rayd`
  (`testdata/secret-gateway/request-paths.json`).
- **`.dockerignore` con la semántica de Docker** en `Template.build`:
  anclado en la raíz, `**` como cero o más directorios (así `**/.env`,
  `**/.git` y el resto de los valores de `docker init` excluyen también los
  de la raíz, que antes acababan en la imagen), un directorio excluido
  excluye lo de dentro y la última coincidencia gana; mismos vectores que el
  SDK de Python. **Cambio**: `*` y `?` ya no cruzan `/` (`*.pyc` sólo
  excluye los de la raíz; usa `**/*.pyc`). Un `process.emitWarning` de tipo
  `RayitoContextWarning` (con las rutas, nunca el contenido) avisa si el
  contexto va a empaquetar `.env*`, `.git/`, `.aws/`, `.ssh/`, `*.pem` o
  `*.key`. Cada fichero del contexto se vuelve a comprobar dentro del
  contexto y se abre con `O_NOFOLLOW` justo antes de leerlo.
- **`Template.build()`**: un rechazo de AWS distinto de la cuota es ahora
  `BuildError({ reason: "aws_error" })` con el resumen saneado como mensaje y
  `cause`, y el adaptador sanea cada llamada al SDK de AWS (el error crudo de
  smithy lleva `$response`, la petición firmada; un error de firma lleva la
  cadena canónica con el token de sesión). El `name` del error se conserva.
  `StackError` usa también el resumen saneado como `cause`.
- **Credenciales de git**: `clone`/`push`/`pull` con `username`/`password`
  corren sin hooks ni credential helpers, se niegan (`GitAuthError`) si la
  configuración de git reescribe URLs (`url.*.insteadOf`), y un `clone` que
  falla sin exit de git intenta igualmente quitar las credenciales de
  `origin`; si una restauración falla, el `logger` del sandbox avisa sin la
  URL. La documentación dice ahora que el token queda al alcance del código
  del sandbox.
- **Salida acotada en memoria**: cada descriptor de un comando o una PTY
  guarda como mucho 64 MiB (`COMMAND_OUTPUT_MAX_BYTES`); se conserva el
  final y `CommandResult.truncated`/`CommandExitError.truncated` lo indican.
  `commands.run`/`connect` aceptan `maxOutputBytes` (`0` no guarda nada; los
  callbacks reciben siempre todo).
- **Access token mínimo**: un token propio (`accessToken`,
  `RAYITO_ACCESS_TOKEN`) tiene que decodificar a al menos 16 bytes
  (`ACCESS_TOKEN_MIN_BYTES`). **Cambio**: uno más corto, aceptado antes,
  ahora es `InvalidArgumentError`. Los generados (32 bytes) no cambian.
- `ProxyToken.jwe` ya no es enumerable: `console.log`/`util.inspect` y
  `JSON.stringify` no lo muestran.
- Tests que fijan que `Template.build()` no sigue enlaces simbólicos dentro
  de un directorio copiado (misma regla que el SDK de Python).
- **`Sandbox.create({ persist })` ligan el sandbox a su prefijo de persistencia** (C-07): el
  `runHookPayload` lleva el bucket y la base `prefix` del `S3Prefix` (nunca
  `prefix/name`), y un `rayd` con la corrección rechaza con
  `permission_denied` cualquier checkpoint o restore fuera de esa base. Con
  un `prefix` por inquilino, el token de un sandbox ya no alcanza el `HOME`
  persistido de otro aunque compartan execution role. Cambia un uso: un
  restore o checkpoint explícito hacia otra base desde un sandbox creado con
  `persist=` ahora falla con `permission_denied`. No añade llamadas a AWS.
- **Recortes y troceo de cadenas en tiempo lineal** (CodeQL
  `js/polynomial-redos`): `git.clone()` con credenciales, `files.download()`
  sin `filename`, el access token y las líneas de `.dockerignore` de un
  template usaban expresiones regulares que retrocedían en tiempo
  cuadrático (50 000 caracteres bloqueaban el bucle de eventos más de un
  segundo). La semántica no cambia: una URL con un fin de línea tras la
  autoridad sigue sin reconocerse como http(s).

### Deprecated

- `EventsOperatorPolicy` (salida `OperatorPolicyArn` de `events-webhooks`):
  sigue siendo la unión de las tres políticas nuevas durante esta versión.
  Vincula a cada identidad la suya (`EventsLauncherPolicy`,
  `EventsReaderPolicy` o `EventsWebhookAdminPolicy`).

## [0.6.1] - 2026-10-04

### Fixed

- **Redesplegar una pila opcional ya no deshace su configuración**
  (`OptionalStacks.deploy()`): los valores por defecto del catálogo sólo se
  aplican al crear la pila; al actualizarla, cada parámetro que no vuelves a
  pasar y la pila ya tiene se manda con `UsePreviousValue`. Antes, por
  ejemplo, redesplegar `s3-mounts` sin repetir `Prefixes` volvía a `'*'`
  (todo el bucket), `metadata-index` sin `TableName` reemplazaba (y borraba)
  la tabla y `secrets-access` sin `KmsKeyArn` quitaba `kms:Decrypt`. Nuevo
  `parameterChanges()` para ver qué cambiaría sin desplegar.
- **Guardián SSRF de los webhooks** (pila `events-webhooks`): una dirección
  IPv6 que encapsula una IPv4 (`::ffff:100.64.0.1`) se clasifica como esa
  IPv4, así que el rango CGNAT queda bloqueado también por esa vía. Se
  regenera el artefacto de la Lambda que el SDK sube.
- `volumes`/`domain` lanzan `UnimplementedError` con "todavía no
  disponible" en vez de "llega en 0.6".
- Se exporta `WorkdirStep` desde `rayito` (paridad con Python: ya era
  miembro de la unión `WireStep`).
- **`reincarnate()` reaplica todas las secciones de `ConfigureSandbox`**
  (`m15-reincarnate-configure-replay`): hasta ahora sólo reenviaba
  `gateways` y un sucesor perdía `mounts`, `events` y `telemetry`.
  `create()` guarda sus opciones 0.6 en `LaunchOptions.features`
  (`relaunchFeatures`: todas menos `size`) y `reincarnate()` las reenvía
  (`relaunchCreateOptions`) al `create()` del sucesor, que las aplica por el
  mismo camino en su único `Configure`: `events` deriva `k_sbx` del nuevo
  `sandboxId`, `mounts` espera otra vez a `mounted` y `telemetry` usa la
  imagen y la memoria del sucesor. De paso, `reincarnate()` de un sandbox
  creado con `size` ya no reenvía el tamaño resuelto junto al ARN de la
  imagen (que ya lo lleva), combinación que `create()` rechaza.
- **Tipos correctos para consumidores CommonJS** (`exports` de
  `package.json`): `"rayito"` y `"rayito/e2b"` declaraban un `types` hermano
  de `import`/`require`, que TypeScript elige antes que ambos, así que un
  proyecto CommonJS con `moduleResolution: node16`/`nodenext` recibía los
  `.d.mts` (ESM) para un `require` que carga el `.cjs` ("Masquerading as
  ESM" de arethetypeswrong). Ahora cada condición lleva su `types` (`import`
  → `.d.mts`, `require` → `.d.cts`), el `types` de nivel superior apunta al
  `.d.cts` de `main`, se exporta `"./package.json"` y `pnpm pack:check`
  rechaza un mapa que vuelva a la forma anterior. Mismos ficheros en tiempo
  de ejecución.

### Documentation

- Bloques "Coste y activación" completos para `mounts`/`S3Mount` y
  `size`/el catálogo de tamaños, y filas de las seis funciones 0.6 en la
  tabla de funciones opcionales; la referencia de la CLI, la paridad con
  E2B, la guía de migración, `limits.md` y las notas de 0.6.0 ya no
  describen como pendientes funciones publicadas en 0.6.0.
- **`DynamoDbIndex` enseña su bloque "Coste y activación" en el hover del
  IDE**: vivía en el comentario de módulo, que tsdown descarta; ahora está
  en el TSDoc de la clase y `check-dts-cost-blocks` lo exige.
- **Metadatos del paquete**: `package.json` declara `homepage` (el sitio de
  documentación) y `bugs`; el README del paquete termina con la licencia, el
  `NOTICE` incluido y la nota de marcas (proyecto independiente, no afiliado
  a E2B). El `NOTICE` conserva además el copyright de E2B en la atribución
  del código git adaptado (Apache-2.0 §4(c)).

## [0.6.0] - 2026-10-03

### Added

- **Convenio `OptionalStack` y `ConfigureSandbox`** (`v06-foundations`,
  M15 foundations, ADR-015/ADR-016, opcional y apagado por defecto): la
  clase `OptionalStacks` (`deploy`/`status`/`destroy`/`components`) sobre
  un catálogo de nueve componentes (`metadata-index` y `secrets-access`,
  migrados sin cambios de comportamiento; los otros siete son stubs hasta
  su propia función), con `@aws-sdk/client-cloudformation` como peer
  opcional; las siete opciones 0.6 de `Sandbox.create()` (`mounts`,
  `volumes`, `size`, `events`, `telemetry`, `gateways`, `domain`) existen
  ya en `SandboxCreateOptions` y lanzan `UnimplementedError` nombrando el
  cambio que las trae mientras sigan siendo un stub, antes de
  `run-microvm`; en cuanto una deja de serlo (`mounts`, ver más abajo),
  `create()` ejecuta sus `configureSections` justo tras el primer
  `Health` (`configure-base.ts`: `requireCapabilities`,
  `buildConfigureRequest`, `checkConfigureResponse`), dentro del mismo
  camino que ya termina el sandbox ante cualquier fallo anterior a
  `agentReady`. Sin ninguna opción nueva, el comportamiento es byte a
  byte el de 0.5.x.
<!-- m15-s3-mounts -->
- **`mounts` (`m15-s3-mounts`, ADR-017, experimental, apagado por
  defecto)**: `Sandbox.create({ mounts })` monta uno o más buckets S3
  (`new S3Mount({ bucket, prefix, readOnly: true, allowOverwrite: false,
  allowDelete: false })`, exportado desde `rayito`) en el guest con
  `mount-s3`/FUSE, sólo sobre `rayito-base-caps` (`requireCapsFor` lo
  exige antes de `run-microvm` cuando la imagen se nombra directamente;
  sobre un ARN opaco la decisión se difiere a `Health.features` tras
  `/run`, que termina el sandbox si falta la capacidad). `mounts` acepta
  un objeto literal (como `volumes`) o un `Map`. `create()` no vuelve
  hasta que cada montaje está montado: sondea `ConfigureStatus` (como
  mucho 15 s) y, si uno falla o no se asienta, termina el sandbox y lanza
  `MountError`. `sbx.mounts()`
  da el estado en vivo de cada montaje (`"pending"`/`"mounted"`/
  `"failed"`, `ConfigureStatus` en cada llamada); una sección rechazada o
  un montaje fallido lanza `MountError` con un `code` cerrado (`network`,
  `iam_denied`, `not_found`, `not_allowed`, `invalid_path`,
  `helper_missing`, `timeout`). `rayd` lanza `mount-s3` como el usuario
  dedicado `rayito-mount` (uid 990) con credenciales resueltas por su
  propio acceso a IMDS, nunca en argv ni en entorno; un daemon caído se
  relanza solo, con backoff. Política IAM `RayitoS3MountAccess` del
  componente `OptionalStack` `s3-mounts` (`infra/s3-mounts.yaml`, pide
  `CAPABILITY_IAM`; su plantilla ya va empaquetada en el SDK), acotada al
  bucket y a sus prefijos (hasta 4) también para leer, escribir y borrar
  objetos. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la
  imagen.
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
- **Catálogo de tamaños** (`m15-sizes-catalog`, ADR-019, opcional y
  apagado por defecto): `Sandbox.create({ size: "4gb" })`/
  `{ memoryMib: ... }` resuelve, enteramente en cliente y sin ningún RPC,
  al primer tamaño del catálogo cerrado (512mb/1gb/2gb/4gb/8gb, Q87) que
  cubra lo pedido (redondea siempre hacia arriba, avisa con
  `process.emitWarning(..., { type: "RayitoCompatWarning" })` si no encaja
  exacto) y antepone el sufijo de imagen (`rayito-base-4gb`) antes de
  resolver el ARN; `size` con un template dado por ARN, o por encima del
  máximo publicado, es `InvalidArgumentError` antes de cualquier llamada a
  AWS. `getInfo()` confirma, con una única llamada cacheada a
  `GetMicrovmImageVersion` por versión de imagen, `SandboxInfo.baselineMemoryMib`
  y `baselineCpu` (vCPU medido exactamente para los cinco tamaños del
  catálogo); `cpuCount`/`memoryMb` siguen siendo lo que el guest reporta de
  verdad. El shim `rayito/e2b` reporta ese mismo baseline en
  `SandboxInfo.cpuCount`/`memoryMB` cuando `size` se usó (como E2B reporta
  lo declarado por el template), la vista real del guest si no.
  Guardarraíles de coste opcional `sizes-guard` (`RayitoRunAllowedSizes`:
  un Deny de `lambda:RunMicrovm` fuera de los ARN de imagen permitidos,
  efectivo aunque la identidad ya tenga el `microvm-image:*` de la
  `CallerPolicy` estándar; `rayito image publish --sizes`/`--env` y
  `rayito image sizes` son sólo CLI Python). Sin `size`, el comportamiento
  sigue siendo exactamente el de 0.5.x.
<!-- m15-events-webhooks -->
- **`LifecycleEvents`** (`m15-events-webhooks`, opcional y apagado por
  defecto): despliega `infra/events-webhooks.yaml` (`deploy`/`status`/
  `destroy`, componente `events-webhooks` de `OptionalStacks`), registra
  webhooks compatibles con E2B (`registerWebhook`/`listWebhooks`/
  `deleteWebhook`, paginado) y lee el historial (`getEvents`, 1–100 filas,
  filtrado por tipo en DynamoDB), con `@aws-sdk/client-dynamodb` y
  `@aws-sdk/client-secrets-manager` como peers opcionales. Los errores de
  AWS llegan como `WebhookError` con sólo el código (`awsCode`). `events` en
  `Sandbox.create()` valida el tipo y que `logging` llegue a CloudWatch
  antes de lanzar y, tras `run-microvm`, manda la clave del sandbox (`k_sbx`,
  derivada de la clave del stack y el `sandboxId`; un `GetSecretValue` por
  instancia de `LifecycleEvents`) en el mismo `ConfigureSandbox` que
  `mounts`/`gateways`/`telemetry` (`LifecycleEventsSectionFactory`); sin la
  pila desplegada, sin `lifecycleEvents` en el agente o con la sección
  rechazada, termina el sandbox (salvo `keepOnFailure`). `events` pasa a
  tiparse `LifecycleEvents` en `SandboxCreateOptions`. Aceptado en AWS real (`AWS_API_NOTES.md`
  Q105–Q108); el `.gen.ts` lleva el código Lambda actualizado (comprobación
  exacta del log stream y una línea JSON por invocación del forwarder y del
  reconciliador).
<!-- m15-rayd-otlp -->
- **`telemetry`: exportación OTLP de `rayd` a CloudWatch** (`m15-rayd-otlp`,
  ADR-021, opcional y apagado por defecto): `TelemetryExport`/`OtlpAuth`
  nuevos (`OtlpAuth.executionRole()`, exige `rayito-base-caps`;
  `OtlpAuth.bearer(secretName)`, experimental, funciona en `rayito-base`;
  el nombre se resuelve bajo `rayito/` por la misma `SecretCache` que
  `secrets`). Se envía como una sección de `ConfigureSandbox` tras `/run`;
  una imagen cuyo `rayd` no exporta termina el sandbox (salvo
  `keepOnFailure`) y lanza `UnimplementedError`. Con `tracerProvider`, cada
  RPC del handle lleva además el `traceparent` del span de esa misma
  llamada hacia `rayd`; sin él, ninguna cabecera nueva. El token de
  `OtlpAuth.bearer(...)` es una API key de CloudWatch Metrics. `sbx.getTelemetryStatus()`
  lee `ConfigureStatus`, como una llamada explícita aparte de `getHealth()`.
  Sin `telemetry`, ningún comportamiento cambia frente a 0.5.x.
<!-- m15-templates -->
- **Templates declarativos** (`m15-templates`, ADR-022, opcional y apagado
  por defecto): `Template` compila un DSL (igual al `Template` de E2B v2)
  a un Dockerfile y un zip deterministas sobre una imagen `rayito-base`/
  `rayito-base-caps` ya publicada; `Template.build()`/`buildInBackground()`/
  `getBuildStatus()`/`exists()` suben el artefacto por hash de contenido y
  llaman a `create`/`update-microvm-image`, reutilizando una versión
  idéntica en vez de reconstruir. Un build fallido se explica con
  `BuildError` (`step`/`command`/`exitCode`/`logTail` del log de BuildKit,
  o `reason: "ready_client_error"|"ready_server_error"` si falló el
  `readyCmd`), sin repetir nada. `fromImage`/`fromTemplate`/
  `fromDockerfile`/`fromGcpRegistry`/`aptInstall` lanzan
  `UnimplementedError` (documentados en ADR-022). El shim
  `rayito/e2b`'s `Template` ya construye de verdad, con la firma de E2B
  (`{ alias, skipCache, memoryMB, cpuCount }`) y `new E2B({ bucket })`;
  `BuildError`/`TemplateError` del shim pasan a ser alias de las clases
  nativas (igual patrón que `NotEnoughSpaceError`/`FileUploadError`).
  `setStartCmd()` necesita una imagen base con `rayd` 0.6. La imagen
  compuesta hereda la configuración de la base
  (`additionalOsCapabilities` incluida); `skipCache()` equivale a
  `force: true`; la cuota de builds de AWS llega como
  `reason: "build_quota"`. El núcleo de build vive en
  `src/images/gateway.ts`. Sin llamar a
  `Template.build()`, el SDK no importa estáticamente
  `@aws-sdk/client-s3` ni carga `@aws-sdk/client-cloudwatch-logs` (peer
  opcional nuevo, sólo para explicar un build fallido). `infra/templates.yaml`
  (`rayito stack deploy templates`): sólo la política IAM
  `RayitoTemplateBuilder`, $0 en reposo, que no puede sobrescribir las
  imágenes base publicadas. Aceptación en AWS real: `pipInstall()` compila
  a `python3 -m pip install --no-cache-dir --break-system-packages`
  (rayito-base no tiene `pip` en el `PATH`); la versión gestionada que se
  hereda se envía como `1`, no como el eco `1.0` que `create-microvm-image`
  rechaza; la política concede `CreateMicrovmImage` sobre `*` (AWS no la
  autoriza por ARN) y `lambda:PassNetworkConnector` sobre los conectores
  gestionados, y su `Deny` cubre `UpdateMicrovmImage` sobre las bases
  (`AWS_API_NOTES.md` Q114-Q116).
<!-- m15-secrets-gateway -->
- **`gateways` — pasarela de secretos en loopback** (`m15-secrets-gateway`,
  M15, ADR-023, opcional y apagado por defecto): `Sandbox.create({ gateways:
  { nombre: new SecretGateway({ upstream, headers, allow, ... }) } })` abre,
  dentro del agente, un listener de loopback por ruta que reenvía sólo lo
  que su `allow` cubre, dentro de su límite de peticiones por minuto,
  inyectando cada cabecera vaultada (resuelta con la misma `SecretCache`
  que `secrets`, nunca antes de `Configure`) y eliminando primero
  cualquier cabecera del mismo nombre que el sandbox intente poner.
  `sbx.gateways.get("nombre")?.url` da la URL de loopback; `refresh()`
  rota el secreto sin recrear el sandbox (relee Secrets Manager aunque la
  `SecretCache` no haya vencido, conserva el puerto y lanza si `rayd`
  rechaza la sección). También `pool.take({ gateways })`. Cualquier fallo
  al configurarla tras `run-microvm` (imagen anterior a 0.6.0, flag
  ausente, secreto que falta, sección rechazada) termina el VM salvo
  `keepOnFailure`. Sin `gateways`, ningún cliente Secrets Manager nuevo se
  construye y no se manda ningún `ConfigureSandbox`.
<!-- m15-custom-domain -->

### Fixed

- `OptionalStacks.deploy("events-webhooks", { artifactBucket })` ya no exige
  repetir `parameters.ArtifactBucket`: `StackArtifact.bucketParameterKey`
  pasa a la plantilla el mismo bucket al que se sube el código, y un valor
  distinto es `InvalidArgumentError` antes de subir nada (aceptación 0.6 en
  AWS real).

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
  apagado por defecto, ADR-014): `tracerProvider` en `Sandbox.create()`
  (también con `pool`), `connect()` (ambas formas) y las variantes
  estáticas `kill`/`pause`/`resume`; instrumenta `commands.run`, `runCode`,
  `files.*` y `kill`/`pause`/`resume` de instancia con spans `rayito.*` de
  `SpanKind.CLIENT`. `@opentelemetry/api` es una peerDependency **opcional**
  importada sólo con `import type` (se borra en el build: ningún
  require/import en tiempo de ejecución, comprobado por `pack:check`), y
  `SpanKind.CLIENT`/`SpanStatusCode.ERROR` son constantes numéricas locales.
  Lista cerrada de atributos (nunca texto de comandos, código, rutas,
  `envs`, secretos ni metadata); un error del span lleva el nombre de la
  clase del error, nunca su `message`. $0 de AWS; el coste (si lo hay) es el
  del backend de exportación del llamante. Fuera de alcance: propagación
  `traceparent`/`tracestate` hacia `rayd`, telemetría del sandbox y el shim
  de E2B.

- **Índice de metadatos sobre DynamoDB** (`m14-metadata-index`, opcional y
  apagado por defecto, ADR-014): `new DynamoDbIndex({ tableName, region,
  credentials, onWriteFailure: "terminate", ttlMarginSeconds: 3600 })` y la
  opción `index` en `Sandbox.create()`, `Sandbox.list()`,
  `Sandbox.paginate()` y `PoolConfig`. `create()` escribe una fila inmutable
  (`PutItemCommand` condicional) tras `run-microvm`; con `metadata` e
  `index`, `list()`/`paginate()` unen `list-microvms` con
  `BatchGetItemCommand` por página y filtran también sandboxes `SUSPENDED`
  sin ninguna sonda. Nuevos errores `SandboxIndexError` e `IndexWriteError`.
  `@aws-sdk/client-dynamodb` es una peerDependency **opcional** que sólo se
  carga con `loadOptionalPeer` al usar el índice. Coste: ~1 WRU por sandbox
  creado y 0,5 RRU por candidato listado. `reincarnate()` conserva `index`:
  el sucesor escribe su propia fila.
- **Shim de E2B**: `Sandbox.list({ query: { metadata, state: ["paused"] },
  index })` y `new E2B({ index })`.
- **Secretos sobre AWS Secrets Manager** (`m13-secrets`, opcional y
  apagado por defecto, ADR-014): `new SecretStore({ region, credentials,
  prefix, kmsKeyId })` con `create`/`update`/`getInfo`/`exists`/`list`/
  `destroy`, y `new SecretCache({ ttlSeconds: 300 })` (una promesa en vuelo
  por clave; los aciertos no llaman a AWS; `refresh()`/`invalidate()`;
  `toJSON`/`inspect` nunca muestran valores). `@aws-sdk/client-secrets-manager`
  es una peerDependency **opcional** que sólo se carga con `loadOptionalPeer`
  al usar la función. Coste: $0,40/secreto-mes hasta `destroy` + $0,05/10 000
  llamadas.
- **`secrets` / `secretCache`** en `Sandbox.create()` (también con `pool`),
  `Sandbox.connect()`, `sbx.connect()`, `pool.take()` y, por llamada, en
  `commands.run`, `pty.create`, `runCode` (contextos Python) y
  `createCodeContext`: entregan el valor como variable de entorno por los
  `envs` que ya viajan a `rayd`, nunca en el `runHookPayload`, `metadata`,
  logs ni errores. La primera vez emiten un `RayitoCompatWarning`
  (`process.emitWarning`): el valor es visible para el código del sandbox.
- `SecretError`/`SecretNotFoundError` en `rayito` y en `rayito/e2b`.
- **Shim de E2B**: `Secret` (estáticos `create`/`update`/`getInfo`/`list`/
  `exists`/`destroy`/`fill`/`iamToken`), `SecretPaginator` y el tipo
  `SecretInfo` sobre `SecretStore`, con los nombres de `e2b` 2.51.0;
  `new E2B({ region }).Secret` usa esa región.

### Changed

- El motivo de `UnimplementedError` de `list(query.state=paused,
  query.metadata)` en `rayito/e2b` nombra ahora la opción `index`.
- `rayito/e2b` `Secret` y `E2B(...).Secret` ya no lanzan `UnimplementedError`.

### Fixed

- `SecretStore.destroy()` (y `Secret.destroy` de `rayito/e2b`) devolvía
  `true` para un nombre que nunca existió, porque AWS acepta el
  `DeleteSecret` forzado de un nombre inexistente sin
  `ResourceNotFoundException` (aceptación de 0.5.0 en AWS real). Ahora hace
  `DescribeSecret` antes y devuelve `false` sin borrar si no existe (o ya
  estaba programado para borrarse, o lo borró otro a la vez), como documenta
  y como E2B. Usa `secretsmanager:DescribeSecret`, ya incluido en
  `RayitoSecretsAdmin`.
- `SecretStore.create()` sobre un nombre recién borrado agotaba su
  presupuesto de reintentos de 30 s: AWS liberó el nombre tras 19–28 s y el
  backoff no llegaba. El presupuesto es ahora de 60 s
  (`CREATE_RETRY_BUDGET_MS` en `src/secrets/store.ts`) con ±25 % de
  jitter.
- `docs/site/docs/secrets.md` avisa de que un `logger` del
  `SecretsManagerClient` del SDK v3 imprime los valores (Rayito nunca lo
  hace) y de que `list()` es eventualmente consistente (~3–5 s).

## [0.4.0] - 2026-09-30

### Cambios que rompen

- `UnimplementedError` es ahora el único tipo para "algo que este sandbox no
  puede dar": un `Unimplemented` de gRPC (un RPC o un kernel de `runCode`/
  `createCodeContext` ausente en la imagen) ya no es `InvalidArgumentError`/
  `SandboxError` con `grpcCode: Code.Unimplemented`, sino `UnimplementedError`,
  fuera de esa jerarquía. `LifecycleUnsupportedError` (un plazo lógico contra
  un agente anterior a M9) pasa a ser subclase de `UnimplementedError`, no de
  `InvalidArgumentError` ni `SandboxError`; su `feature` nombra la llamada que
  pidió el plazo (`connect({ timeoutMs })` también cuando el agente responde
  `Unimplemented` al `SetTimeout` de `connect`) y su `reason` usa los mismos
  textos que el SDK Python. El `reason` de un kernel ausente es el mensaje de
  `rayd` (que ya nombra `rayito-base-poly`), sin la pista de publicar una
  imagen actual. `getMetricsHistory` contra una
  imagen anterior a M9 ya lanzaba `UnimplementedError`; sólo cambia su
  `error.cause`, de `InvalidArgumentError` a `UnimplementedError`.
  **Migración**: cambia `catch (error) { if (error instanceof
  InvalidArgumentError && error.grpcCode === Code.Unimplemented) ... }` o
  `catch (error) { if (error instanceof SandboxError) ... }` alrededor de esos
  RPCs por `error instanceof UnimplementedError` (`LifecycleUnsupportedError`
  sigue distinguiéndose con su propio `instanceof`).
- `Sandbox.probedInfo` (el `static` nativo que usaba internamente `rayito/e2b`)
  se elimina de `rayito`: vivía fuera de lugar en el núcleo nativo y su propio
  docblock ya decía que era interno del shim. **Migración**: ninguna para
  quien no lo llamaba directamente (`rayito/e2b`'s `Sandbox.getInfo` hace
  exactamente lo mismo); quien lo usara pasa a `Sandbox.getInfo(sandboxId)`
  de `rayito/e2b`.

### Changed

- Una opción de conexión de una llamada de instancia del shim de E2B
  (`sbx.kill({ retries, proxy })`, `sbx.setTimeout(ms, { headers })`...) que
  esa llamada no aplica —el canal y el plano ya están construidos— avisa
  ahora con `process.emitWarning` (`RayitoCompatWarning`, nombrando sólo la
  opción, nunca su valor) en vez de perderse en silencio; pásala al crear o
  conectar el sandbox, o usa la variante estática (`Sandbox.<método>(sandboxId,
  ...)`), que sí la aplica. Sin cambio de comportamiento, sólo avisos nuevos.

## [0.3.3] - 2026-09-29

### Security

- `clients/typescript/.npmrc` nuevo (`ignore-scripts=true`): ningún `pnpm
  install` local o de CI ejecuta scripts de ciclo de vida de dependencias.
  `.github/workflows/release.yml`: el job `typescript` se parte en
  `typescript-build` (sin `environment` ni `id-token`, `pnpm install
  --frozen-lockfile --ignore-scripts`) y `typescript-publish` (con el token
  OIDC de npm, sin checkout ni `pnpm`, que verifica con `sha256sum -c` que
  el `.tgz` descargado es el que produjo `typescript-build` antes de `npm
  publish --ignore-scripts`) — cierra C-10/H-02 residuo
  (`docs/SECURITY_AUDIT.md`). Sin cambio de comportamiento en lo publicado
  ni en el Trusted Publisher de npm.

### Changed

- `sbx.files` y `sbx.native.files` del shim de E2B comparten ahora un único
  `TransferClient` (y por tanto una sola caché de soporte de transferencias
  y un solo cliente S3 por sandbox): el `Filesystem` nativo acepta un
  segundo parámetro interno `sharedWith` que reutiliza el `TransferClient`
  del `Filesystem` nativo del que el shim parte, en vez de construir uno
  nuevo sobre el mismo `SandboxCore`. Sin cambio de comportamiento para
  quien sólo use `sbx.files`; `sbx.native.files` deja de duplicar la sonda
  y los clientes S3 (`.d.ts` publicado: sólo gana un parámetro de
  constructor opcional).
- El puerto `ControlPlane` declara ahora `readonly awsClientSettings?:
  AwsClientSettings`: las credenciales y el proxy que los clientes S3 de
  las transferencias heredan del plano de control (ADR-010) ya no llegan
  por un downcast estructural (`AwsClientSettingsSource`, eliminada), sino
  por un campo explícito del puerto que cualquier decorador debe reenviar.
  `awsClientSettingsOf` sigue exportada con el mismo nombre y la misma
  firma; sin cambio de comportamiento.

### Internal

- `sandbox/core.ts` ya no depende de `sandbox/commands.ts`: la clasificación
  de fallos de stream (`STREAM_PROBE_TIMEOUT_MS`, `streamFailureError`) vive
  en el nuevo `sandbox/stream-errors.ts`, sin ciclo con `core.ts` ni con
  `commands.ts`; `commands.ts` reexporta ambos nombres, así que ningún
  import existente cambia. Sin cambio de comportamiento.
- `e2b/compat.ts` (el mapeo puro que refleja `_compat.py`) absorbe
  `historyImageError`, `lifecycleImageError`, `LIFECYCLE_IMAGE_REASON` y la
  nueva `metricsHistoryOrSnapshot`, antes en la clase IO `e2b/sandbox.ts`;
  `Sandbox.getMetrics` y `Sandbox.metricsFor` (estático) llaman a las
  versiones movidas. Sin cambio de comportamiento.

## [0.3.2] - 2026-09-29

### Changed

- `typescript` (dependencia de desarrollo) actualizado de 5.9.3 a 7.0.2 (el
  compilador nativo, `tsgo`, vía `rolldown-plugin-dts`): `pnpm lint
  typecheck build test pack:check` verdes y los `.d.mts`/`.d.cts` generados
  byte a byte idénticos a los de 5.9.3. Sin cambios de API pública.

### Tests

- Cobertura explícita de reconexión tras un `RST_STREAM(CANCEL)` del proxy
  de AWS (`Canceled` con el prefijo `http/2 stream closed`, ya distinguido
  de un `AbortSignal`/`Canceled` propio por `isStreamReset`/`isOwnAbort` en
  `transport/errors.ts`, sin cambio de comportamiento): un stream de
  comandos en vivo reconecta con `Connect(pid, from_seq)`, agota el mismo
  presupuesto de tres reintentos que un `Unavailable`, y `disconnect()`
  sigue ganando la carrera aunque el corte llegue con la forma de un reset
  reconectable justo después (`tests/unit/reconnect.test.ts`), en paridad
  con el arreglo del SDK de Python para el `CANCELLED "Stream removed"` de
  grpcio (`AWS_API_NOTES.md` #33).

## [0.3.1] - 2026-09-28

Versión de mantenimiento, sin cambios de API.

### Documentation

- Instalación del SDK con pnpm, npm, yarn o bun en el README del paquete
  (el de npmjs.com) y en el quickstart; probado en un proyecto limpio con
  ESM, CommonJS, `rayito/e2b` y `tsc --strict`.

### Changed

- Dependencias del AWS SDK v3 actualizadas a 3.1140.0 en el lockfile.

## [0.3.0] - 2026-09-24

Rayito 0.3.0 (M9, paridad con E2B 2.x). Notas completas en
`docs/RELEASE_NOTES_0.3.0.md`; todo lo de M9 exige una imagen publicada con
el `rayd` de M9.

### Cambios que rompen

- Un disco lleno es `DiskFullError` (antes `RateLimitError`): un `catch`
  que distinguía el disco lleno por `RateLimitError` deja de verlo. En
  `rayito/e2b` se exporta también como `NotEnoughSpaceError`.

### Added

- **Transferencias por URLs prefirmadas de S3** (`m9-file-transfer`,
  ADR-010): `Sandbox.create/connect({ transfer })` con un `S3Staging`
  (`bucket` DNS sin puntos, `prefix`, `region`, `maxExpiresIn`,
  `thresholdBytes`, `multipartThresholdBytes`) o las variables
  `RAYITO_TRANSFER_BUCKET`/`_PREFIX`/`_REGION` (`null` lo desactiva), y el
  getter `sandbox.transfer`. `files.uploadUrl(path, { user, expiresIn,
  maxBytes, form })` → `UploadTicket` (la URL como `toString()`, `method`,
  `headers`, `fields`, `wait({ timeoutMs })`, `status()`, `cancel()`; de un
  solo uso) y `files.downloadUrl(path, { user, expiresIn, filename })` →
  `DownloadLink` (una foto con `size` y `sha256`); `sandbox.uploadUrl` /
  `sandbox.downloadUrl(path, { user, useSignatureExpiration })` devuelven la
  URL como `string` (la forma de E2B). El SDK firma con tus credenciales
  (SigV4, host virtual regional, `requestChecksumCalculation:
  "WHEN_REQUIRED"`); `rayd` no guarda ninguna. Las URLs nunca se registran,
  serializan (`toJSON` lanza) ni imprimen.
- Con `transfer` configurado, `files.write`/`writeFiles` de lo que mide
  `>= thresholdBytes` (y de todo `ReadableStream`, subido en streaming con
  `@aws-sdk/lib-storage`) y `files.read` de un fichero así van por S3 con el
  sha256 comprobado; sin `transfer` el camino gRPC no cambia. Los cuatro
  paquetes de S3 se cargan con `import()` sólo al usarlos.
- `files.read(path, { gzip, streamIdleTimeoutMs, format: "blob" })` y
  `files.write/writeFiles(…, { gzip, metadata, useOctetStream })`; el gzip de
  escritura usa un transporte sobre la sesión HTTP/2 de los unarios (nunca
  una tercera conexión). `EntryInfo.metadata` (claves en minúsculas).
  `metadata`, `gzip` al escribir y las transferencias exigen un agente M9:
  en uno anterior lanzan `UnimplementedError` antes de enviar nada.
- Errores `TransferError` (`code`, `reason`), `FileUploadError` y
  `DiskFullError`; los códigos de `StreamError` `failed_precondition`,
  `resource_exhausted`, `unavailable` y `cancelled`.
- Los clientes S3 de las transferencias heredan del plano de control sus
  `credentials` y su `proxy` (`LambdaMicrovmsControlPlane.fromRegion(region,
  { credentials, proxy })`, también detrás de un pool), como el SDK Python
  firma con la sesión del sandbox; antes usaban la cadena por defecto y
  salían sin pasar por el proxy.

- **Plazo del servidor** (`m9-server-timeout`, ADR-011; exige una imagen M9):
  `Sandbox.create({ maxLifetimeMs, onTimeout: "kill" | "pause" })`; con
  cualquiera de los dos, `timeoutMs` es un plazo lógico que `rayd` hace
  cumplir aunque el cliente muera. `sandbox.setTimeout(timeoutMs)` y el
  estático `Sandbox.setTimeout(sandboxId, timeoutMs, { accessToken })`
  (EXACT), `sandbox.connect({ timeoutMs })` y `Sandbox.connect(id, {
  timeoutMs })` (AT_LEAST); `SandboxLifecycle` en `getHealth()`/`getInfo()`;
  `LifecycleUnsupportedError` contra una imagen anterior (el VM se termina).
- **Historial de métricas y listado** (`m9-sandbox-observability`):
  `getMetricsHistory({ start, end, maxPoints })` (instancia y estático con el
  access token; `UnimplementedError` en una imagen anterior, con el mismo
  motivo que Python y el error gRPC en `cause`), `SandboxMetrics.memCacheBytes`, `SandboxHealth.cpuCount` y
  `memoryTotalBytes`, `Sandbox.paginate({ limit, nextToken, order,
  startedAfter })` → `SandboxListPaginator` y `list({ metadata,
  startedAfter, order })`.
- **Política de egress** (`m9-egress-policy`, ADR-012): `Sandbox.create({
  network, allowInternetAccess })`, `updateNetwork()` (instancia y estático),
  `getNetwork()`, `ALL_TRAFFIC`, `EgressEnforcement` y
  `SandboxHealth.egressEnforcement`; sólo en `rayito-base-caps`, en otra
  imagen el SDK termina el VM y lanza `UnimplementedError`.
- `runCode(code, { language: "typescript" })` (alias `ts`) y `"javascript"`
  (`js`) con el kernel de Deno de `rayito-base-poly` (`m9-deno-kernels`).
- `sandbox.git` (`Git`, `GitAuthError`, `GitUpstreamError`), `extraHeaders`
  en `transport` y `proxy` en el plano de control (`m9-e2b-v2-surface`).
- **Entrada `rayito/e2b`** (`m9-e2b-v2-surface`): el shim de la API JS de
  E2B 2.x (`import { Sandbox } from "rayito/e2b"`, export por defecto
  `Sandbox`), con `E2B`, `ConnectionConfig`, estáticos `kill`/`getInfo`/
  `getFullInfo`/`isRunning`/`connect`/`pause`/`setTimeout`/`getMetrics`/
  `list`/`updateNetwork`, `getHost` síncrono, `trafficAccessToken`,
  `NotEnoughSpaceError`, `ServiceBusyError` y `UnimplementedError` explícito
  para lo que no tiene primitiva. `pack:check` exige los cuatro
  `dist/e2b.*`. Como en Python, `getMetrics` (instancia con rango y
  estático) contra una imagen anterior a M9 y `list` con `metadata` y un
  estado `paused` lanzan `UnimplementedError` con el motivo de Python; el
  `getMetrics` estático sin token es `AuthenticationError` (diferencia
  documentada en `docs/site/docs/e2b-compat.md`).

### Fixed

- **El proceso de Node ya no queda vivo hasta el deadline de un stream**: un
  script que llamaba a `runCode` tardaba ~315 s en salir tras su última
  línea (~65 s con `commands.run`). En `@connectrpc/connect` 2.x abortar un
  server-stream sin volver a leerlo no limpia el timer (con ref) de su
  `timeoutMs`, y el SDK abortaba así todo stream ya terminado (tras el
  `EndEvent`) o abandonado. Ahora, al abortar el controller de cualquier
  stream (`runCode`, `commands`, `pty`, `watchDir`, lecturas y vigilancia de
  transferencias), el SDK pide una lectura más que hace a connect soltar el
  timer; el deadline sigue imponiéndose igual mientras el stream vive.
- **Auto-resume tras una pausa por el plazo con el cliente vivo**: en modo
  `onTimeout: "pause"` con `idle.autoResume`, si este cliente suspendió el
  sandbox al vencer y la suspensión real duró menos de 2 s (el vigilante de
  `rayd` no la reconoce como congelación), el sandbox volvía `expired` y la
  siguiente llamada fallaba con `sandbox_timeout`. Ahora el SDK aplica la
  regla de E2B (`max(timeout, 300 s)`, acotada al tope menos 5 s) con un
  `SetTimeout` en el primer `sandbox_timeout` tras reanudar, como en Python.
  Ese `SetTimeout` va por la unaria que reconecta (un `Unavailable`
  transitorio se reintenta), la marca de un solo uso sólo se consume cuando
  la `resumeGeneration` ya avanzó y el `SetTimeout` respondió, y los callers
  concurrentes comparten una sola promesa de reapertura (un único
  `SetTimeout`).
- `sandbox.getInfo()` sólo relee `Health` cuando el sandbox está `RUNNING`
  **con plazo lógico gestionado** (ADR-011): antes sondeaba cualquier
  sandbox `RUNNING`, y cada `Health` es tráfico de entrada que reinicia el
  contador de idle de la plataforma, así que sondear `getInfo()` impedía la
  auto-suspensión. `metadata` y los hechos del guest salen del último
  `Health` sin RPC extra.
- La readiness de `create()`, `connect()` y `resume()` exige además un
  `Health` con `sandboxId`: el proxy deja pasar `Health` antes de que `rayd`
  reciba `/run`, y ese `Health` (kernel del snapshot sin rotar) daba por
  listo un sandbox cuya rotación de `/run` ponía `kernelReady=false` justo
  después (AWS_API_NOTES.md Q78), como en Python.
- `requestTimeoutMs` acota también la pata S3 de una escritura enrutada y de
  una lectura enrutada `bytes`/`text`/`blob` (aborta la subida o el
  `GetObject`, borra el objeto de staging y lanza `TimeoutError`), como en
  Python.
- `signal` llega a `setTimeout`, `connect`, `isRunning`, `getHealth`,
  `getMetrics`, `getMetricsHistory`, `getNetwork` y `updateNetwork` (de
  instancia y estáticos) y a los mismos métodos de `rayito/e2b`.
- `rayito/e2b`: `Sandbox.getInfo(id)` sondea un `Health` sobre un sandbox
  `RUNNING` (`endAt` es el plazo lógico, con `metadata` y `lifecycle`); un
  `httpsPorts` no vacío es `UnimplementedError` (regla QE2, como Python);
  `network`/`allowInternetAccess` siguen D10 (política leída sea cual sea su
  `enforcement`, respaldo en `INTERNET_EGRESS`) y `getInfo()` sólo absorbe
  `UnimplementedError` de `GetNetwork`. `SandboxInfo` gana `ingress`/`egress`.
- Un `nextToken` con `startedAtMs` negativo es `InvalidArgumentError`.
- `SandboxInfo` gana `metadata` (los de `create({ metadata })` leídos del
  último `Health`; `undefined` si no se leyeron), como en Python:
  `sandbox.getInfo()` los trae y, en `rayito/e2b`,
  `(await Sandbox.connect(id)).getInfo().metadata` ya no sale vacío.
- `rayito/e2b`: `runCode`/`createCodeContext` siguen el contrato de kernels
  de Python: `r`, `java` y el resto son `UnimplementedError` sin tocar el
  agente, y el `Unimplemented` de una imagen sin el kernel también (con el
  error nativo en `cause`); `python` viaja sin `language`.
- `rayito/e2b`: un `onEvent` de `files.watchDir` o un `onData` de
  `pty.create`/`pty.connect` asíncrono que rechaza se registra como aviso en
  vez de quedar como una promesa rechazada sin manejar (que termina Node).
- `UnimplementedError` acepta `{ cause }` como cuarto argumento.

### Security

- El access token ya no aparece al inspeccionar (`util.inspect`,
  `console.log`), serializar ni hacer spread de `LaunchPlan`, del núcleo del
  sandbox ni de un `SlotRecord` del pool, ni el `runHookPayload` (con los
  `envs`) de `LaunchRequest`: son propiedades no enumerables, como el
  `repr=False` de Python.
- **`JsonFilePoolBackend` escribe por un temporal exclusivo** (auditoría
  interna, fila H-04; mismo defecto que H-03 en el SDK Python): el fichero del
  pool se escribía por `<path>.tmp`, un nombre fijo que otro usuario del
  sistema podía crear o apuntar con un enlace antes que el SDK; ahora se abre
  con `open(temp, "wx", 0o600)` sobre un nombre aleatorio, se fija el modo con
  `handle.chmod()` antes de escribir y se renombra al destino. La lectura usa
  `O_NOFOLLOW`. Sin esto la promesa de `0600` de T14 no era cierta.
- **Los errores de AWS ya no exponen la petición firmada**: el `cause` de
  los errores que traduce el plano de control (`translateAwsError`) era el
  error crudo del SDK v3, cuyo `$response` arrastra la petición HTTP y los
  buffers del socket; `util.inspect`, `console.error` o el "Serialized
  Error" de vitest imprimían `authorization: AWS4-HMAC-SHA256
  Credential=ASIA…` y `x-amz-security-token`. Ahora el `cause` es un
  resumen (`sanitizeAwsError`, `src/aws/sanitize.ts`) con sólo `name`,
  `code`, el mensaje redactado, `$fault` y `$metadata.{httpStatusCode,
  requestId, extendedRequestId, attempts}`. Los errores de S3 de las
  transferencias (`translateS3Error`), que no llevaban `cause`, ganan el
  mismo resumen sin mensaje (para conservar el `requestId`). El mensaje de
  un `InvalidSignatureException`/`SignatureDoesNotMatch`, que AWS devuelve
  con la cadena canónica (y el token de sesión dentro), pierde esa parte y
  cualquier cabecera de firma, parámetro `X-Amz-*` de una URL prefirmada o
  id de clave de acceso. `statusCode`, `awsCode`, `retryAfter` y
  `quotaCode` no cambian.

## [0.2.0] - 2026-09-17

Primera versión con número de lockstep: el SDK TypeScript salta de 0.0.5 a
0.2.0 para llevar la misma `MAJOR.MINOR` que `rayito` 0.2.0 en Python y
`rayd` 0.2.0 (`docs/RELEASING.md`, plugin `linked-versions`). Aceptado
contra AWS real en M7 (`MILESTONES.md`).

### Added

- **Persistencia del `HOME` en S3** (`m7-s3-persistence`, ADR-009):
  `S3Prefix({ bucket, prefix, name, region })`, `Sandbox.create({ persist,
  persistTimeoutMs })` (exige `executionRoleArn`; con `name` restaura en
  `sandbox.lastRestore`), `Sandbox.connect(id, { persist })`,
  `sandbox.persist`, `checkpointFiles({ target, exclude, timeoutMs,
  onProgress })` → `CheckpointResult`, `restoreFiles({ source, timeoutMs,
  onProgress })` → `RestoreResult` (`NotFoundError` sin checkpoint) y
  `reincarnate({ exclude, persistTimeoutMs })`; nuevos `PersistenceError`
  (`code`), `CheckpointProgress`, `RestoreProgress`, `LaunchOptions`,
  `DEFAULT_PERSIST_TIMEOUT_MS`; `create({ pool })` rechaza `persist`.
- `runCode(code, { language })` y `createCodeContext({ language })` aceptan
  `python`, `bash` y `javascript` (alias `js`, sin distinguir mayúsculas;
  `m7-poly-kernels`): `language` selecciona el contexto por defecto de ese
  kernel (`default-bash`), creado por el agente en la primera celda, y es
  excluyente con `context` (`InvalidArgumentError`). El kernel `bash` sólo
  lo trae la variante de imagen `rayito-base-poly`; `javascript` es un
  nombre reservado que hoy ninguna imagen trae (`AWS_API_NOTES.md` Q57). Un
  kernel que la imagen no trae es `InvalidArgumentError` (`Unimplemented`)
  con `rayito-base-poly` en el mensaje; `envs` por ejecución sólo en
  contextos Python (`docs/site/docs/kernels.md`).
- **Pool de sandboxes suspendidos** (`SandboxPool`, `PoolConfig`,
  `PoolStats`, `PoolSlotInfo`, `PoolBackend`, `InMemoryPoolBackend`,
  `JsonFilePoolBackend`, `PoolClosedError`; ADR-008, `m7-suspended-pool`):
  N MicroVMs calentados con `create()`, asentados con una celda trivial,
  aparcados con `pause({ wait: true })` y entregados por `take()` con
  `resumeMicrovm` explícito y el token de la plaza (sin `getMicrovm` en la
  toma); fallback a `create()` sin plaza; relleno con backoff 1 s → 60 s
  por los token buckets compartidos; reciclado antes del muro de 8 h y
  reconciliación con `listMicrovms`; recuperación desde el backend JSON
  (`rayito.pool/1`, el mismo fichero que escribe Python); temporizadores
  sin referencia; `Sandbox.create({ pool })` como azúcar que rechaza toda
  opción de lanzamiento. Documentación en `docs/site/docs/pool.md`.
- `metadata` y `cpuTimeLimit` en `Sandbox.create()` (el mismo
  `runHookPayload` que Python: `metadata` y `limits.cpu_seconds`), y
  `sandbox.launchInfo` (la `SandboxInfo` con la que se abrió el handle).
- Un endpoint con puerto explícito (`host:puerto`) manda sobre
  `transport.port` (los `rayd` falsos por plaza de los tests).

### Changed

- Licencia MIT → Apache-2.0 (`"license": "Apache-2.0"` en `package.json`,
  `LICENSE` con el texto Apache-2.0 y `NOTICE` incluidos en el tarball;
  `pnpm pack:check` los exige).

## [0.0.5] - 2026-09-16

Primera versión candidata a publicar (**aún no publicada en npm**; el
primer `npm publish` es manual, `docs/RELEASING.md` §3). Es la misma
generación de SDK que `rayito` 0.1.0 en Python: la superficie aceptada contra
AWS real en M6 (`MILESTONES.md`), en camelCase y milisegundos, sólo async.

### Added

- **Ciclo de vida**: `Sandbox.create` (`run-microvm` + JWE del proxy + sondeo
  de `Health` hasta `agentReady` y `kernelReady`), `connect`, `kill`,
  `list`, `getInfo`, `isRunning`, `getHost(port)` con las cabeceras del
  proxy, `getHealth`, `getMetrics`; `idle` (auto-suspensión a los 300 s por
  defecto); `timeoutMs` como vida máxima del MicroVM (tope 8 h);
  `await using` para terminar al salir del bloque.
- **Comandos**: `sbx.commands.run` en foreground y background, `onStdout` /
  `onStderr`, `stdin`, `list`, `kill`, `connect(pid, { fromSeq })`,
  `sendStdin`, `closeStdin`; `CommandHandle` con `wait`, `kill`,
  `disconnect`, iteración `for await` y reconexión.
- **Ficheros**: `sbx.files.read` (`text` / `bytes` / `stream`), `write`,
  `writeFiles` en un solo stream, `list({ depth })`, `exists`, `getInfo`,
  `remove`, `rename`, `makeDir`, `watchDir` con `WatchHandle`.
- **Código**: `sbx.runCode` sobre kernels Jupyter con estado, `Execution`
  con `results` (mime bundles y charts de E2B), `logs` y `error` como dato;
  contextos (`createCodeContext`, `listCodeContexts`, `removeCodeContext`,
  `restartCodeContext`).
- **PTY y suspend/resume**: `sbx.pty` (`create`, `connect`, `sendInput`,
  `resize`, `kill`; `PtyHandle`), `pause()` / `resume()` con procesos, PTYs,
  watches y kernels vivos al otro lado, el contrato de reconexión
  (`Connect(fromSeq)`, `Pty.Connect`, `WatchDir`, `Reattach`) y
  `reconnectTimeoutMs`.
- **Red y rol**: `executionRoleArn` (ninguno por defecto), `ingress` /
  `egress` con nombres gestionados (`ALL_INGRESS`, `INTERNET_EGRESS`, ...) o
  ARNs de conectores propios; `getHealth()` expone `agentVersion`,
  `resumeGeneration`, `clockOffsetMs` y `kernelStateLost`.
- Empaquetado: ESM + CJS con `tsdown`, tipos `.d.mts` / `.d.cts`, `exports`
  map, tarball comprobado (`scripts/pack-check.mjs`), `limits.ts` generado
  desde `limits.json` (`scripts/gen_limits.py`).

### Requisitos de imagen

- `runCode` necesita `rayito-base` ≥ 7.0 (sidecar de kernels); `pty` y la
  reconexión tras `pause()` necesitan ≥ 10.0 (M5). La aceptación de 0.0.5
  corrió sobre `rayito-base` 16.0 (`pnpm test:e2e`, 2026-09-16).

### Limitaciones conocidas

- Sólo async: no hay árbol síncrono (Node no tiene cliente gRPC bloqueante).
- Sin metadatos por sandbox (`create({ metadata })`, `list({ metadata })`),
  sin `cpuTimeLimit` ni campos de endurecimiento en `Health`: la superficie
  de M6 del SDK Python llega al SDK TypeScript en una versión posterior.
- La vida de un sandbox no se puede extender (no existe `UpdateMicrovm`);
  tope 8 h running + suspended.
- Sólo kernels Python; sin URLs firmadas; sin historial de métricas.

## [0.0.1] - [0.0.4]

Builds internos de los hitos M1-M5, nunca publicados.

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.7.1...HEAD
[0.7.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.7.0...typescript-v0.7.1
[0.7.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.6.1...typescript-v0.7.0
[0.6.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.6.0...typescript-v0.6.1
[0.6.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.5.1...typescript-v0.6.0
[0.5.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.5.0...typescript-v0.5.1
[0.5.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.4.0...typescript-v0.5.0
[0.4.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.3...typescript-v0.4.0
[0.3.3]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.2...typescript-v0.3.3
[0.3.2]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.1...typescript-v0.3.2
[0.3.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.0...typescript-v0.3.1
[0.3.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/typescript-v0.3.0
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.2.0.md
