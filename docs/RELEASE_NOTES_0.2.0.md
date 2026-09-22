# Rayito 0.2.0 — notas de la release (2026-09-17)

Segunda release: M7 ("preparación open source") aceptado contra AWS real
(cuenta de desarrollo, `us-east-1`) sobre `rayito-base` con `rayd` 0.2.0.
Todo número de este documento está medido; la fuente de cada uno es
`MILESTONES.md` (fila M7 y bloque "Estado de aceptación") o
`AWS_API_NOTES.md` §16 (Q52–Q57).

## Versiones

Los tres componentes suben a **0.2.0 en lockstep** (`docs/RELEASING.md`,
plugin `linked-versions` de release-please): SDK Python `rayito` 0.1.0 →
0.2.0, SDK TypeScript `rayito` 0.0.5 → 0.2.0 y `rayd` 0.1.0 → 0.2.0
(`Health.agent_version`). `rayito doctor` evalúa la tabla de
`docs/site/docs/limits.md`: el SDK 0.2 exige `agent_version` ≥ 0.2.0
(`Checkpoint`/`Restore` y `ExecuteRequest.language`); la versión de imagen
sigue siendo un contador de builds por cuenta, no un criterio.

Licencia: **Apache-2.0** en los tres manifiestos, `LICENSE` + `NOTICE` en la
raíz, en la wheel (`License-Expression: Apache-2.0`, `licenses/NOTICE`) y en
el tarball de npm (`pnpm pack:check`). `e2b_charts` sigue vendorizado bajo
su licencia MIT (`NOTICE`).

## Qué trae (siete cambios OpenSpec, `MILESTONES.md` M7)

1. **`m7-oss-hygiene`**: Apache-2.0, `CONTRIBUTING.md` (DCO),
   `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, sección de reporte en
   `SECURITY.md`, `CODEOWNERS`, plantillas de issue/PR, `dependabot.yml`,
   metadatos PEP 639, insignias, "Qué corre dónde" en `ARCHITECTURE.md`,
   `docs/RELEASING.md`.
2. **`m7-supply-chain`**: acciones fijadas por SHA + `actionlint`,
   `permissions: contents: read`, harden-runner, OpenSSF Scorecard,
   `deny.toml` + `cargo deny`, `cargo auditable` (`.dep-v0` con 256 crates)
   + SBOM CycloneDX 1.5, `pip-audit` ×4 + `pnpm audit`, job
   `ubuntu-24.04-arm`, `e2e.yml` con OIDC y guardas de coste, release-please
   manifest + `linked-versions`, `release.yml` por tag (PyPI attestations,
   npm trusted publishing, `rayd` firmado con cosign keyless), `Dockerfile`
   `FROM` por digest y `--base-image-version` obligatoria (Q52).
3. **`m7-suspended-pool`** (ADR-008): `SandboxPool`/`AsyncSandboxPool`
   (Python) y `SandboxPool` (TypeScript), `PoolConfig`, backends en memoria y
   JSON (`rayito.pool/1`), `Sandbox.create(pool=)`; custodia del secreto por
   plaza (`SECURITY.md` T14). Medido en la implementación: `take()` →
   primera celda **p50 0,77 / p95 0,90 s** frente a `create()` **p50 6,15 /
   p95 6,49 s** (20 tomas, `hits` 20, `misses` 0), reciclado antes del muro
   de 8 h y recuperación desde JSON con `launched == 0`.
4. **`m7-s3-persistence`** (ADR-009): `FilesystemService.Checkpoint`/`Restore`
   nativos en `rayd` (tar.gz del `HOME` a S3 con las credenciales IMDSv2 del
   execution role como root; uid 1000 sigue sin IMDS en `rayito-base-caps`),
   `Sandbox.create(persist=S3Prefix(...))`, `checkpoint_files()`,
   `restore_files()`, `reincarnate()` para el muro de 8 h;
   `PersistenceBucket`/`PersistencePrefix` en `spike/m0/iam.yaml`;
   `SECURITY.md` T15. Medido en la implementación: 50 MB en 21 entradas,
   checkpoint **1,4–1,5 s de agente (31–35 MB/s)**, restore **0,67 s
   (78 MB/s)** → mismo sha256, `reincarnate()` ≈ 9 s de pared,
   `permission_denied` en ≈ 1 s sin rol. Coste del binario: `rayd`
   4 700 984 → 12 525 664 B (`rustls` + `aws-lc-rs` compilados con
   `zig cc`), build limpio 106 → 222 s.
5. **`m7-mcp-server`**: `rayito.mcp` (extra `rayito[mcp]`, script
   `rayito-mcp`, stdio y streamable HTTP en loopback) con seis herramientas
   (`run_code` con PNG/JPEG como `ImageContent`, `run_command`, `read_file`,
   `write_file`, `list_files`, `list_sandboxes`); un sandbox por proceso,
   terminado al cerrar; adaptadores de ejemplo LangChain y Vercel AI SDK.
6. **`m7-cli`**: CLI `rayito` (extra `rayito[cli]`): `image
   publish|list|prune|zip`, `sandbox list|info|kill|logs`, `doctor` (diez
   comprobaciones, `--launch`, `--json`); los cuatro scripts de `scripts/`
   son shims con el mismo argv; tabla de compatibilidad SDK ↔ `rayd` con
   test de deriva.
7. **`m7-poly-kernels`**: `language` en `CreateContextRequest` y
   `ExecuteRequest` (contexto por defecto por lenguaje creado perezosamente
   por `rayd`); kernel `bash` (`bash_kernel` 0.10.0) en la variante
   `rayito-base-poly`; `javascript` reservado como nombre (`UNIMPLEMENTED`:
   `ijavascript` no compila en al2023 ARM64, Q57). Medido en la
   implementación: primera celda `bash` 4,0 s (arranque perezoso), segunda
   0,2 s; `kernel_ready` de la variante igual que `rayito-base`.

## Aceptación final (2026-09-17)

Imágenes publicadas desde el árbol 0.2.0 con `rayd` 0.2.0 auditable
(12 525 664 B, 256 crates en `.dep-v0`): **`rayito-base` 20.0** (build 195,6 s,
memoria 922 832 896 B, code install 1 321 267 200 B, disco 33 738 752 B),
**`rayito-base-caps` 8.0** (195,7 s) y **`rayito-base-poly` 4.0** (216,0 s).

| Suite | Resultado |
|---|---|
| Python `tests/e2e -m e2e` (M1–M7, 35 tests, una sesión) | **31 passed, 3 skipped, 1 failed en 2042 s**; el fallo (`test_output_budget_and_disk_reserve`) fue un estancamiento de flujo HTTP/2 con dos streams de 20 MB consumidos perezosamente (el agente terminó ambos en 6 s), repetido a solas **3/3 verde** (14,4 / 11,9 / 9,6 s) |
| TypeScript `pnpm test:e2e` (m6, m7, poly, pool) | **4 ficheros / 5 tests passed en 264 s** |
| Pool a solas (`test_m7_pool.py`) | **3 passed en 363 s**: `T_take` p50 0,768 / p95 0,810 s frente a `T_create` p50 6,200 / p95 6,621 s (20 tomas, `hits` 20, `misses` 0), reciclado a los 119 s, recuperación desde JSON 0,768 s |
| MCP a solas (`test_m7_mcp.py`) | **1 passed en 18,5 s**: primera llamada con creación 8,20 s, resto 0,1–0,3 s, `list_sandboxes` 2,0 s, `TERMINATING` 0,13 s tras cerrar |
| `rayito doctor --launch` | **9 OK, 1 WARN, 0 FAIL** (`agent` `rayd 0.2.0`, `compatibility` OK; el WARN es el `implicitDeny` de `lambda:PassNetworkConnector` en el simulador) |

Otros números del run: `kernel_ready` 5,9–11,4 s (`rayito-base`) y 11,1–13,6 s
(caps con rol y logs); checkpoint de 50 MB 1,65 s de pared (31,9 MB/s) y 1,35 s
en caliente (38,9 MB/s), restore 0,87 s (60,6 MB/s), `reincarnate()` 8,9 s;
primera celda `bash` 4,19 s, segunda 0,19 s; `imds_blocked` a los 0,10 s en
caps. Poda posterior: quedan `rayito-base` 19.0 + 20.0, `rayito-base-caps`
7.0 + 8.0 y `rayito-base-poly` 3.0 + 4.0; `rayito-m0-probe` borrada; cero
MicroVMs vivos y el prefijo S3 del e2e vacío al terminar. Coste: Cost Explorer
(`Estimated`) da $2,93 para el 2026-09-16 (implementación de M7) y $1,43
parcial para el 2026-09-17; la aceptación de hoy ≈ $0,6–0,9 y M7 entero
≈ $4,5–5. Corregido durante la aceptación: `copy_sidecar.py` fallaba en
Windows con un symlink de Linux dentro del `.venv` excluido (la exclusión se
evalúa ahora antes de tocar el sistema de ficheros).

## Límites conocidos (nuevos o cambiados en 0.2.0)

- **El pool no cruza procesos** salvo con el backend JSON de un solo
  escritor; los secretos de las plazas aparcadas viven en el proceso del
  pool o en ese fichero `0600` (T14).
- **La persistencia es del `HOME` entero** (con lista de exclusión fija y
  `exclude=` ≤ 64 entradas), una operación por sandbox, sin `DeleteObject`
  en el rol: la política del bucket (SSE-KMS, regla
  `AbortIncompleteMultipartUpload` a 1 día) es del operador (T15).
- **Sin IMDS para uid 1000 sólo en `rayito-base-caps`**; en la imagen por
  defecto `imds_blocked` sigue `false` (fail-open, sin cambios desde 0.1.0).
- **`javascript` no existe en ninguna imagen** (Q57); `bash` sólo en
  `rayito-base-poly`; R y Java siguen fuera.
- El servidor MCP no lleva autenticación en `--http` (loopback) y crea un
  único sandbox por proceso.
- **Handles en background con salida grande consumidos en serie** pueden
  estancarse: el SDK lee cada handle perezosamente en el hilo que lo
  itera, y dos streams de 20 MB sobre la misma conexión HTTP/2, esperando
  el primero sin leer el segundo, agotaron una vez la ventana de la
  conexión hasta el tope de vida del MicroVM (1 de 4 runs en la
  aceptación). Mientras llega la lectura en hilo propio (M8), consume los
  handles a medida que producen o lanza los procesos con mucha salida en
  serie.
- `rayd` pasa de 4,7 MB a 12,5 MB por los crates de S3/TLS; el code install
  del snapshot sube ≈ 15 MB y la memoria queda en la banda de Q50.
- Los límites de 0.1.0 siguen vigentes: tope de 8 h (`reincarnate()` es la
  respuesta, no `set_timeout()`), `runHookPayload` de 4096 caracteres,
  `RLIMIT_NOFILE` 1024, sin cgroups en la imagen por defecto, ancho de
  banda del endpoint (Q32).

## Diferido (fuera de 0.2.0)

- Aceptación en GitHub de `m7-supply-chain` (11.1–11.3): CI/Scorecard sobre
  `main`, `e2e.yml` con el rol OIDC (`infra/ci-oidc-role.yaml`, no
  desplegado), PR de release-please, `cosign verify-blob` desde una máquina
  limpia y coste del run en Cost Explorer; el repositorio aún no está en
  GitHub (no hay `git init`).
- Publicación en PyPI y npm (Trusted Publishers, tags `python-v0.2.0`,
  `typescript-v0.2.0`, `rayd-v0.2.0`), manual según `docs/RELEASING.md`.
- Test lento de persistencia (8.5 de `m7-s3-persistence`, 62 min:
  refresco de credenciales IMDS antes de la `Expiration`, Q1 "sin medir").
- Revisión manual del servidor MCP desde Claude Code (el Inspector CLI y
  el cliente stdio del SDK `mcp` sí corrieron).
- SDKs Rust/Go, kernels R/Java, URLs firmadas, EFS (`SPEC.md` §4).
