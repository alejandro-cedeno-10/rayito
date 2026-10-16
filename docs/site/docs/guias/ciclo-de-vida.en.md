# Lifecycle

Create, connect, inspect, list and destroy sandboxes. Each sandbox is a
Lambda MicroVM in your account, with a maximum lifetime of 8 hours.

## When to use it { #cuando-usarlo }

- Always: it is the starting point for everything else.
- `connect()` when another process (a worker, a retry, another machine)
  has to keep working on the same sandbox.
- Class-level `list()` and `kill()` to clean up sandboxes nobody is using.

## Quick example { #ejemplo-rapido }

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

    1. When the `with` exits the sandbox is already dead: this loop finds
       nothing. It is the pattern for cleaning up orphaned sandboxes.

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
    rayito sandbox list                               # the live ones, with their age
    rayito sandbox info microvm-<id>                  # state, image and metadata
    rayito sandbox kill microvm-<id>                  # or: rayito sandbox kill --all
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

    1. In the shim, `timeout` is E2B's logical deadline (300 s by default),
       not the platform lifetime: see [Server-side deadline](../lifecycle.md#en-el-shim-de-e2b).
    2. E2B has no such token; store it if another process is going to use
       `Sandbox.connect(sandbox_id, access_token=...)`.
    3. An E2B `SandboxPaginator` (`next_items()`, `has_next`,
       `next_token`).

## Step by step { #paso-a-paso }

### Create { #crear }

`Sandbox.create()` launches a MicroVM (`run-microvm`) from an image and
waits for the agent and the kernel to be ready. The image comes, in order,
from the first argument (`template`), from `RAYITO_TEMPLATE`, or it fails.
It can be a name (`rayito-base`, resolved to the ARN in your account) or a
full ARN.

The sandbox size (CPU and memory) is a property of the image: memory is
fixed when it is published. `size="4gb"` (TypeScript: `size: "4gb"`)
picks, without calling AWS, the already-published image of that size
(`rayito-base-4gb`); see [Sizes](../funciones-opcionales/tamanos.md) and
[Limits: size](../limits.md#tamano-cpuram).

### Destroy { #destruir }

`kill()` terminates the MicroVM (`terminate-microvm`) and returns `True` if
it found it. `with` (Python) and `await using` (TypeScript) call it on
exit; without them, call `kill()` yourself in a `finally`. The class form,
`Sandbox.kill(sandbox_id)`, kills a sandbox for which you only have the id
(it does not need the access token). `close()` is something else: it
releases the handle's local resources (channels, token renewal thread) and
leaves the MicroVM alive.

!!! warning "A forgotten sandbox bills"
    It lives until its `timeout` (3600 s by default, cap 8 h) billing
    compute ([pricing](../cost.md#precios)). With the default
    [auto-suspend](pausar-reanudar.md), a sandbox with no traffic is
    suspended after 300 s and stops billing compute, but it keeps paying
    for its snapshot storage.

### Connect { #conectar }

`Sandbox.connect(sandbox_id, access_token=...)` opens a new handle on an
existing sandbox, from any process with AWS credentials. It needs the
sandbox's *access token* (`sbx.access_token`, or the
`RAYITO_ACCESS_TOKEN` variable), which is generated in `create()` and
cannot be recovered later: store it next to the id. If the sandbox was
suspended, `connect()` resumes it; with `timeout=` it also extends its
[logical deadline](../lifecycle.md#set_timeout-y-connecttimeout).

On a handle you already have, `sbx.connect()` (TypeScript:
`await sbx.connect()`) reopens it the same way: it resumes if needed,
waits for the agent and returns the same object. It is the way to continue
after a `pause()` ([Pause and resume](pausar-reanudar.md)).

=== "Python"

    ```python
    from rayito import Sandbox

    sbx = Sandbox.create()
    sandbox_id, token = sbx.sandbox_id, sbx.access_token  # store them together
    sbx.close()  # (1)!

    again = Sandbox.connect(sandbox_id, access_token=token)
    try:
        print(again.commands.run("echo still alive").stdout)
    finally:
        again.kill()
    ```

    1. Releases the local handle; the sandbox keeps running.

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
            print((await again.commands.run("echo still alive")).stdout)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create();
    const { sandboxId, accessToken } = sbx; // store them together
    sbx.close(); // releases the local handle; the sandbox keeps running

    await using again = await Sandbox.connect(sandboxId, { accessToken });
    console.log((await again.commands.run("echo still alive")).stdout);
    ```

### Inspect { #inspeccionar }

`get_info()` returns a `SandboxInfo`: `sandbox_id`, `state`
(`RUNNING`, `SUSPENDED`…), `endpoint`, `template`, `template_version`,
`started_at`, `metadata`, `lifecycle`, `agent_version`, `cpu_count` and
`memory_mb` (plus `size` if you used `size=`). `expires_at` is the current
deadline, `platform_expires_at` the platform cap and `remaining_seconds()`
what is left ([Server-side deadline](../lifecycle.md)).
`Sandbox.get_info(sandbox_id)` does the same without a handle. `sbx.info`
is the last known `SandboxInfo`, without calling AWS. `is_running()` tells
you whether the agent answers, and `get_health()` returns the agent's
`SandboxHealth` (`resume_generation`, `kernel_state_lost`, `lifecycle`…).

### Metadata { #metadatos }

`metadata={"key": "value"}` tags the sandbox when you create it. It is
immutable, travels with the launch and is **not secret**: anyone who can
mint a proxy token for the sandbox reads it. It is useful for filtering
the listing.

### List { #listar }

`Sandbox.list()` walks `list-microvms` lazily and returns
`SandboxListItem` (`sandbox_id`, `state`, `template`, `template_version`,
`started_at`, `metadata`). In synchronous Python it is an iterator; in
`AsyncSandbox.list()` a coroutine that returns the list, and in TypeScript
an `AsyncIterable`. Filters: `template=`, `template_version=`, `states=`,
`metadata=`, `started_after=` and `order=` (TypeScript: `templateVersion`,
`startedAfter`). For a cursor you can store and resume,
`Sandbox.paginate()`
([Metrics and listing](../observability.md#listado-reanudable)).

!!! note "`list(metadata=)` is O(n)"
    Without an index, filtering by metadata probes every `RUNNING` sandbox
    (≈ 0.5-1 s per sandbox) and never sees the suspended ones. For large
    fleets or paused sandboxes, the
    [metadata index](../funciones-opcionales/indice-de-metadatos.md)
    (optional, with a cost) solves it with a DynamoDB query.

## `create()` options { #opciones-de-create }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `template` (1st argument) | `template` | `RAYITO_TEMPLATE` | image name or ARN |
| `template_version` | `templateVersion` | the latest active | specific image version |
| `timeout` | `timeoutMs` | 3600 s / 3,600,000 ms | maximum lifetime (running + suspended), cap 28,800 s; with `max_lifetime` or `on_timeout` it is the [server-side deadline](../lifecycle.md) |
| `max_lifetime`, `on_timeout` | `maxLifetimeMs`, `onTimeout` | — | [server-side deadline](../lifecycle.md) |
| `idle` | `idle` | `IdlePolicy(max_idle_seconds=300)` | auto-suspend on inactivity; `None` / `null` turns it off, and with no `idle` it turns itself off if it does not fit before the `timeout` ([Pause and resume](pausar-reanudar.md)) |
| `envs` | `envs` | — | environment variables for all processes (not secret: they travel in the launch) |
| `metadata` | `metadata` | — | immutable, non-secret tags |
| `cpu_time_limit` | `cpuTimeLimit` | — | CPU seconds per process (`RLIMIT_CPU`) |
| `execution_role_arn` | `executionRoleArn` | — | IAM role inside the sandbox (IMDSv2); without it there are no credentials inside |
| `logging` | `logging` | `"disabled"` | `"cloudwatch"` sends the runtime logs (needs `execution_role_arn`) |
| `allowed_ports` | `allowedPorts` | only 8080 | extra ports on the proxy's main token ([Ports and host](puertos-y-host.md)) |
| `ingress`, `egress` | `ingress`, `egress` | managed connectors | Lambda MicroVMs network connectors |
| `network`, `allow_internet_access` | `network`, `allowInternetAccess` | unrestricted | [Outbound network](../network.md) (only `rayito-base-caps`) |
| `transfer` | `transfer` | `RAYITO_TRANSFER_BUCKET` | bucket for large files and URLs ([Files and S3](../files.md)) |
| `persist`, `persist_timeout` | `persist`, `persistTimeoutMs` | —, 600 s | `HOME` on S3 that survives `kill()` and the 8 h mark ([Persistence](../persistence.md)) |
| `pool` | `pool` | — | take from a [pool](../pool.md) |
| `access_token` | `accessToken` | 32 random bytes | sets the sandbox secret |
| `region`, `session` | `region` | the session's | AWS region and credentials |
| `ready_timeout`, `request_timeout`, `reconnect_timeout` | `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | 90 / 60 / 60 s | client deadlines |
| `keep_on_failure` | `keepOnFailure` | `False` | does not terminate the MicroVM if startup fails (for debugging) |
| `logger` | `logger` | `rayito` logger / silent | where the SDK logs (only ids, states and durations) |
| `control_plane`, `transport` | `controlPlane`, `transport` | real AWS | injectable adapters (tests, [local environment](probar-en-local.md)) |
| — | `signal` | — | `AbortSignal` that cancels creation |
| `secrets`, `secret_cache` | `secrets`, `secretCache` | off | [Secrets](../secrets.md) (optional, with a cost) |
| `index` | `index` | off | [Metadata index](../funciones-opcionales/indice-de-metadatos.md) (optional, with a cost) |
| `tracer_provider` | `tracerProvider` | off | [OpenTelemetry](../funciones-opcionales/opentelemetry.md) |
| `size` | `size` | the usual image | [Sizes](../funciones-opcionales/tamanos.md) (optional) |
| `mounts` | `mounts` | off | [S3 mounts](../funciones-opcionales/montajes-s3.md) (optional, with a cost) |
| `volumes` | `volumes` | off | [EFS volumes](../funciones-opcionales/volumenes-efs.md) (**experimental**, optional, with a cost) |
| `events` | `events` | off | [Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md) (optional, with a cost) |
| `telemetry` | `telemetry` | off | [OTLP export](../funciones-opcionales/exportacion-otlp.md) (optional, with a cost) |
| `gateways` | `gateways` | off | [Secrets gateway](../funciones-opcionales/pasarela-de-secretos.md) (optional, with a cost) |
| `domain` | `domain` | — | reserved for the [custom domain](../funciones-opcionales/dominio-propio.md) (**experimental**): today it raises `UnimplementedError` |

All options with a cost are off by default: see
[Optional features](../optional-features.md).

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | no image (`RAYITO_TEMPLATE`), `envs` + `metadata` above the [payload limit](../limits.md) | fix the argument; AWS was not called |
| `SandboxLifetimeException` | `SandboxLifetimeError` | `timeout` or `max_lifetime` above 28,800 s | lower the deadline; for more, [Persistence](../persistence.md) |
| `SandboxNotFoundException` | `SandboxNotFoundError` | `connect()` or `get_info()` of an id that does not exist or has already ended | create a new one |
| `AuthenticationException` | `AuthenticationError` | wrong access token or expired AWS credentials | check the stored token; `aws sso login` |
| `SandboxNotReadyException` | `SandboxNotReadyError` | the agent was not ready within `ready_timeout` | `rayito doctor`; try `keep_on_failure=True` and look at the logs |
| `QuotaExceededException` | `QuotaExceededError` | the region's memory or MicroVM quota is exhausted | kill sandboxes or request a quota increase |
| `CapacityException` | `CapacityError` | AWS has no capacity for the moment in the region | retry with backoff |
| `UnimplementedError` | `UnimplementedError` | the image is too old for what was requested | publish an image from the current release |

Full table: [Errors](../referencia/errores.md).

## Differences from E2B { #diferencias-con-e2b }

- The maximum lifetime is 8 h, counting suspended time. E2B keeps paused
  sandboxes without a limit.
- `connect()` and the class forms need the sandbox's access token: there
  is no API key that replaces it.
- There is no per-sandbox `cpu_count`/`memory_mb`: the image fixes the
  size (`size=` chooses among already-published images of each size).

## See also { #ver-tambien }

- [Pause and resume](pausar-reanudar.md)
- [Server-side deadline](../lifecycle.md)
- [`Sandbox` reference (Python)](../referencia/python/sandbox.md) and
  [TypeScript](../referencia/typescript.md#sandbox)
- [Costs](../cost.md)
