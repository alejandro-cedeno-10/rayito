# Pausar y reanudar

`pause()` congela el sandbox entero en un snapshot de memoria y `resume()`
lo despierta tal como estaba: procesos, terminales, ficheros y las
variables del kernel siguen vivos al otro lado.

## Cuándo usarlo

- Un agente que espera al usuario minutos u horas: pausado no paga cómputo,
  sólo el almacenamiento del snapshot.
  Para un [agente de código](agente-en-el-sandbox.md) es la opción por
  defecto entre turnos: ver [¿Qué uso?](agente-en-el-sandbox.md#que-uso).
- Trabajo con huecos: la **auto-suspensión** (activa por defecto) pausa el
  sandbox tras 300 s sin tráfico y lo reanuda con la siguiente llamada.
- **Cuándo no**: pausas de menos de ≈ 150 s. Un ciclo suspend/resume cuesta
  lo mismo que ≈ 140 s de cómputo ([Costes](../cost.md#precios)), así que no
  ahorra dinero.
- **Tampoco** para esperas de más de 8 h: el tiempo suspendido cuenta para la
  vida máxima y el sandbox termina igual. Guarda el `HOME` con
  [persistencia](../persistence.md) (`persist=`) y crea otra VM cuando haga
  falta; para un agente, ver el punto 2 de
  [¿Qué uso?](agente-en-el-sandbox.md#que-uso).

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        sbx.pause()  # (1)!
        sbx.resume()
        print(sbx.run_code("x").text)  # "42": el kernel conservó su estado
        print(sbx.get_health().resume_generation)  # 1
    ```

    1. `suspend-microvm`. Tarda ≈ 1,4 s; `resume()` ≈ 0,4 s.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.run_code("x = 42")
            await sbx.pause()
            await sbx.resume()
            print((await sbx.run_code("x")).text)  # "42"
            print((await sbx.get_health()).resume_generation)  # 1


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.runCode("x = 42");
    await sbx.pause();
    await sbx.resume();
    console.log((await sbx.runCode("x")).text); // "42"
    console.log((await sbx.getHealth()).resumeGeneration); // 1
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create() as sbx:
        sbx.run_code("x = 42")
        sbx.pause()  # (1)!
        sbx.connect()  # (2)!
        print(sbx.run_code("x").text)  # "42"
    ```

    1. `pause(keep_memory=False)` es `UnimplementedError`: la pausa siempre
       guarda memoria y disco. `beta_pause()` es un alias.
    2. Como en E2B, `connect()` reanuda; la forma de clase
       `Sandbox.connect(sandbox_id, access_token=...)` lo hace desde otro
       proceso.

## Paso a paso

### Pausa manual

`pause()` pide a AWS la suspensión del MicroVM y espera a `SUSPENDED`.
Devuelve `False` si ya estaba suspendido. Los streams abiertos (un comando
en segundo plano, una PTY, un `watch_dir`, una celda en curso) se cortan con
un final ordenado y **se reenganchan solos** la próxima vez que los lees
tras reanudar.

`resume()` reanuda el MicroVM, espera al agente y registra la nueva
generación (`get_health().resume_generation`). Si un kernel no sobrevivió,
`get_health().kernel_state_lost` lo avisa.

`pause(wait=False)` y `resume(wait=False)` (TypeScript: `{ wait: false }`)
piden la transición y vuelven sin esperarla.

Sin handle, las formas de clase hacen lo mismo:
`Sandbox.pause(sandbox_id)` / `Sandbox.resume(sandbox_id)` (TypeScript:
`Sandbox.pause(sandboxId)` / `Sandbox.resume(sandboxId)`). No necesitan el
access token, pero tampoco pueden esperar al agente: `resume` sólo espera a
que `get-microvm` diga `RUNNING`.

### Continuar desde otro proceso

`connect()` también reanuda: `sbx.connect()` sobre el handle que ya tienes,
o `Sandbox.connect(sandbox_id, access_token=...)` desde otro proceso (un
worker, el siguiente turno de un agente). Es el patrón de una conversación
pausada entre turnos ([Ciclo de vida: conectar](ciclo-de-vida.md#conectar)).

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token
    sbx.pause()
    sbx.close()  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)  # (2)!
    try:
        print(again.commands.run("echo de vuelta").stdout)
    finally:
        again.kill()
    ```

    1. El sandbox sigue suspendido; sólo se suelta el handle local.
    2. Reanuda y espera al agente.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create()
        sandbox_id, token = sbx.sandbox_id, sbx.access_token
        await sbx.pause()
        await sbx.close()

        async with await AsyncSandbox.connect(sandbox_id, access_token=token) as again:
            print((await again.commands.run("echo de vuelta")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx;
    await sbx.pause();
    sbx.close(); // el sandbox sigue suspendido

    await using again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("echo de vuelta")).stdout);
    ```

### Auto-suspensión e `IdlePolicy`

Por defecto todo sandbox se crea con `IdlePolicy(max_idle_seconds=300,
auto_resume=True)`: AWS lo suspende tras 300 s sin tráfico por su endpoint,
y la siguiente llamada del SDK lo despierta sola (≈ 0,7 s).

=== "Python"

    ```python
    from rayito import IdlePolicy, Sandbox

    with Sandbox.create(idle=IdlePolicy(max_idle_seconds=900)) as sbx:  # (1)!
        print(sbx.commands.run("echo despierto").stdout)

    with Sandbox.create(idle=None) as sbx:  # (2)!
        print(sbx.commands.run("echo nunca se suspende").stdout)
    ```

    1. Suspende tras 15 minutos sin tráfico.
    2. Sin auto-suspensión: factura cómputo hasta su `timeout`.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, IdlePolicy


    async def main() -> None:
        async with await AsyncSandbox.create(idle=IdlePolicy(max_idle_seconds=900)) as sbx:
            print((await sbx.commands.run("echo despierto")).stdout)
        async with await AsyncSandbox.create(idle=None) as sbx:
            print((await sbx.commands.run("echo nunca se suspende")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    {
      await using sbx = await Sandbox.create({ idle: { maxIdleSeconds: 900 } });
      console.log((await sbx.commands.run("echo despierto")).stdout);
    }
    {
      await using sbx = await Sandbox.create({ idle: null });
      console.log((await sbx.commands.run("echo nunca se suspende")).stdout);
    }
    ```

!!! warning "Una celda silenciosa puede suspenderse a mitad"
    La inactividad cuenta los bytes que cruzan el endpoint. Una celda o un
    comando que no imprime nada durante `max_idle_seconds` puede suspenderse
    mientras corre. El SDK espera la reanudación y se reengancha, pero si tus
    tareas son largas y silenciosas, sube `max_idle_seconds` o pasa
    `idle=None`.

!!! note "Leer un handle no despierta al sandbox"
    Mientras está suspendido, leer un comando en segundo plano, una PTY o un
    watch se bloquea hasta que algo lo reanude (`resume()`, o cualquier
    llamada nueva como `commands.run` con auto-resume). No consume el
    presupuesto de reconexión.

## Opciones de `IdlePolicy`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `max_idle_seconds` | `maxIdleSeconds` | 300 | segundos sin tráfico antes de suspender (mínimo 60) |
| `auto_resume` | `autoResume` | `True` | la siguiente llamada reanuda un sandbox suspendido |
| `suspended_duration_seconds` | `suspendedDurationSeconds` | `timeout − max_idle_seconds` | cuánto puede seguir suspendido antes de terminarse; `0` termina al suspender |
| `idle=None` | `idle: null` | — | desactiva la auto-suspensión |

## Coste

- **Suspendido**: no paga cómputo, sólo el almacenamiento del snapshot.
- **Cada ciclo suspend + resume** escribe y lee el snapshot entero: cuesta
  lo mismo que ≈ 140 s de cómputo.
- **Corriendo**: el cómputo normal del tamaño de la imagen.

Precios y ejemplos, con fuentes: [Costes](../cost.md#precios).

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `SandboxStateException` | `SandboxStateError` | el sandbox está terminando o en una transición que no admite la operación | espera y reintenta, o crea uno nuevo |
| `SandboxNotFoundException` | `SandboxNotFoundError` | el sandbox ya no existe (llegó a su `timeout`, también suspendido) | crea uno nuevo; para más de 8 h, [Persistencia](../persistence.md) (`persist=`) |
| `RateLimitException` | `RateLimitError` | más de 2 suspensiones por segundo en la cuenta | el SDK ya limita el ritmo por proceso; espacia las pausas |

## Diferencias con E2B

- El tiempo suspendido cuenta para la vida máxima de 8 h: un sandbox pausado
  también termina a las 8 h desde su arranque.
- `pause()` siempre guarda la memoria: en el shim, `pause(keep_memory=False)`
  de E2B es `UnimplementedError`.
- Además de `connect()` (la forma de E2B), Rayito tiene `resume()`
  explícito.

## Ver también

- [Conceptos: plazo, tope e idle](../concepts.md#plazo-tope-e-idle)
- [Plazo del servidor](../lifecycle.md): pausar al vencer un plazo
- [Pool](../pool.md): sandboxes suspendidos y listos para usar
- [Costes](../cost.md)
