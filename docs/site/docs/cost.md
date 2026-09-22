# Modelo de costes

Todo lo de esta página está medido contra AWS real (`us-east-1`, ARM,
imagen `rayito-base` de 2 GB / 1 vCPU) o sale de la lista de precios pública.
Cada fila cita su fuente en el repositorio.

## Precios (fuente: `AWS_API_NOTES.md` §12)

| Concepto | Precio |
|---|---|
| Cómputo mientras `RUNNING` | $0.0000276944 / vCPU-s + $0.0000036667 / GB-s ⇒ **$0.126/h** a 2 GB / 1 vCPU |
| Snapshot write (cada `suspend`) | $0.0038 / GB |
| Snapshot read (cada `run-microvm` o `resume`) | $0.00155 / GB |
| Storage de snapshots (versiones de imagen, VMs suspendidas) | $0.08 / GB-mes, **mínimo una semana** por versión de imagen ⇒ ≈ $0.037 / semana por versión |
| Un ciclo suspend + resume a 2 GB (`rayito-base`, 0,92 GB de snapshot escritos y leídos) | ≈ **$0.0049** (≈ 140 s de cómputo): un `max_idle_seconds` por debajo de ≈ 150 s nunca ahorra (`docs/benchmarks/2026-09-cold-start.md` §8) |
| Un lanzamiento (`run-microvm`, lectura del snapshot de 0,92 GB) | ≈ **$0.0014** |
| Una pasada completa del e2e de un hito | ≈ **$0.03** (+ $0.037 si publica una versión de imagen) |
| Una plaza ociosa del [pool de sandboxes](pool.md) (storage del snapshot + reciclado cada ≈ 7 h) | ≈ **$0.6/mes** por plaza (frente a $91/mes una VM `RUNNING`); una toma ≈ $0.0014 y **p50 0.77 s / p95 0.90 s** hasta la primera celda (M7) |

Sin free tier ni cuota de plan: pagas por segundo mientras el sandbox está
`RUNNING`, por GB en cada snapshot y por el storage de las versiones.

## Tiempos (fuente: `docs/benchmarks/2026-09-cold-start.md`, `MILESTONES.md`; `AWS_API_NOTES.md` §16)

| Operación | Medido |
|---|---|
| `run-microvm` → `Health.agent_ready` (arranque en frío, cliente a ≈ 90 ms de RTT) | **p50 2.3 s / p95 3.0 s** (benchmark M6, 20 secuenciales) |
| `run-microvm` → `kernel_ready` (incluye la rotación y el warm-up del kernel en `/run`) | **p50 5.2 s / p95 6.1 s** secuencial; p50 5.7 s / p95 8.6 s en ráfaga de 20 por el SDK (benchmark M6) |
| `pause()` → `SUSPENDED` | **1.37-1.49 s** (M5, `pause_s`) |
| `resume()` explícito → `Health` con la generación nueva | **p50 0.38 s / p95 0.40 s** (benchmark M6) |
| Auto-resume: primera llamada sobre un sandbox suspendido → `Health` con la generación nueva | **≈ 0.67 s** (benchmark M6; 0.7-1.6 s en M5) |
| `files.write` a 2 GB | **0.65 MB/s** (M3, bench de 50 MB) |
| `files.read` a 2 GB | **6.71 MB/s** (M3: 8 000 000 B en 1.19 s) |
| `Sandbox.get_info(id)` con metadatos (`get-microvm` + JWE + `Health`) | **0.61-0.66 s** (M6, `AWS_API_NOTES.md` Q45) |
| `Sandbox.list(metadata=...)` | **≈ 0.6-0.8 s por sandbox `RUNNING`** (0.76-0.77 s con un sandbox; M6, `AWS_API_NOTES.md` Q45) |
| `get_metrics()` | ≈ 100 ms (dos muestras de `/proc/stat`) |

## Reglas prácticas

- Un sandbox olvidado factura hasta `timeout` (3600 s por defecto): usa `with`
  o `kill()`, y un `timeout` acorde a la tarea.
- La política de idle por defecto (300 s) suspende un sandbox inactivo por
  ≈ $0.0049 el ciclo; para trabajos con huecos cortos, sube `max_idle_seconds`
  o pasa `idle=None`.
- El coste dominante de un arranque corto es el snapshot read (≈ $0.00155 × 1 GB
  por lanzamiento): el tamaño de la imagen importa más que el cómputo en
  sandboxes de segundos.
- `list(metadata=)` pospone la auto-suspensión de cada sandbox sondeado una
  ventana de idle: no lo llames en bucle sobre una flota grande.
