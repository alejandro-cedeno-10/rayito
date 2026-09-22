# Rayito 0.1.0 — notas de la release (2026-09-16)

Primera release: M0–M6 aceptados contra AWS real (cuenta de desarrollo,
`us-east-1`). Todo número de este documento está medido; la fuente de cada
uno es `MILESTONES.md`, `docs/benchmarks/2026-09-cold-start.md` o
`AWS_API_NOTES.md` §16.

## Qué es

Rayito es un SDK de sandboxes para agentes de IA, compatible con la API de
E2B, que corre en **tu propia cuenta de AWS** sobre Lambda MicroVMs
(Firecracker, ARM64). No hay servicio intermedio: el SDK llama a AWS y cada
MicroVM es su propio endpoint.

- **`rayd`**: agente Rust estático (`aarch64-unknown-linux-musl`, 4 698 744 B)
  dentro de la imagen `rayito-base`. Sirve por gRPC (h2c detrás del proxy de
  AWS) `Health`, `ProcessService`, `FilesystemService`, `CodeService` (kernel
  Jupyter vía sidecar Python) y `PtyService`, y atiende los hooks
  `/run`, `/suspend`, `/resume`, `/terminate` en el puerto 9000.
- **SDK Python `rayito` 0.1.0** (`>=3.11`): `Sandbox`/`AsyncSandbox` con la
  misma superficie — `create`/`connect`/`list`/`kill`/`get_info`,
  `commands` (foreground, background, `connect`, `send_stdin`, `kill`),
  `files` (`read`/`write` multi-fichero/`list`/`stat`/`make_dir`/`move`/
  `remove`/`watch_dir`), `run_code` con contextos, `pty`,
  `pause()`/`resume()` con reconexión automática de todos los handles,
  `get_host`, `get_metrics`, `metadata`, `cpu_time_limit`, `egress`.
- **Shim `rayito.e2b`**: los nombres del SDK de E2B 1.x (`Sandbox`,
  `AsyncSandbox`, `SandboxInfo`, `PtySize`, excepciones…) sobre el nativo;
  lo que AWS no tiene se declara `UnimplementedError`, nunca se aproxima.
- **SDK TypeScript `rayito`** (`clients/typescript`, ESM + CJS, Connect-ES
  v2, `@aws-sdk/client-lambda-microvms`): espejo camelCase de la superficie
  Python, mismo `limits.json`, mismo contrato de reconexión.
- **Imagen `rayito-base` 16.0**: AL2023 + Python 3.12 + ipykernel + numpy,
  pandas, matplotlib, scipy, scikit-learn calentados en el snapshot
  (memoria 931 258 368 B, code install 1 299 968 000 B, disco 36 933 632 B,
  build 216 s). Variante `rayito-base-caps` 6.0 (mismo zip con
  `additionalOsCapabilities: ["ALL"]`) con IMDS bloqueado para uid ≥ 1000.

## Números medidos

Cold start y coste (`docs/benchmarks/2026-09-cold-start.md`, 142
lanzamientos, `rayito-base` 10.0, cliente a ≈ 90 ms de RTT):

| | p50 | p95 |
|---|---|---|
| `create()` → `agent_ready` | 2,3 s | 3,0 s |
| `create()` → `kernel_ready` (20 secuenciales) | 5,2 s | 6,1 s |
| `create()` → `kernel_ready` (ráfaga de 20 por el SDK) | 5,7 s | 8,6 s |
| `resume()` explícito → kernel listo | 0,38 s | 0,40 s |
| auto-resume (primera petición sobre un sandbox suspendido) | 0,67 s | 0,68 s |
| primera celda (`1+1`) tras crear o reanudar | 0,10 s | 0,11 s |

- 20 `run-microvm` simultáneos sin bucket: p95 5,7 s, **0 `ThrottlingException`**.
- Coste (precios verificados en Cost Explorer, ±0,21 %): **$0,126/h** activa a
  2 GB / 1 vCPU; ≈ **$0,0014** por lanzamiento (lectura de 0,92 GB de
  snapshot); ≈ **$0,0049** por ciclo `pause()` + `resume()` (≈ 140 s de
  cómputo: un `max_idle_seconds` < 150 s no ahorra); ≈ $0,0001 por hora
  suspendida. Una pasada completa del e2e ≈ $0,03.
- Ancho de banda del endpoint a 2 GB: 0,65 MB/s de escritura, ≈ 6,3–6,7 MB/s
  de lectura.
- Endurecimiento (`tests/e2e/test_m6_hardening.py`, `rayito-base` 15.0/16.0 +
  caps 5.0): `/run` forjado → `already_ran` en 0,30 s; `/suspend` forjado sin
  checkpoint → el watchdog reabre la puerta a los ≈ 20 s sin perder procesos,
  PTY, ficheros ni kernel; `cpu_time_limit=2` → `SIGXCPU` a los 2,18 s;
  `imds_blocked=True` 0,10 s después de `kernel_ready` en la variante caps y
  `PUT /latest/api/token` como uid 1000 falla en 0,39 s; en la imagen por
  defecto IMDS sigue abierto (fail-open, el SDK avisa una vez si hay rol).
- Aceptación final (2026-09-16, `rayito-base` 16.0 + `rayito-base-caps`
  6.0): suite Python M1–M6 **14 passed, 1 skipped en 565 s** (el skip es la
  allowlist de egress, sin VPC propia); suite TypeScript **2 passed en
  196 s**; `imds_blocked` 0,10 s tras readiness y `PUT /latest/api/token`
  como uid 1000 exit 1 en 0,44 s sobre caps 6.0. Detalle en el bloque
  "Estado de aceptación" de M6 en `MILESTONES.md`.
- Corregido en la aceptación (SDK TypeScript): `close()` con un stream vivo
  dejaba armado el PING ocioso de connect-node sobre la sesión destruida y el
  proceso moría 30 s después con `ERR_HTTP2_INVALID_SESSION`;
  `pingIdleConnection` pasa a `false` por defecto (test de regresión
  incluido).

## Decisión sobre el pool

Regla fijada antes de medir (`m6-benchmark-pool` D11): `B20` (p95 de
`kernel_ready` en ráfaga de 20 por el SDK) = 8,57 s ≥ 8 s y `R` (p95 de
`resume()`) = 0,40 s < 2 s ⇒ **pool de MicroVMs suspendidos** (ADR-008),
no un pool de VMs corriendo. Se implementa como el cambio
`m7-suspended-pool`, fuera de 0.1.0.

## Límites conocidos

- **Tope duro de 8 h** por sandbox (`maximumDurationInSeconds` ≤ 28 800,
  running + suspendido), fijo al crear: no existe `UpdateMicrovm`, así que
  `set_timeout()` de E2B es `UnimplementedError` y `connect()` no extiende la
  vida.
- **`runHookPayload` de 4096 caracteres**: es el único canal por VM (hash del
  token + `envs` + `metadata`); el SDK rechaza en cliente lo que no cabe.
- **`RLIMIT_NOFILE` efectivo 1024** por proceso: el hard limit heredado en
  Lambda MicroVMs, sin `CAP_SYS_RESOURCE` para subirlo (objetivo 4096).
- **Sin cgroups en la imagen por defecto**: `CapEff` es 0 y `/sys/fs/cgroup`
  no está montado (límite de plataforma). Con `additionalOsCapabilities:
  ["ALL"]` (`rayito-base-caps`) cgroup2 sí está montado, pero los slices por
  proceso quedan para un cambio posterior; los límites vigentes son
  `RLIMIT_*` (`NPROC` 512, `CORE` 0, `CPU` opcional), presupuesto de salida
  de 128 MiB por sandbox y reserva de disco de 256 MiB.
- **Carrera de inotify en subdirectorios nuevos**: un fichero creado en un
  subdirectorio recién creado antes de que `notify` instale el watch
  recursivo sólo aporta `WRITE` (sin `CREATE`), aproximadamente 1 de cada 2
  veces a 1 vCPU. Espera el `CREATE` del directorio antes de escribir dentro.
- **IMDS abierto en la imagen por defecto**: con `execution_role_arn`,
  cualquier proceso del sandbox puede leer las credenciales; usa
  `rayito-base-caps` (o no pases rol).
- `Connect(from_seq)` puede responder `NotFoundException` antes que en M5:
  el presupuesto de salida de 128 MiB desaloja replays de procesos
  terminados bajo presión (la entrega en vivo no cambia).
- `list(metadata=)` es O(n) sobre los sandboxes `RUNNING` (una sonda `Health`
  por sandbox, ≈ 0,7 s cada una) y pospone su auto-suspensión.
- Omitir `egressNetworkConnectors` **no** cierra la red (hereda el conector
  de la imagen): `allow_internet_access=False` del shim es
  `UnimplementedError`; la allowlist real necesita el conector de
  `infra/egress-connector.yaml`.
- Cuotas de AWS: 5 TPS `run-microvm`/`resume`, 2 TPS `suspend`, 8 conexiones
  concurrentes por MicroVM a 1 vCPU (el SDK usa ≤ 2 canales HTTP/2), 4 MB/s
  por sentido a 2 GB, JWE de 60 min (renovado a los 45 por el SDK).

## Diferido (fuera de 0.1.0)

- **Allowlist de egress medida**: la plantilla `infra/egress-connector.yaml`
  está validada (`validate-template` + `cfn-lint`) pero no desplegada — la
  puerta exige una VPC propia o prestada y la única VPC de la cuenta es de
  otra carga de trabajo (`AWS_API_NOTES.md` Q46, `SECURITY.md` T8).
- **Slices cgroup2 por proceso** (sólo posible en la variante caps).
- **Pool de suspendidos** (`m7-suspended-pool`, ADR-008): punto abierto el
  traspaso del access token fijado en `/run`.
- **Cost Explorer del día del bench** (tareas 5.1/5.3–5.5 de
  `m6-benchmark-pool`): GB reales por lanzamiento y por suspend, VM-segundos
  facturados y salto de storage; se recomprueba el 2026-09-17 y el
  2026-09-23 (mínimo de una semana por versión).
- **Publicación**: registrar el Trusted Publisher en PyPI y empujar el tag
  `python-v0.1.0` (`release.yml`, manual); publicar `rayito` en npm.
- Consolidar el sidecar en Rust (ADR-002), persistencia de filesystem vía
  S3/EFS, kernels no Python, templates declarativos, multi-cloud (SPEC.md §4).
