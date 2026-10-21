# TypeScript reference

The `rayito` npm package (`pnpm add rayito`) exposes the same surface as
the Python SDK, in `camelCase` and with times in milliseconds. Everything is
asynchronous. This page lists the public classes and methods with their
Python equivalent, and at the end the [exports index](#exports) with
everything that `rayito` and `rayito/e2b` export; the full types are in the
package's `.d.ts` files (your editor shows them on hover).

```ts
import { Sandbox } from "rayito"; // ESM; also require("rayito") in CommonJS
import { Sandbox as E2BSandbox } from "rayito/e2b"; // the E2B 2.x shim

await using sbx = await Sandbox.create();
await using e2b = await E2BSandbox.create();
console.log(sbx.sandboxId, e2b.sandboxId);
```

Naming rules: [Python and TypeScript](index.md#python-y-typescript-como-se-corresponden-los-nombres).

## Sandbox { #sandbox }

### Static methods { #metodos-estaticos }

| TypeScript | Python | Returns |
|---|---|---|
| `Sandbox.create(opts?)` | `Sandbox.create(...)` | `Promise<Sandbox>` |
| `Sandbox.connect(sandboxId, opts?)` | `Sandbox.connect(sandbox_id, ...)` | `Promise<Sandbox>`; needs `accessToken` (or `RAYITO_ACCESS_TOKEN`) |
| `Sandbox.list(opts?)` | `Sandbox.list(...)` | `AsyncIterable<SandboxListItem>` |
| `Sandbox.paginate(opts?)` | `Sandbox.paginate(...)` | `SandboxListPaginator` (`nextItems()`, `hasNext`, `nextToken`) |
| `Sandbox.kill(sandboxId, opts?)` | `Sandbox.kill(sandbox_id)` | `Promise<boolean>` |
| `Sandbox.getInfo(sandboxId, opts?)` | `Sandbox.get_info(sandbox_id)` | `Promise<SandboxInfo>` |
| `Sandbox.pause(sandboxId, opts?)` / `Sandbox.resume(sandboxId, opts?)` | `Sandbox.pause(sandbox_id)` / `Sandbox.resume(sandbox_id)` | `Promise<boolean>` / `Promise<void>` |
| `Sandbox.setTimeout(sandboxId, ms, opts)` | `Sandbox.set_timeout(sandbox_id, s, access_token=)` | `Promise<void>` |
| `Sandbox.getMetricsHistory(sandboxId, opts)` | `Sandbox.get_metrics_history(sandbox_id, access_token=)` | `Promise<SandboxMetrics[]>` |
| `Sandbox.updateNetwork(sandboxId, policy, opts)` | `Sandbox.update_network(sandbox_id, policy, access_token=)` | `Promise<NetworkState>` |

### `Sandbox.create` options (`SandboxCreateOptions`) { #opciones-de-sandboxcreate-sandboxcreateoptions }

| Option | Type | Default | Python |
|---|---|---|---|
| `template` | `string` | `RAYITO_TEMPLATE` | `template` |
| `templateVersion` | `string` | the latest active | `template_version` |
| `timeoutMs` | `number` | `3_600_000` (max. `28_800_000`) | `timeout` (s) |
| `maxLifetimeMs`, `onTimeout` | `number`, `"kill" \| "pause"` | — | `max_lifetime`, `on_timeout` |
| `idle` | `IdlePolicyInput \| null` | `{ maxIdleSeconds: 300, autoResume: true }` | `idle` |
| `envs`, `metadata` | `Record<string, string>` | — | `envs`, `metadata` |
| `cpuTimeLimit` | `number` (s) | — | `cpu_time_limit` |
| `executionRoleArn`, `logging` | `string`, `"disabled" \| "cloudwatch"` | —, `"disabled"` | `execution_role_arn`, `logging` |
| `allowedPorts`, `ingress`, `egress` | arrays | `[8080]`, managed | `allowed_ports`, `ingress`, `egress` |
| `network`, `allowInternetAccess` | `NetworkPolicyInput`, `boolean` | unrestricted, `true` | `network`, `allow_internet_access` |
| `transfer` | `S3Staging` | `RAYITO_TRANSFER_BUCKET` | `transfer` |
| `persist`, `persistTimeoutMs` | `S3Prefix`, `number` | —, `600_000` | `persist`, `persist_timeout` |
| `pool` | `SandboxPool` | — | `pool` |
| `accessToken` | `string` | 32 random bytes | `access_token` |
| `region`, `credentials`, `client`, `controlPlane` | — | `AWS_REGION` (or `AWS_DEFAULT_REGION`) and the AWS SDK v3 chain | `region`, `session`, `control_plane` |
| `retries`, `proxy`, `integration` | `number`, `string`, `string` | the AWS SDK's | — (in Python, only the E2B shim, with `ClientSettings`) |
| `transport` | `Partial<TransportSettings>` | TLS to the proxy's 443 | `transport` |
| `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` | `number` | `90_000`, `60_000`, `60_000` | `ready_timeout`, `request_timeout`, `reconnect_timeout` |
| `keepOnFailure` | `boolean` | `false` | `keep_on_failure` |
| `logger` | `Logger` | — | `logger` |
| `signal` | `AbortSignal` | — | — |
| `secrets`, `secretCache` | optional, with cost | off | `secrets`, `secret_cache` |
| `index` | `DynamoDbIndex` (optional, with cost) | off | `index` |
| `tracerProvider` | OpenTelemetry `TracerProvider` | off | `tracer_provider` |
| `mounts` | `Record<string, S3Mount>` or `Map` | off | `mounts` |
| `size` | `"512mb" \| "1gb" \| "2gb" \| "4gb" \| "8gb"` o `{ memoryMib }` | off | `size` |
| `events` | `LifecycleEvents` | off | `events` |
| `telemetry` | `TelemetryExport` | off | `telemetry` |
| `gateways` | `Record<string, SecretGateway>` | off | `gateways` |
| `volumes` | `Record<string, EfsVolume>` | off ([experimental](../funciones-opcionales/volumenes-efs.md)) | `volumes` |
| `domain` | — | not wired up: any value raises `UnimplementedError` before `run-microvm`; for a custom domain use `CustomDomain` ([experimental](../funciones-opcionales/dominio-propio.md)) | `domain` |

### Instance { #instancia }

| TypeScript | Python | What it does |
|---|---|---|
| `sbx.sandboxId`, `sbx.accessToken`, `sbx.endpoint`, `sbx.endpointUrl`, `sbx.region` | `sandbox_id`, `access_token`, `endpoint`, `endpoint_url`, `region` | properties |
| `sbx.info`, `sbx.launchInfo`, `sbx.resumeGeneration` | `info`, `launch_info`, `resume_generation` | the last known `SandboxInfo`, the launch one and how many times it was resumed |
| `sbx.metadata` | `metadata` | the `metadata` from `create({ metadata })` as returned by the last `Health` (frozen copy; `{}` if the image does not return them yet) |
| `sbx.persist`, `sbx.lastRestore`, `sbx.transfer` | `persist`, `last_restore`, `transfer` | the linked `S3Prefix`, the last automatic restore and the transfer bucket |
| `await sbx.kill()` / `await using` | `kill()` / `with` | terminates the MicroVM |
| `sbx.close()` | `close()` | closes the connections without touching the MicroVM |
| `sbx.getInfo()`, `sbx.isRunning()`, `sbx.getHealth()` | `get_info()`, `is_running()`, `get_health()` | state; `SandboxHealth.imdsBlocked` (`imds_blocked`) is `true` only if `rayd` installed and verified the IMDS block (image with capabilities) |
| `sbx.connect({ timeoutMs })` | `connect(timeout=)` | reopens the handle and extends the deadline |
| `sbx.setTimeout(ms)` | `set_timeout(s)` | moves the [server-side deadline](../lifecycle.md) |
| `sbx.pause()`, `sbx.resume()` | `pause()`, `resume()` | [Pause and resume](../guias/pausar-reanudar.md) |
| `sbx.getHost(port)` | `get_host(port)` | `HostAccess` with `host`, `url`, `port` and `headers` |
| `sbx.getMetrics()`, `sbx.getMetricsHistory(opts)` | `get_metrics()`, `get_metrics_history(...)` | [Metrics](../observability.md) |
| `sbx.updateNetwork(policy)`, `sbx.getNetwork()` | `update_network(...)`, `get_network()` | [Outbound network](../network.md) |
| `sbx.checkpointFiles()`, `sbx.restoreFiles()`, `sbx.reincarnate()` | `checkpoint_files()`, `restore_files()`, `reincarnate()` | [Persistence](../persistence.md) |
| `sbx.uploadUrl(path, opts?)`, `sbx.downloadUrl(path, opts?)` | `upload_url(path)`, `download_url(path)` | signed URL as in E2B (`string`) |
| `await sbx.mounts()` | `mounts` (property; `await sbx.mounts()` in `AsyncSandbox`) | live `ReadonlyMap<string, MountStatus>` ([S3 mounts](../funciones-opcionales/montajes-s3.md)) |
| `await sbx.volumes()` | `volumes` (property; `await sbx.volumes()` in `AsyncSandbox`) | live `ReadonlyMap<string, VolumeStatus>` ([EFS volumes](../funciones-opcionales/volumenes-efs.md), experimental) |
| `sbx.getTelemetryStatus()` | `get_telemetry_status()` | `TelemetryHealth` (`exported`, `dropped`, `lastErrorClass`) ([OTLP export](../funciones-opcionales/exportacion-otlp.md)) |
| `sbx.gateways.get(name)?.url`, `sbx.gateways.refresh()` | `gateways[name].url`, `gateways.refresh()` | loopback URL and rotation ([Secret gateway](../funciones-opcionales/pasarela-de-secretos.md)) |
| `sbx.commands`, `sbx.files`, `sbx.pty`, `sbx.git`, `sbx.agent` | same | subclients (below) |

## Run code { #ejecutar-codigo }

| TypeScript | Python | Devuelve |
|---|---|---|
| `sbx.runCode(code, opts?)` | `run_code(code, ...)` | `Promise<Execution>` (`text`, `results`, `logs`, `error`) |
| `sbx.createCodeContext(opts?)` | `create_code_context(...)` | `Promise<CodeContext>` |
| `sbx.listCodeContexts()` | `list_code_contexts()` | `Promise<CodeContext[]>` |
| `sbx.removeCodeContext(ctx)`, `sbx.restartCodeContext(ctx)` | `remove_code_context(ctx)`, `restart_code_context(ctx)` | `Promise<void>` |

`RunCodeOptions`: `language`, `context`, `onStdout`, `onStderr`,
`onResult`, `onError`, `envs`, `timeoutMs` (`300_000`; `0` for no limit),
`requestTimeoutMs`, `signal`, `secrets`. Guide: [Run code](../guias/ejecutar-codigo.md).

## Commands (`sbx.commands`) { #commands }

| TypeScript | Python | Devuelve |
|---|---|---|
| `commands.run(cmd, opts?)` | `commands.run(cmd, ...)` | `CommandResult` (`stdout`, `stderr`, `exitCode`) or, with `background: true`, `CommandHandle` |
| `commands.connect(pid, opts?)` | `commands.connect(pid)` | `CommandHandle` |
| `commands.list()` | `commands.list()` | `ProcessInfo[]` |
| `commands.kill(pid)` | `commands.kill(pid)` | `boolean` |
| `commands.sendStdin(pid, data)`, `commands.closeStdin(pid)` | `send_stdin`, `close_stdin` | `void` |

`CommandOptions`: `background`, `envs`, `user`, `cwd`, `onStdout`,
`onStderr`, `stdin`, `timeoutMs` (`60_000`; `0` for no limit), `tag`,
`maxOutputBytes` (64 MiB kept per descriptor), `requestTimeoutMs`,
`signal`, `secrets`.

`CommandHandle`: `pid`, `stdout`, `stderr`, `exitCode`, `wait()`, `kill()`,
`disconnect()`, `sendStdin(data)`, `closeStdin()` and iteration
`for await (const chunk of handle)` (`{ stdout }`, `{ stderr }`). Guide:
[Commands](../guias/comandos.md).

## Files (`sbx.files`) { #files }

| TypeScript | Python | What it does |
|---|---|---|
| `files.read(path, { format })` | `files.read(path, format=)` | `"text"` (default), `"bytes"`, `"blob"` or `"stream"` |
| `files.write(path, data, opts?)` | `files.write(path, data, ...)` | atomic write; `EntryInfo` |
| `files.writeFiles(entries)` | `files.write_files(entries)` | several files in one stream |
| `files.list(path, { depth })` | `files.list(path, depth=)` | `EntryInfo[]` |
| `files.exists`, `files.getInfo`, `files.remove`, `files.rename`, `files.makeDir` | `exists`, `get_info`, `remove`, `rename`, `make_dir` | as in E2B |
| `files.watchDir(path, opts?)` | `files.watch_dir(path, ...)` | `WatchHandle` (`getNewEvents()`, `stop()`) |
| `files.uploadUrl(path, opts?)` | `files.upload_url(path, ...)` | `UploadTicket` (`url`, `headers`, `wait()`, `status()`, `cancel()`) |
| `files.downloadUrl(path, opts?)` | `files.download_url(path, ...)` | `DownloadLink` (`url`, `size`, `sha256`, `expiresAt`) |

`WriteOptions`: `user`, `gzip`, `metadata`, `useOctetStream`, `mode`,
`requestTimeoutMs`, `signal`. `ReadOptions`: `format`, `user`, `gzip`,
`streamIdleTimeoutMs`, `requestTimeoutMs`, `signal`. Guide:
[Files and S3](../files.md).

## Terminal (`sbx.pty`) { #pty }

| TypeScript | Python | What it does |
|---|---|---|
| `pty.create(opts?)` | `pty.create(...)` | `PtyHandle`; options `size`, `user`, `cwd`, `envs`, `shell`, `onData`, `timeoutMs`, `secrets` |
| `pty.connect(pid, opts?)` | `pty.connect(pid)` | reattaches to a live terminal |
| `handle.sendInput(data)`, `handle.resize(size)`, `handle.kill()` | `send_input`, `resize`, `kill` | keystrokes, size, end |

Iterating a `PtyHandle` yields `{ pty: Uint8Array }`. Guide:
[Terminal (PTY)](../guias/terminal-pty.md).

## Git (`sbx.git`) { #git }

`clone`, `init`, `remoteAdd`, `remoteGet`, `status`, `branches`,
`createBranch`, `checkoutBranch`, `deleteBranch`, `add`, `commit`, `reset`,
`restore`, `push`, `pull`, `setConfig`, `getConfig`,
`dangerouslyAuthenticate` y `configureUser`, with the same options as in
Python in `camelCase`. Guide: [Git](../git.md).

## AI agent (`sbx.agent`) { #agent }

| TypeScript | Python | Devuelve |
|---|---|---|
| `agent.run(prompt, opts)` | `agent.run(prompt, ...)` | `AgentResult`; raises `AgentError` if it fails |
| `agent.stream(prompt, opts)` | `agent.stream(prompt, ...)` | `AgentStream` (`AsyncIterable<AgentEvent>`); never raises on an agent failure |
| `agent.prepare(opts?)` | `agent.prepare(...)` | triggers the runtime warm-up in the background (`runtime`) |

`AgentRunOptions`: `spec` (required), `runtime` (`"opencode"` by
default, `"deepagents"` or `new DeepAgents({...})`), `sessionId`, `model`,
`limits` (`AgentLimits`: `maxSteps`, `timeoutMs`, `maxOutputBytes`,
`maxTotalTokens`), `workdir` (`/home/user`),
`reasoning` (`false`) and `signal` (an `AbortSignal` aborts the stream).
`AgentStream`: `sessionId`, `droppedLines`, `abort()`, `result()`,
`close()` and iteration `for await (const event of stream)`.

The normal start is `Sandbox.create(...)` + `sbx.agent.run(...)`, with
nothing else:

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
const result = await sbx.agent.run("Summarize the README.", { spec });
console.log(result.text);
```

The `rayito-agent` image (with the runtime installed) is built once with
`AgentTemplate` or `rayito agent template build`
([Agent templates](../funciones-opcionales/templates-de-agente.md)).
Fast start (`prefetch`, `agentPoolWarmup` and a warm `SandboxPool`)
is optional: [What do I use?](../guias/agente-en-el-sandbox.md#que-uso).
Full guide: [Agent in the sandbox](../guias/agente-en-el-sandbox.md).

## Deploy to AgentCore (`deployAgent`) { #agentcore }

Since 0.15.0. `deployAgent()` and `planDeploy()` accept
`target: "agentcore"` and `agentcore` (the target's options). Without `target`,
or with `"microvm"`, they return what they always did (`AgentFleet`,
`AgentDeployPlan`) and no AgentCore, CodeBuild or ECR client is loaded. With
`"agentcore"` you need, as optional peers,
`@aws-sdk/client-bedrock-agentcore-control`, `@aws-sdk/client-bedrock-agentcore`,
`@aws-sdk/client-codebuild`, `@aws-sdk/client-ecr`,
`@aws-sdk/client-cloudformation` (the agent's stack) and
`@aws-sdk/client-cloudwatch-logs` (only when deleting). Guide:
[Deploy to AgentCore](../guias/desplegar-en-agentcore.md).

!!! info "Cost and activation"
    - **Turns it on:** `target: "agentcore"` in `deployAgent()`. `planDeploy()` makes no AWS call.
    - **AWS resources and calls:** one CloudFormation stack per agent, one
      CodeBuild build for each new content and one AgentCore runtime.
    - **Approximate cost** (us-east-1, list prices, 2026-10-19): runtime at
      $0.0895 per vCPU-hour and $0.00945 per GB-hour of actual use; build at
      $0.0034 per minute; image in ECR at $0.10 per GB-month
      ([Pricing](../cost.md#coste-de-un-agente-en-agentcore)).
    - **IAM:** [the guide's permissions](../guias/desplegar-en-agentcore.md#antes-de-empezar).
    - **How to turn it off:** `await fleet.delete()`.
    - **Example:** the one below.

| TypeScript | Python | Returns |
|---|---|---|
| `deployAgent(path, { target: "agentcore", options?, agentcore? })` | `deploy_agent(path, target="agentcore", agentcore=...)` | `AgentCoreFleet` |
| `planDeploy(path, { target: "agentcore", ... })` | `plan_deploy(path, target="agentcore", ...)` | `AgentCoreDeployPlan` (makes no AWS call) |
| `AgentCoreFleet.connect(name, { region?, credentials? })` | `AgentCoreFleet.connect(name, region=..., session=...)` | `AgentCoreFleet` |
| `fleet.invoke(prompt, { sessionId?, model?, reasoning? })` | `fleet.invoke(prompt, session_id=..., model=..., reasoning=...)` | `AgentResult`; the same signature as `AgentFleet` |
| `fleet.stream(prompt, opts?)` | `fleet.stream(prompt, ...)` | `AgentCoreFleetStream` (`AsyncIterable<AgentEvent>`, `sessionId`, `result()`, `close()`) |
| `fleet.status()` | `fleet.status()` | `AgentCoreStatus` |
| `fleet.stopSession(sessionId)` | `fleet.stop_session(session_id)` | `boolean` |
| `fleet.delete()` | `fleet.delete()` | `AgentCoreDeleteResult` |

`AgentCoreOptions`: `bedrockModels`, `readableSecrets`, `idleSessionTimeoutMs`,
`maxSessionLifetimeMs` (milliseconds, converted to whole seconds: a value that
is not a multiple of 1000 is `lifecycle_invalid`) and `tags`. `AgentCoreFleet`
has `name`, `region`, `runtimeName`, `runtimeId`, `runtimeArn`,
`runtimeVersion`, `imageUri` and `logGroup`; `AgentCoreStatus` adds `status`, and
`AgentCoreDeleteResult` carries `runtimeDeleted`, `stackDeleted` and
`logGroupDeleted`. Its own failures are `AgentCoreError` (`code`, `exitCode`;
[Errors](errores.md#agentcore)).

```ts
import { deployAgent } from "rayito";

const fleet = await deployAgent("./my-agent", {
  options: { bucket: "amzn-s3-demo-bucket" },
  target: "agentcore",
  agentcore: {
    bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
    readableSecrets: ["my-app/external-key"],
    idleSessionTimeoutMs: 600_000,
    maxSessionLifetimeMs: 3_600_000,
    tags: { Owner: "platform" },
  },
});
console.log(fleet.runtimeArn, fleet.runtimeVersion);
```

## CDK constructs (`rayito/cdk`) { #cdk }

Since 0.15.0. A separate entry point, with `aws-cdk-lib`
(`^{{cdkAwsCdkLibMinVersion}}`) and `constructs` (`^{{cdkConstructsMinVersion}}`)
as optional peers: neither `rayito`, nor `rayito/e2b`, nor the CLI, nor the MCP
server import them. Everything is synchronous except
`MicrovmImageArtifact.downloadRelease()`, and `cdk synth` makes no AWS call.
Guide: [Deploy with CDK](../guias/desplegar-con-cdk.md).

!!! info "Cost and activation"
    - **Turns it on:** instantiating a construct in a CDK app and `cdk deploy`.
    - **AWS resources and calls:** `RayitoAgent`, those of the AgentCore deploy
      plus two Lambdas and a state machine for the `Provider`;
      `RayitoMicrovmImage`, a build role, a log group and one image per size.
    - **Approximate cost:** see [Deploy with CDK](../guias/desplegar-con-cdk.md).
    - **IAM:** the CDK bootstrap execution role.
    - **How to turn it off:** `cdk destroy`.
    - **Example:** [the guide](../guias/desplegar-con-cdk.md#rayitoagent).

| TypeScript | Python | What it is |
|---|---|---|
| `new RayitoAgent(scope, id, { project, name?, bedrockModels?, readableSecrets?, idleSessionTimeoutSeconds?, maxSessionLifetimeSeconds?, tags?, build?, exportOutputs? })` | `RayitoAgent(scope, id, *, project, name=None, bedrock_models=(), ...)` | an agent on AgentCore Runtime; attributes `agentName`, `runtimeName`, `runtimeArn`, `runtimeId`, `runtimeVersion`, `roleArn`, `repositoryUri`, `imageUri`, `runtime` (`IBedrockAgentRuntime`, from `aws-cdk-lib/aws-bedrockagentcore`) |
| `agent.grantInvoke(grantee)` | `agent.grant_invoke(grantee)` | `bedrock-agentcore:InvokeAgentRuntime` on the runtime (`iam.Grant`) |
| `AgentImageBuild.codeBuild({ timeoutMinutes? })` / `AgentImageBuild.localDocker()` | `AgentImageBuild.code_build(*, timeout_minutes=None)` / `AgentImageBuild.local_docker()` | how the image is built; CodeBuild by default |
| `new RayitoMicrovmImage(scope, id, { name, artifact, baseImageVersion, variant?, withEfs?, osCapabilities?, memoryMib?, sizes?, allowedMountBuckets?, environment?, tags? })` | `RayitoMicrovmImage(scope, id, *, name, artifact, base_image_version, ...)` | a MicroVMs image with `rayd`; attributes `imageName`, `imageArn`, `latestActiveImageVersion`, `buildRoleArn`, `logGroupName`, `sizeImageArns` |
| `MicrovmImageArtifact.fromFile(path, { sha256? })` / `await MicrovmImageArtifact.downloadRelease({ version?, directory? })` | `MicrovmImageArtifact.from_file(path, *, sha256=None)` / `MicrovmImageArtifact.download_release(*, version=None, directory=None)` | the image zip |
| `RayitoCdkError` (`code`, `fields`) | `RayitoCdkException` | synth errors ([codes](errores.md#cdk)) |

## Pool { #pool }

| TypeScript | Python |
|---|---|
| `await new SandboxPool({ size, template, ... }).start()` | `SandboxPool(PoolConfig(size=, template=, ...))` + `start()` / `with` |
| `pool.take({ waitMs, secrets, gateways })` | `pool.take(wait=, secrets=, gateways=)` |
| `pool.stats()` | `pool.stats()` |
| `pool.close({ drain })` / `await using pool` | `pool.close(drain=)` / `with` |
| `InMemoryPoolBackend`, `JsonFilePoolBackend` | same |

In TypeScript, `PoolConfig` is the constructor options. Guide:
[Pool](../pool.md).

## Optional features { #opcionales }

Off by default; each one loads its *peerDependency* only when activated.

| Class | Optional peer | Guide |
|---|---|---|
| `SecretStore`, `SecretCache`, `SecretRef` | `@aws-sdk/client-secrets-manager` | [Secrets](../secrets.md) |
| `DynamoDbIndex` | `@aws-sdk/client-dynamodb` | [Metadata index](../funciones-opcionales/indice-de-metadatos.md) |
| `VolumeStore`, `EfsVolume` (experimental) | `@aws-sdk/client-efs` | [EFS volumes](../funciones-opcionales/volumenes-efs.md) |
| `EfsVolumes` (experimental) | `@aws-sdk/client-ec2` (`check`), `@aws-sdk/client-efs` (deleting the file system) | [EFS volumes in your VPC](../funciones-opcionales/volumenes-efs-vpc.md) |
| `CustomDomain` (experimental) | `@aws-sdk/client-cloudformation`, `@aws-sdk/client-cloudfront-keyvaluestore` y `@aws-sdk/signature-v4a` | [Custom domain](../funciones-opcionales/dominio-propio.md) |
| `tracerProvider` option | `@opentelemetry/api` (types only) | [OpenTelemetry](../funciones-opcionales/opentelemetry.md) |
| `S3Mount` (`mounts` option) | none: `rayd` mounts it | [S3 mounts](../funciones-opcionales/montajes-s3.md) |
| `size` option | none | [Sizes](../funciones-opcionales/tamanos.md) |
| `LifecycleEvents` (`events` option) | `@aws-sdk/client-dynamodb`, `@aws-sdk/client-secrets-manager` y `@aws-sdk/client-cloudformation` (locates the stack) | [Events and webhooks](../funciones-opcionales/eventos-y-webhooks.md) |
| `TelemetryExport`, `OtlpAuth` (`telemetry` option) | `@aws-sdk/client-secrets-manager` only with `OtlpAuth.bearer(...)` | [OTLP export](../funciones-opcionales/exportacion-otlp.md) |
| `SecretGateway` (`gateways` option) | `@aws-sdk/client-secrets-manager` | [Secret gateway](../funciones-opcionales/pasarela-de-secretos.md) |
| `Template` (`build`, `buildInBackground`, `getBuildStatus`, `exists`) | `@aws-sdk/client-cloudwatch-logs` only to read the build log | [Templates](../funciones-opcionales/templates.md) |
| `AgentTemplate` (`build`) | as `Template` | [Agent templates](../funciones-opcionales/templates-de-agente.md) |
| `OptionalStacks` (`components`, `status`, `parameterChanges`, `deploy`, `destroy`) | `@aws-sdk/client-cloudformation` | [Optional stacks](../funciones-opcionales/pilas-opcionales.md) |
| `Quotas` (`report`) | `@aws-sdk/client-service-quotas` | [Management dashboard](../funciones-opcionales/panel-de-gestion.md) |
| `UsageCost` (`billed`: paid, $0.01/request; `estimate`: free) | `@aws-sdk/client-cost-explorer` only for `billed` | [Management dashboard](../funciones-opcionales/panel-de-gestion.md) |
| `Fleet` (`sandboxes`, `images`, `imageVersions`, `events`, `planBulk`, `runBulk`) | none of its own: `events` uses those of `LifecycleEvents` | [Management dashboard](../funciones-opcionales/panel-de-gestion.md) |
| `deployAgent(path, { target: "agentcore" })`, `AgentCoreFleet` | `@aws-sdk/client-bedrock-agentcore-control`, `@aws-sdk/client-bedrock-agentcore` (invoking), `@aws-sdk/client-codebuild`, `@aws-sdk/client-ecr`, `@aws-sdk/client-cloudformation` and `@aws-sdk/client-cloudwatch-logs` (only when deleting) | [Deploy to AgentCore](../guias/desplegar-en-agentcore.md) |
| `rayito/cdk` (`RayitoAgent`, `RayitoMicrovmImage`) | `aws-cdk-lib` and `constructs` | [Deploy with CDK](../guias/desplegar-con-cdk.md) |

## Errors { #errores }

`SandboxError` and its subclasses, `AuthenticationError`, `QuotaExceededError`,
`CapacityError`, `UnimplementedError`, `AgentCoreError` (the AgentCore deploy) and
`RayitoCdkError` (from `rayito/cdk`, the CDK constructs). Full table with their Python
equivalent: [Errors](errores.md).

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

## E2B shim (`rayito/e2b`) { #e2b }

`rayito/e2b` re-exports the E2B 2.x JS API: `Sandbox` (also as the
default export), `E2B`, `ConnectionConfig`, `Secret`, `Template`, `Volume`
(experimental), `Git`, `Filesystem`, `Pty`, `Execution`, `Result`, E2B's
errors and `UnimplementedError`. The native sandbox is in
`sbx.native`. Full list: [exports index](#exports) and
[Differences with E2B](../e2b-compat.md#tabla-de-imports).

## Version { #version }

`import { VERSION } from "rayito"` gives the package version.

## Exports index { #exports }

Everything exported by the package's entry points (`rayito/cdk` since
0.15.0). What is not here is private.

### `rayito`: classes, functions and constants { #rayito-clases-funciones-y-constantes }

| Area | Exports |
|---|---|
| Sandbox | `Sandbox`, `SandboxListPaginator`, `LambdaMicrovmsControlPlane`, `PortSpec`, `VERSION` |
| Subclients and handles | `Commands`, `CommandHandle`, `Filesystem`, `WatchHandle`, `UploadTicket`, `DownloadLink`, `Pty`, `PtyHandle`, `Git`, `CodeClient`, `Agent`, `AgentStream` |
| Models | `Execution`, `Result`, `FileType`, `FilesystemEventType`, `EgressEnforcement`, `HostAccess`, `S3Prefix`, `ALL_TRAFFIC`, `DEFAULT_PERSIST_TIMEOUT_MS`, `defaultIdlePolicy` |
| Charts | `ChartType`, `ScaleType`, `parseChart` |
| AI agent | `AgentSpec`, `AgentModel`, `AgentLimits`, `AgentPermissions`, `SubAgent`, `McpLocal`, `McpRemote`, `DeepAgents`, `TokenUsage`, `agentFailed`, `bedrockGateway`, `anthropicGateway`, `openaiCompatibleGateway`, `AgentTemplate`, `AGENT_TEMPLATE_RUNTIMES`, `DEFAULT_AGENT_TEMPLATE_BASE`, `DEFAULT_AGENT_TEMPLATE_NAME`, `agentPoolWarmup` |
| AgentCore deploy | `AgentCoreFleet`, `AgentCoreFleetStream` |
| Pool | `SandboxPool`, `InMemoryPoolBackend`, `JsonFilePoolBackend`, `validatePoolConfig` |
| Optional features | `SecretStore`, `SecretCache`, `SecretRef`, `DynamoDbIndex`, `S3Mount`, `LifecycleEvents`, `TelemetryExport`, `OtlpAuth`, `DEFAULT_INTERVAL_S`, `MIN_INTERVAL_S`, `MAX_INTERVAL_S`, `DEFAULT_SERVICE_NAME`, `SecretGateway`, `GatewayStatus`, `GatewayHandle`, `Template`, `ReadyCommand`, `waitForPort`, `waitForUrl`, `waitForProcess`, `waitForFile`, `OptionalStacks`, `Quotas`, `UsageCost`, `Fleet`, `QuotaReport`, `QuotaEntry`, `BulkResult`, `VolumeStore`, `EfsVolume`, `EfsVolumes` (experimental), `CustomDomain` (experimental) |
| Errors | `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `SandboxNotReadyError`, `SandboxStateError`, `SandboxLifetimeError`, `PoolClosedError`, `CommandExitError`, `PersistenceError`, `DiskFullError`, `TransferError`, `FileUploadError`, `RateLimitError`, `GitUpstreamError`, `SecretError`, `SecretNotFoundError`, `SandboxIndexError`, `IndexWriteError`, `MountError`, `VolumeError`, `VolumeMountError`, `VolumeNotFoundError`, `VolumePathNotFoundError`, `BuildError`, `TemplateError`, `StackError`, `WebhookError`, `DashboardError`, `CustomDomainError`, `AgentError`, `AgentCoreError`, `AuthenticationError`, `GitAuthError`, `QuotaExceededError`, `CapacityError`, `UnimplementedError`, `LifecycleUnsupportedError` ([Errors](errores.md)) |

### `rayito`: types { #rayito-tipos }

| Area | Types |
|---|---|
| Sandbox options | `SandboxCreateOptions`, `SandboxConnectOptions`, `ControlPlaneOptions`, `InstanceConnectOptions`, `SandboxListOptions`, `SandboxPaginateOptions`, `SandboxSetTimeoutOptions`, `PauseOptions`, `StaticPauseOptions`, `UpdateNetworkOptions`, `StaticUpdateNetworkOptions`, `StaticMetricsHistoryOptions`, `MetricsHistoryOptions`, `SignedUrlOptions`, `LoggingOption`, `PortLike`, `OnTimeout`, `ListOrder`, `LaunchOptions`, `TransportSettings`, `Logger` |
| Control plane | `ControlPlane`, `CommandSender`, `ListMicrovmsOptions`, `ListMicrovmsPageOptions`, `MicrovmListPage` |
| Modelos | `SandboxInfo`, `SandboxListItem`, `SandboxHealth`, `SandboxMetrics`, `SandboxLifecycle`, `IdlePolicy`, `IdlePolicyInput`, `CommandResult`, `ProcessInfo`, `PtySize`, `EntryInfo`, `FilesystemEvent`, `WriteEntry`, `WriteData`, `CodeContext`, `Logs`, `OutputMessage`, `OutputChunk`, `ExecutionError`, `NetworkPolicyInput`, `NetworkSelector`, `NetworkSelectorContext`, `NetworkState`, `EgressProxyInput`, `S3Staging`, `ResolvedS3Staging`, `TransferStatus`, `TransferDirectionName`, `TransferPhaseName` |
| Charts | `Chart`, `LineChart`, `ScatterChart`, `BarChart`, `BarData`, `PieChart`, `PieData`, `BoxAndWhiskerChart`, `BoxAndWhiskerData`, `PointData`, `SuperChart` |
| Commands, files, PTY and code | `CommandOptions`, `ConnectOptions`, `RequestOptions`, `OutputCallback`, `ReadOptions`, `ReadFormat`, `ReadResult`, `WriteOptions`, `WriteFilesOptions`, `ListOptions`, `RemoveOptions`, `UserOptions`, `WatchOptions`, `EventCallback`, `ExitCallback`, `UploadUrlOptions`, `DownloadUrlOptions`, `WaitOptions`, `PtyCreateOptions`, `PtyConnectOptions`, `PtyDataCallback`, `RunCodeOptions`, `CreateContextOptions`, `ContextLike`, `StdoutCallback`, `ResultCallback`, `ErrorCallback` |
| Git | `GitStatus`, `GitFileStatus`, `GitBranches`, `GitResetMode`, `GitConfigScope`, `GitStatusLabel`, `GitAddOpts`, `GitCloneOpts`, `GitCommitOpts`, `GitConfigOpts`, `GitDangerouslyAuthenticateOpts`, `GitDeleteBranchOpts`, `GitInitOpts`, `GitPullOpts`, `GitPushOpts`, `GitRemoteAddOpts`, `GitRequestOpts`, `GitResetOpts`, `GitRestoreOpts` |
| Persistence | `S3PrefixInit`, `CheckpointFilesOptions`, `CheckpointProgress`, `CheckpointProgressCallback`, `CheckpointResult`, `RestoreFilesOptions`, `RestoreProgress`, `RestoreProgressCallback`, `RestoreResult`, `ReincarnateOptions` |
| Agente de IA | `AgentRunOptions`, `AgentPrepareOptions`, `AgentSpecOptions`, `AgentModelOptions`, `AgentLimitsOptions`, `AgentPermissionsOptions`, `SubAgentOptions`, `McpLocalOptions`, `McpRemoteOptions`, `McpServer`, `ModelProvider`, `PermissionAction`, `ToolPermission`, `DeepAgentsOptions`, `AgentEvent`, `TextDelta`, `Text`, `Reasoning`, `ToolCall`, `StepStarted`, `StepFinished`, `AgentFailed`, `AgentFailedOptions`, `AgentFailureReason`, `Done`, `AgentResult`, `TokenUsageOptions`, `BedrockGatewayOptions`, `AnthropicGatewayOptions`, `OpenAiCompatibleGatewayOptions`, `AgentRuntime`, `RunCommand`, `RunRequest`, `RuntimeFile`, `RuntimeFiles`, `RuntimeState`, `TemplateStep`, `WarmupStep`, `AgentTemplateOptions`, `AgentTemplateBuildOptions`, `AgentTemplateManifest`, `AgentTemplateRuntime` (deprecated), `StrandsRuntime`, `StrandsRuntimeOptions`, `HermesRuntime`, `HermesRuntimeOptions`, `CustomRuntime`, `CustomRuntimeOptions`, `HttpApp`, `HttpAppOptions`, `InstallableRuntime`, `ServableRuntime`, `HostConfig`, `GatewayWiring`, `DirectWiring`, `ModelWiring`, `InstallTarget`, `gatewayWiring`, `directWiring`, `hostConfig`, `installTarget`, `isInstallable`, `isServable`, `AgentProject`, `AgentProjectOptions`, `AgentProjectRunOptions`, `AgentProjectDeployOptions`, `AgentProjectSandboxOptions`, `ServeOptions`, `ServeOptionsInput`, `AgentExport`, `AgentExportOptions`, `AgentDeployment`, `AgentDeploymentStream`, `AgentInvokeOptions`, `DeploymentStatus`, `deployAgent`, `planDeploy`, `DeployAgentOptions`, `DeployConnection`, `AgentDeployOptions`, `AgentDeployOptionsInput`, `AgentDeployPlan`, `AgentDeployStep`, `AgentDeployCostEstimate`, `AgentPlannedSecret`, `DetectOverrides`, `AgentFleet`, `AgentFleetConnectOptions`, `AgentFleetOptions`, `AgentFleetStream`, `FleetMemberStatus`, `FleetStatus`, `ScalingOptions`, `ScalingOptionsInput`, `SessionMode` |
| AgentCore deploy | `AgentCoreOptions`, `AgentCoreDeployPlan`, `AgentCoreStatus`, `AgentCoreDeleteResult` |
| Pool | `PoolConfig`, `ResolvedPoolConfig`, `PoolBackend`, `PoolStats`, `PoolSlotInfo`, `SlotRecord`, `SandboxPoolOptions`, `TakeOptions`, `CloseOptions` |
| Secrets and index | `SecretOptions`, `SecretsInput`, `SecretLike`, `SecretRefOptions`, `SecretCacheOptions`, `SecretStoreOptions`, `SecretInfo`, `SecretPage`, `SecretsManagerApi`, `DynamoDbIndexOptions`, `DynamoDbApi`, `WriteFailurePolicy`, `IndexRecord` |
| Mounts, sizes, events, OTLP and gateway | `S3MountOptions`, `MountStatus`, `SizeInput`, `SizeName`, `SizeRequest`, `ResolvedSize`, `LifecycleEventsOptions`, `DeployWebhooksOptions`, `EventRecord`, `EventKind`, `KillReason`, `WebhookInfo`, `TelemetryExportOptions`, `TelemetryHealth`, `NameStyleOption`, `SecretGatewayOptions`, `AllowRule` |
| Templates | `TemplateSpec`, `BaseImageRef`, `StartSpec`, `ReadyPoll`, `RunStep`, `CopyStep`, `EnvStep`, `WorkdirStep`, `UserStep`, `WireStep`, `BuildOptions`, `BuildClients`, `BuildHandle`, `BuildInfo`, `BuildState`, `BuildStatus` |
| Management dashboard | `QuotasOptions`, `UsageCostOptions`, `BilledOptions`, `FleetOptions`, `BilledCost`, `BilledLine`, `CostEstimate`, `SandboxCostEstimate`, `MemoryUsage`, `QuotaWarning`, `TpsLimit`, `SandboxView`, `ImageSummary`, `ImageVersionSummary`, `BulkPlan`, `BulkProgress`, `BulkItemResult` |
| Optional stacks | `StackComponent`, `StackParameter`, `StackStatus`, `StackArtifact`, `CostStatement`, `DeployPlan`, `DeployAction`, `ParameterChange`, `ParameterPlan`, `DeployTarget`, `StackProvisioner`, `UpdateOutcome`, `OptionalStacksOptions`, `DeployOptions`, `DestroyOptions`, `ParameterChangesOptions` |
| EFS volumes and custom domain (experimental) | `VolumeStoreOptions`, `EfsVolumeOptions`, `VolumeStatus`, `MountState`, `EfsVolumesOptions`, `EfsVolumesDeployOptions`, `EfsNetworkReport`, `NetworkFinding`, `FindingLevel`, `NetworkInspector`, `CustomDomainOptions`, `DeployCustomDomainOptions`, `RegisterRouteOptions`, `CustomDomainRoute` |
| Error options | `AgentErrorOptions`, `BuildErrorOptions`, `MountErrorOptions`, `PersistenceErrorOptions`, `StackErrorOptions`, `TransferErrorOptions`, `VolumeMountErrorOptions` |

### `rayito/e2b` { #rayitoe2b }

| Area | Exports |
|---|---|
| Classes and functions | `Sandbox` (also `export default`), `SandboxPaginator`, `E2B`, `ConnectionConfig`, `Filesystem`, `Pty`, `Git`, `Secret`, `SecretPaginator`, `Template`, `Volume`, `bindVolume`, `getSignature`, `WatchHandle`, `Execution`, `Result`, `FileType`, `FilesystemEventType`, `ChartType`, `ScaleType`, `ALL_TRAFFIC`, `DEFAULT_WATCH_TIMEOUT_MS` |
| Errors (the native classes under E2B's names) | `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `CommandExitError`, `FileUploadError`, `RateLimitError`, `GitUpstreamError`, `SecretError`, `SecretNotFoundError`, `BuildError`, `TemplateError`, `AuthenticationError`, `GitAuthError`, `UnimplementedError`, `NotEnoughSpaceError` (= `DiskFullError`), `ServiceBusyError` (= `CapacityError`) |
| Types | `SandboxOpts`, `SandboxConnectOpts`, `SandboxInstanceConnectOpts`, `SandboxListOpts`, `SandboxInfo`, `SandboxInfoLifecycle`, `SandboxLifecycle`, `SandboxState`, `SandboxMetrics`, `SandboxMetricsOpts`, `SandboxNetworkInfo`, `SandboxNetworkOpts`, `SandboxNetworkSelector`, `SandboxNetworkUpdate`, `SandboxOnResume`, `SandboxOnTimeout`, `SandboxPauseOpts`, `SandboxUrlOpts`, `E2BClientOpts`, `ConnectionOpts`, `Username`, `WatchOpts`, `WatchEventCallback`, `PtyCreateOpts`, `PtyConnectOpts`, `PtyOutputCallback`, `PtySize`, `SecretInfo`, `SecretConnectionOpts`, `SecretCreateOpts`, `SecretDestroyOpts`, `SecretExistsOpts`, `SecretGetInfoOpts`, `SecretListOpts`, `SecretUpdateOpts`, `CommandHandle`, `CommandResult`, `ProcessInfo`, `Context` (= `CodeContext`), `EntryInfo`, `WriteInfo` (= `EntryInfo`), `WriteEntry`, `ExecutionError`, `FilesystemEvent`, `Logs`, `OutputMessage`, `Chart`, `LineChart`, `ScatterChart`, `BarChart`, `BarData`, `PieChart`, `PieData`, `BoxAndWhiskerChart`, `BoxAndWhiskerData`, `SuperChart`, `PointData`, `GitStatus`, `GitFileStatus`, `GitBranches`, `GitResetMode`, `Logger` |

### `rayito/cdk` { #rayitocdk }

| Area | Exports |
|---|---|
| Constructs and factories | `RayitoAgent`, `AgentImageBuild`, `RayitoMicrovmImage`, `MicrovmImageArtifact` |
| Errors | `RayitoCdkError` |
| Types | `RayitoAgentProps`, `CodeBuildOptions`, `RayitoMicrovmImageProps`, `FromFileOptions`, `DownloadReleaseOptions` |
