# Terminal (PTY)

`sbx.pty` opens a real terminal inside the sandbox: an interactive login
shell with its size, its colors and its job control, to which you send keys
and from which you receive bytes.

## When to use it { #cuando-usarlo }

- Interactive programs that need a terminal (`vim`, `htop`, a REPL, an
  installer that asks questions).
- Give a person a console inside the sandbox (for example, with `xterm.js`
  in the browser).
- **When not to**: to run a command and read its output, use
  [commands](comandos.md); it is simpler and keeps `stdout` apart from
  `stderr`.

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import PtySize, Sandbox

    with Sandbox.create() as sbx:
        pty = sbx.pty.create(size=PtySize(cols=120, rows=40), timeout=None)
        pty.send_input("echo hello\n")
        for _, _, data in pty:  # (1)!
            if data and b"hello\r\n" in data:
                break
        pty.resize(PtySize(cols=80, rows=24))
        pty.kill()
    ```

    1. Each chunk arrives as `(None, None, bytes)`: the terminal's raw
       output, with escape sequences.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, PtySize


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            pty = await sbx.pty.create(size=PtySize(cols=120, rows=40), timeout=None)
            await pty.send_input("echo hello\n")
            async for _, _, data in pty:
                if data and b"hello\r\n" in data:
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
    await pty.sendInput("echo hello\n");
    for await (const { pty: data } of pty) {
      if (data !== undefined && new TextDecoder().decode(data).includes("hello\r\n")) break;
    }
    await pty.resize({ cols: 80, rows: 24 });
    await pty.kill();
    ```

=== "CLI"

    ```bash
    rayito sandbox connect microvm-<id> --token-file ~/.rayito/demo.token   # Ctrl-D to exit
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import PtySize, Sandbox

    with Sandbox.create() as sbx:
        chunks: list[bytes] = []
        pty = sbx.pty.create(PtySize(rows=24, cols=80), on_data=chunks.append)  # (1)!
        sbx.pty.send_stdin(pty.pid, b"echo hello\n")
        sbx.pty.resize(pty.pid, PtySize(rows=40, cols=120))
        sbx.pty.kill(pty.pid)
    ```

    1. E2B's order, `PtySize(rows, cols)`, and its positional signatures; it
       returns the same native `PtyHandle`.

## Step by step { #paso-a-paso }

1. `pty.create()` starts the user's login shell (`<shell> -i -l`, uid 1000)
   with `TERM=xterm-256color`. It returns a `PtyHandle`, which is a
   `CommandHandle` whose chunks are terminal bytes.
2. `send_input(data)` writes keys (text or bytes; `"\x03"` is Ctrl-C);
   `send_stdin(data)` is an alias with E2B's name.
3. Read the output by iterating the handle or with an `on_data` callback
   (TypeScript: `onData`), which receives each chunk of bytes.
4. `resize(size)` changes rows and columns: the program receives
   `SIGWINCH`.
5. `kill()` ends the terminal; `disconnect()` lets go of it without killing
   it and `pty.connect(pid)` reattaches later, from this or another
   process, with `on_data=`, `from_seq=` and `timeout=` (TypeScript:
   `onData`, `fromSeq`, `timeoutMs`).

Without the handle, the same methods go by `pid` on `sbx.pty`:
`send_input(pid, data)` (or `send_stdin`), `resize(pid, size)` and
`kill(pid)`. The `PtyHandle` also has `pid`, `exit_code`, `wait()` and
`close_stdin()` like any `CommandHandle`, and live PTYs show up in
`commands.list()` with `kind="pty"`.

To connect a web terminal, forward the `on_data` bytes to the browser and
whatever the user types to `send_input`.

## `pty.create` options { #opciones-de-ptycreate }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `size` | `size` | 80×24 | `PtySize(cols, rows)` / `{ cols, rows }` |
| `user` | `user` | `"user"` | shell user |
| `cwd` | `cwd` | the `HOME` | initial directory |
| `envs` | `envs` | — | shell environment variables |
| `shell` | `shell` | the login one | absolute path of another shell |
| `on_data` | `onData` | — | callback with each chunk of bytes |
| `timeout` | `timeoutMs` | 60 s / 60,000 ms | maximum life of the terminal; `None` / `0` for no limit |
| `secrets` | `secrets` | — | [secrets](../secrets.md) in the shell environment (optional, with a cost) |

`PtySize` in the native SDK is `PtySize(cols, rows)`; in the E2B shim it
keeps E2B's order, `PtySize(rows, cols)`.

## Reconnection { #reconexion }

Like commands, a PTY survives a pause and a proxy drop: the handle
reattaches on its own (`Pty.Connect`) the next time you read it, without
losing output. Reading a PTY of a suspended sandbox does not wake it.

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `TimeoutException` | `TimeoutError` | the terminal reached its `timeout` | pass `timeout=None` for long sessions |
| `RateLimitException` | `RateLimitError` | too many live processes and PTYs ([Limits](../limits.md)) | close terminals you do not use |
| `InvalidArgumentException` | `InvalidArgumentError` | a `shell` that is not an absolute path, an invalid size | fix the argument |

## Differences from E2B { #diferencias-con-e2b }

- The native SDK's `PtySize` order is `(cols, rows)`; the E2B shim keeps
  `(rows, cols)`.
- E2B's `send_stdin` also exists in the native SDK as an alias of
  `send_input`.

## See also { #ver-tambien }

- [Commands](comandos.md)
- [CLI: `rayito sandbox connect`](../cli.md)
- Reference: [Python](../referencia/python/subclientes.md) and
  [TypeScript](../referencia/typescript.md#pty)
