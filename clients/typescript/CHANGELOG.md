# Changelog

Todos los cambios notables del paquete `rayito` (SDK TypeScript). El formato
sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [0.3.0](https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.2.0...typescript-v0.3.0) (2026-09-24)


### ⚠ BREAKING CHANGES

* el shim rayito.e2b / rayito/e2b sigue la superficie de E2B 2.x (pause devuelve bool, set_timeout ya no lanza UnimplementedError, nuevos órdenes posicionales); ver la sección de cambios incompatibles de los CHANGELOG de Python y TypeScript.

### Added

* M9, paridad con E2B (Rayito 0.3.0) ([8409453](https://github.com/alejandro-cedeno-10/rayito/commit/8409453792b8f34cc1d61ecaed0c388542eac91f))
* Rayito 0.2.0, SDK de sandboxes para agentes sobre AWS Lambda MicroVMs ([dde681c](https://github.com/alejandro-cedeno-10/rayito/commit/dde681c4858ae548aa550b2e7ab58f21ef8a29d3))


### Fixed

* **sdk:** no exponer credenciales de AWS en los errores traducidos ([815b03d](https://github.com/alejandro-cedeno-10/rayito/commit/815b03d89987c5a5a6d44e3e23ed0148766a225c))

## [Unreleased]

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

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.2.0...HEAD
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.0.5...typescript-v0.2.0
[0.0.5]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/typescript-v0.0.5
