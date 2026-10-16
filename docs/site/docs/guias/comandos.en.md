# Commands

`sbx.commands` runs processes inside the sandbox with `/bin/bash -l -c`
(a login shell), as the `user` user (uid 1000): in the foreground (you wait
for the result) or in the background (you get a handle and move on).

## When to use it { #cuando-usarlo }

- Install packages, run scripts, compile or launch tests.
- Start a server in the background and reach it with
  [`get_host()`](puertos-y-host.md).
- **When not to**: for Python code with state between calls, use
  [`run_code`](ejecutar-codigo.md); for an interactive session, a
  [terminal (PTY)](terminal-pty.md).

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import CommandExitException, Sandbox

    with Sandbox.create() as sbx:
        result = sbx.commands.run("echo hello")
        print(result.stdout, result.exit_code)  # "hello\n" 0

        server = sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
        print(server.pid)
        server.kill()

        try:
            sbx.commands.run("exit 3")
        except CommandExitException as error:  # (1)!
            print(error.exit_code)  # 3
    ```

    1. A non-zero exit code is an exception, as in E2B.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, CommandExitException


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            result = await sbx.commands.run("echo hello")
            print(result.stdout, result.exit_code)  # "hello\n" 0

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
    const result = await sbx.commands.run("echo hello");
    console.log(result.stdout, result.exitCode); // "hello\n" 0

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

    1. `0` means "no limit" in TypeScript; in Python, `timeout=None`.

=== "CLI"

    ```bash
    rayito sandbox exec microvm-<id> --token-file ~/.rayito/demo.token -- python3 -c 'print(42)'
    rayito sandbox exec microvm-<id> --background --token-file ~/.rayito/demo.token -- sleep 600
    ```

## Step by step { #paso-a-paso }

### Foreground { #primer-plano }

`commands.run(cmd)` waits for the process to end and returns a
`CommandResult` with `stdout`, `stderr`, `exit_code` and `truncated`. The
`timeout` (60 s by default) is enforced by the agent inside the sandbox:
when it expires it sends `SIGTERM` to the process group, `SIGKILL` 5 s
later, and raises `TimeoutException`. A pause does not consume that
deadline.

Whatever the command takes out of its group (`setsid`, `nohup ... &` with a
double fork, a server that daemonizes itself) does not receive those
signals. With `kill_tree=True` (TypeScript: `killTree: true`) it does: the
agent makes the process a *child subreaper* of its descendants, so they
stay hanging from it even if their parent exits; the timeout's `SIGTERM`
reaches the whole tree and, after the 5 s grace period, the agent freezes
(`SIGSTOP`) and kills (`SIGKILL`) whatever is left. The end of the command
does not arrive until none is left (if they all exit earlier, the grace
period ends earlier). `kill()` does the same without the grace period: it
freezes the tree, kills it and then kills the group. What a command leaves
alive when it ends by itself is not touched, with or without `kill_tree`;
a process outside the tree is never signaled.

To see the output as it arrives, pass callbacks:

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

### Background, `stdin` and reconnection { #segundo-plano-stdin-y-reconexion }

With `background=True` you receive a `CommandHandle` as soon as the process
starts. With it you can wait (`wait()`), kill (`kill()`), write to its
input (`send_stdin()`, with `stdin=True` when launching it) or let go of it
(`disconnect()`): the process stays alive and another process can reattach
with `commands.connect(pid)`.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        handle = sbx.commands.run("cat", background=True, stdin=True)
        handle.send_stdin("hello\n")
        handle.close_stdin()  # (1)!
        print(handle.wait().stdout)  # "hello\n"

        worker = sbx.commands.run("sleep 30; echo done", background=True)
        worker.disconnect()  # (2)!
        again = sbx.commands.connect(worker.pid)
        print(again.wait().stdout)  # "done\n"
        print([p.pid for p in sbx.commands.list()])  # the live processes
    ```

    1. Closes standard input: `cat` ends.
    2. Lets go of the stream; the process keeps running inside the sandbox.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            handle = await sbx.commands.run("cat", background=True, stdin=True)
            await handle.send_stdin("hello\n")
            await handle.close_stdin()
            print((await handle.wait()).stdout)  # "hello\n"

            worker = await sbx.commands.run("sleep 30; echo done", background=True)
            worker.disconnect()
            again = await sbx.commands.connect(worker.pid)
            print((await again.wait()).stdout)  # "done\n"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const handle = await sbx.commands.run("cat", { background: true, stdin: true });
    await handle.sendStdin("hello\n");
    await handle.closeStdin();
    console.log((await handle.wait()).stdout); // "hello\n"

    const worker = await sbx.commands.run("sleep 30; echo done", { background: true });
    worker.disconnect();
    const again = await sbx.commands.connect(worker.pid);
    console.log((await again.wait()).stdout); // "done\n"
    console.log((await sbx.commands.list()).map((p) => p.pid));
    ```

You can also iterate the handle to process the output in chunks:
`for stdout, stderr, _ in handle:` in Python (`async for` on
`AsyncSandbox`) and `for await (const chunk of handle)` in TypeScript. The
handle also exposes `pid`, `stdout` and `stderr` accumulated so far,
`exit_code` (`None`/`undefined` while it runs), `error`, `reconnects` and
`last_seq` (TypeScript: `exitCode`, `lastSeq`).

`commands.connect(pid)` accepts `from_seq=` (reattach from a specific
output position, `0` is from the start of what is retained),
`on_stdout=`/`on_stderr=`, `timeout=` and `max_output_bytes=` (TypeScript:
`fromSeq`, `onStdout`, `timeoutMs`, `maxOutputBytes`). `handle.wait()` also
accepts the callbacks in Python: `handle.wait(on_stdout=print)`.

### Environment, user and directory { #entorno-usuario-y-directorio }

`envs=`, `user=` and `cwd=` apply to a specific command. The process
environment is built from scratch: it only carries the image's variables,
those of `create(envs=)` and those of the command (plus those of
`secrets=`, if you use it). `tag=` puts a free-form label on the process to
find it later in `commands.list()` (`ProcessInfo.tag`).

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(envs={"APP_ENV": "dev"}) as sbx:
        out = sbx.commands.run("echo $APP_ENV $MODE; pwd", envs={"MODE": "test"}, cwd="/tmp")
        print(out.stdout)  # "dev test\n/tmp\n"
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ envs: { APP_ENV: "dev" } });
    const out = await sbx.commands.run("echo $APP_ENV $MODE; pwd", {
      envs: { MODE: "test" },
      cwd: "/tmp",
    });
    console.log(out.stdout); // "dev test\n/tmp\n"
    ```

`user="root"` only works if the image allows it (`RAYITO_ALLOW_ROOT=1` in
the image); by default everything runs as `user`.

### After a pause or a drop { #despues-de-una-pausa-o-un-corte }

If the sandbox is suspended or the proxy cuts the connection, each handle
reattaches on its own the next time you read it, without losing output
(`Connect(from_seq)`). The output is retained in the agent: up to 64 chunks
of 32 KiB per subscriber; if nobody reads for 30 s and it fills up, the
command ends with `output_truncated`.

## `commands.run` options { #opciones-de-commandsrun }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `cmd` | `cmd` | — | the command, run with `/bin/bash -l -c` |
| `background` | `background` | `False` | returns a `CommandHandle` without waiting |
| `envs` | `envs` | — | environment variables for this process |
| `user` | `user` | `"user"` | user that runs the process |
| `cwd` | `cwd` | the user's `HOME` | working directory |
| `on_stdout`, `on_stderr` | `onStdout`, `onStderr` | — | callbacks with each chunk of output |
| `stdin` | `stdin` | `False` | leaves standard input open for `send_stdin` |
| `timeout` | `timeoutMs` | 60 s / 60,000 ms | server-side limit; `None` / `0` for no limit |
| `request_timeout` | `requestTimeoutMs` | the sandbox's (60 s) | deadline of each call to the agent |
| `tag` | `tag` | — | free-form label that `commands.list()` returns |
| `kill_tree` | `killTree` | `False` | the timeout and `kill()` also reach descendants that left the group (see [Foreground](#primer-plano)) |
| `secrets` | `secrets` | — | [secrets](../secrets.md) as environment variables (optional, with a cost) |
| `max_output_bytes` | `maxOutputBytes` | 64 MiB | cap on the output kept per descriptor; see [Stored output](#salida-guardada) |
| — | `signal` | — | `AbortSignal` that cancels the call |

### Stored output { #salida-guardada }

The SDK keeps in memory the output of each command (that of
`result.stdout` and that of the handle) up to
`max_output_bytes`/`maxOutputBytes` per descriptor: 64 MiB by default
(`COMMAND_OUTPUT_MAX_BYTES`). If a process writes more, it keeps the end,
discards the oldest and flags the result with `truncated` (also
`CommandExitException`/`CommandExitError`). Callbacks always receive
everything. With `0` it stores nothing: useful when you already consume the
output through callbacks. That way a command in the sandbox that writes
nonstop does not exhaust your process's memory; PTYs use the same cap.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        result = sbx.commands.run(
            "cat build.log", on_stdout=print, max_output_bytes=1024 * 1024
        )
        if result.truncated:
            print("only the last MiB is in result.stdout")
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
      console.log("only the last MiB is in result.stdout");
    }
    ```

The other methods: `commands.list()` (a list of `ProcessInfo` with `pid`,
`cmd`, `args`, `envs`, `cwd`, `tag` and `kind`, which includes PTYs),
`commands.kill(pid)` (`SIGKILL` to the group, and before that to the whole
tree if it was launched with `kill_tree`; `False` if the process no longer
exists), `commands.send_stdin(pid, data)`, `commands.close_stdin(pid)` and
`commands.connect(pid)` (TypeScript: `sendStdin`, `closeStdin`).

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `CommandExitException` | `CommandExitError` | the process exited with a non-zero code (`exit_code`, `stdout`, `stderr`) | catch it if the failure is expected |
| `TimeoutException` | `TimeoutError` | the command's `timeout` expired | raise `timeout` or use `background=True` |
| `RateLimitException` | `RateLimitError` | too many live processes and PTYs in the sandbox ([Limits](../limits.md)) | kill processes you no longer use |
| `SandboxException` with `output_truncated` | `SandboxError` | nobody read the output for 30 s and the buffer filled up | consume the handle or redirect to a file |
| `NotFoundException` | `NotFoundError` | `connect(pid)` or `send_stdin(pid)` of a process that no longer exists (`kill(pid)` returns `False`) | — |

Full table: [Errors](../referencia/errores.md).

## Differences from E2B { #diferencias-con-e2b }

- In TypeScript, the shim uses the same native `Commands`. In Python,
  `rayito.e2b` wraps it with the positional signatures of E2B 2.x
  (`run(cmd, background, envs, user, cwd, ...)`) and returns the same
  `CommandResult`/`CommandHandle`; `tag=`, `secrets=` and
  `max_output_bytes=` are only in the native one, `sbx.native.commands`.
- A background command does not wake a suspended sandbox when read: it
  waits for something to resume it.

## See also { #ver-tambien }

- [Terminal (PTY)](terminal-pty.md)
- [Run code](ejecutar-codigo.md)
- [Ports and host](puertos-y-host.md)
- Reference: [Python](../referencia/python/subclientes.md) and
  [TypeScript](../referencia/typescript.md#commands)
