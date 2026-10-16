# Other languages (gRPC)

The official SDKs are Python and TypeScript. `rayd` is a gRPC **server**
inside the MicroVM and the SDK is a client generated from
[`proto/rayito/v1/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/proto/rayito/v1)
plus the lifecycle, the tokens and reconnection. Any language with a gRPC
client and an AWS SDK can use Rayito.

## Compared with other sandboxes { #comparado-con-otros-sandboxes }

| Product | Agent inside the VM | Official client SDKs |
|---|---|---|
| E2B | `envd` (Go) | Python, JS/TS (Go: community forks) |
| Daytona | Go daemon | Python, TypeScript, Ruby, Go, Java + CLI |
| Modal | proprietary | Python; JS and Go via libmodal / modal-client |
| Rayito | `rayd` (Rust) | Python, TypeScript; any other language by generating a gRPC client from `proto/rayito/v1/` |

## What a new client needs { #que-hace-falta-para-un-cliente-nuevo }

1. **Call the `lambda-microvms` control plane** of your account with that
   language's AWS SDK: `run-microvm`, `get-microvm`, `suspend` /
   `resume` / `terminate` and `create-microvm-auth-token` for the proxy
   token (a JWE).
2. **Speak gRPC over HTTPS/2** to `rayd` through the AWS proxy, with
   the four metadata headers in lowercase: `x-aws-proxy-auth` (the
   JWE), `x-aws-proxy-port` (`8080`), `x-aws-proxy-force-h2` (`true`) and
   `x-access-token` (the sandbox secret; `Health` is the only RPC that
   does not require it).
3. **Implement the readiness wait on `Health`** (`agent_ready`,
   `kernel_ready`) and, if you use pause and resume, the reconnection
   contract (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`, `Reattach`;
   see [Streams and reconnection](../concepts.md#streams-y-reconexion)).

The access token is fixed when the sandbox is created: the SDK generates 32
random bytes and sends only their sha256 in the `runHookPayload` of
`run-microvm`.

## `rayd` services { #servicios-de-rayd }

Eight services in the `rayito.v1` package, all on port `8080`:

| Service (`.proto`) | RPCs | What it covers in the SDKs |
|---|---|---|
| `HealthService` (`health.proto`) | `Health`, `Metrics`, `MetricsHistory` | readiness, `get_health()`, `get_metrics()`, `get_metrics_history()` |
| `ProcessService` (`process.proto`) | `Start`, `Connect`, `SendInput`, `CloseStdin`, `SendSignal`, `List` | `sbx.commands` (and `sbx.git` and `sbx.agent`, which run commands) |
| `FilesystemService` (`filesystem.proto`) | `Read`, `Write`, `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir`, `Checkpoint`, `Restore`, `StartImport`, `StartExport`, `GetTransfer`, `WatchTransfer`, `CancelTransfer` | `sbx.files`, persistence and S3 transfers |
| `PtyService` (`pty.proto`) | `Create`, `Connect`, `SendInput`, `Resize`, `Kill` | `sbx.pty` |
| `CodeService` (`code.proto`) | `CreateContext`, `Execute`, `Reattach`, `ListContexts`, `DestroyContext`, `RestartContext` | `run_code()` and the code contexts |
| `LifecycleService` (`lifecycle.proto`) | `SetTimeout` | the server-side deadline (`set_timeout()`, `connect(timeout=)`) |
| `NetworkService` (`network.proto`) | `UpdateNetwork`, `GetNetwork` | the egress policy (`update_network()`, `get_network()`) |
| `ConfigureService` (`configure.proto`) | `Configure`, `ConfigureStatus` | the sections of the optional features: events (`lifecycle_events.proto`), OTLP (`telemetry_export.proto`), secret gateway (`secret_gateway.proto`), S3 mounts (`s3_mounts.proto`) and EFS volumes (`efs_volumes.proto`) |

`common.proto` defines `StreamError` (the error codes inside a stream) and
`features.proto`, the capabilities the agent announces. The mapping of status
and codes to exceptions worth replicating is in
[Errors](errores.md#como-se-elige-la-excepcion).

## Planned clients { #clientes-previstos }

- A **Rust** client is trivial to build (`aws-sdk-lambdamicrovms` exists on
  crates.io and the `tonic` client is already in the workspace) and will be
  published when someone asks for it.
- A **Go** client is not planned: generate your own from the `.proto`.
- Publishing `rayito-proto` on crates.io is deferred.

??? info "Sources and measurements"
    E2B: [`github.com/e2b-dev/infra`](https://github.com/e2b-dev/infra)
    (`envd` in Go; official SDKs only Python and JS/TS). Daytona:
    [daytona.io/docs/en/getting-started](https://www.daytona.io/docs/en/getting-started/)
    (Python, TypeScript, Ruby, Go, Java and CLI). Modal:
    [modal.com/docs/guide/sdk-javascript-go](https://modal.com/docs/guide/sdk-javascript-go)
    (JS and Go via libmodal / modal-client).
