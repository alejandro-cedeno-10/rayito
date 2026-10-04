# Comandos

`sbx.commands` ejecuta procesos dentro del sandbox con `/bin/sh -c`, como el
usuario `user` (uid 1000): en primer plano (esperas el resultado) o en
segundo plano (recibes un handle y sigues).

## Cuándo usarlo

- Instalar paquetes, ejecutar scripts, compilar o lanzar tests.
- Arrancar un servidor en segundo plano y llegar a él con
  [`get_host()`](puertos-y-host.md).
- **Cuándo no**: para código Python con estado entre llamadas, usa
  [`run_code`](ejecutar-codigo.md); para una sesión interactiva, una
  [terminal (PTY)](terminal-pty.md).

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import CommandExitException, Sandbox

    with Sandbox.create() as sbx:
        result = sbx.commands.run("echo hola")
        print(result.stdout, result.exit_code)  # "hola\n" 0

        server = sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
        print(server.pid)
        server.kill()

        try:
            sbx.commands.run("exit 3")
        except CommandExitException as error:  # (1)!
            print(error.exit_code)  # 3
    ```

    1. Un código de salida distinto de cero es una excepción, como en E2B.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, CommandExitException


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            result = await sbx.commands.run("echo hola")
            print(result.stdout, result.exit_code)  # "hola\n" 0

            server = await sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
            print(server.pid)
            await server.kill()

            try:
                await sbx.commands.run("exit 3")
            except CommandExitException as error:
                print(error.exit_code)  # 3


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { CommandExitError, Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const result = await sbx.commands.run("echo hola");
    console.log(result.stdout, result.exitCode); // "hola\n" 0

    const server = await sbx.commands.run("python3 -m http.server 3000", {
      background: true,
      timeoutMs: 0, // (1)!
    });
    console.log(server.pid);
    await server.kill();

    try {
      await sbx.commands.run("exit 3");
    } catch (error) {
      if (error instanceof CommandExitError) console.log(error.exitCode); // 3
    }
    ```

    1. `0` es "sin límite" en TypeScript; en Python, `timeout=None`.

=== "CLI"

    ```bash
    rayito sandbox exec microvm-<id> --token-file ~/.rayito/demo.token -- python3 -c 'print(42)'
    rayito sandbox exec microvm-<id> --background --token-file ~/.rayito/demo.token -- sleep 600
    ```

## Paso a paso

### Primer plano

`commands.run(cmd)` espera a que el proceso termine y devuelve un
`CommandResult` con `stdout`, `stderr` y `exit_code`. El `timeout` (60 s por
defecto) lo impone el agente dentro del sandbox: al vencer mata el proceso y
lanza `TimeoutException`.

Para ver la salida mientras llega, pasa callbacks:

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.commands.run(
            "for i in 1 2 3; do echo $i; sleep 1; done",
            on_stdout=lambda line: print("out:", line, end=""),
            on_stderr=lambda line: print("err:", line, end=""),
        )
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            await sbx.commands.run(
                "for i in 1 2 3; do echo $i; sleep 1; done",
                on_stdout=lambda line: print("out:", line, end=""),
            )


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.commands.run("for i in 1 2 3; do echo $i; sleep 1; done", {
      onStdout: (line) => process.stdout.write(`out: ${line}`),
      onStderr: (line) => process.stderr.write(`err: ${line}`),
    });
    ```

### Segundo plano, `stdin` y reconexión

Con `background=True` recibes un `CommandHandle` en cuanto el proceso
arranca. Con él puedes esperar (`wait()`), matar (`kill()`), escribir en su
entrada (`send_stdin()`, con `stdin=True` al lanzarlo) o soltarlo
(`disconnect()`): el proceso sigue vivo y otro proceso puede volver a
engancharse con `commands.connect(pid)`.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        handle = sbx.commands.run("cat", background=True, stdin=True)
        handle.send_stdin("hola\n")
        handle.close_stdin()  # (1)!
        print(handle.wait().stdout)  # "hola\n"

        worker = sbx.commands.run("sleep 30; echo listo", background=True)
        worker.disconnect()  # (2)!
        again = sbx.commands.connect(worker.pid)
        print(again.wait().stdout)  # "listo\n"
        print([p.pid for p in sbx.commands.list()])  # los procesos vivos
    ```

    1. Cierra la entrada estándar: `cat` termina.
    2. Suelta el stream; el proceso sigue corriendo dentro del sandbox.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            handle = await sbx.commands.run("cat", background=True, stdin=True)
            await handle.send_stdin("hola\n")
            await handle.close_stdin()
            print((await handle.wait()).stdout)  # "hola\n"

            worker = await sbx.commands.run("sleep 30; echo listo", background=True)
            worker.disconnect()
            again = await sbx.commands.connect(worker.pid)
            print((await again.wait()).stdout)  # "listo\n"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const handle = await sbx.commands.run("cat", { background: true, stdin: true });
    await handle.sendStdin("hola\n");
    await handle.closeStdin();
    console.log((await handle.wait()).stdout); // "hola\n"

    const worker = await sbx.commands.run("sleep 30; echo listo", { background: true });
    worker.disconnect();
    const again = await sbx.commands.connect(worker.pid);
    console.log((await again.wait()).stdout); // "listo\n"
    console.log((await sbx.commands.list()).map((p) => p.pid));
    ```

También puedes iterar el handle para procesar la salida a trozos:
`for stdout, stderr, _ in handle:` en Python y
`for await (const chunk of handle)` en TypeScript.

### Entorno, usuario y directorio

`envs=`, `user=` y `cwd=` se aplican a un comando concreto. El entorno del
proceso se construye desde cero: sólo lleva las variables de la imagen, las
de `create(envs=)` y las del comando.

```python
from rayito import Sandbox

with Sandbox.create(envs={"APP_ENV": "dev"}) as sbx:
    out = sbx.commands.run("echo $APP_ENV $MODE; pwd", envs={"MODE": "test"}, cwd="/tmp")
    print(out.stdout)  # "dev test\n/tmp\n"
```

`user="root"` sólo funciona si la imagen lo permite (`RAYITO_ALLOW_ROOT=1`
en la imagen); por defecto todo corre como `user`.

### Después de una pausa o un corte

Si el sandbox se suspende o el proxy corta la conexión, cada handle se
reengancha solo la próxima vez que lo lees, sin perder salida
(`Connect(from_seq)`). La salida se retiene en el agente: hasta 64 trozos de
32 KiB por suscriptor; si nadie lee en 30 s y se llena, el comando termina
con `output_truncated`.

## Opciones de `commands.run`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `cmd` | `cmd` | — | la orden, ejecutada con `/bin/sh -c` |
| `background` | `background` | `False` | devuelve un `CommandHandle` sin esperar |
| `envs` | `envs` | — | variables de entorno de este proceso |
| `user` | `user` | `"user"` | usuario que ejecuta el proceso |
| `cwd` | `cwd` | el `HOME` del usuario | directorio de trabajo |
| `on_stdout`, `on_stderr` | `onStdout`, `onStderr` | — | callbacks con cada trozo de salida |
| `stdin` | `stdin` | `False` | deja la entrada estándar abierta para `send_stdin` |
| `timeout` | `timeoutMs` | 60 s / 60 000 ms | límite en el servidor; `None` / `0` sin límite |
| `request_timeout` | `requestTimeoutMs` | 60 s | plazo de cada llamada al agente |
| `secrets` | `secrets` | — | [secretos](../secrets.md) como variables de entorno (opcional, con coste) |
| `max_output_bytes` | `maxOutputBytes` | 64 MiB | tope de la salida guardada por descriptor; ver [Salida guardada](#salida-guardada) |
| — | `signal` | — | `AbortSignal` que cancela la llamada |

### Salida guardada

El SDK guarda en memoria la salida de cada comando (la de `result.stdout`
y la del handle) hasta `max_output_bytes`/`maxOutputBytes` por descriptor:
64 MiB por defecto (`COMMAND_OUTPUT_MAX_BYTES`). Si un proceso escribe más,
se queda con el final, descarta lo más antiguo y marca el resultado con
`truncated` (también `CommandExitException`/`CommandExitError`). Los
callbacks reciben siempre todo. Con `0` no guarda nada: útil cuando ya
consumes la salida por callbacks. Así un comando del sandbox que escribe sin
parar no agota la memoria de tu proceso; las PTY usan el mismo tope.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        result = sbx.commands.run(
            "cat build.log", on_stdout=print, max_output_bytes=1024 * 1024
        )
        if result.truncated:
            print("sólo el último MiB está en result.stdout")
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const result = await sbx.commands.run("cat build.log", {
      onStdout: (text) => process.stdout.write(text),
      maxOutputBytes: 1024 * 1024,
    });
    if (result.truncated) {
      console.log("sólo el último MiB está en result.stdout");
    }
    ```

Los demás métodos: `commands.list()`, `commands.kill(pid)`,
`commands.send_stdin(pid, data)`, `commands.close_stdin(pid)` y
`commands.connect(pid)` (TypeScript: `sendStdin`, `closeStdin`).

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `CommandExitException` | `CommandExitError` | el proceso salió con código distinto de cero (`exit_code`, `stdout`, `stderr`) | captúrala si el fallo es esperable |
| `TimeoutException` | `TimeoutError` | venció el `timeout` del comando | sube `timeout` o usa `background=True` |
| `RateLimitException` | `RateLimitError` | más de 256 procesos y PTYs vivos en el sandbox | mata procesos que ya no uses |
| `SandboxException` con `output_truncated` | `SandboxError` | nadie leyó la salida en 30 s y se llenó el búfer | consume el handle o redirige a un fichero |
| `NotFoundException` | `NotFoundError` | `connect(pid)` o `kill(pid)` de un proceso que ya no existe | — |

Tabla completa: [Errores](../referencia/errores.md).

## Diferencias con E2B

- Ninguna en la API: `rayito.e2b` usa el mismo `Commands`.
- Un comando en segundo plano no despierta a un sandbox suspendido al
  leerlo: espera a que algo lo reanude.

## Ver también

- [Terminal (PTY)](terminal-pty.md)
- [Ejecutar código](ejecutar-codigo.md)
- [Puertos y host](puertos-y-host.md)
- Referencia: [Python](../referencia/python/subclientes.md) y
  [TypeScript](../referencia/typescript.md#commands)
