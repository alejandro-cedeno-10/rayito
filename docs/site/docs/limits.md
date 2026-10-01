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

## Tamaño (CPU/RAM)

El tamaño (memoria y vCPU) **es propiedad de la imagen**
(`resources[0].minimumMemoryInMiB` en `create-microvm-image`,
`AWS_API_NOTES.md` §4), no un parámetro de `Sandbox.create()`. Es la misma
forma que usa E2B: `Sandbox.create()` de E2B tampoco tiene `cpu`/`memoria`;
el tamaño se fija por **build de template**, con `Template.build(cpu_count=,
memory_mb=)` (`e2b template create --cpu-count --memory-mb`). Rayito no
tiene un catálogo de tamaños ni un resolvedor `resources=` en `create()`
(fuera de alcance de M12; ver `MILESTONES.md`).

Tabla de la **documentación de AWS** (`AWS_API_NOTES.md` §4; ancho de banda
del endpoint, entrada + salida), con el coste de la hora en baseline
derivado de los precios de §12 (`AWS_API_NOTES.md` §12 / [Costes](cost.md)):
**nada de esta tabla salvo lo marcado está medido en una cuenta real**.

| `minimumMemoryInMiB` | baseline | pico (4x) | disco | ancho de banda | $/h en baseline |
|---|---|---|---|---|---|
| 512 | 0.5 GB / 0.25 vCPU | 2 GB / 1 vCPU | 8 GB | 1 MB/s | $0.0315 |
| 1024 | 1 GB / 0.5 vCPU | 4 GB / 2 vCPU | 8 GB | 2 MB/s | $0.0631 |
| 2048 (default de `rayito image publish`) | 2 GB / 1 vCPU | 8 GB / 4 vCPU | 8 GB | 4 MB/s (**medido** 4.54 MB/s bajando 16 MiB, `AWS_API_NOTES.md` §7) | $0.1261 |
| 4096 | 4 GB / 2 vCPU | 16 GB / 8 vCPU | 16 GB | 8 MB/s | $0.2522 |
| 8192 | 8 GB / 4 vCPU | 32 GB / 16 vCPU | 32 GB | 16 MB/s | $0.5044 |

Lo único de esta tabla verificado en una cuenta real (M0/M9, `AWS_API_NOTES.md`
§7 y Q68) es la fila de 2048: el ancho de banda medido (4.54 MB/s bajando 16
MiB) y lo que ve el guest (`cpu_count=4`, `memory_mb=8016`, el pico del
rango, no el baseline). El resto de la tabla —baseline, pico, disco y ancho
de banda de los otros cuatro tamaños— viene tal cual de la documentación de
AWS, sin medir; el `$/h` es aritmética sobre esos números y los precios
verificados de §12, no una factura observada. Otros valores de
`minimumMemoryInMiB` fuera de esta tabla no están documentados ni medidos
(RES-1): no los publiques sin medirlos tú mismo antes en tu cuenta.

**Cómo elegir tamaño hoy**: publica una imagen por tamaño, con un nombre que
lo diga, y crea sandboxes contra esa imagen:

```bash
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 \
    --bucket amzn-s3-demo-bucket --image-name myimg-4gb --memory-mib 4096
```

```python
from rayito import Sandbox

sbx = Sandbox.create("myimg-4gb")
```

```typescript
const sbx = await Sandbox.create({ template: "myimg-4gb" });
```

La CLI no valida `--memory-mib` contra la tabla de arriba (los valores no
medidos podrían ser válidos igualmente): un valor fuera de lo verificado
simplemente no tiene número de referencia aquí todavía.

**Coste**: mientras el sandbox está `RUNNING`, AWS factura por segundo al
vCPU/GB de **baseline** de la columna de arriba (columna `$/h en baseline`,
precios en [Costes](cost.md)); si el sandbox consume por encima del
baseline (hasta el pico 4x), **ese exceso se factura aparte, a los vCPU/GB
realmente consumidos** (`AWS_API_NOTES.md` §12), no al precio del tamaño
siguiente de la tabla. Cada versión de imagen publicada, sea cual sea su
tamaño, cuesta además el storage del snapshot (mínimo 1 semana de
retención, ver [Costes](cost.md) e [Imágenes e IAM](images.md#publicar-las-tres)).

**Lo que ve el guest no es la línea base**: `SandboxInfo.cpu_count` y
`SandboxInfo.memory_mb` informan la vista del guest (`nproc` y `MemTotal`),
no `minimumMemoryInMiB`. Con una imagen de 2048 MiB, `get_info()` **midió**
`cpu_count=4` y `memory_mb=8016` — el pico del rango, no el baseline
(`AWS_API_NOTES.md` Q68). Código que decide su paralelismo mirando `nproc`
o la memoria total puede sobrepasar la línea base contratada — y, por el
párrafo anterior, eso **no es sólo una cuestión de rendimiento: ese exceso
se paga** a los vCPU/GB consumidos por encima del baseline, no sólo corre
más rápido gratis.

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
| `0.4` | `0.4.0` | 0.4: UnimplementedError único, SetTimeout validado en el dominio y mensajes del agente en español exigen el rayd del tag rayd-v0.4.0 |
| `0.5` | `0.5.0` | 0.5: /suspend con sync acotado por sistema de ficheros y las funciones opcionales (secretos, índice, OTel) se validan con el rayd del tag rayd-v0.5.0 |
