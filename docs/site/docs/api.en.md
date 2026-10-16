# Python reference

Generated from the docstrings of the `rayito` package (`pip install rayito`);
the docstrings themselves are written in Spanish.
Everything shown here is imported from `rayito` (or from `rayito.e2b` for
the E2B shim); modules with a leading underscore are internal.

| Page | What it contains |
|---|---|
| [Sandbox](referencia/python/sandbox.md) | `Sandbox` and `AsyncSandbox`: create, connect, run, pause, destroy |
| [Subclients](referencia/python/subclientes.md) | `sbx.commands`, `sbx.files`, `sbx.pty`, `sbx.git`, `sbx.agent` (with `AgentSpec`, its events and the model gateways), their handles and the listing paginator |
| [Pool](referencia/python/pool.md) | `SandboxPool`, `AsyncSandboxPool`, `PoolConfig` and the backends |
| [Models](referencia/python/modelos.md) | the data types: `SandboxInfo`, `Execution`, `Result`, `EntryInfo`, `S3Staging`, the charts, `ClientSettings`, `TransportSettings`... |
| [Optional](referencia/python/opcionales.md) | the features that cost money, off by default: secrets, index, S3 mounts, sizes, events, OTLP, secret gateway, templates, optional stacks, EFS volumes and custom domain (the last two experimental) |
| [Exceptions](referencia/python/excepciones.md) | the error hierarchy |
| [E2B shim](referencia/python/shim-e2b.md) | `rayito.e2b`: the E2B 2.x SDK on top of Rayito |

`rayito.__version__` returns the version of the installed package.

Conventions:

- Times are in **seconds** (`timeout=60`); in TypeScript, in
  milliseconds (`timeoutMs: 60_000`).
- `Sandbox` and `AsyncSandbox` have the same surface; in `AsyncSandbox`
  every method is a coroutine.
- Several methods work both as an instance method and as a class method
  taking the sandbox id (`sbx.kill()` and `Sandbox.kill(sandbox_id)`).

TypeScript equivalents: [TypeScript reference](referencia/typescript.md).
