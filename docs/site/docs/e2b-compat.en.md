# E2B compatibility

`rayito.e2b` (Python) and `rayito/e2b` (TypeScript) are an import-level
drop-in for the E2B **2.x** SDKs (contract: `e2b` 2.51.0 and
`e2b-code-interpreter` 2.10.0). Change the import line and the rest of the
program stays the same. What exists and what does not, row by row, is in
[E2B parity](e2b-parity.md); this page explains how to migrate and where
behavior differs.

!!! note "Trademarks"
    E2B is a trademark of its owner. Rayito is an independent project, not
    affiliated with, sponsored or endorsed by E2B; the name is used only to
    describe which SDK the `rayito.e2b` / `rayito/e2b` API is compatible with.

## Migrate: change one import { #migrar-cambia-un-import }

=== "Python"

    ```python
    # before
    # from e2b_code_interpreter import Sandbox
    from rayito.e2b import Sandbox

    with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)            # "2"
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        sbx.files.write("/home/user/a.txt", "a")
        sbx.set_timeout(600)                         # rayd moves the deadline
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito.e2b import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=300) as sbx:
            print((await sbx.run_code("1 + 1")).text)
            await sbx.set_timeout(600)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    // before: import { Sandbox } from "@e2b/code-interpreter";
    import { Sandbox } from "rayito/e2b";

    await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hello")).stdout);
    await sbx.setTimeout(600_000);
    ```

What changes outside the code:

1. **AWS credentials instead of `E2B_API_KEY`.** The SDK uses boto3's /
   AWS SDK v3's default chain (`AWS_PROFILE=<your-profile>`, `AWS_REGION`);
   `api_key`, `domain` and the like are accepted and ignored with a
   `RayitoCompatWarning`.
2. **A 0.3.0 or later image in your account** instead of E2B's template:
   `RAYITO_TEMPLATE=rayito-base` (or `template=`). For JS/TS you need
   `rayito-base-poly` and for `allow_internet_access=False`/`network=`,
   `rayito-base-caps` ([Images and IAM](images.md)).
3. **`upload_url`/`download_url`** need a bucket of your own:
   `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` ([Files](files.md)).

!!! warning "The shims require a 0.3.0 or later image"
    The shim always asks `rayd` for a logical deadline. Against an image
    older than 0.3.0, `create()` terminates the VM and raises
    `UnimplementedError("lifecycle")` asking for a new image. Publish
    `rayito-base` from this tree (`rayito image publish`) before migrating.

## Import table { #tabla-de-imports }

=== "Python"

    | Before | After |
    |---|---|
    | `from e2b_code_interpreter import X` | `from rayito.e2b import X` |
    | `from e2b import X` | `from rayito.e2b import X` |
    | `from e2b.exceptions import X` | `from rayito.e2b.exceptions import X` (the exceptions are also exported from `rayito.e2b`) |

    Names exported by `rayito.e2b` (its `__all__`):

    | Group | Names |
    |---|---|
    | Sandbox and client | `Sandbox`, `AsyncSandbox`, `E2B`, `ConnectionConfig`, `SandboxInfo`, `SandboxMetrics`, `SandboxQuery`, `SandboxState`, `SandboxPaginator`, `AsyncSandboxPaginator`, `ListedSandbox`, `ALL_TRAFFIC` |
    | Commands and PTY | `CommandHandle`, `AsyncCommandHandle`, `CommandResult`, `ProcessInfo`, `PtySize`, `PtyOutput`, `Stdout`, `Stderr`, `OutputHandler`, `Username` |
    | Files | `EntryInfo`, `WriteInfo`, `WriteEntry`, `FileType`, `FilesystemEvent`, `FilesystemEventType`, `WatchHandle`, `AsyncWatchHandle`, `UploadTicket`, `DownloadLink` |
    | Git | `Git`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode` ([Git](git.md)) |
    | Code interpreter | `Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`, `Context`, `MIMEType`, `RunCodeLanguage` and the charts `ChartType`, `ScaleType`, `Chart`, `Chart2D`, `PointData`, `LineChart`, `ScatterChart`, `BarChart`, `BarData`, `PieChart`, `PieData`, `BoxAndWhiskerChart`, `BoxAndWhiskerData`, `SuperChart` |
    | Secrets (CRUD over AWS Secrets Manager, see [Secrets](#secretos-secret-asyncsecret)) | `Secret`, `AsyncSecret`, `SecretInfo`, `SecretPaginator`, `AsyncSecretPaginator` |
    | Exceptions | `SandboxException`, `TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`, `InvalidArgumentException`, `AuthenticationException`, `RateLimitException`, `CommandExitException`, `NotEnoughSpaceException`, `ServiceBusyException`, `FileUploadException`, `GitAuthException`, `GitUpstreamException`, `TemplateException`, `BuildException` (the native ones raised by `Template.build()`; unlike E2B, `BuildException` is a `SandboxException`), `SecretException`, `SecretNotFoundException`, `VolumeException`, `VolumeNotFoundException`, `VolumePathNotFoundException` |
    | Declarative templates (since 0.6.0, see [Templates](funciones-opcionales/templates.md)) | `Template`, `AsyncTemplate` really build; `alias_exists`/`assign_tags`/`remove_tags`/`get_tags` still have no equivalent |
    | EFS volumes (since 0.7.0, experimental, see [EFS volumes](funciones-opcionales/volumenes-efs.md#shim-e2b-e2bvolume_store)) | `Volume`, `AsyncVolume` do real CRUD with `E2B(volume_store=...)`; without it, and on content operations, they raise |
    | No primitive (they import, but every call raises) | `get_signature` |
    | Rayito only | `UnimplementedError`, `RayitoCompatWarning` |

=== "TypeScript"

    | Before | After |
    |---|---|
    | `import { Sandbox } from "@e2b/code-interpreter"` | `import { Sandbox } from "rayito/e2b"` |
    | `import Sandbox from "e2b"` | `import Sandbox from "rayito/e2b"` (default export) |
    | `import { Sandbox, X } from "e2b"` | `import { Sandbox, X } from "rayito/e2b"` |

    Names exported by `rayito/e2b`:

    | Group | Names |
    |---|---|
    | Sandbox and client | `Sandbox` (also default), `SandboxPaginator`, `E2B`, `ConnectionConfig`, `ALL_TRAFFIC`; types `SandboxOpts`, `SandboxConnectOpts`, `SandboxInstanceConnectOpts`, `SandboxInfo`, `SandboxInfoLifecycle`, `SandboxMetrics`, `SandboxMetricsOpts`, `SandboxLifecycle`, `SandboxOnTimeout`, `SandboxOnResume`, `SandboxNetworkOpts`, `SandboxNetworkInfo`, `SandboxNetworkSelector`, `SandboxNetworkUpdate`, `SandboxListOpts`, `SandboxPauseOpts`, `SandboxUrlOpts`, `SandboxState`, `ConnectionOpts`, `E2BClientOpts`, `Username`, `Logger` |
    | Commands, files and PTY | `Filesystem`, `Pty`, `FileType`, `FilesystemEventType`, `DEFAULT_WATCH_TIMEOUT_MS`; types `CommandHandle`, `CommandResult`, `EntryInfo`, `WriteInfo`, `FilesystemEvent`, `WatchOpts`, `WatchEventCallback`, `PtySize`, `PtyCreateOpts`, `PtyConnectOpts`, `PtyOutputCallback` |
    | Git | `Git`; types `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode` ([Git](git.md)) |
    | Code interpreter | `Execution`, `Result`; types `Context`, `Logs`, `OutputMessage`, `ExecutionError` |
    | Secrets (CRUD over AWS Secrets Manager, see [Secrets](#secretos-secret-asyncsecret)) | `Secret`, `SecretPaginator`; types `SecretInfo`, `SecretCreateOpts`, `SecretUpdateOpts`, `SecretGetInfoOpts`, `SecretExistsOpts`, `SecretDestroyOpts`, `SecretListOpts`, `SecretConnectionOpts` |
    | Errors (`rayito`'s native classes: `instanceof` works across both) | `SandboxError`, `TimeoutError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `InvalidArgumentError`, `AuthenticationError`, `RateLimitError`, `CommandExitError`, `NotEnoughSpaceError` (= `DiskFullError`), `ServiceBusyError` (= `CapacityError`), `FileUploadError`, `GitAuthError`, `GitUpstreamError`, `TemplateError`, `BuildError`, `UnimplementedError`, `SecretError`, `SecretNotFoundError` |
    | Declarative templates (since 0.6.0, see [Templates](funciones-opcionales/templates.md)) | `Template` really builds; `aliasExists`/`assignTags`/`removeTags`/`getTags` still have no equivalent |
    | EFS volumes (since 0.7.0, experimental, see [EFS volumes](funciones-opcionales/volumenes-efs.md#shim-e2b-e2bvolume_store)) | `Volume` (and `bindVolume`, which binds it to a `VolumeStore`) does real CRUD with `new E2B({ volumeStore })`; without it, and on content operations, it raises. The volume errors (`VolumeError`, `VolumeNotFoundError`, `VolumePathNotFoundError`) are not re-exported here: import them from `rayito` |
    | No primitive (they import, but every call raises) | `getSignature` |

    `rayito/e2b` is an entry point of the same `rayito` npm package (ESM and
    CommonJS): nothing else needs installing.

## Defaults { #valores-por-defecto }

The shims follow **E2B**'s defaults, not the native SDK's: `timeout=300` is
the sandbox's logical deadline with `on_timeout='kill'` (enforced by `rayd`,
[below](#plazo-y-ciclo-de-vida)), no idle auto-suspend, public endpoint
(`ALL_INGRESS`) and internet egress (`INTERNET_EGRESS`). The platform cap
(`max_lifetime`, a Rayito-only kwarg; TS `maxLifetimeMs`) is `max(3600,
min(timeout + 60, 28800))` if you do not pass it. If you want Rayito's
defaults (auto-suspend after 300 s of inactivity, explicit connectors), use
`rayito.Sandbox`; the native one is always at `sbx.native`. What Lambda
MicroVMs cannot do raises `UnimplementedError` (a `NotImplementedError`,
never a `SandboxException`; it is the same class as
`rayito.UnimplementedError`) with the feature and the reason: nothing is
approximated silently.

## Works unchanged { #funciona-sin-cambios }

| E2B | Rayito |
|---|---|
| `with Sandbox.create() as sbx:` / `async with await AsyncSandbox.create()` / `await Sandbox.create()` | same (`RAYITO_TEMPLATE` instead of E2B's default template) |
| `sbx.run_code(code)` → `Execution` with `text`, `results`, `logs.stdout`, `error`; charts (`Result.chart`, `png`) | same |
| `run_code(language="python" / "bash")`, `create_code_context`, `list_code_contexts`, `remove_code_context`, `restart_code_context` | same; `bash` only on `rayito-base-poly` |
| `sbx.commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, stdin, timeout, request_timeout)` (also positional), `commands.list/kill/connect/send_stdin/close_stdin`, `CommandHandle.wait(on_pty, on_stdout, on_stderr)` | the native `Commands` |
| `sbx.files.read/write/write_files/list/exists/get_info/remove/rename/make_dir`, `files.watch_dir(path, on_event=...)` | same |
| `sbx.pty.create(PtySize(rows, cols), on_data=...)`, `send_stdin`, `resize`, `kill`, `connect(pid)` | same (`PtySize` in E2B's order) |
| `sbx.get_host(port)` | a `str` with the hostname plus `.headers` with the proxy headers (TS: synchronous `getHost(port)` returns the hostname) |
| `sbx.kill()`, `Sandbox.kill(id)`, `sbx.is_running(request_timeout=)`, `sbx.sandbox_id`, `sbx.sandbox_domain` | same |
| `sbx.pause()` / `beta_pause()` → `bool`, `Sandbox.connect(id)` on a paused sandbox | `suspend-microvm` and `resume-microvm` |
| `Sandbox.list(query=SandboxQuery(metadata=...))` on `RUNNING` sandboxes | client-side filter, O(n) ([cost](#el-coste-de-listquerysandboxquerymetadata)) |
| The exceptions of `e2b.exceptions` (`TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`, `CommandExitException`, `GitAuthException`, ...) | the same native classes under those names; `NotEnoughSpaceException` is `DiskFullException` and is raised when the disk is full; `ServiceBusyException` is raised on `InsufficientCapacityException`; since 0.6.0, `Template.build()` really raises `TemplateException` and `BuildException`: those of `rayito.e2b` (Python) and `rayito/e2b` (TS) are the same native classes, so `except`/`instanceof` works with either import ([Templates](funciones-opcionales/templates.md)) |

## Maps, with a note { #se-mapea-con-una-nota }

| E2B | Rayito |
|---|---|
| `timeout` (300 s by default) | logical deadline enforced by `rayd` under `max_lifetime` (3600 by default, ≤ 28800; Rayito-only kwarg): `max(3600, min(timeout + 60, 28800))` if you do not pass it. On expiry, `rayd` exits and the VM goes to `TERMINATED` ≈ 15 s later (Q58) |
| `sbx.set_timeout(t)` / `Sandbox.set_timeout(id, t)` | `SetTimeout` EXACT: extends or shortens the deadline, up to `max_lifetime`; beyond that, `InvalidArgumentException` with the deadline untouched. The class form needs the access token (`access_token=` or `RAYITO_ACCESS_TOKEN`) |
| `Sandbox.connect(id, timeout)` / `sbx.connect(timeout)` | resumes if needed and **only extends** the deadline (`AT_LEAST`, like E2B); the class form needs the access token, which E2B does not have |
| `lifecycle={"on_timeout": ..., "auto_resume": ...}` | `on_timeout='kill'` exact; `'pause'` suspends on expiry with a live client and, without a client, the idle policy does it at `max_idle` (300 s); `auto_resume=True` resumes with a new deadline of `max(timeout, 300 s)`, `False` has `rayd` enforce it |
| `Sandbox.beta_create(auto_pause=True)` | `lifecycle={"on_timeout": "pause"}` (E2B removed it in 2.23.0; it is kept) |
| `TimeoutException` | also when a call runs into the deadline (`sandbox_timeout`) |
| `sbx.upload_url(path, user, use_signature_expiration)` / `uploadUrl` | S3 URL presigned with **your** credentials ([Files](files.md)): `PUT` with the raw body (`form=True` gives the fields of a `POST`); the file lands asynchronously (barrier in `rayd` and `ticket.wait()`); single use; expiry always set (≤ 7 days and ≤ the life of your credentials); `use_signature_expiration <= 0` raises; requires a transfers bucket: the shim does not accept `transfer=`, so it is configured with `RAYITO_TRANSFER_BUCKET` (and `RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`); without it `UnimplementedError`; `upload_url(path=None)` raises. Python returns the native `UploadTicket` (a `str` with `.headers` and `wait()`); TS returns the URL as a `string` (`sbx.native.files.uploadUrl()` gives the ticket with `wait()`) |
| `sbx.download_url(...)` / `downloadUrl` | presigned `GET` URL (supports `Range`) over a snapshot taken at call time; a file that does not exist raises at call time; in the async shim both methods are coroutines |
| `files.read(gzip=)`, `files.write(gzip=)`, `metadata=`, `stream_idle_timeout=`, `use_octet_stream=`; JS `format: "blob"` | gRPC gzip (responses ask for it with the `rayito-compress` header); `metadata` as `user.rayito.*` xattrs (lowercase keys, overwriting replaces the set); `use_octet_stream` is accepted with no effect (gRPC has no multipart form) |
| `sbx.get_metrics(start, end)` → `[SandboxMetrics]` in bytes, with `mem_cache` | `rayd`'s history (one sample every 5 s, 8 h ring, **gap** while suspended; [Observability](observability.md)); without `start`/`end`, an empty history or an image older than 0.3.0 give the `get_metrics()` snapshot as the only point; with a range on an image older than 0.3.0, `UnimplementedError` |
| `Sandbox.get_metrics(id, start, end)` | needs the sandbox's access token; without it, `UnimplementedError` without calling anything |
| `Sandbox.list(limit=, next_token=, query=SandboxQuery(state=, started_after=, template=), order=)` → `SandboxPaginator` | `next_token` is an opaque cursor over `list-microvms`' `nextToken`; `order` is computed client-side (walks all the pages before the first item); a resumed token skips by identity; `state=[PAUSED]` = AWS's `SUSPENDING`/`SUSPENDED` |
| `allow_internet_access=False` / `allowInternetAccess: false` | deny-all policy **inside the guest** on `rayito-base-caps` ([Outbound network](network.md)): no connection leaves the VM and, since 0.3.2, DNS does not resolve either for the sandbox's processes; on any other image the shim terminates the VM and raises `UnimplementedError` (without an egress connector the MicroVM still reaches out: `AWS_API_NOTES.md` Q44, Q60) |
| `network={"allow_out", "deny_out", "egress_proxy"}` (CIDR, IP, `ALL_TRAFFIC`, `*.domain`, callable selectors) | E2B's semantics on `rayito-base-caps`: per-uid routes and, for host names or `egress_proxy`, a local `rayd` proxy; clients that do not honor `HTTPS_PROXY` fail closed |
| `sbx.update_network(network)` / `Sandbox.update_network(id, network)` | `NetworkService.UpdateNetwork`: changes the guest's policy atomically for new connections; returns `None` |
| `network.https_ports` | validated (1–65535); a non-empty list is `UnimplementedError`: measured, the proxy does not forward end-to-end TLS to a guest port (row Q67 of `AWS_API_NOTES.md`); `get_host(port)` serves plain HTTP |
| `network.allow_public_traffic=False` | accepted with no effect: it is the permanent behavior |
| `run_code(language="javascript" / "js" / "typescript" / "ts")` | the Deno kernel of `rayito-base-poly` ([Kernels](kernels.md)); on another image `UnimplementedError` naming `rayito-base-poly` |
| `sbx.git` (`clone`, `status`, `commit`, `push`, `pull`, `dangerously_authenticate`, ...) | E2B's git API over `commands.run` with the image's `git-core` ([Git](git.md)); the credentials of `dangerously_authenticate` stay in `~/.git-credentials`, **visible to the sandbox's code** |
| `sbx.get_info()` / `Sandbox.get_info(id)` → 2.x `SandboxInfo` | `sandbox_domain` = endpoint; `cpu_count`/`memory_mb` as the guest sees them; `envd_version` = `agent_version`; `end_at` = the logical deadline; `lifecycle`, `network` and `allow_internet_access` filled in; `volume_mounts` always empty |
| `sbx.traffic_access_token` (TS `trafficAccessToken`), `envd_api_url`, `envd_direct_url` (Python only) | the current `x-aws-proxy-auth` JWE (it rotates, TTL ≤ 60 min: it is a bearer credential) and `https://<endpoint>`; every request needs the proxy headers |
| `metadata`, `envs` | travel in `runHookPayload` (4096 characters between the two); they are not secrets |
| `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`, `validate_api_key`, `api_headers`, `secure=False` | ignored with a `RayitoCompatWarning` each: the credentials are AWS's |
| `proxy=` | honored (only `http://host:port`) on the gRPC channels and in botocore |
| `headers=`, `retries=`, `ConnectionConfig.set_integration`, `sbx.connection_config` | extra headers on each RPC (the proxy's, the token's and gRPC's are reserved), botocore retries and `user_agent_extra` |
| `headers=`, `proxy=`, `retries=` on `sbx.kill()`, `sbx.pause()` or `sbx.connect()` (instance calls) | 0.4.0: `RayitoCompatWarning` for each one (before, they were lost silently); `sbx.<call>()` operates on this sandbox's already-built channel and plane and cannot rebuild them, unlike `Sandbox.<call>(sandbox_id, ...)`, which does apply them; `sbx.pause()`/`sbx.kill()` also warn about `request_timeout=` (the native one does not accept it there), `sbx.connect(request_timeout=)` does apply it; `headers={}` does not warn, and the warnings come out in alphabetical order (same in TS) |
| TS: any `ConnectionOpts` key on an instance call that method does not apply | 0.4.0: `RayitoCompatWarning` for each one, same reason as Python. Covers `kill`, `pause`, `getInfo`, `isRunning`, `setTimeout`, `connect`, `getMetrics` and `updateNetwork`; only `signal` is always applied. `kill`/`pause` also warn about `requestTimeoutMs` (the native one does not accept it there); `getInfo`/`isRunning`/`setTimeout`/`connect`/`getMetrics`/`updateNetwork` do apply it. The remaining connection-specific keys (`headers`, `proxy`, `retries`, `logger`, `region`, `controlPlane`, `accessToken`, `transport`) always warn when they appear on the instance call (empty `headers`, `{}`, do not warn); the static variants (`Sandbox.<method>(sandboxId, opts)`) apply them all, as before |
| `logger=` | that sandbox's SDK logs go to the given `logging.Logger` (TS: a `Logger` with `debug`/`info`/`warn`/`error`), different from the native `logging=`, which is CloudWatch |
| JS `sbx.getHost(port)` | synchronous, returns the hostname like E2B; the proxy headers every request needs come from `await sbx.getHostHeaders(port)` |
| JS `signal` (`AbortSignal`) in `ConnectionOpts` | cancels the control-plane calls and in-flight RPCs; rejects with `signal.reason` |
| `E2B(...)` (bound client) | binds `region`, `session` and `control_plane`; `.Secret`/`.AsyncSecret` (TS: `.Secret`) use that `region` and `session` against Secrets Manager; since 0.6.0 `.Template`/`.AsyncTemplate` are the real class; since 0.7.0 `.Volume`/`.AsyncVolume` do real CRUD with `volume_store=` (without it, they raise) |
| Native kwargs (`region`, `session`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `control_plane`, `transport`, ...) | passed as is; `idle`, `egress` and `pool` are not accepted (`TypeError`) |

## Raises `UnimplementedError` { #lanza-unimplementederror }

The message is always `<feature> no está disponible en Rayito: <reason>. Ver
docs/site/docs/e2b-compat.md` (the SDK's messages are in Spanish), and the
error carries `feature`, `reason` and `doc`. Python raises on call; in
TypeScript an async method returns the rejected promise. The keys and
reasons in this table are those of
`clients/python/src/rayito/e2b/_unimplemented.py` and
`clients/typescript/src/e2b/unimplemented.ts`, identical in both SDKs. The
reasons are literal strings in Spanish in the code; here they are
translated:

| Feature (Python) | Feature (TypeScript) | Reason, translated |
|---|---|---|
| `fork`, `create_snapshot`, `list_snapshots`, `delete_snapshot` | `fork`, `createSnapshot`, `listSnapshots`, `deleteSnapshot` | no Lambda MicroVMs operation copies the memory of a running MicroVM (AWS_API_NOTES.md §1 and §15); the files-only analogue is checkpoint_files() + create(persist=) |
| `connect(on_resume='reboot')` | `connect({ onResume: 'reboot' })` | resume-microvm always restores memory and disk (AWS_API_NOTES.md §5); the analogue is reincarnate(), with a new id |
| `pause(keep_memory=False)`, `lifecycle.on_timeout.keep_memory=False` | `pause({ keepMemory: false })`, `lifecycle.onTimeout.keepMemory=false` | suspend-microvm always saves memory and disk (AWS_API_NOTES.md §5); the analogue is checkpoint_files() + kill() |
| `network.rules` | `network.rules` | there is no egress proxy outside the VM where headers could be injected: the Lambda MicroVMs proxy only handles ingress (AWS_API_NOTES.md §7) |
| `network.mask_request_host` | `network.maskRequestHost` | the Lambda MicroVMs proxy always forwards Host: &lt;endpoint&gt; and does not rewrite it (AWS_API_NOTES.md §7) |
| `network.allow_public_traffic=True` | `network.allowPublicTraffic=true` | unauthenticated access does not exist: every request to the endpoint requires X-aws-proxy-auth (AWS_API_NOTES.md §3 and §7); allow_public_traffic=False is the permanent behavior |
| `iam` | `iam` | MicroVMs do not issue tokens with an audience: the only identity is the execution role through IMDSv2 (AWS_API_NOTES.md §9) |
| `mcp`, `get_mcp_url`, `get_mcp_token` | `mcp`, `getMcpUrl`, `getMcpToken` | every request to the endpoint also needs a JWE in a header with a TTL of 60 min at most (AWS_API_NOTES.md §3 and §7), so a URL with a fixed token does not work; use the rayito-mcp server |
| `volume_mounts`, `Volume` (and `AsyncVolume`) without `volume_store=`/`volumeStore` | `volumeMounts`, `Volume` | without a volume_store/volumeStore configured on the E2B client there is no volume: pass E2B(volume_store=VolumeStore(...)) (and volume_connector_arn= for volume_mounts, on the optional image with amazon-efs-utils, AWS_API_NOTES.md §22; m15-efs-volumes, experimental), or use persist= (S3) or upload_url/download_url |
| `volume.content`: `read_file`/`write_file`/`make_dir`/`list_files`/`remove`/`update_metadata` of a `Volume` | `volume.content`: `readFile`/`writeFile`/`makeDir`/`list`/`remove`/`updateMetadata` | there is no file data plane outside a MicroVM (SPEC.md §4); connect a sandbox and mount the volume, or use upload_url/download_url over persist= |
| `get_signature` | `getSignature` | an envd signature does not authenticate at the proxy: the JWE only travels in a header or in the WebSocket subprotocol (AWS_API_NOTES.md §7); use upload_url/download_url, which sign on S3 |
| `Template.alias_exists`/`assign_tags`/`remove_tags`/`get_tags` (and on `AsyncTemplate`) | `Template.aliasExists`/`assignTags`/`removeTags`/`getTags` | create/update-microvm-image does not support per-version tags (only per image, with lambda:TagResource separately, AWS_API_NOTES.md §27): use the image's ARN with the AWS CLI in the meantime |

!!! note "`agent`: not an entry in this table"
    The 0.8.0 AI agent (`sbx.agent`) is not E2B API, so `rayito.e2b.Sandbox`
    has no `agent` attribute: touching it is a normal Python
    `AttributeError` (in TypeScript, a type error), not an
    `UnimplementedError`, and there is no `agent` key in
    `_unimplemented.py` or `unimplemented.ts`. Use the native sandbox the
    shim carries inside: `sbx.native.agent.run(...)` (same in TypeScript), or
    `rayito.Sandbox.connect(sandbox_id)` from another process. The sandbox
    needs the agent's image and gateway: see
    [Agent in the sandbox](guias/agente-en-el-sandbox.md).

Since 0.6.0, `Template`/`AsyncTemplate` (and `E2B(...).Template`/
`.AsyncTemplate`) really build images: see
[Templates](funciones-opcionales/templates.md). `E2B(...).Volume` without
`volume_store=` raises the same as `Volume`; `Secret.iam_token`
(TS: `Secret.iamToken`) raises that of `iam`. The following cases depend on
the image or the configuration and carry their own reason (Python text;
TypeScript uses the same reason with the feature names in camelCase, except
for the differences listed after the table):

| Case | Feature | Reason, translated |
|---|---|---|
| `run_code(language=...)` / `create_code_context(language=...)` with anything other than `python`, `bash`, `javascript`/`js`, `typescript`/`ts` (`r`, `java`) | `run_code(language='r')` | available kernels: python on every image; bash, javascript and typescript on the rayito-base-poly variant; R and Java no (SPEC.md §4) |
| a kernel the image does not carry (`bash`, `javascript` or `typescript` outside `rayito-base-poly`) | `run_code(language='typescript')` | this kernel only exists in the rayito-base-poly variant (publish it with make image-publish-poly and use it as the template) |
| `Sandbox.list(query=SandboxQuery(metadata=...), state=[PAUSED])` **without** `index=` (TS: `list({ query: { metadata, state: ["paused"] } })` without `index`; with it, it maps, see [below](#el-coste-de-listquerysandboxquerymetadata)) | `list(state=PAUSED, query.metadata)` (TS: `list(query.state=paused, query.metadata)`) | the metadata lives in the agent; reading it would wake the sandbox. Pass index=DynamoDbIndex(...) (optional table in your account, see optional-features.md) to filter paused sandboxes by metadata without waking them (TS: `index: new DynamoDbIndex({...})`) |
| `allow_internet_access=False` or `network=` outside `rayito-base-caps` (the VM is terminated before launching) | `allow_internet_access=False` / `network` | the image does not apply an egress policy in the guest (Health.egress_enforcement=NONE): use a 0.3.0 or later image of rayito-base-caps (additionalOsCapabilities ALL) or, at platform level, rayito.Sandbox.create(egress=[&lt;ConnectorArn from infra/egress-connector.yaml&gt;]); the sandbox has been terminated |
| `update_network` outside `rayito-base-caps` | `update_network` | the image has no CAP_NET_ADMIN: the egress policy requires a 0.3.0 or later image of rayito-base-caps (additionalOsCapabilities ALL) |
| non-empty `network.https_ports` | `network.https_ports` | the Lambda MicroVMs proxy does not forward end-to-end TLS to a guest port (measured, row 67 QE2 of AWS_API_NOTES.md §16); get_host(port) serves plain HTTP |
| `upload_url`/`download_url` without a transfers bucket | `upload_url` / `download_url` | configure transfer=S3Staging(...) or RAYITO_TRANSFER_BUCKET (TS: configure transfer: { bucket } (S3Staging) or RAYITO_TRANSFER_BUCKET) |
| an upload or download URL against a `rayd` older than 0.3.0 (no transfers) | `upload_url` / `download_url` | update the image: this rayd has no transfers |
| the metrics history through the class form, without an access token or `RAYITO_ACCESS_TOKEN` | `Sandbox.get_metrics(sandbox_id)` | rayd requires the sandbox's access token (x-access-token): pass it with access_token= or define RAYITO_ACCESS_TOKEN |
| the metrics history with a range (`start`/`end`) or through the class form, against an image older than 0.3.0 | `get_metrics(start=, end=)` / `Sandbox.get_metrics(sandbox_id)` (TS: `getMetrics({ start, end })` / `Sandbox.getMetrics(sandboxId)`) | the image is older than 0.3.0 (rayd without MetricsHistory): publish a 0.3.0 or later image |
| any `create()` against an image older than 0.3.0 (the VM is terminated before launching) | `lifecycle` | the image does not enforce the server-side timeout: publish a 0.3.0 or later image |
| per-sandbox `cpu`/`memory` | — | they are not `create()` parameters: the size is a property of the image, just as `Template.build(cpu_count=, memory_mb=)` in E2B; the native SDK picks among already published images with `size=` ([Sizes](funciones-opcionales/tamanos.md), [Limits](limits.md#tamano-cpuram)) |
| E2B's snapshots/fork CLI | — | they do not exist: out of scope (`SPEC.md` §4); the templates one is `rayito template` ([CLI](cli.md#rayito-template)) |

The native SDK follows the same rule: `get_metrics_history()` (TS
`getMetricsHistory()`), on the instance and in the class form, against a
`rayd` older than 0.3.0 raises `UnimplementedError` with the same reason
(feature `get_metrics_history` / `Sandbox.get_metrics_history(sandbox_id)`;
TS `getMetricsHistory` / `Sandbox.getMetricsHistory(sandboxId)`), like the
transfers and the server-side deadline; the generic `UnimplementedError`
from the unary table stays in `__cause__` (TS `cause`), and the gRPC
`UNIMPLEMENTED` error one level below, in `__cause__.__cause__` (TS
`cause.cause`). Without a range, the shim's `get_metrics()` still falls back
to the snapshot.

Real differences between the two shims:

- **`list` with `paused` and `metadata`.** Both raise `UnimplementedError`
  with the same reason before touching AWS; the feature is
  `list(state=PAUSED, query.metadata)` in Python and
  `list(query.state=paused, query.metadata)` in TS.
- **Class-form `getMetrics` without a token.** Without `accessToken` or
  `RAYITO_ACCESS_TOKEN`, TS raises `AuthenticationError` (the same error as
  the native SDK's `Sandbox.getMetricsHistory(sandboxId)`), not
  `UnimplementedError`: in TS a missing credential is an authentication
  error on purpose. Python raises `UnimplementedError` with the reason from
  the table. In both cases AWS is not called.
- **Volume errors.** Python exports them from `rayito.e2b`
  (`VolumeException`, `VolumeNotFoundException`,
  `VolumePathNotFoundException`, the native ones); TS does not re-export them
  from `rayito/e2b`: import them from `rayito` (`VolumeError`...).
- **`UnimplementedError` message.** `feature` and `reason` match; the text of
  `str(error)` / `error.message` differs in the link phrase (Python: "no está
  disponible en Rayito"; TS: "no está disponible"), so compare `feature` and
  `reason`, not the message.

<a id="diferencias-por-cambio-de-m9"></a>

## Differences by area { #diferencias-por-area }

<a id="plazo-y-ciclo-de-vida-m9-server-timeout"></a>

### Deadline and lifecycle { #plazo-y-ciclo-de-vida }

- The `max_lifetime` cap counts suspended time: 8 h at most from the start.
  E2B keeps paused sandboxes without limit.
- In `pause` mode, processes keep running between the deadline and the
  suspension (≈ 3 s with a live client, up to `max_idle` without one), and
  the idle policy may suspend before the deadline: a detached job freezes
  after `max_idle` without traffic.
- After an exit in `kill` mode, ≈ 15 s of 502 are billed (Q58).
- The deadline never goes past `max_lifetime − 60 s` from the start.
- A real suspension of less than 2 s that crosses the deadline is not
  recognized as a freeze.
- Beyond `max_lifetime` only `rayito.Sandbox.reincarnate()` remains
  ([Persistence](persistence.md)).

<a id="ficheros-y-urls-m9-file-transfer"></a>

### Files and URLs { #ficheros-y-urls }

- Upload by `PUT` with the raw body instead of envd's multipart `POST`
  (`form=True` gives a `POST`'s fields for browsers).
- The upload lands asynchronously: a later read, command or cell waits for it
  up to 2 s (a `rayd` barrier); `ticket.wait()` waits for all of it.
- Single-use tickets; the download is a snapshot taken at call time (an
  export that is re-queued after a suspension reads the file again).
- Expiry always set; a transfers bucket is required.

<a id="red-saliente-m9-egress-policy"></a>

### Outbound network { #red-saliente }

- Only on `rayito-base-caps`; on other images it fails closed.
- Host-name rules only apply on 80/443 and through the local proxy; clients
  that do not honor it fail closed.
- Under deny-all (`allow_internet_access=False`) no connection leaves the VM
  and, since 0.3.2, DNS for the sandbox's processes is also blocked;
  UDP/QUIC do not go through the proxy; a change affects new connections;
  root is not filtered. Details in [Outbound network](network.md).

<a id="kernels-m9-deno-kernels"></a>

### Kernels { #kernels }

- `javascript` and `typescript` are the Deno 2.9.7 kernel on
  `rayito-base-poly`, with lazy start; `r` and `java` remain out.

<a id="observabilidad-m9-sandbox-observability"></a>

### Observability { #observabilidad }

- The metrics history has a gap while the sandbox is suspended and
  `memory_mb` is the guest's view.
- `order` and `list`'s filters are computed client-side.

<a id="superficie-2x-m9-e2b-v2-surface"></a>

### 2.x surface { #superficie-2x }

- `git` is a client-side wrapper over `commands.run` (E2B marks it as
  deprecated).
- `traffic_access_token` is the proxy JWE, which rotates every 45 min.

## Secrets (`Secret`, `AsyncSecret`) { #secretos-secret-asyncsecret }

Since 0.5.0, `Secret`/`AsyncSecret` (TS: `Secret`) no longer raise
`UnimplementedError`: they are E2B's CRUD over **AWS Secrets Manager in your
account** (the native SDK's `SecretStore`, see [Secrets](secrets.md)).
Contract: PyPI's `e2b` **2.51.0** (`e2b/secret/{base,secret_sync,
secret_async,types}.py`) and npm's `e2b` **2.51.0** (`src/secret.ts` in
`dist/index.d.ts`), downloaded and read on 2026-09-30; it is the same
version as the rest of this page. Same names, parameters and results
(`create`, `update`, `get_info`/`getInfo`, `list`, `exists`, `destroy`,
`fill`, `iam_token`/`iamToken`, `SecretInfo`, `SecretPaginator` with
`has_next`/`next_token`/`next_items`); each difference, here:

| What | E2B 2.51 | Rayito |
|---|---|---|
| Where the secret lives | E2B's API (private beta) | a Secrets Manager secret `<secret_prefix><name>` in your account (`rayito/` by default); it costs $0.40/month until `destroy` |
| `SecretInfo.secret_id` / `secretId` | `sec_…` | the Secrets Manager ARN (it changes if you delete and recreate, as in E2B) |
| Names | 1–128 `[A-Za-z0-9_-]`, lowercase, `sec_` reserved (validated by the server) | the same rule, validated in the SDK **before** calling AWS; the error never repeats the name. The shim lowercases the name (`Secret.create("OpenAI", …)` stores `rayito/openai`), but the native SDK's `secrets=` and `SecretStore` do **not** normalize: refer to it in lowercase (`secrets={"K": "openai"}`), or `"OpenAI"` gives `SecretNotFoundException` |
| `secret` in `update`/`get_info`/`exists`/`destroy` | id or name | the name or the ARN (`secret_id`) |
| `metadata` | ≤ 8 KiB | ≤ 2048 encoded characters (Secrets Manager's `Description`) |
| Secret cap | 100 per project | no cap of its own (your AWS account's) |
| `version` | increasing integer | the same integer, encoded in the `ClientRequestToken` (`rayito-secret-version-{n:020d}`); `get_info` gives 0 if the current version was not written by Rayito (an external rotation), and `update` writes the highest Rayito version `DescribeSecret` still lists + 1 |
| `fill(name)` | placeholder that E2B's egress proxy resolves into `network.rules` headers | the same string `${e2b.secrets.<name>}`, without calling AWS, but **nothing resolves it**: there is no egress injector (`network.rules` is still `UnimplementedError`) and no SDK path substitutes placeholders inside `envs`. To deliver a secret use the native SDK's `secrets=` ([Secrets](secrets.md)) |
| `iam_token` / `iamToken` | JWT-SVID identity token | `UnimplementedError` (reason of `iam`) |
| Very frequent `update` | no documented limit | Secrets Manager recommends ≤ 1 write every 10 min; the SDK warns (`UserWarning`/`process.emitWarning`) the first time it is exceeded |
| `destroy` and recreating the same name | no retry in the SDK | `destroy` returns `False` if the name did not exist, like E2B (it checks with `DescribeSecret` before deleting). `DeleteSecret` is asynchronous and AWS takes ~20–30 s to release the name (measured in the 0.5.0 acceptance): `create` retries with backoff and jitter up to 60 s while AWS answers `InvalidRequestException` mentioning the deletion (`AWS_API_NOTES.md` §19); any other code comes out as `SecretException` without a retry |
| Connection options (`api_key`, `domain`, `request_timeout`, …) | E2B's API ones | they do not apply: `RayitoCompatWarning` and they are ignored; its own are `region`, `session` (TS: `credentials`), `secret_prefix` (TS: `secretPrefix`) and `kms_key_id` (TS: `kmsKeyId`) |
| `SecretException` | inherits from `Exception` (TS: `SecretError` from `Error`) | inherits from `SandboxException` (TS: `SandboxError`); `SecretNotFoundException` is also the native `NotFoundException` in Python (in TS only `SecretError`: single inheritance) |
| `SecretPaginator` (TS) | `new SecretPaginator(opts?)` | accepts an internal second argument (the bound options of `E2B(...)`) |

## Migrating from the 1.x shim { #migrar-desde-el-shim-1x }

Rayito 0.2.0 shipped an E2B 1.x shim. These changes **break** code written
against it:

| Before (1.x shim) | Now (2.x shim) |
|---|---|
| `sbx.pause()` returned the sandbox id | returns `bool` (`Sandbox.pause(id)` the same) |
| synchronous `files.watch_dir(path, on_event)` | `watch_dir(path, on_event=cb)`; the second positional is `user`; it lives until `stop()` |
| synchronous `pty.create(size, on_data)` | `pty.create(size, on_data=cb)`; the second positional is `user` |
| `proxy=` warned and was ignored | honored (only `http://`) |
| `NotEnoughSpaceException` was never raised | alias of `DiskFullException`, raised when the disk is full |
| `sbx.connection_config` raised `UnimplementedError` | returns a `ConnectionConfig` |
| `set_timeout`, `upload_url`/`download_url`, `get_metrics(start=, end=)`, `list(next_token=)`, `allow_internet_access=False` raised `UnimplementedError` | they map (table above) |
| `mcp=`, `network=`, `lifecycle=` were `TypeError` | they map or raise `UnimplementedError` |
| `timeout` was the MicroVM's immutable lifetime | it is `rayd`'s logical deadline; `max_lifetime` is the cap, and a 0.3.0 or later image is required |
| TS: a full disk was `RateLimitError` | `DiskFullError` (`NotEnoughSpaceError` in `rayito/e2b`) |

## Parity { #paridad }

The full table (113 rows: implemented, divergent, out by SPEC, impossible on
the platform, with each one's page) is in [E2B parity](e2b-parity.md).

**OTel (row 108, divergent).** E2B's sandbox telemetry export (Enterprise)
does not exist in Rayito: there is no service that collects the MicroVM's
metrics or logs and exports them for you. What does exist, native and opt-in
(since 0.5.0), are **client-side** spans over the calls you already make
(`tracer_provider=`/`tracerProvider`, `rayito.*` names), described in
[OpenTelemetry](funciones-opcionales/opentelemetry.md); the E2B shim
(`rayito.e2b`/`rayito/e2b`) is not instrumented, so
`from rayito.e2b import Sandbox` emits no span for now.

## The cost of `list(query=SandboxQuery(metadata=...))` { #el-coste-de-listquerysandboxquerymetadata }

`list-microvms` does not know about metadata. The filter is O(n) over the
`RUNNING` sandboxes: for each one, `get-microvm` +
`create-microvm-auth-token` + a `Health` (≈ 0.6-0.8 s per sandbox, measured:
`AWS_API_NOTES.md` Q45), and each probe counts as traffic for that sandbox's
idle policy. Filter by `template` first if you have many.

<a id="con-el-indice-de-metadatos-index-opcional-m14"></a>

### With the metadata index (`index=`, optional) { #con-el-indice-de-metadatos-index-opcional }

Rayito's `index=DynamoDbIndex("rayito-sandboxes")` extension (TS
`index: new DynamoDbIndex({ tableName: "rayito-sandboxes" })`), in
`Sandbox.list(...)` or bound to the client with `E2B(index=...)`, resolves
`query.metadata` with an **optional DynamoDB table in your account**
(`infra/metadata-index.yaml`): one `dynamodb:BatchGetItem` per `list-microvms`
page, no `get-microvm`, token or `Health`, so it supports `state=[PAUSED]`
without waking any sandbox. Differences from E2B:

- Only sandboxes **created with the index** appear
  (`rayito.Sandbox.create(index=)` of the native SDK or a
  `PoolConfig(index=)`); the shim's `create()` does not write it. A sandbox
  without a row is omitted: its metadata is unknown.
- The state always comes from `list-microvms` (`PAUSED` =
  `SUSPENDING|SUSPENDED`).
- Cost: 0.5 RRU per candidate sandbox and 1 WRU per `create` (DynamoDB
  on-demand); details, IAM and how to turn it off in
  [Optional features](optional-features.md#metadata-index).

=== "Python"

    ```python
    from rayito import DynamoDbIndex
    from rayito.e2b import Sandbox, SandboxQuery, SandboxState

    idx = DynamoDbIndex("rayito-sandboxes")
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED])
    paused = Sandbox.list(query=query, index=idx).next_items()
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex } from "rayito";
    import { Sandbox } from "rayito/e2b";

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    const paginator = Sandbox.list({ query: { metadata: { user: "42" }, state: ["paused"] }, index });
    const paused = await paginator.nextItems();
    ```
