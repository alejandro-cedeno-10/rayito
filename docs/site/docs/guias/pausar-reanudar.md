# Pausar y reanudar

`pause()` congela el sandbox entero en un snapshot de memoria y `resume()`
lo despierta tal como estaba: procesos, terminales, ficheros y las
variables del kernel siguen vivos al otro lado.

## Cuándo usarlo

- Un agente que espera al usuario minutos u horas: pausado no paga cómputo,
  sólo el almacenamiento del snapshot.
- Trabajo con huecos: la **auto-suspensión** (activa por defecto) pausa el
  sandbox tras 300 s sin tráfico y lo reanuda con la siguiente llamada.
- **Cuándo no**: pausas de menos de ≈ 150 s. Un ciclo suspend/resume cuesta
  lo mismo que ≈ 140 s de cómputo, así que no ahorra dinero.

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

Sin handle, las formas de clase hacen lo mismo:
`Sandbox.pause(sandbox_id)` / `Sandbox.resume(sandbox_id)` (TypeScript:
`Sandbox.pause(sandboxId)` / `Sandbox.resume(sandboxId)`).

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

| Concepto | Coste (us-east-1, 2026-09, imagen de 2 GB) |
|---|---|
| Suspendido | sólo el almacenamiento del snapshot, ≈ $0,08 por GB-mes |
| Un ciclo suspend + resume | ≈ $0,0049 (escribir y leer ≈ 0,92 GB de snapshot), igual que ≈ 140 s de cómputo |
| Corriendo | ≈ $0,126/h |

Detalle y fuentes: [Costes](../cost.md#precios).

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `SandboxStateException` | `SandboxStateError` | el sandbox está terminando o en una transición que no admite la operación | espera y reintenta, o crea uno nuevo |
| `SandboxNotFoundException` | `SandboxNotFoundError` | el sandbox ya no existe (llegó a su `timeout`, también suspendido) | crea uno nuevo; para más de 8 h, [Persistencia](../persistence.md) |
| `RateLimitException` | `RateLimitError` | más de 2 suspensiones por segundo en la cuenta | el SDK ya limita el ritmo por proceso; espacia las pausas |

## Diferencias con E2B

- El tiempo suspendido cuenta para la vida máxima de 8 h: un sandbox pausado
  también termina a las 8 h desde su arranque.
- `pause()` siempre guarda la memoria: `pause(keep_memory=False)` de E2B no
  existe.

## Ver también

- [Conceptos: plazo, tope e idle](../concepts.md#plazo-tope-e-idle)
- [Plazo del servidor](../lifecycle.md): pausar al vencer un plazo
- [Pool](../pool.md): sandboxes suspendidos y listos para usar
- [Costes](../cost.md)
