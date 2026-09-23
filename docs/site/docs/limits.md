# Límites

Cuotas y límites de Lambda MicroVMs tal como los ve el SDK. Fuente:
`AWS_API_NOTES.md` §2, §6 y §11 (medidos en la cuenta de desarrollo el
2026-09-15) y `clients/python/src/rayito/_limits.py`, que es donde el SDK los
valida en cliente.

| Límite | Valor | Qué hace el SDK |
|---|---|---|
| Vida máxima de un sandbox (`timeout`, o `max_lifetime` desde M9) | 28 800 s (8 h), running + suspended, **no ajustable** | `SandboxLifetimeException` por encima; el tope no se puede extender después |
| Plazo lógico (`timeout` con `max_lifetime`/`on_timeout`, M9) | ≥ 1 s y como mucho `max_lifetime − 60 s` desde el arranque (`max_lifetime` 120–28 800 s, por defecto `timeout + 60`; 3600 en el shim) | `set_timeout` por encima del tope: `InvalidArgumentException` con el plazo intacto |
| Historial de métricas (M9) | una muestra cada 5 s mientras corre; anillo de 5 760 muestras (8 h), ≈ 350 KB por respuesta completa | `max_points` ≥ 1 reduce la serie |
| URLs de transferencia (M9) | 3600 s por defecto, tope `S3Staging.max_expires_in` (86 400) y 604 800 s (7 días de SigV4) | `InvalidArgumentException` con `use_signature_expiration <= 0` |
| Ficheros por S3 (M9) | desde `threshold_bytes` (8 MiB; mínimo 1 MiB); exportación multiparte desde 5 GiB, ≤ 1 000 partes de ≥ 8 MiB; `PUT` único ≤ 5 GiB | el SDK elige la ruta |
| Transferencias por sandbox (M9) | 16 activas, 2 moviendo bytes a la vez, 64 terminadas retenidas 30 min | `RateLimitException` (`RESOURCE_EXHAUSTED`) |
| Metadatos por fichero (M9) | ≤ 64 claves y ≤ 4 000 B en total (xattrs `user.rayito.*`) | `InvalidArgumentException` antes de llamar |
| Política de egress (M9) | ≤ 256 entradas por lista, ≤ 64 nombres de host, ≤ 4 096 prefijos por familia tras restar | `InvalidArgumentException` |
| `runHookPayload` (`envs` + `metadata` + hash del token) | 4096 caracteres | `InvalidArgumentException` antes de llamar a AWS, nombrando `envs` y `metadata` |
| Conexiones concurrentes por MicroVM | 8 (1 vCPU), 16, 32, 64, 128 | ≤ 2 canales HTTP/2 por sandbox: unarios y streams |
| Ancho de banda del endpoint | 1 / 2 / 4 / 8 / 16 MB/s por tamaño | medido: 0,65 MB/s escritura, 6,71 MB/s lectura a 2 GB |
| `list-microvms` | 50 items por página | pagina por dentro; `list(metadata=)` añade una sonda por sandbox |
| Procesos + PTYs vivos por sandbox | 256 (impuesto por `rayd`) | `RateLimitException` (`RESOURCE_EXHAUSTED`) |
| Kernels (contextos de código) por sandbox | 8 (impuesto por `rayd`) | `RateLimitException` |
| JWE del proxy | 60 min | renovado a los 45 min por un hilo/task del SDK |
| `create-microvm-auth-token` | 50 TPS | token bucket por proceso alineado con la cuota |
| `run-microvm` / `resume` / `suspend` / `terminate` | 5 / 5 / 2 / 10 TPS | token bucket por proceso alineado con la cuota |
| `get-microvm` | 100 TPS | token bucket por proceso |
| Conectores de red por `run-microvm` | 10 | `InvalidArgumentException` |
| `envs` de un comando | como variables de entorno del proceso | sin límite propio; el payload de creación sí |
| Salida retenida por proceso | 64 × 32 KiB por suscriptor; `output_truncated` si nadie lee en 30 s | `SandboxException("output_truncated: ...")` |
| Tamaño de un `result` de `run_code` | > 12 MiB recortado a `rayito/omitted` | aparece en `Result.extra` |

Las cuotas TPS son por cuenta y región (`AWS_API_NOTES.md` §11); cuentas
nuevas pueden empezar con valores menores.

## Compatibilidad SDK ↔ rayd ↔ imagen

Los SDKs (Python y TypeScript) y `rayd` avanzan `MAJOR.MINOR` en lockstep
(el patch puede divergir); cada fila fija el `agent_version` mínimo que trae
las funciones que ese SDK usa. La versión de imagen (`imageVersion`) **no es
un criterio**: es el contador de builds de cada imagen en cada cuenta
(`rayito-base` 17.0, `rayito-base-poly` 3.0 y la primera imagen de una cuenta
nueva, 1.0, llevan el mismo `rayd`), y `agent_version` sale del binario dentro
de la imagen, así que ya prueba de qué tag se construyó. `rayito doctor`
evalúa esta tabla en su comprobación `compatibility` (y muestra la versión de
imagen sólo a título informativo) y `rayito.cli._compat.COMPATIBILITY` es la
misma tabla en código: un test (`tests/unit/cli/test_compat.py`) impide que
diverjan, y una release que suba un mínimo añade la fila en los dos sitios.

| SDK | rayd mínimo | Nota |
|---|---|---|
| `0.1` | `0.1.0` | M6: imds_blocked, hook_anomalies y metadata exigen el rayd del tag rayd-v0.1.0 |
| `0.2` | `0.2.0` | M7: Checkpoint/Restore (persist=) y language= exigen el rayd del tag rayd-v0.2.0 |
| `0.3` | `0.3.0` | M9: max_lifetime/on_timeout, set_timeout, get_metrics_history, network= y los kernels Deno exigen el rayd del tag rayd-v0.3.0 |
