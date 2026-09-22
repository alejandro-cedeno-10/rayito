# Pool de sandboxes

Un `SandboxPool` mantiene N MicroVMs **suspendidos** y ya calientes
(`agent_ready`, kernel rotado, warm-up hecho) para que `take()` entregue un
sandbox usable en menos de un segundo en vez de los 5-6 s de un `create()`.
Es la decisión ADR-008 de `ARCHITECTURE.md`, implementada en
`m7-suspended-pool` y medida contra AWS real (`clients/python/tests/e2e/test_m7_pool.py`).

## Por qué suspendidos y no `RUNNING`

El benchmark de M6 (`docs/benchmarks/2026-09-cold-start.md` §9) midió un
`create()` → `kernel_ready` de **p50 5,2 s / p95 6,1 s** (p95 8,6 s en ráfaga
de 20) y un `resume()` explícito → `Health` de **p50 0,38 s / p95 0,40 s**. Un
VM `SUSPENDED` sólo paga el storage de su snapshot (0,92 GB × $0,08/GB-mes ≈
**$0,074/mes**); uno `RUNNING` paga $0,126/h ≈ **$91/mes**. Como reanudar
cuesta menos de 2 s, la regla fijada antes de medir eligió el pool de
suspendidos.

## Medido (`test_pool_take_latency`, 20 tomas con plaza lista frente a 20 `create()`)

| | p50 | p95 | min | max |
|---|---|---|---|---|
| `T_take`: `take()` → `run_code("1+1")` | **0,770 s** | **0,897 s** | 0,739 s | 0,922 s |
| `T_create`: `create()` → `run_code("1+1")` | 6,151 s | 6,488 s | 3,029 s | 6,500 s |

Una toma son ≈ 0,14 s de `resume-microvm`, ≈ 0,5 s de acuñar el JWE, abrir el
canal y ver `Health` con el sondeo `TakePoll` (0,1 → 0,5 s), y ≈ 0,1 s de la
celda. Los 20 `take()` fueron aciertos (`hits == 20`, `misses == 0`); run
del 2026-09-16 (`rayito-base` 17.0, RTT medido 109 ms; los tres tests del
fichero en 336 s, ≈ $0,25). Percentil nearest-rank como en el benchmark de M6.

## Cuándo compensa

- Agentes a ráfagas que necesitan un sandbox **ya** (una herramienta por
  turno de conversación, un evaluador que abre y cierra sandboxes): el pool
  convierte 5-6 s en menos de 1 s.
- **No** para trabajos por lotes largos: una plaza tomada es un sandbox normal
  que factura $0,126/h mientras corre, y el `create()` de 6 s es despreciable
  frente a minutos de trabajo.
- La configuración de lanzamiento es **por pool** (`envs`, `metadata`,
  `cpu_time_limit`, política de idle, conectores, rol, `timeout`): dos
  configuraciones son dos pools.

## API

=== "Python (sync)"

    ```python
    from rayito import PoolConfig, SandboxPool

    config = PoolConfig(size=3, template="rayito-base")
    with SandboxPool(config) as pool:      # start() al entrar, close(drain=True) al salir
        sbx = pool.take()                  # < 1 s con plaza lista; create() si no hay
        print(sbx.run_code("1+1").text)    # "2"
        sbx.kill()
        print(pool.stats())                # ready, warming, takes, hits, misses, ...
    ```

    `Sandbox.create(pool=pool)` es azúcar de `pool.take()`: acepta
    `ready_timeout`, `request_timeout` y `reconnect_timeout` y rechaza con
    `InvalidArgumentException` cualquier otro kwarg de lanzamiento o de plano
    (`template`, `timeout`, `envs`, `region`, `control_plane`...). `pool`
    recibe un `SandboxPool` ya arrancado, nunca un `PoolConfig`: un pool tiene
    un hilo, N VMs y una factura, y se arranca y cierra explícitamente.

=== "Python (async)"

    ```python
    from rayito import AsyncSandboxPool, PoolConfig

    async with AsyncSandboxPool(PoolConfig(size=3, template="rayito-base")) as pool:
        sbx = await pool.take()
        print((await sbx.run_code("1+1")).text)
        await sbx.kill()
    ```

    `await AsyncSandbox.create(pool=pool)` con las mismas reglas.

=== "TypeScript"

    ```ts
    import { SandboxPool } from "rayito";

    await using pool = await new SandboxPool({ size: 3, template: "rayito-base" }).start();
    const sbx = await pool.take();
    console.log((await sbx.runCode("1+1")).text);
    await sbx.kill();
    console.log(pool.stats());
    ```

    `Sandbox.create({ pool })` rechaza con `InvalidArgumentError` cualquier
    opción de lanzamiento junto a `pool`. Los temporizadores del pool van con
    `unref()`: un pool ocioso no mantiene vivo el proceso, así que llama a
    `pool.close()` (o `await using`) antes de salir o las plazas quedan
    aparcadas hasta su `timeoutMs`.

### `PoolConfig`

| Campo | Por defecto | Qué es |
|---|---|---|
| `size` | — | plazas aparcadas (`1..=64`; 64 × 2 GB son 128 GB de la cuota regional de 1 024 GB) |
| `template`, `template_version` | `RAYITO_TEMPLATE`, última | la imagen: un pool es una imagen y una versión |
| `timeout` | 28 800 | vida máxima de cada plaza (`maximumDurationInSeconds`, cuenta el tiempo suspendido); aparcar todo lo que AWS permite |
| `idle` | `IdlePolicy()` | obligatorio y con `auto_resume=True`; `suspended_duration_seconds` se resuelve a `timeout − max_idle_seconds`, la red de seguridad de una plaza olvidada |
| `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `ingress`, `egress`, `logging` | como en `create()` | fijos en el `runHookPayload` y en `run-microvm`: por pool, no por toma |
| `min_remaining_seconds` | 3 600 | vida mínima con la que se entrega una plaza; por debajo se recicla |
| `fill_concurrency` | 4 | calentamientos en vuelo (acota handles y memoria; el ritmo lo ponen los token buckets) |
| `sweep_interval_seconds` | 30 | cadencia del reciclado y la reconciliación |
| `ready_timeout` | 90 | plazo de readiness de cada calentamiento |

No hay `access_token` (uno por plaza, ver custodia) ni `allowed_ports`
(`get_host(port)` acuña por puerto tras la toma, como después de `create()`).

## Qué hace una toma

1. Bajo el lock del pool elige la plaza `ready` que **caduca antes** con al
   menos `min_remaining_seconds` de vida (las más viejas primero, así ninguna
   se pudre) y **borra su registro del backend antes de tocar la red**: desde
   ese instante el secreto sólo vive en el `Sandbox` que recibe quien toma.
2. `resume-microvm` explícito (bucket de 5 TPS): 0,38 s medidos frente a
   0,67 s del auto-resume, y una plaza muerta falla rápido (`False`, `SandboxNotFoundException`,
   `SandboxStateException` → `terminate-microvm`, `lost += 1`, fallback).
3. Acuña el JWE y abre el handle con el token de la plaza sondeando `Health`
   de 0,1 s doblando a 0,5 s (`TakePoll`); **sin `get-microvm`** en el camino
   (es eventualmente consistente y el registro ya trae todo). Un fallo aquí
   termina el VM, cuenta `failed` y cae al fallback.
4. Devuelve el `Sandbox` (`access_token == el de la plaza`, `get_info()` desde
   el registro, `metadata` desde `Health`).
5. **Fallback**: sin plaza lista (tras esperar hasta `take(wait=w)` segundos,
   0 por defecto) o con la plaza perdida, un `create()` normal con la misma
   configuración y un token fresco (`misses += 1`). El pool es una
   optimización de latencia, **nunca un semáforo**: siempre hay sandbox o la
   excepción que `create()` habría lanzado. `take()` sobre un pool no
   arrancado o cerrado es `PoolClosedException`.

## Relleno, cuotas y backoff

Un hilo (`rayito-pool-filler-<n>`; una `asyncio.Task` en async, una promesa
en TypeScript) mantiene `ready + warming == size`. Cada calentamiento es el
`create()` normal con un `secrets.token_bytes(32)` fresco, **una celda
trivial** (`pass`: `rayd` retiene `Execute` mientras rota el kernel de
`/run`, así la plaza nunca se aparca a mitad de la rotación, ver más abajo),
`pause(wait=True)` (`suspend-microvm`, bucket de **2 TPS**, `get-microvm`
hasta `SUSPENDED`) y `close()` del handle: el pool guarda datos, nunca
canales ni JWE. Todas las llamadas van por el plano de control compartido del
proceso, así que el pool y los `create()` de la aplicación se reparten los
mismos buckets (`RunMicrovm` 5, `SuspendMicrovm` 2, `ResumeMicrovm` 5,
`TerminateMicrovm` 10, `CreateMicrovmAuthToken` 50 TPS): un pool de 20 tarda
≥ 10 s en llenarse por el bucket de suspend y nunca deja sin cuota a la
aplicación (orden de llegada). Tras cualquier calentamiento fallido el relleno
espera 1 s doblando hasta 60 s (±25 % de jitter), cuenta `failed`, avisa una
vez con la clase de la excepción y sigue: `size` fallos seguidos no paran el
pool.

**Dimensionado**: `size` ≈ el pico de sandboxes que necesitas dentro de una
ventana de relleno (≈ 8 s por plaza con `fill_concurrency=4`); una ráfaga mayor
que `size` cae a `create()` a los 5-6 s de siempre.

## Reciclado, reconciliación y `list()`

Cada `sweep_interval_seconds` (y una vez en `start()`):

- **Reciclado**: toda plaza `ready` con menos de `min_remaining_seconds` de
  vida se termina y se repone (`recycled += 1`). Con los defaults (28 800 /
  3 600) una plaza intacta se recicla ≈ 7 h después de lanzarse: ninguna
  llega al muro de 8 h aparcada y toda plaza entregada tiene ≥ 1 h. **El muro
  aplica también a quien toma**: `get_info().remaining_seconds()` dice la
  verdad; una plaza del pool es para trabajo corto y a ráfagas.
- **Reconciliación**: un `list-microvms` filtrado por la imagen (y la versión
  si está fijada) y, por plaza `ready`: listada `SUSPENDED|SUSPENDING|PENDING`
  → nada; `RUNNING` → alguien la reanudó fuera del pool, `suspend-microvm` y
  un aviso; ausente → `get-microvm` y, si es terminal o no existe, registro
  borrado, `lost += 1` y un aviso con el `stateReason`. Un `list-microvms`
  fallido aborta ese barrido con un aviso; el siguiente reintenta.
- Las plazas aparcadas **aparecen como `SUSPENDED` en `Sandbox.list()`** y
  cuentan 2 GB cada una contra la cuota regional de memoria (1 024 GB en
  `us-east-1`, `RUNNING + SUSPENDED`).

## Backends

`PoolBackend` es la costura para un almacén compartido futuro; el pool
serializa toda llamada bajo su propio lock, así que un backend no necesita ser
thread-safe.

- `InMemoryPoolBackend` (por defecto): los registros mueren con el proceso.
  Un pool que muere sin `close()` deja `size` VMs suspendidos que **se
  terminan solos** en su `timeout` (política de idle): la fuga máxima son
  `size` plazas de storage durante ≤ 8 h (≈ $0,0008 por plaza).
- `JsonFilePoolBackend(path)`: `{"schema": "rayito.pool/1", "slots": [...]}`
  con los access tokens **en claro**, fechas ISO-8601 UTC, escrito de forma
  atómica (`<path>.tmp` + `os.replace`) con modo `0600` (sin efecto en
  Windows). Es de **un solo proceso** (sin bloqueo ni coordinación entre
  hosts), pensado para los tests y para recuperar un pool en el mismo host
  tras un reinicio; el mismo fichero lo leen los dos SDKs. El fichero es tan
  sensible como `RAYITO_ACCESS_TOKEN`.

`close(drain=False)` deja las plazas aparcadas y sus registros para otro pool
y **sólo se admite con un backend persistente**. En `start()` el pool carga el
backend: un registro `warming` es un huérfano de un calentamiento
interrumpido (se termina, `lost += 1`) y cada `ready` pasa por un barrido
antes de poder tomarse (`launched` no cambia). `stats()` devuelve `PoolStats`
(`size, ready, warming, takes, hits, misses, launched, recycled, lost,
failed, slots`) sin ningún secreto.

## Custodia del secreto

El sha256 del access token viaja en el `runHookPayload` de `run-microvm` y
`/run` se acepta **una vez por arranque** (ADR-004, `SECURITY.md` T2), así que
**el token que abre una plaza es el que acuñó el pool, durante toda la vida
del VM**: no hay rotación en `take()` ni puede añadirse sin un segundo `/run`,
que `rayd` rechaza por diseño. Consecuencias (`SECURITY.md` T14):

- **Un secreto fresco de 32 bytes por plaza**, nunca uno por pool: una fuga
  abre un VM, no la flota.
- El secreto vive en el proceso del pool (backend en memoria) o en el fichero
  `0600` (backend JSON) sólo hasta `take()`, que borra el registro antes de
  tocar la red; después sólo lo tiene el `Sandbox` de quien tomó.
  `close(drain=False)` es el único camino que deja secretos en reposo a
  propósito.
- Quien opera el pool puede leer todos los secretos aparcados: es el **mismo
  nivel de confianza** que ya tiene quien opera el SDK (posee las
  credenciales IAM que acuñan JWEs y terminan VMs). Lo que cambia es que una
  plaza tiene un secreto *antes* de tener usuario: una aplicación que reparte
  sandboxes tomados entre inquilinos distintos debe tratar el proceso del
  pool como componente de confianza propio, exactamente como trata hoy al
  proceso que llama a `create()`.
- El JWE se acuña al tomar y nunca se guarda: una plaza aparcada sólo es
  alcanzable por quien puede acuñar un JWE para ella (IAM) **y** conoce su
  secreto (la autenticación en dos niveles de ADR-004 no cambia).
- Residual: un volcado del proceso o el fichero JSON exponen los secretos
  aparcados; `list-microvms` revela los ids a cualquier principal con
  `ListMicrovms`; un principal con `ResumeMicrovm` puede reanudar una plaza y
  arrancar su contador (el barrido la vuelve a aparcar en ≤ un intervalo y lo
  avisa).

## La rotación de `/run` y la celda de asentado

`create()` da un VM por listo con `agent_ready and kernel_ready`. `rayd`
responde 200 a `/run` y encarga la rotación del kernel por defecto en segundo
plano; entre ese 200 (que abre el tráfico) y el arranque de la rotación hay
una ventana en la que `Health` aún dice `kernel_ready` del kernel sin rotar.
En el e2e de M7, 2 de 40 calentamientos vieron `kernel_ready` a los 2-3 s (en
vez de ≈ 6 s) y, aparcados en ese estado, perdieron el kernel al reanudar
(`kernel_state_lost`, 6-12 s hasta la primera celda). Por eso cada
calentamiento ejecuta una celda trivial antes de aparcar: `rayd` retiene
`Execute` mientras el contexto por defecto reinicia, así la celda sólo
vuelve sobre el kernel rotado, y un `Health` tras 0,3 s confirma que no
arrancó una rotación después. Una plaza llega, por tanto, con
`execution_count == 1`. El arreglo de raíz (marcar `Rotating` en el propio
handler de `/run`) es de `rayd`, `AWS_API_NOTES.md` Q53.

## Coste por plaza (`rayito-base`, 0,92 GB de snapshot; `AWS_API_NOTES.md` §12 y `docs/benchmarks/2026-09-cold-start.md`)

| Concepto por plaza | Coste |
|---|---|
| Storage mientras está aparcada | 0,92 GB × $0,08/GB-mes ≈ **$0,074/mes** ($0,0001/h) |
| Aparcar (snapshot write al `pause()`) | 0,92 × $0,0038 ≈ **$0,0035** |
| Tomar (snapshot read al `resume`) | 0,92 × $0,00155 ≈ **$0,0014** |
| Calentar (lanzamiento: read + ≈ 7 s `RUNNING`) | $0,0014 + $0,00025 ≈ **$0,0017** |
| Reciclado de una plaza ociosa (cada ≈ 7 h con los defaults: calentar + aparcar) | ≈ **$0,005** ⇒ ≈ 3,4/día ⇒ ≈ **$0,52/mes** |
| **Total plaza ociosa** | ≈ **$0,6/mes** (frente a $91/mes una VM `RUNNING`) |
| Plaza tomada | la toma ($0,0014) + el sandbox normal ($0,126/h mientras corre) |

El e2e completo (≈ 46 lanzamientos de segundos: 20 tomas + 20 `create()` +
las plazas de relleno, el reciclado y la recuperación) cuesta ≈ $0,25.
