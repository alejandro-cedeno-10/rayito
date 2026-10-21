# Reference

Every parameter, class and command. If you are looking for how to do
something, start with the [Guides](../guias/index.md); this section is for
looking things up.

<div class="grid cards" markdown>

-   :material-language-python:{ .lg .middle } **Python**

    ---

    `Sandbox`, `AsyncSandbox`, subclients (`commands`, `files`, `pty`,
    `git`, `agent`...), `deploy_agent`, `AgentFleet` and `AgentCoreFleet`, pool,
    models, optional features, the CDK constructs (`rayito.cdk`), exceptions and
    the E2B shim, generated from the docstrings.

    [:octicons-arrow-right-24: Python reference](../api.md)

-   :material-language-typescript:{ .lg .middle } **TypeScript**

    ---

    The same classes in `camelCase` and milliseconds, with the
    equivalence table.

    [:octicons-arrow-right-24: TypeScript reference](typescript.md)

-   :material-console:{ .lg .middle } **CLI**

    ---

    `rayito setup`, `doctor`, `image`, `sandbox`, `stack`, `events`,
    `template`, `domain`, `dashboard` and `agent`.

    [:octicons-arrow-right-24: CLI](../cli.md)

-   :material-alert-circle:{ .lg .middle } **Errors**

    ---

    Every exception in Python and TypeScript, when it happens and what to do.

    [:octicons-arrow-right-24: Errors](errores.md)

</div>

All the pages in the section:

| Page | What it contains |
|---|---|
| [Python: overview](../api.md) | what is imported from `rayito` and from `rayito.e2b`, and where to start |
| [Python: Sandbox](python/sandbox.md) | `Sandbox` and `AsyncSandbox`: create, connect, lifecycle, network, persistence |
| [Python: Subclients](python/subclientes.md) | `commands`, `files`, `pty`, `git` and `agent`, with their handles; `deploy_agent`, `AgentFleet` and `AgentCoreFleet` ([deploy and fleets](python/subclientes.md#despliegue-sin-configuracion-y-flotas)) |
| [Python: Pool](python/pool.md) | `SandboxPool`, `AsyncSandboxPool`, `PoolConfig` and the backends |
| [Python: Models](python/modelos.md) | the data types that calls return and accept |
| [Python: Optional](python/opcionales.md) | the classes of the optional features (secrets, mounts, events, templates...) and the [CDK constructs](python/opcionales.md#cdk) of `rayito.cdk` |
| [Python: Exceptions](python/excepciones.md) | the exception hierarchy |
| [Python: E2B shim](python/shim-e2b.md) | `rayito.e2b` |
| [TypeScript](typescript.md) | public classes and methods of the npm package and of `rayito/e2b` |
| [CLI](../cli.md) | every `rayito` command and its options |
| [Errors](errores.md) | every error in Python and TypeScript, when it happens and what to do |
| [Environment variables](variables-de-entorno.md) | `RAYITO_*` and the AWS variables read by the SDK and the CLI |
| [Limits](../limits.md) | platform limits, versioning and SDK <-> `rayd` compatibility |
| [Other languages (gRPC)](otros-lenguajes.md) | talking to `rayd` from a language without an official SDK |
| [Changelog](changelog.md) | the changes in each version |

## Python and TypeScript: how names map to each other { #python-y-typescript-como-se-corresponden-los-nombres }

The two SDKs have the same surface. Three rules turn one name into the
other:

| Rule | Python | TypeScript |
|---|---|---|
| `snake_case` -> `camelCase` | `run_code`, `get_info`, `write_files`, `sandbox_id` | `runCode`, `getInfo`, `writeFiles`, `sandboxId` |
| seconds -> milliseconds, with an `Ms` suffix | `timeout=60`, `request_timeout=30` | `timeoutMs: 60_000`, `requestTimeoutMs: 30_000` |
| named arguments -> one options object | `commands.run("ls", background=True, cwd="/tmp")` | `commands.run("ls", { background: true, cwd: "/tmp" })` |

Exceptions to the milliseconds rule: fields that the AWS API gives in seconds
stay in seconds in both SDKs (`IdlePolicy.max_idle_seconds` /
`idle.maxIdleSeconds`, `expires_in` / `expiresIn` of the S3 URLs).

| Concept | Python | TypeScript |
|---|---|---|
| Release on exit | `with Sandbox.create() as sbx:` | `await using sbx = await Sandbox.create();` |
| No time limit | `timeout=None` | `timeoutMs: 0` |
| No auto-suspend | `idle=None` | `idle: null` |
| Iterate a listing | `for item in Sandbox.list():` | `for await (const item of Sandbox.list())` |
| Exceptions | `...Exception` | `...Error` |
| Cancel a call | `request_timeout=` | `signal: AbortSignal` (in addition to `requestTimeoutMs`) |
