# Changelog

Todos los cambios notables del paquete `rayito` (SDK TypeScript). El formato
sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Security

- **`JsonFilePoolBackend` escribe por un temporal exclusivo** (auditoría
  interna, fila H-04; mismo defecto que H-03 en el SDK Python): el fichero del
  pool se escribía por `<path>.tmp`, un nombre fijo que otro usuario del
  sistema podía crear o apuntar con un enlace antes que el SDK; ahora se abre
  con `open(temp, "wx", 0o600)` sobre un nombre aleatorio, se fija el modo con
  `handle.chmod()` antes de escribir y se renombra al destino. La lectura usa
  `O_NOFOLLOW`. Sin esto la promesa de `0600` de T14 no era cierta.

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
