# Changelog

Todos los cambios notables del agente `rayd` (`crates/rayd`, con
`crates/rayd-core` y `crates/rayito-proto`, que comparten versión por
`[workspace.package]`). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/). `rayd` no se publica en crates.io: se
distribuye como binario estático `aarch64-unknown-linux-musl` dentro de la
imagen `rayito-base` y como asset de la GitHub Release del tag `rayd-v*`.

## [Unreleased]

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
