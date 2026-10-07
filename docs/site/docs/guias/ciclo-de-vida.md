# Ciclo de vida

Crear, conectar, inspeccionar, listar y destruir sandboxes. Cada sandbox es
un MicroVM de Lambda en tu cuenta, con una vida máxima de 8 horas.

## Cuándo usarlo

- Siempre: es el punto de partida de todo lo demás.
- `connect()` cuando otro proceso (un worker, un reintento, otra máquina)
  tiene que seguir trabajando en el mismo sandbox.
- `list()` y `kill()` de clase para limpiar sandboxes que nadie usa.

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create("rayito-base", timeout=1800, metadata={"run": "42"}) as sbx:
        info = sbx.get_info()
        print(info.sandbox_id, info.state, info.metadata)  # ... RUNNING {'run': '42'}
        print(sbx.is_running())  # True

    for item in Sandbox.list(metadata={"run": "42"}):  # (1)!
        Sandbox.kill(item.sandbox_id)
    ```

    1. Al salir del `with` el sandbox ya está muerto: este bucle no
       encuentra nada. Es el patrón para limpiar sandboxes huérfanos.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create("rayito-base", timeout=1800, metadata={"run": "42"})
        async with sbx:
            info = await sbx.get_info()
            print(info.sandbox_id, info.state, info.metadata)
            print(await sbx.is_running())

        for item in await AsyncSandbox.list(metadata={"run": "42"}):
            await AsyncSandbox.kill(item.sandbox_id)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    {
      await using sbx = await Sandbox.create({
        template: "rayito-base",
        timeoutMs: 1_800_000,
        metadata: { run: "42" },
      });
      const info = await sbx.getInfo();
      console.log(info.sandboxId, info.state, info.metadata);
      console.log(await sbx.isRunning()); // true
    }

    for await (const item of Sandbox.list({ metadata: { run: "42" } })) {
      await Sandbox.kill(item.sandboxId);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list                               # los vivos, con su edad
    rayito sandbox info microvm-<id>                  # estado, imagen y metadatos
    rayito sandbox kill microvm-<id>                  # o: rayito sandbox kill --all
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create("rayito-base", timeout=1800, metadata={"run": "42"}) as sbx:  # (1)!
        print(sbx.get_info().sandbox_id, sbx.is_running())
        token = sbx.native.access_token  # (2)!

    for item in Sandbox.list().next_items():  # (3)!
        print(item.sandbox_id, item.state)
    ```

    1. En el shim, `timeout` es el plazo lógico de E2B (300 s por defecto),
       no la vida de la plataforma: ver [Plazo del servidor](../lifecycle.md#en-el-shim-de-e2b).
    2. E2B no tiene este token; guárdalo si otro proceso va a usar
       `Sandbox.connect(sandbox_id, access_token=...)`.
    3. Un `SandboxPaginator` de E2B (`next_items()`, `has_next`,
       `next_token`).

## Paso a paso

### Crear

`Sandbox.create()` lanza un MicroVM (`run-microvm`) desde una imagen y
espera a que el agente y el kernel estén listos. La imagen sale, por orden,
del primer argumento (`template`), de `RAYITO_TEMPLATE` o falla. Puede ser un
nombre (`rayito-base`, resuelto al ARN de tu cuenta) o un ARN completo.

El tamaño del sandbox (CPU y memoria) es una propiedad de la imagen: la
memoria se fija al publicarla. `size="4gb"` (TypeScript: `size: "4gb"`)
elige, sin llamar a AWS, la imagen de ese tamaño ya publicada
(`rayito-base-4gb`); ver [Tamaños](../funciones-opcionales/tamanos.md) y
[Límites: tamaño](../limits.md#tamano-cpuram).

### Destruir

`kill()` termina el MicroVM (`terminate-microvm`) y devuelve `True` si lo
encontró. `with` (Python) y `await using` (TypeScript) lo llaman al salir;
sin ellos, llama tú a `kill()` en un `finally`. La forma de clase,
`Sandbox.kill(sandbox_id)`, mata un sandbox del que sólo tienes el id (no
necesita el access token). `close()` es otra cosa: libera los recursos
locales del handle (canales, hilo de renovación del token) y deja el
MicroVM vivo.

!!! warning "Un sandbox olvidado factura"
    Vive hasta su `timeout` (3600 s por defecto, tope 8 h) facturando
    cómputo ([precios](../cost.md#precios)). Con la
    [auto-suspensión](pausar-reanudar.md) por defecto, un sandbox sin
    tráfico se suspende a los 300 s y deja de facturar cómputo, pero sigue
    pagando el almacenamiento de su snapshot.

### Conectar

`Sandbox.connect(sandbox_id, access_token=...)` abre un handle nuevo sobre
un sandbox existente, desde cualquier proceso con credenciales de AWS. Hace
falta el *access token* del sandbox (`sbx.access_token`, o la variable
`RAYITO_ACCESS_TOKEN`), que se genera en `create()` y no se puede recuperar
después: guárdalo junto al id. Si el sandbox estaba suspendido, `connect()`
lo reanuda; con `timeout=` alarga además su
[plazo lógico](../lifecycle.md#set_timeout-y-connecttimeout).

Sobre un handle que ya tienes, `sbx.connect()` (TypeScript:
`await sbx.connect()`) lo reabre igual: reanuda si hace falta, espera al
agente y devuelve el mismo objeto. Es la forma de continuar después de un
`pause()` ([Pausar y reanudar](pausar-reanudar.md)).

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token  # guárdalos juntos
    sbx.close()  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)
    try:
        print(again.commands.run("echo sigo vivo").stdout)
    finally:
        again.kill()
    ```

    1. Suelta el handle local; el sandbox sigue corriendo.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create()
        sandbox_id, token = sbx.sandbox_id, sbx.access_token
        await sbx.close()

        again = await AsyncSandbox.connect(sandbox_id, access_token=token)
        async with again:
            print((await again.commands.run("echo sigo vivo")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx; // guárdalos juntos
    sbx.close(); // suelta el handle local; el sandbox sigue corriendo

    await using again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("echo sigo vivo")).stdout);
    ```

### Inspeccionar

`get_info()` devuelve un `SandboxInfo`: `sandbox_id`, `state`
(`RUNNING`, `SUSPENDED`…), `endpoint`, `template`, `template_version`,
`started_at`, `metadata`, `lifecycle`, `agent_version`, `cpu_count` y
`memory_mb` (más `size` si usaste `size=`). `expires_at` es el plazo
vigente, `platform_expires_at` el tope de la plataforma y
`remaining_seconds()` lo que queda ([Plazo del servidor](../lifecycle.md)).
`Sandbox.get_info(sandbox_id)` hace lo mismo sin handle. `sbx.info` es la
última `SandboxInfo` conocida, sin llamar a AWS. `is_running()` responde si
el agente contesta, y `get_health()` devuelve el `SandboxHealth` del agente
(`resume_generation`, `kernel_state_lost`, `lifecycle`…).

### Metadatos

`metadata={"clave": "valor"}` etiqueta el sandbox al crearlo. Es inmutable,
viaja con el lanzamiento y **no es secreto**: cualquiera que pueda acuñar un
token del proxy para el sandbox lo lee. Sirve para filtrar el listado.

### Listar

`Sandbox.list()` recorre `list-microvms` de forma perezosa y devuelve
`SandboxListItem` (`sandbox_id`, `state`, `template`, `template_version`,
`started_at`, `metadata`). En Python síncrono es un iterador; en
`AsyncSandbox.list()` una corrutina que devuelve la lista, y en TypeScript
un `AsyncIterable`. Filtros: `template=`, `template_version=`, `states=`,
`metadata=`, `started_after=` y `order=` (TypeScript: `templateVersion`,
`startedAfter`). Para un cursor que se pueda guardar y reanudar,
`Sandbox.paginate()`
([Métricas y listado](../observability.md#listado-reanudable)).

!!! note "`list(metadata=)` es O(n)"
    Sin índice, filtrar por metadatos sondea cada sandbox `RUNNING` (≈ 0,5–1 s
    por sandbox) y nunca ve los suspendidos. Para flotas grandes o sandboxes
    en pausa, el [índice de metadatos](../funciones-opcionales/indice-de-metadatos.md)
    (opcional, con coste) lo resuelve con una consulta a DynamoDB.

## Opciones de `create()`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `template` (1.er argumento) | `template` | `RAYITO_TEMPLATE` | nombre o ARN de la imagen |
| `template_version` | `templateVersion` | la última activa | versión concreta de la imagen |
| `timeout` | `timeoutMs` | 3600 s / 3 600 000 ms | vida máxima (running + suspendido), tope 28 800 s; con `max_lifetime` u `on_timeout` es el [plazo del servidor](../lifecycle.md) |
| `max_lifetime`, `on_timeout` | `maxLifetimeMs`, `onTimeout` | — | [plazo del servidor](../lifecycle.md) |
| `idle` | `idle` | `IdlePolicy(max_idle_seconds=300)` | auto-suspensión por inactividad; `None` / `null` la desactiva, y sin `idle` se desactiva sola si no cabe antes del `timeout` ([Pausar y reanudar](pausar-reanudar.md)) |
| `envs` | `envs` | — | variables de entorno de todos los procesos (no secretas: viajan en el lanzamiento) |
| `metadata` | `metadata` | — | etiquetas inmutables y no secretas |
| `cpu_time_limit` | `cpuTimeLimit` | — | segundos de CPU por proceso (`RLIMIT_CPU`) |
| `execution_role_arn` | `executionRoleArn` | — | rol IAM dentro del sandbox (IMDSv2); sin él no hay credenciales dentro |
| `logging` | `logging` | `"disabled"` | `"cloudwatch"` envía los logs del runtime (necesita `execution_role_arn`) |
| `allowed_ports` | `allowedPorts` | sólo 8080 | puertos extra del token principal del proxy ([Puertos y host](puertos-y-host.md)) |
| `ingress`, `egress` | `ingress`, `egress` | conectores gestionados | conectores de red de Lambda MicroVMs |
| `network`, `allow_internet_access` | `network`, `allowInternetAccess` | sin restricción | [Red saliente](../network.md) (sólo `rayito-base-caps`) |
| `transfer` | `transfer` | `RAYITO_TRANSFER_BUCKET` | bucket para ficheros grandes y URLs ([Ficheros y S3](../files.md)) |
| `persist`, `persist_timeout` | `persist`, `persistTimeoutMs` | —, 600 s | `HOME` en S3 que sobrevive a `kill()` y a las 8 h ([Persistencia](../persistence.md)) |
| `pool` | `pool` | — | tomar de un [pool](../pool.md) |
| `access_token` | `accessToken` | 32 bytes aleatorios | fija el secreto del sandbox |
| `region`, `session` | `region` | la de la sesión | región y credenciales de AWS |
| `ready_timeout`, `request_timeout`, `reconnect_timeout` | `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | 90 / 60 / 60 s | plazos del cliente |
| `keep_on_failure` | `keepOnFailure` | `False` | no termina el MicroVM si el arranque falla (para depurar) |
| `logger` | `logger` | logger `rayito` / en silencio | dónde registra el SDK (sólo ids, estados y duraciones) |
| `control_plane`, `transport` | `controlPlane`, `transport` | AWS real | adaptadores inyectables (tests, [entorno local](probar-en-local.md)) |
| — | `signal` | — | `AbortSignal` que cancela la creación |
| `secrets`, `secret_cache` | `secrets`, `secretCache` | apagado | [Secretos](../secrets.md) (opcional, con coste) |
| `index` | `index` | apagado | [Índice de metadatos](../funciones-opcionales/indice-de-metadatos.md) (opcional, con coste) |
| `tracer_provider` | `tracerProvider` | apagado | [OpenTelemetry](../funciones-opcionales/opentelemetry.md) |
| `size` | `size` | la imagen de siempre | [Tamaños](../funciones-opcionales/tamanos.md) (opcional) |
| `mounts` | `mounts` | apagado | [Montajes S3](../funciones-opcionales/montajes-s3.md) (opcional, con coste) |
| `volumes` | `volumes` | apagado | [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md) (**experimental**, opcional, con coste) |
| `events` | `events` | apagado | [Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md) (opcional, con coste) |
| `telemetry` | `telemetry` | apagado | [Exportación OTLP](../funciones-opcionales/exportacion-otlp.md) (opcional, con coste) |
| `gateways` | `gateways` | apagado | [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md) (opcional, con coste) |
| `domain` | `domain` | — | reservado para el [dominio propio](../funciones-opcionales/dominio-propio.md) (**experimental**): hoy lanza `UnimplementedError` |

Todas las opciones con coste están apagadas por defecto: ver
[Funciones opcionales](../optional-features.md).

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | sin imagen (`RAYITO_TEMPLATE`), `envs` + `metadata` por encima del [límite del payload](../limits.md) | corrige el argumento; no se llamó a AWS |
| `SandboxLifetimeException` | `SandboxLifetimeError` | `timeout` o `max_lifetime` por encima de 28 800 s | baja el plazo; para más, [Persistencia](../persistence.md) |
| `SandboxNotFoundException` | `SandboxNotFoundError` | `connect()` o `get_info()` de un id que no existe o ya terminó | crea uno nuevo |
| `AuthenticationException` | `AuthenticationError` | access token incorrecto o credenciales de AWS caducadas | revisa el token guardado; `aws sso login` |
| `SandboxNotReadyException` | `SandboxNotReadyError` | el agente no estuvo listo en `ready_timeout` | `rayito doctor`; prueba `keep_on_failure=True` y mira los logs |
| `QuotaExceededException` | `QuotaExceededError` | cuota de memoria o de MicroVMs de la región agotada | mata sandboxes o pide aumento de cuota |
| `CapacityException` | `CapacityError` | AWS sin capacidad momentánea en la región | reintenta con backoff |
| `UnimplementedError` | `UnimplementedError` | la imagen es demasiado antigua para lo pedido | publica una imagen de la release actual |

Tabla completa: [Errores](../referencia/errores.md).

## Diferencias con E2B

- La vida máxima es de 8 h, contando el tiempo suspendido. E2B guarda
  sandboxes pausados sin límite.
- `connect()` y las formas de clase necesitan el access token del sandbox:
  no hay API key que lo sustituya.
- No hay `cpu_count`/`memory_mb` por sandbox: el tamaño lo fija la imagen
  (`size=` elige entre imágenes ya publicadas de cada tamaño).

## Ver también

- [Pausar y reanudar](pausar-reanudar.md)
- [Plazo del servidor](../lifecycle.md)
- [Referencia de `Sandbox` (Python)](../referencia/python/sandbox.md) y
  [TypeScript](../referencia/typescript.md#sandbox)
- [Costes](../cost.md)
