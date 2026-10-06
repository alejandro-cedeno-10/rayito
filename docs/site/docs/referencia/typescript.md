# Referencia de TypeScript

El paquete npm `rayito` (`pnpm add rayito`) expone la misma superficie que
el SDK de Python, en `camelCase` y con los tiempos en milisegundos. Todo es
asíncrono. Esta página lista las clases y métodos públicos con su
equivalente en Python; los tipos completos están en los `.d.ts` del paquete
(tu editor los muestra al pasar el cursor).

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
| `region`, `credentials`, `client`, `controlPlane` | — | la cadena del AWS SDK v3 | `region`, `session`, `control_plane` |
| `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | `number` | `90_000`, `60_000`, `60_000` | `ready_timeout`, `request_timeout`, `reconnect_timeout` |
| `keepOnFailure` | `boolean` | `false` | `keep_on_failure` |
| `logger` | `Logger` | — | `logger` |
| `signal` | `AbortSignal` | — | — |
| `secrets`, `secretCache` | opcional, con coste | apagado | `secrets`, `secret_cache` |
| `index` | `DynamoDbIndex` (opcional, con coste) | apagado | `index` |
| `tracerProvider` | `TracerProvider` de OpenTelemetry | apagado | `tracer_provider` |
| `mounts` | `Record<string, S3Mount>` o `Map` | apagado (0.6) | `mounts` |
| `size` | `"512mb" \| "1gb" \| "2gb" \| "4gb" \| "8gb"` o `{ memoryMib }` | apagado (0.6) | `size` |
| `events` | `LifecycleEvents` | apagado (0.6) | `events` |
| `telemetry` | `TelemetryExport` | apagado (0.6) | `telemetry` |
| `gateways` | `Record<string, SecretGateway>` | apagado (0.6) | `gateways` |
| `volumes` | `Record<string, EfsVolume>` | apagado (0.6, [experimental](../funciones-opcionales/volumenes-efs.md)) | `volumes` |
| `domain` | — | [en desarrollo](../novedades/index.md#en-desarrollo): lanza `UnimplementedError`; usa `CustomDomain` | `domain` |

### Instancia

| TypeScript | Python | Qué hace |
|---|---|---|
| `sbx.sandboxId`, `sbx.accessToken`, `sbx.endpoint`, `sbx.region` | `sandbox_id`, `access_token`, `endpoint`, `region` | propiedades |
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
| `await sbx.mounts()` | `mounts` (propiedad; `await sbx.mounts()` en `AsyncSandbox`) | `ReadonlyMap<string, MountStatus>` en vivo ([Montajes S3](../funciones-opcionales/montajes-s3.md)) |
| `sbx.getTelemetryStatus()` | `get_telemetry_status()` | `TelemetryHealth` (`exported`, `dropped`, `lastErrorClass`) ([Exportación OTLP](../funciones-opcionales/exportacion-otlp.md)) |
| `sbx.gateways.get(name)?.url`, `sbx.gateways.refresh()` | `gateways[name].url`, `gateways.refresh()` | URL de loopback y rotación ([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)) |
| `sbx.commands`, `sbx.files`, `sbx.pty`, `sbx.git` | igual | subclientes (abajo) |

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
`requestTimeoutMs`, `signal`, `secrets`.

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

`WriteOptions`: `user`, `gzip`, `metadata`, `mode`, `requestTimeoutMs`, `signal`.
`ReadOptions`: `format`, `user`, `gzip`, `streamIdleTimeoutMs`. Guía:
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
| `agent.prepare(opts?)` | `agent.prepare(...)` | dispara el calentamiento del runtime en segundo plano |

`AgentRunOptions`: `spec`, `runtime`, `sessionId`, `model`, `limits`,
`workdir`, `attach`, `reasoning`, `signal` (un `AbortSignal` aborta el
stream). `AgentStream`: `sessionId`, `droppedLines`, `abort()`, `result()`,
`close()`.

## Pool { #pool }

| TypeScript | Python |
|---|---|
| `await new SandboxPool({ size, template, ... }).start()` | `SandboxPool(PoolConfig(size=, template=, ...))` + `start()` / `with` |
| `pool.take({ waitMs, secrets })` | `pool.take(wait=, secrets=)` |
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
defecto), `E2B`, `ConnectionConfig`, `Secret`, `Git`, `Filesystem`, `Pty`,
`Execution`, `Result`, los errores de E2B y `UnimplementedError`. El
sandbox nativo está en `sbx.native`. Lista completa:
[Diferencias con E2B](../e2b-compat.md#tabla-de-imports).

## Versión

`import { VERSION } from "rayito"` da la versión del paquete.
