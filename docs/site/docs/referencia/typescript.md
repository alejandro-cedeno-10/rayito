# Referencia de TypeScript

El paquete npm `rayito` (`pnpm add rayito`) expone la misma superficie que
el SDK de Python, en `camelCase` y con los tiempos en milisegundos. Todo es
asíncrono. Esta página lista las clases y métodos públicos con su
equivalente en Python, y al final el [índice de exports](#exports) con todo
lo que exportan `rayito` y `rayito/e2b`; los tipos completos están en los
`.d.ts` del paquete (tu editor los muestra al pasar el cursor).

```ts
import { Sandbox } from "rayito"; // ESM; también require("rayito") en CommonJS
import { Sandbox as E2BSandbox } from "rayito/e2b"; // el shim de E2B 2.x

await using sbx = await Sandbox.create();
await using e2b = await E2BSandbox.create();
console.log(sbx.sandboxId, e2b.sandboxId);
```

Reglas de nombres: [Python y TypeScript](index.md#python-y-typescript-como-se-corresponden-los-nombres).

## Sandbox { #sandbox }

### Métodos estáticos

| TypeScript | Python | Devuelve |
|---|---|---|
| `Sandbox.create(opts?)` | `Sandbox.create(...)` | `Promise<Sandbox>` |
| `Sandbox.connect(sandboxId, opts?)` | `Sandbox.connect(sandbox_id, ...)` | `Promise<Sandbox>`; necesita `accessToken` (o `RAYITO_ACCESS_TOKEN`) |
| `Sandbox.list(opts?)` | `Sandbox.list(...)` | `AsyncIterable<SandboxListItem>` |
| `Sandbox.paginate(opts?)` | `Sandbox.paginate(...)` | `SandboxListPaginator` (`nextItems()`, `hasNext`, `nextToken`) |
| `Sandbox.kill(sandboxId, opts?)` | `Sandbox.kill(sandbox_id)` | `Promise<boolean>` |
| `Sandbox.getInfo(sandboxId, opts?)` | `Sandbox.get_info(sandbox_id)` | `Promise<SandboxInfo>` |
| `Sandbox.pause(sandboxId, opts?)` / `Sandbox.resume(sandboxId, opts?)` | `Sandbox.pause(sandbox_id)` / `Sandbox.resume(sandbox_id)` | `Promise<boolean>` / `Promise<void>` |
| `Sandbox.setTimeout(sandboxId, ms, opts)` | `Sandbox.set_timeout(sandbox_id, s, access_token=)` | `Promise<void>` |
| `Sandbox.getMetricsHistory(sandboxId, opts)` | `Sandbox.get_metrics_history(sandbox_id, access_token=)` | `Promise<SandboxMetrics[]>` |
| `Sandbox.updateNetwork(sandboxId, policy, opts)` | `Sandbox.update_network(sandbox_id, policy, access_token=)` | `Promise<NetworkState>` |

### Opciones de `Sandbox.create` (`SandboxCreateOptions`)

| Opción | Tipo | Por defecto | Python |
|---|---|---|---|
| `template` | `string` | `RAYITO_TEMPLATE` | `template` |
| `templateVersion` | `string` | la última activa | `template_version` |
| `timeoutMs` | `number` | `3_600_000` (máx. `28_800_000`) | `timeout` (s) |
| `maxLifetimeMs`, `onTimeout` | `number`, `"kill" \| "pause"` | — | `max_lifetime`, `on_timeout` |
| `idle` | `IdlePolicyInput \| null` | `{ maxIdleSeconds: 300, autoResume: true }` | `idle` |
| `envs`, `metadata` | `Record<string, string>` | — | `envs`, `metadata` |
| `cpuTimeLimit` | `number` (s) | — | `cpu_time_limit` |
| `executionRoleArn`, `logging` | `string`, `"disabled" \| "cloudwatch"` | —, `"disabled"` | `execution_role_arn`, `logging` |
| `allowedPorts`, `ingress`, `egress` | arrays | `[8080]`, gestionados | `allowed_ports`, `ingress`, `egress` |
| `network`, `allowInternetAccess` | `NetworkPolicyInput`, `boolean` | sin restricción, `true` | `network`, `allow_internet_access` |
| `transfer` | `S3Staging` | `RAYITO_TRANSFER_BUCKET` | `transfer` |
| `persist`, `persistTimeoutMs` | `S3Prefix`, `number` | —, `600_000` | `persist`, `persist_timeout` |
| `pool` | `SandboxPool` | — | `pool` |
| `accessToken` | `string` | 32 bytes aleatorios | `access_token` |
| `region`, `credentials`, `client`, `controlPlane` | — | `AWS_REGION` (o `AWS_DEFAULT_REGION`) y la cadena del AWS SDK v3 | `region`, `session`, `control_plane` |
| `retries`, `proxy`, `integration` | `number`, `string`, `string` | los del AWS SDK | — (en Python, sólo el shim de E2B, con `ClientSettings`) |
| `transport` | `Partial<TransportSettings>` | TLS al 443 del proxy | `transport` |
| `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | `number` | `90_000`, `60_000`, `60_000` | `ready_timeout`, `request_timeout`, `reconnect_timeout` |
| `keepOnFailure` | `boolean` | `false` | `keep_on_failure` |
| `logger` | `Logger` | — | `logger` |
| `signal` | `AbortSignal` | — | — |
| `secrets`, `secretCache` | opcional, con coste | apagado | `secrets`, `secret_cache` |
| `index` | `DynamoDbIndex` (opcional, con coste) | apagado | `index` |
| `tracerProvider` | `TracerProvider` de OpenTelemetry | apagado | `tracer_provider` |
| `mounts` | `Record<string, S3Mount>` o `Map` | apagado | `mounts` |
| `size` | `"512mb" \| "1gb" \| "2gb" \| "4gb" \| "8gb"` o `{ memoryMib }` | apagado | `size` |
| `events` | `LifecycleEvents` | apagado | `events` |
| `telemetry` | `TelemetryExport` | apagado | `telemetry` |
| `gateways` | `Record<string, SecretGateway>` | apagado | `gateways` |
| `volumes` | `Record<string, EfsVolume>` | apagado ([experimental](../funciones-opcionales/volumenes-efs.md)) | `volumes` |
| `domain` | — | sin cablear: cualquier valor lanza `UnimplementedError` antes de `run-microvm`; para un dominio propio usa `CustomDomain` ([experimental](../funciones-opcionales/dominio-propio.md)) | `domain` |

### Instancia

| TypeScript | Python | Qué hace |
|---|---|---|
| `sbx.sandboxId`, `sbx.accessToken`, `sbx.endpoint`, `sbx.endpointUrl`, `sbx.region` | `sandbox_id`, `access_token`, `endpoint`, `endpoint_url`, `region` | propiedades |
| `sbx.info`, `sbx.launchInfo`, `sbx.resumeGeneration` | `info`, `launch_info`, `resume_generation` | la última `SandboxInfo` conocida, la del lanzamiento y cuántas veces se reanudó |
| `sbx.persist`, `sbx.lastRestore`, `sbx.transfer` | `persist`, `last_restore`, `transfer` | el `S3Prefix` enlazado, el último restore automático y el bucket de transferencias |
| `await sbx.kill()` / `await using` | `kill()` / `with` | termina el MicroVM |
| `sbx.close()` | `close()` | cierra las conexiones sin tocar el MicroVM |
| `sbx.getInfo()`, `sbx.isRunning()`, `sbx.getHealth()` | `get_info()`, `is_running()`, `get_health()` | estado |
| `sbx.connect({ timeoutMs })` | `connect(timeout=)` | reabre el handle y alarga el plazo |
| `sbx.setTimeout(ms)` | `set_timeout(s)` | mueve el [plazo del servidor](../lifecycle.md) |
| `sbx.pause()`, `sbx.resume()` | `pause()`, `resume()` | [Pausar y reanudar](../guias/pausar-reanudar.md) |
| `sbx.getHost(port)` | `get_host(port)` | `HostAccess` con `host`, `url`, `port` y `headers` |
| `sbx.getMetrics()`, `sbx.getMetricsHistory(opts)` | `get_metrics()`, `get_metrics_history(...)` | [Métricas](../observability.md) |
| `sbx.updateNetwork(policy)`, `sbx.getNetwork()` | `update_network(...)`, `get_network()` | [Red saliente](../network.md) |
| `sbx.checkpointFiles()`, `sbx.restoreFiles()`, `sbx.reincarnate()` | `checkpoint_files()`, `restore_files()`, `reincarnate()` | [Persistencia](../persistence.md) |
| `sbx.uploadUrl(path, opts?)`, `sbx.downloadUrl(path, opts?)` | `upload_url(path)`, `download_url(path)` | URL firmada como en E2B (`string`) |
| `await sbx.mounts()` | `mounts` (propiedad; `await sbx.mounts()` en `AsyncSandbox`) | `ReadonlyMap<string, MountStatus>` en vivo ([Montajes S3](../funciones-opcionales/montajes-s3.md)) |
| `await sbx.volumes()` | `volumes` (propiedad; `await sbx.volumes()` en `AsyncSandbox`) | `ReadonlyMap<string, VolumeStatus>` en vivo ([Volúmenes EFS](../funciones-opcionales/volumenes-efs.md), experimental) |
| `sbx.getTelemetryStatus()` | `get_telemetry_status()` | `TelemetryHealth` (`exported`, `dropped`, `lastErrorClass`) ([Exportación OTLP](../funciones-opcionales/exportacion-otlp.md)) |
| `sbx.gateways.get(name)?.url`, `sbx.gateways.refresh()` | `gateways[name].url`, `gateways.refresh()` | URL de loopback y rotación ([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)) |
| `sbx.commands`, `sbx.files`, `sbx.pty`, `sbx.git`, `sbx.agent` | igual | subclientes (abajo) |

## Ejecutar código { #ejecutar-codigo }

| TypeScript | Python | Devuelve |
|---|---|---|
| `sbx.runCode(code, opts?)` | `run_code(code, ...)` | `Promise<Execution>` (`text`, `results`, `logs`, `error`) |
| `sbx.createCodeContext(opts?)` | `create_code_context(...)` | `Promise<CodeContext>` |
| `sbx.listCodeContexts()` | `list_code_contexts()` | `Promise<CodeContext[]>` |
| `sbx.removeCodeContext(ctx)`, `sbx.restartCodeContext(ctx)` | `remove_code_context(ctx)`, `restart_code_context(ctx)` | `Promise<void>` |

`RunCodeOptions`: `language`, `context`, `onStdout`, `onStderr`,
`onResult`, `onError`, `envs`, `timeoutMs` (`300_000`; `0` sin límite),
`requestTimeoutMs`, `signal`, `secrets`. Guía: [Ejecutar código](../guias/ejecutar-codigo.md).

## Comandos (`sbx.commands`) { #commands }

| TypeScript | Python | Devuelve |
|---|---|---|
| `commands.run(cmd, opts?)` | `commands.run(cmd, ...)` | `CommandResult` (`stdout`, `stderr`, `exitCode`) o, con `background: true`, `CommandHandle` |
| `commands.connect(pid, opts?)` | `commands.connect(pid)` | `CommandHandle` |
| `commands.list()` | `commands.list()` | `ProcessInfo[]` |
| `commands.kill(pid)` | `commands.kill(pid)` | `boolean` |
| `commands.sendStdin(pid, data)`, `commands.closeStdin(pid)` | `send_stdin`, `close_stdin` | `void` |

`CommandOptions`: `background`, `envs`, `user`, `cwd`, `onStdout`,
`onStderr`, `stdin`, `timeoutMs` (`60_000`; `0` sin límite), `tag`,
`maxOutputBytes` (64 MiB guardados por descriptor), `requestTimeoutMs`,
`signal`, `secrets`.

`CommandHandle`: `pid`, `stdout`, `stderr`, `exitCode`, `wait()`, `kill()`,
`disconnect()`, `sendStdin(data)`, `closeStdin()` e iteración
`for await (const chunk of handle)` (`{ stdout }`, `{ stderr }`). Guía:
[Comandos](../guias/comandos.md).

## Ficheros (`sbx.files`) { #files }

| TypeScript | Python | Qué hace |
|---|---|---|
| `files.read(path, { format })` | `files.read(path, format=)` | `"text"` (por defecto), `"bytes"`, `"blob"` o `"stream"` |
| `files.write(path, data, opts?)` | `files.write(path, data, ...)` | escritura atómica; `EntryInfo` |
| `files.writeFiles(entries)` | `files.write_files(entries)` | varios ficheros en un stream |
| `files.list(path, { depth })` | `files.list(path, depth=)` | `EntryInfo[]` |
| `files.exists`, `files.getInfo`, `files.remove`, `files.rename`, `files.makeDir` | `exists`, `get_info`, `remove`, `rename`, `make_dir` | como en E2B |
| `files.watchDir(path, opts?)` | `files.watch_dir(path, ...)` | `WatchHandle` (`getNewEvents()`, `stop()`) |
| `files.uploadUrl(path, opts?)` | `files.upload_url(path, ...)` | `UploadTicket` (`url`, `headers`, `wait()`, `status()`, `cancel()`) |
| `files.downloadUrl(path, opts?)` | `files.download_url(path, ...)` | `DownloadLink` (`url`, `size`, `sha256`, `expiresAt`) |

`WriteOptions`: `user`, `gzip`, `metadata`, `useOctetStream`, `mode`,
`requestTimeoutMs`, `signal`. `ReadOptions`: `format`, `user`, `gzip`,
`streamIdleTimeoutMs`, `requestTimeoutMs`, `signal`. Guía:
[Ficheros y S3](../files.md).

## Terminal (`sbx.pty`) { #pty }

| TypeScript | Python | Qué hace |
|---|---|---|
| `pty.create(opts?)` | `pty.create(...)` | `PtyHandle`; opciones `size`, `user`, `cwd`, `envs`, `shell`, `onData`, `timeoutMs`, `secrets` |
| `pty.connect(pid, opts?)` | `pty.connect(pid)` | se reengancha a una terminal viva |
| `handle.sendInput(data)`, `handle.resize(size)`, `handle.kill()` | `send_input`, `resize`, `kill` | teclas, tamaño, fin |

Iterar un `PtyHandle` da `{ pty: Uint8Array }`. Guía:
[Terminal (PTY)](../guias/terminal-pty.md).

## Git (`sbx.git`) { #git }

`clone`, `init`, `remoteAdd`, `remoteGet`, `status`, `branches`,
`createBranch`, `checkoutBranch`, `deleteBranch`, `add`, `commit`, `reset`,
`restore`, `push`, `pull`, `setConfig`, `getConfig`,
`dangerouslyAuthenticate` y `configureUser`, con las mismas opciones que en
Python en `camelCase`. Guía: [Git](../git.md).

## Agente de IA (`sbx.agent`) { #agent }

| TypeScript | Python | Devuelve |
|---|---|---|
| `agent.run(prompt, opts)` | `agent.run(prompt, ...)` | `AgentResult`; lanza `AgentError` si falla |
| `agent.stream(prompt, opts)` | `agent.stream(prompt, ...)` | `AgentStream` (`AsyncIterable<AgentEvent>`); nunca lanza por un fallo del agente |
| `agent.prepare(opts?)` | `agent.prepare(...)` | dispara el calentamiento del runtime en segundo plano (`runtime`, `serve`) |

`AgentRunOptions`: `spec` (obligatoria), `runtime` (`"opencode"` por
defecto, `"deepagents"` o `new DeepAgents({...})`), `sessionId`, `model`,
`limits` (`AgentLimits`: `maxSteps`, `timeoutMs`, `maxOutputBytes`,
`maxTotalTokens`), `workdir` (`/home/user`), `attach` (`"auto"`),
`reasoning` (`false`) y `signal` (un `AbortSignal` aborta el stream).
`AgentStream`: `sessionId`, `droppedLines`, `abort()`, `result()`,
`close()` e iteración `for await (const event of stream)`.

El arranque normal es `Sandbox.create(...)` + `sbx.agent.run(...)`, sin
nada más:

```ts
import { AgentModel, AgentSpec, Sandbox, bedrockGateway } from "rayito";

const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const spec = new AgentSpec({
  model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
});

await using sbx = await Sandbox.create({
  template: "rayito-agent",
  allowInternetAccess: false,
  gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
});
const result = await sbx.agent.run("Resume el README.", { spec });
console.log(result.text);
```

La imagen `rayito-agent` (con el runtime instalado) se construye una vez con
`AgentTemplate` o `rayito agent template build`
([Templates de agente](../funciones-opcionales/templates-de-agente.md)).
El arranque rápido (`prefetch`, `agentPoolWarmup` y un `SandboxPool`
caliente) es opcional: [¿Qué uso?](../guias/agente-en-el-sandbox.md#que-uso).
Guía completa: [Agente en el sandbox](../guias/agente-en-el-sandbox.md).

## Pool { #pool }

| TypeScript | Python |
|---|---|
| `await new SandboxPool({ size, template, ... }).start()` | `SandboxPool(PoolConfig(size=, template=, ...))` + `start()` / `with` |
| `pool.take({ waitMs, secrets, gateways })` | `pool.take(wait=, secrets=, gateways=)` |
| `pool.stats()` | `pool.stats()` |
| `pool.close({ drain })` / `await using pool` | `pool.close(drain=)` / `with` |
| `InMemoryPoolBackend`, `JsonFilePoolBackend` | igual |

En TypeScript, `PoolConfig` son las opciones del constructor. Guía:
[Pool](../pool.md).

## Funciones opcionales { #opcionales }

Apagadas por defecto; cada una carga su *peerDependency* sólo al activarse.

| Clase | Peer opcional | Guía |
|---|---|---|
| `SecretStore`, `SecretCache`, `SecretRef` | `@aws-sdk/client-secrets-manager` | [Secretos](../secrets.md) |
| `DynamoDbIndex` | `@aws-sdk/client-dynamodb` | [Índice de metadatos](../funciones-opcionales/indice-de-metadatos.md) |
| `VolumeStore`, `EfsVolume` (experimental) | `@aws-sdk/client-efs` | [Volúmenes EFS](../funciones-opcionales/volumenes-efs.md) |
| `EfsVolumes` (experimental) | `@aws-sdk/client-ec2` (`check`), `@aws-sdk/client-efs` (borrar el sistema de ficheros) | [Volúmenes EFS en tu VPC](../funciones-opcionales/volumenes-efs-vpc.md) |
| `CustomDomain` (experimental) | `@aws-sdk/client-cloudformation`, `@aws-sdk/client-cloudfront-keyvaluestore` y `@aws-sdk/signature-v4a` | [Dominio propio](../funciones-opcionales/dominio-propio.md) |
| opción `tracerProvider` | `@opentelemetry/api` (sólo tipos) | [OpenTelemetry](../funciones-opcionales/opentelemetry.md) |
| `S3Mount` (opción `mounts`) | ninguno: lo monta `rayd` | [Montajes S3](../funciones-opcionales/montajes-s3.md) |
| opción `size` | ninguno | [Tamaños](../funciones-opcionales/tamanos.md) |
| `LifecycleEvents` (opción `events`) | `@aws-sdk/client-dynamodb`, `@aws-sdk/client-secrets-manager` y `@aws-sdk/client-cloudformation` (localiza la pila) | [Eventos y webhooks](../funciones-opcionales/eventos-y-webhooks.md) |
| `TelemetryExport`, `OtlpAuth` (opción `telemetry`) | `@aws-sdk/client-secrets-manager` sólo con `OtlpAuth.bearer(...)` | [Exportación OTLP](../funciones-opcionales/exportacion-otlp.md) |
| `SecretGateway` (opción `gateways`) | `@aws-sdk/client-secrets-manager` | [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md) |
| `Template` (`build`, `buildInBackground`, `getBuildStatus`, `exists`) | `@aws-sdk/client-cloudwatch-logs` sólo para leer el log del build | [Templates](../funciones-opcionales/templates.md) |
| `AgentTemplate` (`build`) | como `Template` | [Templates de agente](../funciones-opcionales/templates-de-agente.md) |
| `OptionalStacks` (`components`, `status`, `parameterChanges`, `deploy`, `destroy`) | `@aws-sdk/client-cloudformation` | [Pilas opcionales](../funciones-opcionales/pilas-opcionales.md) |

## Errores { #errores }

`SandboxError` y sus subclases, `AuthenticationError`, `QuotaExceededError`,
`CapacityError` y `UnimplementedError`. Tabla completa con su equivalente en
Python: [Errores](errores.md).

```ts
import { CommandExitError, Sandbox, UnimplementedError } from "rayito";

await using sbx = await Sandbox.create();
try {
  await sbx.runCode("1 + 1", { language: "typescript" });
} catch (error) {
  if (error instanceof UnimplementedError) console.log(error.feature, error.reason);
  else if (error instanceof CommandExitError) console.log(error.exitCode);
  else throw error;
}
```

## Shim de E2B (`rayito/e2b`) { #e2b }

`rayito/e2b` reexporta la API JS de E2B 2.x: `Sandbox` (también por
defecto), `E2B`, `ConnectionConfig`, `Secret`, `Template`, `Volume`
(experimental), `Git`, `Filesystem`, `Pty`, `Execution`, `Result`, los
errores de E2B y `UnimplementedError`. El sandbox nativo está en
`sbx.native`. Lista completa: [índice de exports](#exports) y
[Diferencias con E2B](../e2b-compat.md#tabla-de-imports).

## Versión

`import { VERSION } from "rayito"` da la versión del paquete.

## Índice de exports { #exports }

Todo lo que exportan los dos puntos de entrada del paquete. Lo que no está
aquí es privado.

### `rayito`: clases, funciones y constantes

| Área | Exports |
|---|---|
| Sandbox | `Sandbox`, `SandboxListPaginator`, `LambdaMicrovmsControlPlane`, `PortSpec`, `VERSION` |
| Subclientes y handles | `Commands`, `CommandHandle`, `Filesystem`, `WatchHandle`, `UploadTicket`, `DownloadLink`, `Pty`, `PtyHandle`, `Git`, `CodeClient`, `Agent`, `AgentStream` |
| Modelos | `Execution`, `Result`, `FileType`, `FilesystemEventType`, `EgressEnforcement`, `HostAccess`, `S3Prefix`, `ALL_TRAFFIC`, `DEFAULT_PERSIST_TIMEOUT_MS`, `defaultIdlePolicy` |
| Gráficos | `ChartType`, `ScaleType`, `parseChart` |
| Agente de IA | `AgentSpec`, `AgentModel`, `AgentLimits`, `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `DeepAgents`, `TokenUsage`, `agentFailed`, `bedrockGateway`, `anthropicGateway`, `openaiCompatibleGateway`, `AgentTemplate`, `AGENT_TEMPLATE_RUNTIMES`, `DEFAULT_AGENT_TEMPLATE_BASE`, `DEFAULT_AGENT_TEMPLATE_NAME`, `agentPoolWarmup` |
| Pool | `SandboxPool`, `InMemoryPoolBackend`, `JsonFilePoolBackend`, `validatePoolConfig` |
| Funciones opcionales | `SecretStore`, `SecretCache`, `SecretRef`, `DynamoDbIndex`, `S3Mount`, `LifecycleEvents`, `TelemetryExport`, `OtlpAuth`, `DEFAULT_INTERVAL_S`, `MIN_INTERVAL_S`, `MAX_INTERVAL_S`, `DEFAULT_SERVICE_NAME`, `SecretGateway`, `GatewayStatus`, `GatewayHandle`, `Template`, `ReadyCommand`, `waitForPort`, `waitForUrl`, `waitForProcess`, `waitForFile`, `OptionalStacks`, `VolumeStore`, `EfsVolume`, `EfsVolumes` (experimentales), `CustomDomain` (experimental) |
| Errores | `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `SandboxNotReadyError`, `SandboxStateError`, `SandboxLifetimeError`, `PoolClosedError`, `CommandExitError`, `PersistenceError`, `DiskFullError`, `TransferError`, `FileUploadError`, `RateLimitError`, `GitUpstreamError`, `SecretError`, `SecretNotFoundError`, `SandboxIndexError`, `IndexWriteError`, `MountError`, `VolumeError`, `VolumeMountError`, `VolumeNotFoundError`, `VolumePathNotFoundError`, `BuildError`, `TemplateError`, `StackError`, `WebhookError`, `GatewayError`, `CustomDomainError`, `AgentError`, `AuthenticationError`, `GitAuthError`, `QuotaExceededError`, `CapacityError`, `UnimplementedError`, `LifecycleUnsupportedError` ([Errores](errores.md)) |

### `rayito`: tipos

| Área | Tipos |
|---|---|
| Opciones del sandbox | `SandboxCreateOptions`, `SandboxConnectOptions`, `ControlPlaneOptions`, `InstanceConnectOptions`, `SandboxListOptions`, `SandboxPaginateOptions`, `SandboxSetTimeoutOptions`, `PauseOptions`, `StaticPauseOptions`, `UpdateNetworkOptions`, `StaticUpdateNetworkOptions`, `StaticMetricsHistoryOptions`, `MetricsHistoryOptions`, `SignedUrlOptions`, `LoggingOption`, `PortLike`, `OnTimeout`, `ListOrder`, `LaunchOptions`, `TransportSettings`, `Logger` |
| Plano de control | `ControlPlane`, `CommandSender`, `ListMicrovmsOptions`, `ListMicrovmsPageOptions`, `MicrovmListPage` |
| Modelos | `SandboxInfo`, `SandboxListItem`, `SandboxHealth`, `SandboxMetrics`, `SandboxLifecycle`, `IdlePolicy`, `IdlePolicyInput`, `CommandResult`, `ProcessInfo`, `PtySize`, `EntryInfo`, `FilesystemEvent`, `WriteEntry`, `WriteData`, `CodeContext`, `Logs`, `OutputMessage`, `OutputChunk`, `ExecutionError`, `NetworkPolicyInput`, `NetworkSelector`, `NetworkSelectorContext`, `NetworkState`, `EgressProxyInput`, `S3Staging`, `ResolvedS3Staging`, `TransferStatus`, `TransferDirectionName`, `TransferPhaseName` |
| Gráficos | `Chart`, `LineChart`, `ScatterChart`, `BarChart`, `BarData`, `PieChart`, `PieData`, `BoxAndWhiskerChart`, `BoxAndWhiskerData`, `PointData`, `SuperChart` |
| Comandos, ficheros, PTY y código | `CommandOptions`, `ConnectOptions`, `RequestOptions`, `OutputCallback`, `ReadOptions`, `ReadFormat`, `ReadResult`, `WriteOptions`, `WriteFilesOptions`, `ListOptions`, `RemoveOptions`, `UserOptions`, `WatchOptions`, `EventCallback`, `ExitCallback`, `UploadUrlOptions`, `DownloadUrlOptions`, `WaitOptions`, `PtyCreateOptions`, `PtyConnectOptions`, `PtyDataCallback`, `RunCodeOptions`, `CreateContextOptions`, `ContextLike`, `StdoutCallback`, `ResultCallback`, `ErrorCallback` |
| Git | `GitStatus`, `GitFileStatus`, `GitBranches`, `GitResetMode`, `GitConfigScope`, `GitStatusLabel`, `GitAddOpts`, `GitCloneOpts`, `GitCommitOpts`, `GitConfigOpts`, `GitDangerouslyAuthenticateOpts`, `GitDeleteBranchOpts`, `GitInitOpts`, `GitPullOpts`, `GitPushOpts`, `GitRemoteAddOpts`, `GitRequestOpts`, `GitResetOpts`, `GitRestoreOpts` |
| Persistencia | `S3PrefixInit`, `CheckpointFilesOptions`, `CheckpointProgress`, `CheckpointProgressCallback`, `CheckpointResult`, `RestoreFilesOptions`, `RestoreProgress`, `RestoreProgressCallback`, `RestoreResult`, `ReincarnateOptions` |
| Agente de IA | `AgentRunOptions`, `AgentPrepareOptions`, `AgentSpecOptions`, `AgentModelOptions`, `AgentLimitsOptions`, `AgentPermissionsOptions`, `SubAgentOptions`, `McpLocalOptions`, `McpRemoteOptions`, `McpServer`, `ModelProvider`, `PermissionAction`, `ToolPermission`, `DeepAgentsOptions`, `AgentEvent`, `TextDelta`, `Text`, `Reasoning`, `ToolCall`, `StepStarted`, `StepFinished`, `AgentFailed`, `AgentFailedOptions`, `AgentFailureReason`, `Done`, `AgentResult`, `TokenUsageOptions`, `BedrockGatewayOptions`, `AnthropicGatewayOptions`, `OpenAiCompatibleGatewayOptions`, `AgentRuntime`, `RunCommand`, `RunRequest`, `RuntimeFile`, `RuntimeFiles`, `RuntimeState`, `TemplateStep`, `WarmupStep`, `AgentTemplateOptions`, `AgentTemplateBuildOptions`, `AgentTemplateManifest`, `AgentTemplateRuntime` |
| Pool | `PoolConfig`, `ResolvedPoolConfig`, `PoolBackend`, `PoolStats`, `PoolSlotInfo`, `SlotRecord`, `SandboxPoolOptions`, `TakeOptions`, `CloseOptions` |
| Secretos e índice | `SecretOptions`, `SecretsInput`, `SecretLike`, `SecretRefOptions`, `SecretCacheOptions`, `SecretStoreOptions`, `SecretInfo`, `SecretPage`, `SecretsManagerApi`, `DynamoDbIndexOptions`, `DynamoDbApi`, `WriteFailurePolicy`, `IndexRecord` |
| Montajes, tamaños, eventos, OTLP y pasarela | `S3MountOptions`, `MountStatus`, `SizeInput`, `SizeName`, `SizeRequest`, `ResolvedSize`, `LifecycleEventsOptions`, `DeployWebhooksOptions`, `EventRecord`, `EventKind`, `KillReason`, `WebhookInfo`, `TelemetryExportOptions`, `TelemetryHealth`, `NameStyleOption`, `SecretGatewayOptions`, `AllowRule` |
| Templates | `TemplateSpec`, `BaseImageRef`, `StartSpec`, `ReadyPoll`, `RunStep`, `CopyStep`, `EnvStep`, `WorkdirStep`, `UserStep`, `WireStep`, `BuildOptions`, `BuildClients`, `BuildHandle`, `BuildInfo`, `BuildState`, `BuildStatus` |
| Pilas opcionales | `StackComponent`, `StackParameter`, `StackStatus`, `StackArtifact`, `CostStatement`, `DeployPlan`, `DeployAction`, `ParameterChange`, `ParameterPlan`, `DeployTarget`, `StackProvisioner`, `UpdateOutcome`, `OptionalStacksOptions`, `DeployOptions`, `DestroyOptions`, `ParameterChangesOptions` |
| Volúmenes EFS y dominio propio (experimentales) | `VolumeStoreOptions`, `EfsVolumeOptions`, `VolumeStatus`, `MountState`, `EfsVolumesOptions`, `EfsVolumesDeployOptions`, `EfsNetworkReport`, `NetworkFinding`, `FindingLevel`, `NetworkInspector`, `CustomDomainOptions`, `DeployCustomDomainOptions`, `RegisterRouteOptions`, `CustomDomainRoute` |
| Opciones de errores | `AgentErrorOptions`, `BuildErrorOptions`, `GatewayErrorOptions`, `MountErrorOptions`, `PersistenceErrorOptions`, `StackErrorOptions`, `TransferErrorOptions`, `VolumeMountErrorOptions` |

### `rayito/e2b`

| Área | Exports |
|---|---|
| Clases y funciones | `Sandbox` (también `export default`), `SandboxPaginator`, `E2B`, `ConnectionConfig`, `Filesystem`, `Pty`, `Git`, `Secret`, `SecretPaginator`, `Template`, `Volume`, `bindVolume`, `getSignature`, `Execution`, `Result`, `FileType`, `FilesystemEventType`, `ALL_TRAFFIC`, `DEFAULT_WATCH_TIMEOUT_MS` |
| Errores (las clases nativas con el nombre de E2B) | `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `CommandExitError`, `FileUploadError`, `RateLimitError`, `GitUpstreamError`, `SecretError`, `SecretNotFoundError`, `BuildError`, `TemplateError`, `AuthenticationError`, `GitAuthError`, `UnimplementedError`, `NotEnoughSpaceError` (= `DiskFullError`), `ServiceBusyError` (= `CapacityError`) |
| Tipos | `SandboxOpts`, `SandboxConnectOpts`, `SandboxInstanceConnectOpts`, `SandboxListOpts`, `SandboxInfo`, `SandboxInfoLifecycle`, `SandboxLifecycle`, `SandboxState`, `SandboxMetrics`, `SandboxMetricsOpts`, `SandboxNetworkInfo`, `SandboxNetworkOpts`, `SandboxNetworkSelector`, `SandboxNetworkUpdate`, `SandboxOnResume`, `SandboxOnTimeout`, `SandboxPauseOpts`, `SandboxUrlOpts`, `E2BClientOpts`, `ConnectionOpts`, `Username`, `WatchOpts`, `WatchEventCallback`, `PtyCreateOpts`, `PtyConnectOpts`, `PtyOutputCallback`, `PtySize`, `SecretInfo`, `SecretConnectionOpts`, `SecretCreateOpts`, `SecretDestroyOpts`, `SecretExistsOpts`, `SecretGetInfoOpts`, `SecretListOpts`, `SecretUpdateOpts`, `CommandHandle`, `CommandResult`, `Context` (= `CodeContext`), `EntryInfo`, `WriteInfo` (= `EntryInfo`), `ExecutionError`, `FilesystemEvent`, `Logs`, `OutputMessage`, `GitStatus`, `GitFileStatus`, `GitBranches`, `GitResetMode`, `Logger` |
