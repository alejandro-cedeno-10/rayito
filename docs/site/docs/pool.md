# Pool de sandboxes

Un `SandboxPool` mantiene N MicroVMs **suspendidos** y ya calientes
(`agent_ready`, kernel rotado, warm-up hecho) para que `take()` entregue un
sandbox usable en menos de un segundo en vez de los 5-6 s de un `create()`.
Está medido contra AWS real (detalle al final, en "Fuentes y mediciones").

## Por qué suspendidos y no `RUNNING`

El benchmark de arranque en frío midió un
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
fichero en 336 s, ≈ $0,25). Percentil nearest-rank como en el benchmark de arranque.

## Cuándo compensa

- Agentes a ráfagas que necesitan un sandbox **ya** (una herramienta por
  turno de conversación, un evaluador que abre y cierra sandboxes): el pool
  convierte 5-6 s en menos de 1 s.
- **No** para trabajos por lotes largos: una plaza tomada es un sandbox normal
  que factura $0,126/h mientras corre, y el `create()` de 6 s es despreciable
  frente a minutos de trabajo.
- La configuración de lanzamiento es **por pool** (`envs`, `metadata`,
  `cpu_time_limit`, política de idle, conectores, rol, `timeout`, egress):
  dos configuraciones son dos pools.
- **Agente en el sandbox**: no hace falta un pool. El arranque normal es
  `Sandbox.create(...)` + `sbx.agent.run(...)`, y los turnos de una misma
  conversación van mejor pausando su VM; un pool con
  [`warmup`](#calentamiento-warmup) sólo compensa con
  muchas conversaciones **nuevas** que necesitan el primer mensaje rápido
  ([¿Qué uso?](guias/agente-en-el-sandbox.md#que-uso)).

## API

=== "Python"

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
    `ready_timeout`, `request_timeout`, `reconnect_timeout`, `secrets`,
    `secret_cache`, `transfer` y `logger`, y rechaza con
    `InvalidArgumentException` cualquier otro kwarg de lanzamiento o de plano
    (`template`, `timeout`, `envs`, `region`, `control_plane`, `gateways`,
    `persist`, `index`...). Para abrir una pasarela en la plaza tomada (un
    agente, por ejemplo) usa `pool.take(gateways=...)`. `pool`
    recibe un `SandboxPool` ya arrancado, nunca un `PoolConfig`: un pool tiene
    un hilo, N VMs y una factura, y se arranca y cierra explícitamente.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandboxPool, PoolConfig


    async def main() -> None:
        async with AsyncSandboxPool(PoolConfig(size=3, template="rayito-base")) as pool:
            sbx = await pool.take()
            print((await sbx.run_code("1+1")).text)
            await sbx.kill()


    asyncio.run(main())
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

En TypeScript `PoolConfig` es el objeto que recibe `new SandboxPool(...)`,
con los mismos campos en camelCase y los tiempos en milisegundos.

| Campo (Python / TypeScript) | Por defecto | Qué es |
|---|---|---|
| `size` | — | plazas aparcadas (`1..=64`; 64 × 2 GB son 128 GB de la cuota regional de 1 024 GB) |
| `template`, `template_version` / `templateVersion` | `RAYITO_TEMPLATE`, última | la imagen: un pool es una imagen y una versión |
| `timeout` / `timeoutMs` | 28 800 s | vida máxima de cada plaza (`maximumDurationInSeconds`, cuenta el tiempo suspendido); aparcar todo lo que AWS permite |
| `idle` | `IdlePolicy()` | obligatorio y con `auto_resume=True`; `suspended_duration_seconds` se resuelve a `timeout − max_idle_seconds`, la red de seguridad de una plaza olvidada |
| `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `ingress`, `egress`, `logging` | como en `create()` | fijos en el `runHookPayload` y en `run-microvm`: por pool, no por toma |
| `allow_internet_access` / `allowInternetAccess`, `network` | `True`, ninguna | la política de egress de todas las plazas, como en `create()`; un pool de agentes pasa `allow_internet_access=False` |
| `min_remaining_seconds` / `minRemainingMs` | 3 600 s | vida mínima con la que se entrega una plaza; por debajo se recicla |
| `fill_concurrency` / `fillConcurrency` | 4 | calentamientos en vuelo (`1..=8`; acota handles y memoria; el ritmo lo ponen los token buckets) |
| `sweep_interval_seconds` / `sweepIntervalMs` | 30 s | cadencia del reciclado y la reconciliación (mínimo 5 s) |
| `ready_timeout` / `readyTimeoutMs` | 90 s | plazo de readiness de cada calentamiento |
| `index` | ninguno | `DynamoDbIndex(...)`: escribe la fila del [índice de metadatos](optional-features.md) de cada plaza al lanzarla (coste propio, apagado por defecto) |
| `warmup` | `()` / `[]` | pasos que cada plaza corre antes de aparcarse; ver [Calentamiento](#calentamiento-warmup) |

No hay `access_token` (uno por plaza, ver custodia) ni `allowed_ports`
(`get_host(port)` acuña por puerto tras la toma, como después de `create()`).
Tampoco `gateways` ni `secrets`: las plazas calientes nunca llevan secretos,
que se enlazan al tomar.

### `take()`

| Python | TypeScript | Por defecto | Qué es |
|---|---|---|---|
| `wait` | `waitMs` | 0 | cuánto esperar a que el relleno aparque una plaza antes de caer a `create()` |
| `ready_timeout`, `request_timeout`, `reconnect_timeout` | `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | como en `create()` | plazos del handle que se entrega |
| `secrets`, `secret_cache` | `secrets`, `secretCache` | ninguno | [secretos](optional-features.md) inyectados en el sandbox tomado |
| `gateways` | `gateways` | ninguna | abre la [pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) en el sandbox tomado, igual que `create(gateways=)`; es lo que necesita un agente |
| — | `logger` | ninguno | logger del handle |

`close(drain=True)` (TS: `close({ drain })`) y `stats()` completan la API.

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
En el e2e del pool, 2 de 40 calentamientos vieron `kernel_ready` a los 2-3 s (en
vez de ≈ 6 s) y, aparcados en ese estado, perdieron el kernel al reanudar
(`kernel_state_lost`, 6-12 s hasta la primera celda). Por eso cada
calentamiento ejecuta una celda trivial antes de aparcar: `rayd` retiene
`Execute` mientras el contexto por defecto reinicia, así la celda sólo
vuelve sobre el kernel rotado, y un `Health` tras 0,3 s confirma que no
arrancó una rotación después. Una plaza llega, por tanto, con
`execution_count == 1`. El arreglo de raíz (marcar `Rotating` en el propio
handler de `/run`) es de `rayd`, `AWS_API_NOTES.md` Q53.

## Calentamiento (`warmup`)

!!! info "Desde 0.8.0"
    `PoolConfig.warmup` llega con
    [0.8.0](novedades/0.8.0.md). Las cifras de
    esta sección están medidas en AWS real el 2026-10-07 (Q147 de
    `AWS_API_NOTES.md`); los costes son precios de lista (consultados
    2026-10-06) por esos tiempos, con el detalle en
    [Precios](cost.md#coste-de-la-vm-con-fast-start).
    Guía: [Agente en el sandbox](guias/agente-en-el-sandbox.md).

Un [agente](guias/agente-en-el-sandbox.md) paga su primer `exec` (19,6 s de
mediana tras `create()` sin prefetch, medido) en cada VM nueva. `PoolConfig.warmup` deja ese coste en
el calentamiento de la plaza, antes de aparcarla, en vez de en la toma.
Sólo compensa si llegan muchas conversaciones **nuevas** cuyo primer mensaje
tiene que ser rápido: para los turnos de una misma conversación basta con
pausar la VM entre ellos, sin pool (ver
[¿Qué uso?](guias/agente-en-el-sandbox.md#que-uso)).

=== "Python"

    ```python
    from rayito import PoolConfig, agent_pool_warmup

    config = PoolConfig(
        size=3,
        template="rayito-agent",
        allow_internet_access=False,  # un pool de agentes cierra el egress
        warmup=agent_pool_warmup("opencode"),
    )
    ```

=== "TypeScript"

    ```ts
    import { SandboxPool, agentPoolWarmup } from "rayito";

    const pool = new SandboxPool({
      size: 3,
      template: "rayito-agent",
      allowInternetAccess: false, // un pool de agentes cierra el egress
      warmup: agentPoolWarmup("opencode"),
    });
    console.log(pool);
    ```

La pasarela del modelo se abre al tomar la plaza, con
`pool.take(gateways=...)`: ejemplo completo en
[Agente en el sandbox: pool de agentes](guias/agente-en-el-sandbox.md#pool-de-agentes-c).

`agent_pool_warmup(runtime="opencode")` (TS: `agentPoolWarmup(runtime)`)
devuelve una lista de `WarmupStep`
(`cmd`, `background=False`, `timeout_seconds`/`timeoutMs`, `tag`); puedes
pasar tus propios pasos en `warmup` igual. Cada paso corre tras la celda de
asentado (abajo) y antes de `pause()`; un fallo
cuenta como un calentamiento fallido (se termina el VM, se aplica el mismo
backoff que un calentamiento normal, `failed += 1`). Un paso en segundo
plano (`background=True`) se lanza y se suelta: el relleno no espera a que
acabe. Un reciclado relanza la plaza y vuelve a correrlos.

| Opción | Qué precalienta | Medido (pool de 2, n=5) | Coste por ciclo de reciclado (≈ cada 7 h) | Coste por plaza al mes |
|---|---|---|---|---|
| **C: `agent_pool_warmup("opencode")`** | el binario de OpenCode ya en la caché de páginas (`agent_pool_warmup("deepagents")` hace lo propio con deepagents) | toma → primer token **p50 5,1 s / p95 6,7 s**; plaza lista en 15,5 s | lanzamiento $0,0014 + 15,5 s de cómputo $0,0005 + aparcar ≈ 0,92 GB $0,0035 ⇒ ≈ **$0,0054** | ≈ **$0,64** (frente a $0,60 de una plaza base) |

La API no devuelve el tamaño del snapshot de un `suspend`: el de C se toma
igual al de la imagen (la memoria usada del guest tras la toma, 503 MiB, es
menor que la de un `create()` fresco). Una toma lee ese snapshot: ≈ $0,0014.

### Resultado de la medida en AWS

El prefetch (opción A de
[Templates de agente](funciones-opcionales/templates-de-agente.md)) baja el
primer `exec` tras `create()` un 76 % (19,6 s → 4,7 s de mediana), así que
sigue encendido por defecto. C deja el primer token a 5,1 s de la toma y es
la opción recomendada cuando hace falta un pool. Cómo elegir entre A, B, C
y no usar pool: [Agente en el sandbox](guias/agente-en-el-sandbox.md#arranque-rapido).

## Coste por plaza (`rayito-base`, 0,92 GB de snapshot; `AWS_API_NOTES.md` §12 y `docs/benchmarks/2026-09-cold-start.md`)

| Concepto por plaza | Coste |
|---|---|
| Storage mientras está aparcada | 0,92 GB × $0,08/GB-mes ≈ **$0,074/mes** ($0,0001/h) |
| Aparcar (snapshot write al `pause()`) | 0,92 × $0,0038 ≈ **$0,0035** |
| Tomar (snapshot read al `resume`) | 0,92 × $0,00155 ≈ **$0,0014** |
| Calentar (lanzamiento: read + ≈ 7 s `RUNNING`) | $0,0014 + $0,00025 ≈ **$0,0017** |
| Reciclado de una plaza ociosa (cada ≈ 7 h con los defaults: calentar + aparcar) | ≈ **$0,0052** ⇒ ≈ 3,4/día (≈ 103/mes) ⇒ ≈ **$0,53/mes** |
| **Total plaza ociosa** | ≈ **$0,60/mes** (frente a $91/mes una VM `RUNNING`) |
| Plaza tomada | la toma ($0,0014) + el sandbox normal ($0,126/h mientras corre) |

El e2e completo (≈ 46 lanzamientos de segundos: 20 tomas + 20 `create()` +
las plazas de relleno, el reciclado y la recuperación) cuesta ≈ $0,25.

??? info "Fuentes y mediciones"
    - Diseño: ADR-008 en [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md);
      custodia del secreto: T2 y T14 en [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
    - Latencias del pool: [`clients/python/tests/e2e/test_m7_pool.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/tests/e2e/test_m7_pool.py)
      (run del 2026-09-16); arranque en frío:
      [`docs/benchmarks/2026-09-cold-start.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/benchmarks/2026-09-cold-start.md).
    - Precios: `AWS_API_NOTES.md` §12; la rotación de `/run`: Q53.
