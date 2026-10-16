# E2B parity

Rayito's scope table against E2B 2.51 (`e2b` 2.51.0 and
`e2b-code-interpreter` 2.10.0), row by row: 113 features from the SDK, the
CLI and the recipes in E2B's documentation. How to migrate a program and what
behaves differently is in [E2B compatibility](e2b-compat.md); this page only
says **what exists** and where it is documented.

!!! note "Accepted against real AWS"
    The 0.3.0 rows were accepted against real AWS on 2026-09-24: Python e2e
    regression 62/62, E2B examples corpus 18/18 and TypeScript 26/26.
    The rows that depend on a measurement cite their row in
    [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md)
    (Qnn).

!!! note "Which rows changed in each version"
    - **0.8.0**: none. The AI agent (`sbx.agent`) is not an E2B API: the shim
      does not expose it and it is used from the native sandbox
      (`sbx.native.agent`, see
      [Compatibility](e2b-compat.md#lanza-unimplementederror)).
    - **0.7.0**: EFS volumes (row 26, experimental: `Volume` does real CRUD on
      access points and `volume_mounts=` mounts on the optional
      `rayito-base-caps-efs` image) and custom domain (row 110,
      `CustomDomain`, experimental and not yet verified end to end on real
      AWS). [What's new in 0.7.0](novedades/0.7.0.md).
    - **0.6.0**: real templates (rows 56, 81, 89) and, as optional features in
      your account, events and webhooks (107), telemetry export (108), bucket
      mounts (111) and per-image size (82); `gateways=` covers the
      `Secret.fill()` case in the network rules.
      [What's new in 0.6.0](novedades/0.6.0.md).

## Statuses { #estados }

| Status | What it means | Rows |
|---|---|---|
| implemented | the E2B feature works with the same contract; "before 0.3.0" if it was already in 0.2.0 | 72 (24 before 0.3.0, 48 in 0.3.0) |
| **divergent** | works, with a difference written in the note (a specific image, a bucket, the access token, a different cap, an optional table or stack) | 28 |
| out by SPEC | could be built, but `SPEC.md` §4 leaves it out or it is not there yet (R/Java kernels, desktop); raises `UnimplementedError` or does not exist | 2 |
| impossible on the platform | Lambda MicroVMs does not have the primitive; raises `UnimplementedError` with the reason (or is ignored with `RayitoCompatWarning`) | 11 |

No row is approximated silently: what is not implemented raises
`UnimplementedError` (a `NotImplementedError`, never `TypeError` or
`AttributeError`) with the feature and the reason; the exact texts are in
[E2B compatibility](e2b-compat.md#lanza-unimplementederror).

## Which image each row needs { #que-imagen-necesita-cada-fila }

Most of the 0.3.0 rows work on any image published from this tree. Three
families do not:

- egress policy (rows 10–13, 19): `rayito-base-caps`;
- `bash`, `javascript` and `typescript` kernels (rows 94–96): `rayito-base-poly`;
- the whole E2B shim: a 0.3.0 or later image (the shim always asks for the
  server-side deadline).

Details in [Images and IAM](images.md).

## The table { #la-tabla }

**Status** says the version in which each row arrived ("before 0.3.0" if it
was already in 0.2.0). **Doc** links the page that explains how to use it.

| # | E2B API | Status | Note | Doc |
|---|---|---|---|---|
| 1 | `Sandbox.create(template=)` | implemented (before 0.3.0) | image name or ARN; `RAYITO_TEMPLATE` replaces E2B's default template | [Compatibility](e2b-compat.md) |
| 2 | `Sandbox.create(timeout=)` / JS `timeoutMs` | divergent (0.3.0) | `rayd` enforces a movable deadline: it exits and the VM goes to `TERMINATED` ≈ 15 s later (Q58); `max_lifetime` ceiling ≤ 28800 s; E2B Pro's 86400 s is impossible (the cap counts suspended time) | [Deadline](lifecycle.md) |
| 3 | `Sandbox.create(metadata=, envs=)` | implemented (before 0.3.0) | in `runHookPayload`, 4096 characters between the two, not secrets | [Compatibility](e2b-compat.md) |
| 4 | `Sandbox.create(secure=)` | implemented (before 0.3.0) | E2B ignores it too; every RPC requires `x-access-token`; warning with `secure=False` | [Compatibility](e2b-compat.md) |
| 5 | `ApiParams` `api_key` / `domain` / `debug` / `api_url` / `sandbox_url` / `validate_api_key` / `api_headers` | impossible on the platform | there is no E2B API to configure; the credentials are AWS's; they are accepted and ignored with `RayitoCompatWarning` | [Compatibility](e2b-compat.md) |
| 6 | `ApiParams` `proxy=` | implemented (0.3.0) | honored on the gRPC channel (`grpc.http_proxy`) and in botocore; since 0.4.0, on an instance call (`sbx.kill()`/`pause()`/`connect()`) it is not applied and warns with `RayitoCompatWarning` | [Compatibility](e2b-compat.md#se-mapea-con-una-nota) |
| 7 | `ConnectionConfig` `request_timeout` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 8 | `ConnectionConfig` `headers` / `retries` / `set_integration` and `sbx.connection_config` | implemented (0.3.0) | `headers` = extra gRPC metadata (reserved keys rejected); `retries` to botocore's `Config`; `set_integration` to `user_agent_extra`; since 0.4.0, `headers`/`retries` on an instance call are not applied and warn with `RayitoCompatWarning` | [Compatibility](e2b-compat.md#se-mapea-con-una-nota) |
| 9 | `Sandbox.create(logger=)` / `ApiParamsWithLogger` | implemented (0.3.0) | SDK logs go to the given `logging.Logger`; the native `logging=` (CloudWatch) is something else | [Compatibility](e2b-compat.md) |
| 10 | `allow_internet_access=False` / JS `allowInternetAccess: false` | divergent (0.3.0) | in the `rayito-base-caps` guest, or outside with `egress=[your own VPC connector]`; on the default image the VM is terminated and `UnimplementedError` is raised (Q60: there is no managed connector without egress) | [Outbound network](network.md) |
| 11 | `network.allow_out` / `deny_out` with CIDR, IP, `ALL_TRAFFIC` or callable selectors | divergent (0.3.0) | exact E2B semantics through `uidrange` policy routes, `rayito-base-caps` only | [Outbound network](network.md) |
| 12 | `network.allow_out` / `deny_out` with hostnames | divergent (0.3.0) | proxy-only mode: `rayd`'s local proxy filters by name and by resolved IP and blocks direct egress; a client that ignores the proxy fails closed, whereas E2B filters transparently | [Outbound network](network.md) |
| 13 | `network.egress_proxy` (SOCKS5) | divergent (0.3.0) | the local proxy chains to the operator's SOCKS5 after filtering; caps only; fails closed for clients without a proxy | [Outbound network](network.md) |
| 14 | `network.rules` / `SandboxNetworkRule` (header injection, IAM placeholders) | impossible on the platform | its reason for existing is to inject credentials from outside the sandbox; Lambda MicroVMs has no egress hook on the host, so the injector would run in the same VM as the untrusted code and would need to intercept TLS | [Outbound network](network.md) |
| 15 | `network.allow_public_traffic=True` (E2B's default) | impossible on the platform | there is no unauthenticated mode: `X-aws-proxy-auth` is mandatory (§3, §7); `False` is Rayito's permanent behavior. With `CustomDomain` (experimental) a port answers without that header under your domain, with the CloudFront distribution adding it for you (row 110) | [Outbound network](network.md), [Custom domain](funciones-opcionales/dominio-propio.md) |
| 16 | `Sandbox.traffic_access_token` | divergent (0.3.0) | returns the current `x-aws-proxy-auth` JWE; it rotates (TTL ≤ 60 min) and the header has a different name | [Compatibility](e2b-compat.md) |
| 17 | `network.mask_request_host` | impossible on the platform | the AWS proxy always forwards `Host: <endpoint>` (§7) | [Outbound network](network.md) |
| 18 | `network.https_ports` | divergent (0.3.0) | measured (Q67, QE2): the Lambda MicroVMs proxy does not forward end-to-end TLS to a guest port, so a non-empty list is `UnimplementedError`; `get_host(port)` serves plain HTTP | [Outbound network](network.md) |
| 19 | `Sandbox.update_network(network)` / `update_network(id, network)` / JS `updateNetwork` | divergent (0.3.0) | `NetworkService.UpdateNetwork` changes the guest's policy atomically on caps; platform connectors remain immutable (there is no `UpdateMicrovm`) | [Outbound network](network.md) |
| 20 | `lifecycle.on_timeout='kill'` | implemented (0.3.0) | `rayd` exits on expiry and the VM terminates without IAM (Q58) | [Deadline](lifecycle.md) |
| 21 | `lifecycle.on_timeout='pause'` | divergent (0.3.0) | a live SDK suspends on expiry; with no client, the idle policy does it at `max_idle`; the load is not frozen in the meantime; applies idle suspension before the deadline; paused ones still die at the 8 h cap | [Deadline](lifecycle.md) |
| 22 | `lifecycle.auto_resume` | implemented (0.3.0) | `True` = `autoResumeEnabled` plus E2B's 5 min minimum after resuming; `False` is enforced by `rayd` (a bare resume stays expired until `connect()`) | [Deadline](lifecycle.md) |
| 23 | `lifecycle.on_timeout={"action": "pause", "keep_memory": False}` and `pause(keep_memory=False)` | impossible on the platform | `suspend-microvm` always saves memory and a files-only resume would require the same id with a cold start (§5); the analog is `checkpoint_files()` + `kill()`, with a new id | [Deadline](lifecycle.md) |
| 24 | `Sandbox.create(iam=)` + `Secret.iam_token` (JWT-SVID identity) | impossible on the platform | MicroVMs do not issue tokens with an audience; the only identity is the execution role via IMDSv2 (§9) and an issuer of our own would be a control-plane service (`SPEC.md` §4) | [Security](security.md) |
| 25 | `Sandbox.create(mcp=)` + `get_mcp_url()` / `get_mcp_token()` | impossible on the platform | the contract (URL, fixed token) does not hold: each request also needs a JWE in the header with TTL ≤ 60 min (§3, §7) and there is no Docker for the catalog's servers; Rayito ships `rayito-mcp` on the client | [MCP](mcp.md) |
| 26 | `Sandbox.create(volume_mounts=)` + `Volume`/`AsyncVolume` API | divergent (0.7.0, experimental) | `VolumeStore` does real CRUD of EFS access points; `volume_id`/`volumeId` is the volume's name; `volume_mounts=` mounts through the native `volumes=` with `E2B(volume_store=..., volume_connector_arn=...)` on the optional `rayito-base-caps-efs` image (that sandbox egresses only through the volume's connector: a MicroVM supports one egress connector); `Volume` content operations still have no data plane of their own | [EFS volumes](funciones-opcionales/volumenes-efs.md) |
| 27 | `Sandbox.connect(id, timeout=)` (class form) | divergent (0.3.0) | `timeout` extends the deadline (`AT_LEAST`, E2B semantics); the sandbox's access token is still required | [Deadline](lifecycle.md) |
| 28 | `sbx.connect()` (instance form) | implemented (0.3.0) | since 0.4.0, `headers=`/`proxy=`/`retries=` warn with `RayitoCompatWarning` and are not applied (`Sandbox.connect(sandbox_id, ...)` does apply them) | [Compatibility](e2b-compat.md#se-mapea-con-una-nota) |
| 29 | `connect(on_resume='reboot')` | impossible on the platform | `resume-microvm` always restores memory and disk (§5); the analog is `reincarnate()`, with a new id | [Persistence](persistence.md) |
| 30 | `Sandbox.kill()` / `Sandbox.kill(id)` → `bool` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 31 | `sbx.is_running(request_timeout=)` | implemented (before 0.3.0) | now accepts `request_timeout` | [Compatibility](e2b-compat.md) |
| 32 | `sbx.set_timeout(timeout)` | implemented (0.3.0) | sets the exact deadline, can shorten it, up to `max_lifetime`; beyond that, `InvalidArgumentException`; replaces ADR-007 | [Deadline](lifecycle.md) |
| 33 | `Sandbox.set_timeout(id, timeout)` / JS static `setTimeout` | divergent (0.3.0) | needs the access token (`access_token=` or `RAYITO_ACCESS_TOKEN`) | [Deadline](lifecycle.md) |
| 34 | `sbx.get_info()` / `Sandbox.get_info(id)` → 2.x `SandboxInfo` | implemented (0.3.0) | `sandbox_domain` = endpoint; `cpu_count`/`memory_mb` from the guest; `envd_version` = `agent_version`; `lifecycle`, `network` and `allow_internet_access` filled in; `volume_mounts` always empty; `end_at` = the logical deadline | [Compatibility](e2b-compat.md) |
| 35 | JS `Sandbox.getFullInfo` | implemented (0.3.0) | — | [Compatibility](e2b-compat.md) |
| 36 | `Sandbox.list(limit, next_token)` + `SandboxPaginator` `has_next` / `next_items` / `next_token` | implemented (0.3.0) | opaque token over `list-microvms`' `nextToken` | [Observability](observability.md) |
| 37 | `Sandbox.list(order="asc" / "desc")` | implemented (0.3.0) | sorted on the client by `startedAt` over all pages: AWS does not sort | [Observability](observability.md) |
| 38 | `SandboxQuery.state` / `started_after` / `template` | implemented (0.3.0) | client-side filters over the items | [Observability](observability.md) |
| 39 | `SandboxQuery.metadata` on `RUNNING` sandboxes | implemented (before 0.3.0) | one `Health` probe per `RUNNING` sandbox, O(n) (Q45) | [Observability](observability.md) |
| 40 | `SandboxQuery.metadata` on `PAUSED` sandboxes | divergent (0.5.0, pending acceptance on real AWS) | with the `index=DynamoDbIndex(...)` extension (TS `index: new DynamoDbIndex({...})`, also `E2B(index=)`): an optional DynamoDB table in your account (`infra/metadata-index.yaml`, ADR-014, on-demand, ~$0 at rest); only sandboxes created with the index from the native SDK appear (`rayito.Sandbox.create(index=)` or `PoolConfig(index=)`): the shim's `Sandbox.create()` (`rayito.e2b` / `rayito/e2b`) never writes the index, so the full PAUSED flow does not fit inside the shim; state always comes from `list-microvms` and no sandbox is probed or woken; without the index it is still `UnimplementedError`, with the hint about `index=` | [Observability](observability.md#listado-por-metadatos-con-indice-opcional), [Optional features](optional-features.md#metadata-index) |
| 41 | `sbx.get_metrics(start, end)` → `List[SandboxMetrics]` with `mem_cache` | implemented (0.3.0) | 5 s ring in `rayd` (8 h) with a gap while suspended | [Observability](observability.md) |
| 42 | `Sandbox.get_metrics(id)` / JS static `getMetrics` | divergent (0.3.0) | needs the sandbox's access token | [Observability](observability.md) |
| 43 | `sbx.pause()` → `bool` / `Sandbox.pause(id)` | implemented (0.3.0) | returns `bool` like E2B 2.x (previously `str`) | [Compatibility](e2b-compat.md) |
| 44 | `sbx.beta_pause()` / JS `betaPause` | implemented (0.3.0) | Python already had it; JS gains it with the TS shim | [Compatibility](e2b-compat.md) |
| 45 | `Sandbox.beta_create(auto_pause=, network=, mcp=)` | implemented (0.3.0) | E2B removed it in 2.23.0; it is kept: `auto_pause` is `lifecycle` pause and `network`/`mcp` follow their rows; Python only (the TS shim has no `betaCreate`) | [Deadline](lifecycle.md) |
| 46 | `create_snapshot` / `list_snapshots` / `delete_snapshot` / `SnapshotInfo` / `SnapshotPaginator` / `create(template=<snapshot_id>)` | impossible on the platform | no operation copies the memory of a running MicroVM (§1, §15); the files-only analog is `checkpoint_files`; explicit `UnimplementedError` | [Persistence](persistence.md) |
| 47 | `Sandbox.fork(timeout, count)` / `Sandbox.fork(id)` | impossible on the platform | forking in-memory state has no primitive (§1, §15) | [Persistence](persistence.md) |
| 48 | `sbx.upload_url(path, user, use_signature_expiration)` / JS `uploadUrl` | divergent (0.3.0) | presigned S3 `PUT` (`form=True` gives a `POST`); it lands asynchronously with `rayd`'s barrier and `ticket.wait()`; single use; expiry always set; requires a transfer bucket; in the TS shim it returns the URL as a `string` (no `wait()`) | [Files](files.md) |
| 49 | `sbx.download_url(path, user, use_signature_expiration)` / JS `downloadUrl` | divergent (0.3.0) | presigned S3 `GET` with `Range`; serves a snapshot taken at call time; expiry always set; a missing file raises immediately; requires a transfer bucket (`S3Staging` or `RAYITO_TRANSFER_BUCKET`) | [Files](files.md) |
| 50 | `get_signature(path, operation, user, envd_access_token, expiration_in_seconds)` | impossible on the platform | an envd signature does not authenticate at the proxy (JWE only in the header, §7); Rayito's URLs are signed in S3 inside `upload_url`/`download_url` | [Files](files.md) |
| 51 | `sbx.get_host(port)` (Python) | implemented (before 0.3.0) | hostname plus the mandatory proxy headers (`HostAccess`); the platform has no public URL without headers | [Compatibility](e2b-compat.md) |
| 52 | JS `sandbox.getHost(port): string` (synchronous) | divergent (0.3.0) | the TS shim returns the hostname synchronously; requests still need the proxy headers | [Compatibility](e2b-compat.md) |
| 53 | `sbx.sandbox_id` / `sbx.sandbox_domain` | implemented (before 0.3.0) | `sandbox_domain` is the endpoint hostname | [Compatibility](e2b-compat.md) |
| 54 | `sbx.envd_api_url` / `sbx.envd_direct_url` | implemented (0.3.0) | `https://<endpoint>`; requests need the proxy headers; Python only | [Compatibility](e2b-compat.md) |
| 55 | `with Sandbox() as sbx` / `async with` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 56 | bound client `E2B(api_key, domain, ...)` with `.Sandbox` / `.AsyncSandbox` / `.Template` / `.Volume` / `.Secret` | implemented (0.3.0) | binds `region`/`session`/`control_plane`; since 0.5.0 `.Secret`/`.AsyncSecret` work (Secrets Manager with that `region`/`session`; accepted on real AWS); since 0.6.0 `.Template`/`.AsyncTemplate` are the real class (`build`, `build_in_background`, `get_build_status`, see row 81); since 0.7.0 `.Volume`/`.AsyncVolume` do real CRUD with `volume_store=` (experimental, see row 26) and without it raise `UnimplementedError` | [Compatibility](e2b-compat.md) |
| 57 | `commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, stdin, timeout, request_timeout)` with positional arguments | implemented (0.3.0) | the native call already worked; the shim now accepts E2B's positional order | [Compatibility](e2b-compat.md) |
| 58 | `commands.connect` / `commands.list` / `ProcessInfo` / `commands.kill` / `send_stdin` / `close_stdin` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 59 | `CommandHandle.wait(on_pty, on_stdout, on_stderr)` (sync) | implemented (0.3.0) | — | [Compatibility](e2b-compat.md) |
| 60 | `AsyncCommandHandle` / `CommandResult` / `CommandExitException` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 61 | types `Stdout`, `Stderr`, `PtyOutput`, `OutputHandler`, `AsyncCommandHandle`, `AsyncWatchHandle`, `Username` | implemented (0.3.0) | — | [Compatibility](e2b-compat.md) |
| 62 | `pty.create` / `pty.send_stdin` / `pty.resize` / `pty.kill` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 63 | `pty.connect(pid)` | implemented (0.3.0) | the native one already existed; the shim now exposes it | [Compatibility](e2b-compat.md) |
| 64 | JS `pty.create({cols, rows, onData, user, cwd, envs, timeoutMs})` | implemented (0.3.0) | top-level `cols`/`rows` in the TS shim | [Compatibility](e2b-compat.md) |
| 65 | `files.read(format text / bytes / stream, user, request_timeout)` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 66 | `files.read(gzip=)` / `files.write(gzip=)` | implemented (0.3.0) | standard gRPC gzip; responses request it with the `rayito-compress` header; it crosses the AWS proxy in both directions: 20 MB of text go from 0.80 to 52.34 MB/s (Q74) | [Files](files.md) |
| 67 | `files.read(stream_idle_timeout=)` | implemented (0.3.0) | — | [Files](files.md) |
| 68 | `files.write(metadata=)` / `write_files(metadata=)` + `WriteInfo.metadata` / `EntryInfo.metadata` | implemented (0.3.0) | `user.rayito.*` xattrs; lowercase keys; overwriting replaces the set (envd semantics) | [Files](files.md) |
| 69 | `files.write(use_octet_stream=)` | implemented (0.3.0) | accepted with no effect: gRPC streams have no multipart form | [Files](files.md) |
| 70 | `files.write`/`files.read` of large files (performance) | implemented (0.3.0) | they go through S3 automatically above a threshold when there is an `S3Staging` (Q59: 55–106 MB/s versus 0.68 MB/s through the proxy); without `S3Staging` they still go over gRPC, as before | [Files](files.md) |
| 71 | `files.write_files(List[WriteEntry])` → `List[WriteInfo]` | implemented (0.3.0) | the native one already existed; the shim now exposes it | [Compatibility](e2b-compat.md) |
| 72 | `files.list(depth)` / `exists` / `get_info` / `remove` / `rename` / `make_dir` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 73 | `FileType.FILE` / `DIR` / `SYMLINK` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 74 | `files.watch_dir(include_entry=)` | implemented (0.3.0) | passed through to the native call | [Compatibility](e2b-compat.md) |
| 75 | `files.watch_dir(allow_network_mounts=)` | implemented (0.3.0) | accepted; the sandbox has no network mounts | [Compatibility](e2b-compat.md) |
| 76 | `WatchHandle.get_new_events` / `stop`, `AsyncWatchHandle.stop`, `FilesystemEvent`, `FilesystemEventType` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 77 | JS `files.read(format: "blob")` | implemented (0.3.0) | — | [Files](files.md) |
| 78 | JS `files.watchDir(path, onEvent, opts)` | implemented (0.3.0) | positional `onEvent` in the TS shim | [Compatibility](e2b-compat.md) |
| 79 | `sandbox.git` (`clone`, `init`, `remote_add`, `remote_get`, `status`, `branches`, `create_branch`, `checkout_branch`, `delete_branch`, `add`, `commit`, `reset`, `restore`, `push`, `pull`, `set_config`, `get_config`, `dangerously_authenticate`, `configure_user`; `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`) | implemented (0.3.0) | client-side wrapper over `commands.run`, with `git-core` in `rayito-base`; E2B marks the module as deprecated | [Git](git.md) |
| 80 | `Secret` / `AsyncSecret` (`create`, `update`, `get_info`, `list`, `exists`, `destroy`, `fill`; `SecretInfo`, `SecretPaginator`) | divergent (0.5.0, accepted on real AWS) | CRUD over AWS Secrets Manager in your account (ADR-014, optional, $0.40/secret-month); `secret_id` is the ARN, not `sec_…`; `metadata` ≤ 2048 characters (E2B: 8 KiB), no cap of 100; no `network.rules` injector: `fill()` returns the placeholder, which nothing resolves (delivery is the native SDK's `secrets=`, visible to the sandbox's code); `iam_token` is still `UnimplementedError` (row 24) | [Secrets](secrets.md), [Compatibility](e2b-compat.md#secretos-secret-asyncsecret) |
| 81 | template build API (`TemplateBase`, `TemplateBuilder`, `Template.build`, ...) | divergent (0.6.0) | `Template`/`AsyncTemplate` with `build`, `build_in_background` and `get_build_status`: the same DSL, composed on the client over a published `rayito-base` image (`from_base_image` instead of `from_docker_image`), with no layer cache and ARM64 only; `alias_exists`/`assign_tags`/`remove_tags`/`get_tags` raise `UnimplementedError`; also `rayito template build` | [Templates](funciones-opcionales/templates.md) |
| 82 | `cpu_count` / `memory_mb` per sandbox | divergent (0.6.0) | per image, like E2B per template build: `Template.build(memory_mb=)` rounds up to the next supported size with `RayitoCompatWarning` and `cpu_count` is ignored with a warning (CPU comes from memory, Q87); the native SDK adds `Sandbox.create(size=)`, which chooses among the five-size images published with `rayito image publish --sizes` (or your own image per size with `--memory-mib`); `SandboxInfo`'s `cpu_count`/`memory_mb` are the guest's view (Q68) | [Sizes](funciones-opcionales/tamanos.md), [Templates](funciones-opcionales/templates.md) |
| 83 | exceptions `AuthenticationException`, `CommandExitException`, `InvalidArgumentException`, `NotFoundException`, `RateLimitException`, `SandboxException`, `TimeoutException` | implemented (before 0.3.0) | `TimeoutException` now also covers `sandbox_timeout` (`m9-server-timeout`) | [Compatibility](e2b-compat.md) |
| 84 | `FileNotFoundException` / `SandboxNotFoundException` exported from `rayito.e2b` | implemented (0.3.0) | — | [Compatibility](e2b-compat.md) |
| 85 | `NotEnoughSpaceException` | implemented (0.3.0) | alias of `DiskFullException`, which is actually raised | [Compatibility](e2b-compat.md) |
| 86 | `ServiceBusyException` | implemented (0.3.0) | raised on `InsufficientCapacityException` | [Compatibility](e2b-compat.md) |
| 87 | `FileUploadException` | implemented (0.3.0) | raised when an import fails (`m9-file-transfer`) | [Compatibility](e2b-compat.md) |
| 88 | `GitAuthException` / `GitUpstreamException` | implemented (0.3.0) | — | [Git](git.md) |
| 89 | `TemplateException` / `BuildException` | divergent (0.6.0) | raised by `Template.build()`: `TemplateException` on an invalid template or base image, `BuildException` when the build fails (with the reason, including the build quota). Those in `rayito.e2b` and `rayito/e2b` are the native classes, and `BuildException` is a `SandboxException` ([Compatibility](e2b-compat.md#tabla-de-imports)) | [Templates](funciones-opcionales/templates.md) |
| 90 | `Volume*Exception` / `Secret*Exception` | divergent (0.5.0, accepted on real AWS) | `SecretException`/`SecretNotFoundException` (TS: `SecretError`/`SecretNotFoundError`) exist and are raised by `Secret`, with E2B's hierarchy plus one difference: `SecretException` inherits from `SandboxException` (TS: `SandboxError`) and, in Python, `SecretNotFoundException` is also `NotFoundException`; since 0.7.0, `VolumeException`/`VolumeNotFoundException`/`VolumePathNotFoundException` also exist (the native ones, exported by `rayito.e2b`; in TS, `VolumeError`/`VolumeNotFoundError`/`VolumePathNotFoundError` are imported from `rayito`) and are raised by volume operations with `volume_store=` (row 26) | [Compatibility](e2b-compat.md#secretos-secret-asyncsecret) |
| 91 | `SandboxState` `RUNNING` / `PAUSED` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 92 | full `AsyncSandbox` parity | implemented (0.3.0) | the same gaps closed as in the sync shim | [Compatibility](e2b-compat.md) |
| 93 | `run_code(code, language / context, on_stdout, on_stderr, on_result, on_error, envs, timeout, request_timeout)` → `Execution` | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 94 | `run_code(language="python" / "bash")` | implemented (before 0.3.0) | `bash` only on `rayito-base-poly` | [Kernels](kernels.md) |
| 95 | `run_code(language="javascript")` | implemented (0.3.0) | Deno kernel on `rayito-base-poly` with lazy start (Q61); other images raise `UnimplementedError` naming poly | [Kernels](kernels.md) |
| 96 | `run_code(language="typescript")` | implemented (0.3.0) | the same Deno kernel, `rayito-base-poly` only | [Kernels](kernels.md) |
| 97 | `run_code(language="r" / "java")` | out by SPEC | `SPEC.md` §4; measured: R via conda 1.4 GB, R-core 127 MB plus graphics dependencies; Corretto 21,262 MB plus an unmaintained IJava | [Kernels](kernels.md) |
| 98 | `create_code_context(cwd, language, request_timeout)` → `Context` | implemented (before 0.3.0) | `javascript`/`typescript` added in poly | [Kernels](kernels.md) |
| 99 | `list_code_contexts` / `remove_code_context` / `restart_code_context` | implemented (0.3.0) | the native one already existed; the shim now exposes it | [Compatibility](e2b-compat.md) |
| 100 | `Execution` / `Logs` / `OutputMessage` / `ExecutionError` / `Result` / charts (`ChartType`, `ScaleType`, `Chart`, `PointChart`, `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart`, data classes) | implemented (before 0.3.0) | — | [Compatibility](e2b-compat.md) |
| 101 | `Logs.to_json` / `ExecutionError.to_json` / `Context.from_json` / `Chart2D` / `MIMEType` / `RunCodeLanguage` / `OutputHandler` (code-interpreter) | implemented (0.3.0) | Python (`rayito.e2b`) | [Compatibility](e2b-compat.md) |
| 102 | JS drop-in import (`import { Sandbox } from 'e2b'` / `'@e2b/code-interpreter'`) | implemented (0.3.0) | new `rayito/e2b` path in `exports` | [Compatibility](e2b-compat.md) |
| 103 | JS `Sandbox.create(template?, opts)` with `ConnectionOpts` (`requestTimeoutMs`, `retries`, `logger`, `headers`, `proxy`, `signal`) | implemented (0.3.0) | `apiKey`/`domain` ignored with a warning; the `AbortSignal` goes to Connect's call options; since 0.4.0, on an instance call only `signal` (and `requestTimeoutMs` except in `kill`/`pause`) is applied, the rest warn with `RayitoCompatWarning` | [Compatibility](e2b-compat.md#se-mapea-con-una-nota) |
| 104 | JS statics `Sandbox.kill` / `getInfo` / `isRunning` / `connect` / `pause` | implemented (0.3.0) | static forms in the TS shim (also `getFullInfo`, `betaPause`, `setTimeout`, `getMetrics`, `list` and `updateNetwork`); the native one has the instance ones | [Compatibility](e2b-compat.md) |
| 105 | JS `Sandbox.list(opts)` → `SandboxPaginator` | implemented (0.3.0) | the paginator shape plus the metadata filter, as in Python | [Observability](observability.md) |
| 106 | camelCase surface of JS `commands` / `files` / `runCode` / contexts | implemented (before 0.3.0) | the native TS mirror already matched; the TS shim re-exposes it | [Compatibility](e2b-compat.md) |
| 107 | lifecycle events REST API and webhooks (docs) | divergent (0.6.0) | `events=LifecycleEvents(...)` in `create()` plus the optional `events-webhooks` stack in your account (`rayito events deploy`, ≈ $0.40/month): `created`/`paused`/`resumed`/`killed` stored in DynamoDB (`get_events`) and delivered to your webhooks with E2B's signature; no hosted REST API | [Events and webhooks](funciones-opcionales/eventos-y-webhooks.md) |
| 108 | OTel telemetry export (docs, Enterprise) | divergent (0.6.0) | `telemetry=TelemetryExport(...)` in `create()`: `rayd` exports 7 CPU, memory and disk metrics over OTLP/HTTP (to CloudWatch by default; `names="e2b"` uses E2B's names); no sandbox logs or traces. Since 0.5.0, also client-side spans with `tracer_provider=`/`tracerProvider` | [OTLP export](funciones-opcionales/exportacion-otlp.md), [Observability](observability.md#trazas-opentelemetry-del-sdk-opcional) |
| 109 | SSH access recipe (sshd + websocat, docs) | divergent (0.3.0) | `rayito sandbox connect` gives an interactive PTY terminal; an sshd recipe needs an image of your own and a client that speaks WebSocket-subprotocol authentication | [CLI](cli.md) |
| 110 | custom domain via reverse proxy (docs) | divergent (0.7.0, experimental) | for development: `rayito sandbox proxy <id> --port N` exposes a guest port on localhost; a public domain is `CustomDomain` (experimental): a CloudFront distribution in your account (ADR-014) with `https://<port>-<alias>.<your-domain>` URLs that you register (`register()`), with no reverse proxy of its own; not yet verified end to end on real AWS and not wired to `Sandbox.create(domain=)` | [Custom domain](funciones-opcionales/dominio-propio.md), [CLI](cli.md#proxy) |
| 111 | s3fs/gcsfuse bucket mounts (docs) | divergent (0.6.0) | `mounts={"/mnt/data": S3Mount(...)}` in `create()`: Mountpoint for Amazon S3 (`mount-s3`), S3 only (no GCS) and only on `rayito-base-caps`, with the buckets allowed by the image (`RAYITO_ALLOWED_MOUNT_BUCKETS`) and the policy of the `s3-mounts` stack | [S3 mounts](funciones-opcionales/montajes-s3.md) |
| 112 | E2B CLI (`auth`, `sandbox list/create/connect/exec/kill/metrics`, `template`, `snapshots`, `fork`) | divergent (0.3.0) | adds `sandbox create/connect/exec/metrics` to the `list/kill/logs` that already existed; since 0.6.0 `rayito template build/status/logs`; `auth`, `snapshots` and `fork` remain out | [CLI](cli.md) |
| 113 | desktop SDK (`e2b-desktop`) | out by SPEC | `SPEC.md` §4 (Desktop/GUI) | [Compatibility](e2b-compat.md) |

## What to do about what is missing { #que-hacer-con-lo-que-no-esta }

| If you used | In Rayito |
|---|---|
| `fork`, snapshots, `pause(keep_memory=False)` | `checkpoint_files()` + `create(persist=)` or `reincarnate()`: the files survive, with a new id ([Persistence](persistence.md)) |
| `Volume`, `volume_mounts` (without the image with `amazon-efs-utils`) | `persist=` (S3) and `upload_url`/`download_url` ([Files](files.md)); for a mounted bucket, `mounts=` ([S3 mounts](funciones-opcionales/montajes-s3.md)); with it, [EFS volumes](funciones-opcionales/volumenes-efs.md) |
| `Secret` (the CRUD) | the shim's `Secret`/`AsyncSecret` or `SecretStore`, over AWS Secrets Manager in your account ([Secrets](secrets.md)) |
| `network.rules` with `Secret.fill()` | `gateways=` (0.6.0): a loopback gateway inside the sandbox injects the real header toward a fixed `upstream`, so the code can use the secret without reading it ([Secret gateway](funciones-opcionales/pasarela-de-secretos.md)); or `secrets={"ENV": "name"}`, which delivers it as a visible environment variable ([Secrets](secrets.md)) |
| `mcp=` | the [`rayito-mcp`](mcp.md) server, on the client |
| `iam=` | `execution_role_arn` (IMDSv2); on `rayito-base-caps` the sandbox's code does not see IMDS ([Security](security.md)) |
| R or Java in `run_code` | there is no kernel: Python, and `bash`/JavaScript/TypeScript on `rayito-base-poly` ([Kernels](kernels.md)) |
