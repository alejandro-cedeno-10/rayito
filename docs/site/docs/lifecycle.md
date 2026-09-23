# Plazo del servidor

Desde M9 (`m9-server-timeout`, ADR-011) un sandbox puede tener un **plazo
lógico** que impone `rayd` dentro del MicroVM, aunque tu proceso muera: al
vencer, el sandbox se termina (`on_timeout="kill"`) o se suspende
(`on_timeout="pause"`). El plazo se mueve en caliente con `set_timeout()` y
se alarga con `connect(timeout=)`, como en E2B. El **tope** de la plataforma
(`max_lifetime`, el `maximumDurationInSeconds` de `run-microvm`) se fija en
`create()` y no se mueve: no existe `UpdateMicrovm`. La tabla de los tres
relojes (plazo, tope, idle) está en [Conceptos](concepts.md#plazo-tope-e-idle).

!!! note "Exige una imagen M9"
    Pedir un ciclo de vida (`max_lifetime` u `on_timeout`) a una imagen
    anterior a M9 termina el VM (salvo `keep_on_failure`) y lanza
    `LifecycleUnsupportedException` (TS `LifecycleUnsupportedError`). Sin
    `max_lifetime` ni `on_timeout`, `create()` se comporta exactamente como en
    0.2.0: `timeout` es la vida de la plataforma y no se mueve.

## Crear con plazo

| Parámetro (Python / TS) | Valor | Qué hace |
|---|---|---|
| `timeout` / `timeoutMs` | ≥ 1 s (por defecto 3600 s) | el plazo lógico desde el arranque |
| `max_lifetime` / `maxLifetimeMs` | 120–28 800 s (TS: múltiplo de 1000 ms); por defecto `timeout + 60` (al menos 120) | el tope de la plataforma, running + suspendido; el plazo nunca pasa de `max_lifetime − 60 s` desde el arranque |
| `on_timeout` / `onTimeout` | `"kill"` (por defecto) o `"pause"` | qué hace `rayd` al vencer |
| `idle` | `IdlePolicy(...)` (por defecto 300 s y `auto_resume=True`) | en modo `pause` es obligatoria (`idle=None` es `InvalidArgumentException`), `max_idle_seconds` debe ser menor que `max_lifetime` y `auto_resume` es la regla de E2B tras el plazo |

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
    print(sbx.get_info().expires_at)          # el plazo lógico (ahora + 600 s)
    sbx.set_timeout(1800)                     # EXACT: ahora + 1800 s; puede acortar
    sbx.connect(timeout=900)                  # AT_LEAST: nunca acorta
    print(sbx.get_health().lifecycle)         # SandboxLifecycle(phase='active', ...)

    token, sandbox_id = sbx.access_token, sbx.sandbox_id
    # Desde otro proceso, sin handle: hace falta el access token
    Sandbox.set_timeout(sandbox_id, 3600, access_token=token)
    again = Sandbox.connect(sandbox_id, access_token=token, timeout=1200)
    again.kill()
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        sbx = await AsyncSandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
        try:
            await sbx.set_timeout(1800)
            await sbx.connect(timeout=900)
            info = await sbx.get_info()
            print(info.expires_at, info.lifecycle)
            await AsyncSandbox.set_timeout(sbx.sandbox_id, 3600, access_token=sbx.access_token)
        finally:
            await sbx.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      timeoutMs: 600_000,
      maxLifetimeMs: 7_200_000,
      onTimeout: "kill",
    });
    console.log((await sbx.getInfo()).expiresAt); // el plazo lógico
    await sbx.setTimeout(1_800_000); // EXACT
    await sbx.connect({ timeoutMs: 900_000 }); // AT_LEAST
    console.log((await sbx.getHealth()).lifecycle?.phase); // "active"

    await Sandbox.setTimeout(sbx.sandboxId, 3_600_000, { accessToken: sbx.accessToken });
    const again = await Sandbox.connect(sbx.sandboxId, {
      accessToken: sbx.accessToken,
      timeoutMs: 1_200_000,
    });
    again.close();
    ```

## `set_timeout` y `connect(timeout=)`

- **`set_timeout(t)`** (`SetTimeout` `EXACT`): el plazo pasa a ser ahora +
  `t`, más largo o más corto. Por encima de `max_lifetime − 60 s` desde el
  arranque es `InvalidArgumentException` y el plazo queda intacto; más allá
  del tope sólo queda `reincarnate()` ([Persistencia](persistence.md)). Sobre
  un sandbox creado sin `max_lifetime` ni `on_timeout` también es
  `InvalidArgumentException`. En modo `pause`, reabre un sandbox cuyo plazo
  venció pero que aún no se suspendió.
- **`connect(timeout=t)`** (`SetTimeout` `AT_LEAST`, la semántica de E2B):
  reanuda si hace falta y el plazo pasa a ser **al menos** ahora + `t`; nunca
  lo acorta.
- Las formas de clase (`Sandbox.set_timeout(id, t)`, `Sandbox.connect(id,
  timeout=t)`; TS `Sandbox.setTimeout(id, ms, { accessToken })`) necesitan
  el access token del sandbox (`access_token=` o `RAYITO_ACCESS_TOKEN`).
- `get_info().expires_at` es el plazo lógico vigente (lo relee de `Health`
  si otro cliente lo movió) y `platform_expires_at` el tope;
  `remaining_seconds()` cuenta lo que queda. `get_health().lifecycle` trae
  `phase` (`unmanaged`, `active`, `resume_grace`, `expired`), `deadline`,
  `cap`, `timeout_seconds`, `on_timeout`, `auto_resume` y `extensions`.

## Al vencer: `kill`

`rayd` cierra los streams con `sandbox_timeout`, manda `SIGTERM`/`SIGKILL` a
todos los procesos y sale con código 124; la VM pasa a `TERMINATED` ≈ 15 s
después sin ninguna llamada IAM (Q58). Durante esos segundos toda llamada
salvo `Health` y `SetTimeout` falla con `TimeoutException` (TS
`TimeoutError`). Después, `Sandbox.get_info(id).timed_out` es `True` (el
código 124 en el `stateReason`: la plataforma lo propaga como
`Container Stopped with Exit Code: 124`, medido en Q63, con `TERMINATED`
entre ≈ 15 y 19 s después del plazo). Se facturan esos segundos de 502.

## Al vencer: `pause`

=== "Python"

    ```python
    from rayito import IdlePolicy, Sandbox

    sbx = Sandbox.create(
        timeout=600,
        max_lifetime=7200,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create({
      timeoutMs: 600_000,
      maxLifetimeMs: 7_200_000,
      onTimeout: "pause",
      idle: { maxIdleSeconds: 300, autoResume: true },
    });
    ```

Modo **honesto-parcial**: con un cliente vivo (un `Sandbox` abierto en algún
proceso) el SDK suspende el sandbox ≈ 1 s después del plazo; **sin cliente**,
lo suspende la política de idle de la plataforma al cumplirse
`max_idle_seconds` sin tráfico. Entre el vencimiento y la suspensión los
procesos siguen corriendo (≈ 3 s con cliente vivo, hasta `max_idle` sin él),
y la política de idle puede suspender antes del plazo si no hay tráfico.

Con `auto_resume=True` la siguiente llamada lo reanuda y el plazo se reabre a
`max(timeout, 300 s)` desde ese momento (la regla de E2B), acotado al tope.
`rayd` la aplica cuando detecta la congelación; si la suspensión real duró
menos de 2 s, la aplica el SDK que suspendió con un `SetTimeout` en la
primera llamada tras reanudar. Con `auto_resume=False` el sandbox sigue
`expired` tras un resume suelto hasta que `connect(timeout=)` o
`set_timeout()` lo reabren. Un sandbox pausado sigue muriendo en el tope
(`max_lifetime`), también suspendido.

## En el shim de E2B

`rayito.e2b` / `rayito/e2b` siempre piden un plazo lógico: `timeout=300` (TS
`timeoutMs: 300_000`) con `on_timeout="kill"`, `max_lifetime` por defecto
`max(3600, min(timeout + 60, 28800))`, y `lifecycle={"on_timeout": "pause",
"auto_resume": True}` como en E2B ([Compatibilidad con E2B](e2b-compat.md)).

```python
from rayito.e2b import Sandbox

sbx = Sandbox.create(timeout=300, lifecycle={"on_timeout": "pause", "auto_resume": True})
sbx.set_timeout(900)
token = sbx.native.access_token  # E2B no tiene este token; guárdalo junto al sandbox_id
Sandbox.set_timeout(sbx.sandbox_id, 1200, access_token=token)
same = Sandbox.connect(sbx.sandbox_id, timeout=600, access_token=token)
```

En TypeScript el token está en `sbx.native.accessToken`.

## Diferencias con E2B

- El tope cuenta el tiempo suspendido: 8 h como mucho desde el arranque; E2B
  guarda sandboxes pausados sin límite. Las 86 400 s de E2B Pro no existen.
- En modo `pause` los procesos siguen corriendo entre el vencimiento y la
  suspensión, y sin cliente vivo la suspensión llega en `max_idle`.
- Las formas de clase necesitan el access token del sandbox.
- Una suspensión real de menos de 2 s que cruza el plazo no la reconoce
  `rayd` (la cubre el SDK que suspendió, arriba).
