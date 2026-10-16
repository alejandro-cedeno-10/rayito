# Concepts

## What runs where { #que-corre-donde }

A sandbox is two worlds: your process, where the SDK lives, and a Lambda
MicroVM in your account, where everything else lives. Inside the MicroVM
these processes run, as this user and talking over these transports:

```
AWS Lambda MicroVM (Firecracker, ARM64)
│
├─ rayd (Rust, static musl, root)
│    :8080  gRPC h2c  ── called by the SDK through the AWS proxy
│    :9000  HTTP/1.1  ── called only by Lambda (hooks /ready /run /suspend /resume /terminate)
│    │
│    └─ child: kernel-sidecar (Python 3.12, uid 1000)
│         stdin/stdout JSON lines  ◄──►  rayd
│         │
│         └─ children: ipykernel × context (Python, uid 1000)
│              ZMQ ipc:// under /run/rayito/k/<context>/
│              ►► user code runs here (run_code)
│
├─ processes and PTYs from commands.run / pty.create (uid 1000, direct children of rayd)
│    also the sbx.agent agent (OpenCode or deepagents)
│
└─ secret gateway (inside rayd, only with gateways=)
     HTTP on 127.0.0.1:<port>  ── adds the credential and forwards to the upstream
```

| Piece | Language | Where it runs | Who uses it |
|---|---|---|---|
| `rayd` | Rust | inside the MicroVM, as root, one process per VM | the SDK (gRPC :8080) and Lambda (hooks :9000) |
| kernel-sidecar | Python 3.12 | inside the MicroVM, child of `rayd`, uid 1000 | only `rayd` (JSON lines over stdio) |
| ipykernel (one per context) | Python | inside the MicroVM, child of the sidecar, uid 1000; **user code runs here** | the sidecar (`jupyter_client`, ZMQ `ipc://`) |
| processes and PTYs (`commands`, `pty`) | whatever the user launches | inside the MicroVM, children of `rayd`, uid 1000 | the SDK via `ProcessService` / `PtyService` |
| `sbx.agent` agent (OpenCode or deepagents) | the `rayito-agent` image's runtime | inside the MicroVM, a `commands` process, uid 1000 | the SDK, which launches it and reads its events |
| Python SDK (`rayito`) | Python ≥ 3.11 | **outside** the MicroVM, in the client process (your app, your agent, your CI) | your code |
| TypeScript SDK (`rayito`) | TypeScript / Node ≥ 20 | **outside**, in the client process | your code |
| control-plane calls | boto3 / AWS SDK JS v3 inside the SDK | **outside**, from the client process to the `lambda-microvms` API | the SDK (`create`, `list`, `pause`, `resume`, `kill`, tokens) |

`rayd` is the MicroVM's PID 1, so it also inherits processes that are left
orphaned: a daemon that double-forks ends up hanging off `rayd`. Since
`rayd` 0.6.1, `rayd` reaps those orphans when they exit (on every `SIGCHLD`
and every 5 s), never touching its own children, whose exit codes keep
reaching `commands`, `pty` and the kernels. Every child of its own is
spawned through a single process registry that records it before the
reaping can see it.

The sidecar is Python by written decision: the kernel is Python anyway and
`jupyter_client` is the proven client of the Jupyter protocol, so what takes
effort (formatters, plots, warm-up, supervision, reseed) stays in the same
language. The stdio hop is negligible next to running a cell and the
`.proto` does not depend on the backend, so the decision is reversible
without touching clients. Consolidating it in Rust with `jupyter-zmq-client`
remains an evaluated option, not a goal.

If this page and `ARCHITECTURE.md` differ, `ARCHITECTURE.md` wins.

## The agent inside the sandbox { #el-agente-dentro-del-sandbox }

`sbx.agent.run()` does not run any model in your process: it launches the
agent runtime (OpenCode by default) as one more process of the sandbox, with
the same filesystem, the same commands and the same egress as any other
code, and reads its events. The agent calls its model through the
[secret gateway](funciones-opcionales/pasarela-de-secretos.md), which `rayd`
serves on loopback: it adds the credential to each request without the
sandbox code being able to read it. The usual way is one `Sandbox.create()`
and `sbx.agent.run(...)`; a warmed pool only shortens the start
([Which one do I use?](guias/agente-en-el-sandbox.md#que-uso)), and
`persist=` keeps the files if the conversation stops for more than 8 h.

<a id="desde-que-lenguajes-se-usa-rayito"></a>

## From other languages { #desde-otros-lenguajes }

The official SDKs are Python and TypeScript. `rayd` is a gRPC server
generated from `proto/rayito/v1/`, so any language with a gRPC client can
talk to it: what is needed, in
[Other languages (gRPC)](referencia/otros-lenguajes.md).

## Deadline, cap and idle { #plazo-tope-e-idle }

Three different clocks govern a sandbox's life:

| | Who enforces it | Can it move? | What happens when it expires |
|---|---|---|---|
| **Logical deadline** (`timeout`) | `rayd`, inside the VM, even if your process dies | yes: `set_timeout()` (exact, can shorten it) and `connect(timeout=)` (only extends) | `on_timeout='kill'`: `rayd` exits and the VM goes to `TERMINATED` ≈ 15 s later; `on_timeout='pause'`: it is suspended |
| **Cap** (`max_lifetime` = `maximumDurationInSeconds`) | the platform | no: there is no `UpdateMicrovm` | the VM terminates, whether running or suspended |
| **Idle** (`idle=IdlePolicy(...)`) | the platform | no | suspends after `max_idle_seconds` without traffic through the endpoint |

- With `max_lifetime` or `on_timeout` in `create()` (requires a 0.3.0 or
  later image), `timeout` is the logical deadline and `max_lifetime`
  (120–28,800 s, `timeout + 60` by default) the cap; the deadline never goes
  past `max_lifetime − 60 s` from the start. `get_info().expires_at` is the
  logical deadline.
- Without either, `timeout` is the maximum lifetime (running + suspended,
  capped at 8 h) and does not move.
- The cap **counts suspended time**: a paused sandbox still dies 8 h after
  its start. In `pause` mode, processes keep running between the deadline
  and the suspension (≈ 3 s with a live client, up to `max_idle` without
  one), and the idle policy may suspend before the deadline. A real
  suspension of less than 2 s that crosses the deadline is not recognized as
  such.
- How to use it, in Python (sync and async) and TypeScript:
  [Server-side deadline](lifecycle.md).
- To go on past `max_lifetime` with the same files, `sbx.reincarnate()`
  saves `HOME` to S3 and restores it in a new sandbox
  ([Persistence](persistence.md)); kernel variables, processes and PTYs do
  not survive.

The idle policy (`create(idle=IdlePolicy(...))`, 300 s by default) suspends
the MicroVM when it receives no traffic and resumes it on the next call
(`auto_resume=True`). A suspend/resume cycle costs ≈ $0.0049 at 2 GB, the
same as ≈ 140 s of compute, so a `max_idle_seconds` below ≈ 150 s never
saves money ([Pricing](cost.md#componentes-del-precio)). `idle=None` turns
auto-suspend off, and without `idle` the default one turns itself off when
it does not fit before the `timeout`. Guide: [Pause and
resume](guias/pausar-reanudar.md).

`pause()` and `resume()` do the same by hand: processes, PTYs, watches and
kernel variables stay alive on the other side of the snapshot.

## Two tokens { #dos-tokens }

| Token | Who issues it | What for | Lifetime |
|---|---|---|---|
| Proxy JWE (`x-aws-proxy-auth`) | `create-microvm-auth-token` (AWS) | getting through the AWS proxy to a MicroVM port | 60 min; the SDK renews it at 45 |
| Access token (`x-access-token`) | the SDK, in `create()` | authenticating each RPC to `rayd`; only its sha256 travels in `runHookPayload` | the sandbox's lifetime |

`Health` is the only RPC without `x-access-token`: it is the readiness probe
and the one that reads the metadata. Save `sbx.access_token` next to the
`sandbox_id` so you can `connect()` later. `RAYITO_ACCESS_TOKEN` exists, but
both calls read it: if you export it, every `create()` in that process
reuses the same secret and a leak opens all its sandboxes, not one (by
default `create()` generates 32 fresh bytes per sandbox). Use it for
`connect()` from another process; to fix the secret of one specific
sandbox, pass it with `access_token=` / `accessToken`.

## Two channels per sandbox { #dos-canales-por-sandbox }

A 1 vCPU MicroVM allows 8 concurrent connections through the proxy. The SDK
uses at most two HTTP/2 channels: one for unary calls and foreground
commands and another, opened lazily, for long streams (background commands,
PTYs, `watch_dir`). Rotating the JWE does not rebuild channels or cut
streams.

## Streams and reconnection { #streams-y-reconexion }

Commands, PTYs, `watch_dir` and `run_code` are server streams. When a
`pause()`, the auto-resume, a proxy 502 or a proxy `RST_STREAM` before the
real deadline (`CANCELLED "Stream removed"` in grpcio, `Canceled` with
`http/2 stream closed` in Connect-ES) cuts them, each handle reattaches by
itself the next time it is read (`Connect(from_seq)`, `Pty.Connect`,
`WatchDir` again, `Reattach` for a cell in progress), and a cut unary call
is retried once. A handle that the client itself cancels (`disconnect()`, an
`AbortSignal`) never reconnects, even if the cut arrived right afterwards
shaped like a reset. `reconnect_timeout` (60 s) bounds how long the SDK
waits for the agent once the MicroVM is back to `RUNNING`; while it is
suspended, reading a background handle does **not** wake it.

`get_health().resume_generation` counts the accepted `/resume` calls;
`kernel_state_lost` warns if a kernel did not survive the resume.

## Code contexts { #contextos-de-codigo }

`run_code` runs in a Jupyter kernel with state (the `default` context).
`create_code_context(cwd=...)` starts another kernel with its own scope
(at most 8 per sandbox). A kernel error is data (`execution.error`), never
an exception; a timeout ends in `error.name == "ExecutionTimeout"`.

## PTY { #pty }

`sbx.pty.create(size=PtySize(cols, rows))` opens a real terminal with the
user's login shell (uid 1000). The `PtyHandle` is a `CommandHandle` that
delivers raw bytes; `send_input`, `resize` and `kill` are unary.

## Metadata { #metadatos }

`create(metadata={...})` travels in the only per-MicroVM channel
(`runHookPayload`, 4096 characters together with `envs`) and `rayd` returns
it in `Health`. It is immutable, not secret (any principal that can mint a
JWE for the sandbox reads it) and `Sandbox.list(metadata=...)` filters it
client-side by probing each `RUNNING` sandbox: O(n), ≈ 0.5-1 s per sandbox.

## Metrics history { #historial-de-metricas }

`get_metrics()` is a procfs snapshot (two reads of `/proc/stat` 100 ms
apart). `rayd` also samples every 5 s while the sandbox runs and keeps an
8 h ring (5,760 samples) that `get_metrics_history(start=, end=,
max_points=)` returns in ascending order: each sample's `cpu_used_pct` is
the average since the previous one, and `max_points` downsamples the series
into buckets (the last sample of each bucket with the CPU averaged). While
the sandbox is suspended nothing is sampled: the series has a **gap**.
Sampling does not touch the network, so it does not count as activity for
the idle policy. `cpu_count` and `memory_total_bytes` (in `get_health()`)
and `memory_mb` are the **guest's view**, which may not match the image
size (measured: 8016 MiB and 4 CPUs with a 2048 MiB image). The class form
(`Sandbox.get_metrics_history(sandbox_id, access_token=...)`) needs the
access token and never wakes a suspended sandbox.

Examples in Python and TypeScript: [Metrics and listing](observability.md).

## Listing { #listado }

`Sandbox.list()` lazily paginates `list-microvms` (pages of 50).
`Sandbox.paginate(limit=, next_token=, order=, started_after=, states=,
metadata=)` gives a resumable `SandboxListPaginator`: `next_items()`,
`has_next` and `next_token`, an **opaque** cursor (it carries AWS's
`nextToken` and the filter, and is never interpreted) that you can store and
pass to another process.

- `order="asc" | "desc"` by `startedAt` is computed client-side: AWS does
  not sort, so the first item arrives after walking **all** the pages
  (O(pages)). A token resumed with an order skips already-delivered items by
  identity.
- `states`, `started_after` and `template` filter; `template` goes to the
  server.
- `metadata=` is O(n): one `Health` probe per `RUNNING` sandbox
  (see [Metadata](#metadatos)).

??? info "Sources and measurements"
    - Architecture and decisions (Python sidecar, server-side deadline,
      immutable lifetime):
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md),
      ADR-002, ADR-007 and ADR-011. If this page and `ARCHITECTURE.md`
      differ, `ARCHITECTURE.md` wins.
    - Guest view versus image size: row Q68 of
      [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Cost of the suspend/resume cycle: [Pricing](cost.md#componentes-del-precio).
