# Terminal (PTY)

`sbx.pty` abre una terminal real dentro del sandbox: un shell de login
interactivo con su tamaño, sus colores y su control de trabajos, al que le
mandas teclas y del que recibes bytes.

## Cuándo usarlo

- Programas interactivos que necesitan una terminal (`vim`, `htop`, un REPL,
  un instalador que pregunta).
- Dar a una persona una consola dentro del sandbox (por ejemplo, con
  `xterm.js` en el navegador).
- **Cuándo no**: para ejecutar una orden y leer su salida, usa
  [comandos](comandos.md); es más simple y separa `stdout` de `stderr`.

## Ejemplo rápido

=== "Python"

    ```python
    from rayito import PtySize, Sandbox

    with Sandbox.create() as sbx:
        pty = sbx.pty.create(size=PtySize(cols=120, rows=40), timeout=None)
        pty.send_input("echo hola\n")
        for _, _, data in pty:  # (1)!
            if data and b"hola\r\n" in data:
                break
        pty.resize(PtySize(cols=80, rows=24))
        pty.kill()
    ```

    1. Cada trozo llega como `(None, None, bytes)`: la salida cruda de la
       terminal, con secuencias de escape.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, PtySize


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            pty = await sbx.pty.create(size=PtySize(cols=120, rows=40), timeout=None)
            await pty.send_input("echo hola\n")
            async for _, _, data in pty:
                if data and b"hola\r\n" in data:
                    break
            await pty.resize(PtySize(cols=80, rows=24))
            await pty.kill()


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const pty = await sbx.pty.create({ size: { cols: 120, rows: 40 }, timeoutMs: 0 });
    await pty.sendInput("echo hola\n");
    for await (const { pty: data } of pty) {
      if (data !== undefined && new TextDecoder().decode(data).includes("hola\r\n")) break;
    }
    await pty.resize({ cols: 80, rows: 24 });
    await pty.kill();
    ```

=== "CLI"

    ```bash
    rayito sandbox connect microvm-<id> --token-file ~/.rayito/demo.token   # Ctrl-D para salir
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import PtySize, Sandbox

    with Sandbox.create() as sbx:
        chunks: list[bytes] = []
        pty = sbx.pty.create(PtySize(rows=24, cols=80), on_data=chunks.append)  # (1)!
        sbx.pty.send_stdin(pty.pid, b"echo hola\n")
        sbx.pty.resize(pty.pid, PtySize(rows=40, cols=120))
        sbx.pty.kill(pty.pid)
    ```

    1. El orden de E2B, `PtySize(rows, cols)`, y sus firmas posicionales;
       devuelve el mismo `PtyHandle` nativo.

## Paso a paso

1. `pty.create()` arranca el shell de login del usuario (`<shell> -i -l`,
   uid 1000) con `TERM=xterm-256color`. Devuelve un `PtyHandle`, que es un
   `CommandHandle` cuyos trozos son bytes de la terminal.
2. `send_input(data)` escribe teclas (texto o bytes; `"\x03"` es Ctrl-C);
   `send_stdin(data)` es un alias con el nombre de E2B.
3. Lee la salida iterando el handle o con un callback `on_data` (TypeScript:
   `onData`), que recibe cada trozo de bytes.
4. `resize(size)` cambia filas y columnas: el programa recibe `SIGWINCH`.
5. `kill()` termina la terminal; `disconnect()` la suelta sin matarla y
   `pty.connect(pid)` vuelve a engancharse más tarde, desde este u otro
   proceso, con `on_data=`, `from_seq=` y `timeout=` (TypeScript: `onData`,
   `fromSeq`, `timeoutMs`).

Sin el handle, los mismos métodos van por `pid` en `sbx.pty`:
`send_input(pid, data)` (o `send_stdin`), `resize(pid, size)` y
`kill(pid)`. El `PtyHandle` tiene además `pid`, `exit_code`, `wait()` y
`close_stdin()` como cualquier `CommandHandle`, y las PTY vivas aparecen en
`commands.list()` con `kind="pty"`.

Para conectar una terminal web, reenvía los bytes de `on_data` al
navegador y lo que teclea el usuario a `send_input`.

## Opciones de `pty.create`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `size` | `size` | 80×24 | `PtySize(cols, rows)` / `{ cols, rows }` |
| `user` | `user` | `"user"` | usuario del shell |
| `cwd` | `cwd` | el `HOME` | directorio inicial |
| `envs` | `envs` | — | variables de entorno del shell |
| `shell` | `shell` | el de login | ruta absoluta de otro shell |
| `on_data` | `onData` | — | callback con cada trozo de bytes |
| `timeout` | `timeoutMs` | 60 s / 60 000 ms | vida máxima de la terminal; `None` / `0` sin límite |
| `secrets` | `secrets` | — | [secretos](../secrets.md) en el entorno del shell (opcional, con coste) |

`PtySize` en el SDK nativo es `PtySize(cols, rows)`; en el shim de E2B
mantiene el orden de E2B, `PtySize(rows, cols)`.

## Reconexión

Como los comandos, una PTY sobrevive a una pausa y a un corte del proxy: el
handle se reengancha solo (`Pty.Connect`) la próxima vez que lo lees, sin
perder salida. Leer una PTY de un sandbox suspendido no lo despierta.

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `TimeoutException` | `TimeoutError` | la terminal llegó a su `timeout` | pasa `timeout=None` para sesiones largas |
| `RateLimitException` | `RateLimitError` | demasiados procesos y PTYs vivos ([Límites](../limits.md)) | cierra terminales que no uses |
| `InvalidArgumentException` | `InvalidArgumentError` | `shell` que no es una ruta absoluta, tamaño inválido | corrige el argumento |

## Diferencias con E2B

- El orden de `PtySize` del SDK nativo es `(cols, rows)`; el shim de E2B
  conserva `(rows, cols)`.
- `send_stdin` de E2B existe también en el SDK nativo como alias de
  `send_input`.

## Ver también

- [Comandos](comandos.md)
- [CLI: `rayito sandbox connect`](../cli.md)
- Referencia: [Python](../referencia/python/subclientes.md) y
  [TypeScript](../referencia/typescript.md#pty)
