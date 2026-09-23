## Why

`rayito.e2b` promises an import-level drop-in for the E2B Python SDK **1.x**.
E2B is at **2.51.0** (`e2b` and the JS `e2b` package, 2026-09-18) and
`e2b-code-interpreter` 2.10.0 / `@e2b/code-interpreter` 2.8.0. The 2.x line
changed signatures that the shim still follows from 1.x:

- `pause()` returns a `bool`.
- `files.write_files` is a separate method.
- Sync `watch_dir` and sync `pty.create` take `user` as their second positional argument.
- `commands.run` accepts positional arguments.
- `CommandHandle.wait` takes output callbacks.

2.x also added surfaces the shim does not have: `list/remove/restart_code_context`, `pty.connect`, the instance `connect()`, `ConnectionConfig`, the bound `E2B(...)` client, `logger`, `headers`, `retries`, `proxy`, `git`, the new exceptions and type aliases, and the SandboxInfo 2.x fields. Many 2.x features that Lambda MicroVMs cannot provide fail today with `TypeError` or `AttributeError`, not with the honest `UnimplementedError` that the constitution (rule 7) requires. Examples are `fork`, snapshots, `network.rules` and `mcp`.

The TypeScript SDK has **no** E2B entry point, so a JS program cannot switch by changing only its import. The E2B CLI's `sandbox create|connect|exec|metrics` has no Rayito equivalent.

The research digest verified the E2B surface at commit `ccaf9fc` (`e2b-inventory`). The scope phase assigned the M9 ledger rows to six changes. This one is implemented **last**: it maps everything the other five M9 changes deliver and closes the shim-level gaps.

## What Changes

Every decision is closed in `design.md`. No `.proto` edits and no `rayd` code; the only image change is one pinned `dnf` package.

- **Python `rayito.e2b` moves to the E2B 2.51 contract (sync and async identical):**
  - The docstring and `__all__` change to 2.x.
  - `Sandbox.create` takes E2B's 2.x positional order. The deprecated `Sandbox(...)` constructor keeps its 1.x keyword surface.
  - New shim methods, each mapped onto native methods that already exist:
    - `list_code_contexts`, `remove_code_context`, `restart_code_context`
    - `files.write_files`, keeping the 1.x `write(list)` form
    - `watch_dir(include_entry=, allow_network_mounts=)`
    - `pty.connect(pid)`
    - the instance `sbx.connect(timeout=)`
    - `is_running(request_timeout=)`
  - `pause()` returns a `bool`.
  - A shim `Commands` gives E2B's positional `run`/`connect`. `CommandHandle.wait(on_pty, on_stdout, on_stderr)` is added natively.
- **Connection options:**
  - `logger=` routes the SDK's per-sandbox log records to the given `logging.Logger`.
  - `headers=` adds extra gRPC metadata. The proxy and access-token keys are refused.
  - `proxy=` sets the `grpc.http_proxy` channel argument and botocore `proxies`.
  - `retries=` sets botocore `retries`.
  - `ConnectionConfig`, `ConnectionConfig.set_integration` (botocore `user_agent_extra`) and the `sbx.connection_config` property are added.
  - `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`, `validate_api_key`, `api_headers` and `secure=False` are accepted, warned about and ignored.
- **Instance data:**
  - `envd_api_url` and `envd_direct_url` are both `https://<endpoint>`.
  - `traffic_access_token` is the current proxy JWE.
  - `SandboxInfo` gets its 2.x fields: `sandbox_domain`, `cpu_count`, `memory_mb`, `envd_version`, `allow_internet_access`, `network`, `lifecycle`, and `volume_mounts` (always `[]`).
- **Bound client:** `E2B(...)` exposes `.Sandbox` and `.AsyncSandbox` bound to region, session, control plane and connection options. `.Template`, `.Volume` and `.Secret` raise `UnimplementedError`.
- **Exceptions and types:**
  - Re-exported: `FileNotFoundException` and `SandboxNotFoundException`.
  - Aliases: `NotEnoughSpaceException is DiskFullException`, which is now actually raised, and `ServiceBusyException is CapacityException`.
  - Now raised: `FileUploadException` (on transfer import failures), `GitAuthException` and `GitUpstreamException`.
  - Defined but never raised: `TemplateException` and `BuildException`.
  - Type exports: `Stdout`, `Stderr`, `PtyOutput`, `OutputHandler`, `AsyncCommandHandle`, `AsyncWatchHandle`, `Username`, `MIMEType`, `RunCodeLanguage`, `Chart2D`, `ALL_TRAFFIC`.
  - New methods: `Logs.to_json`, `ExecutionError.to_json`, `CodeContext.from_json`.
- **Explicit `UnimplementedError`**, each citing its `AWS_API_NOTES.md` fact or `SPEC.md` §4 clause, instead of `TypeError`/`AttributeError`, for:
  - `fork`, `create_snapshot`, `list_snapshots`, `delete_snapshot`
  - `connect(on_resume='reboot')`, `pause(keep_memory=False)`, lifecycle `keep_memory=False`
  - `network.rules`, `network.mask_request_host`, `network.allow_public_traffic=True`
  - `iam=`, `mcp=`, `get_mcp_url`, `get_mcp_token`, `volume_mounts=`
  - `get_signature`, `Volume`, `Secret`, `Template`
- **Git module (new capability `sandbox-git`):**
  - A native `rayito.Git` / `rayito.AsyncGit` and a TS `Git`, exposed as `sandbox.git` natively and in both shims.
  - A pure client-side wrapper over `commands.run` with E2B's method set and models: `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`.
  - `dangerously_authenticate` stores credentials exactly as E2B does. That is documented as visible to sandbox code, and the SDK never logs or echoes credentials.
  - `image/Dockerfile` installs the pinned `git-core-2.50.1-1.amzn2023.0.1`. `scripts/check_pins.py` gains a gate for it.
- **TypeScript `rayito/e2b` subpath export** (package `exports` plus a second `tsdown` entry) that mirrors E2B's JS SDK:
  - `Sandbox.create(template?, opts)` with E2B's `ConnectionOpts`: `requestTimeoutMs`, `retries`, `logger`, `headers`, `proxy`, `signal`.
  - The statics `kill`, `getInfo`, `getFullInfo`, `isRunning`, `connect`, `pause`, `betaPause`, `setTimeout`, `getMetrics`, `list`, `updateNetwork`.
  - `uploadUrl` and `downloadUrl`.
  - A synchronous `getHost(port): string` plus `getHostHeaders(port)`.
  - `files.watchDir(path, onEvent, opts)`, `pty.create({cols, rows, onData})` and `pty.connect`, `runCode` and the code-context methods, and `git`.
  - E2B error classes, and the same explicit `UnimplementedError` rows as Python.
  - Native TS gains `extraHeaders`, an HTTP CONNECT `proxy`, `retries` and integration user agent, and `AbortSignal` on every call. It also gains `DiskFullError` and `UnimplementedError`.
- **CLI (`rayito[cli]`):** `rayito sandbox create|connect|exec|metrics`. `connect` (and `create` without `--detach`) is an interactive PTY terminal. The access token lives in a `0600` token file or `RAYITO_ACCESS_TOKEN`, and is never printed or passed in argv.
- **Docs:**
  - `docs/site/docs/e2b-compat.md` is rewritten against E2B 2.51, with the full parity ledger and the divergence notes of all six M9 changes.
  - A new `docs/site/docs/git.md`.
  - `cli.md`, both READMEs and CHANGELOGs, `SPEC.md` §3/§4, `SECURITY.md` (T3/T9 notes), `NOTICE` (E2B git attribution), an `AWS_API_NOTES.md` §16 row for the git-core size, and the `MILESTONES.md` M9 line.

Breaking changes for programs written against the 1.x shim are listed in `design.md` "Migration Plan":

- `pause()` returns a `bool`.
- The positional order of sync `watch_dir` and sync `pty.create` changes.
- `proxy=` is honoured instead of ignored with a warning.
- `NotEnoughSpaceException` becomes the class that is actually raised.
- TS disk-full errors become `DiskFullError` instead of `RateLimitError`.

## Capabilities

### New Capabilities
- `sandbox-git`: the git module (native Python sync and async, TypeScript, exposed as `sandbox.git`), its models, errors and credential handling, and `git-core` in the default image.

### Modified Capabilities
- `e2b-compat`: the drop-in contract moves to 2.x. It gets the create/connect kwarg mapping with connection options, the extended `UnimplementedError` list, the 2.x instance models, exceptions and async parity. Added: the connection configuration, the bound client, positional commands, code-context helpers and the real-AWS 2.x corpus acceptance.
- `typescript-sdk`: the `rayito/e2b` subpath entry point with the E2B JS surface, the native connection options (`extraHeaders`, `proxy`, `retries`, `AbortSignal`), `DiskFullError`/`UnimplementedError`, and the real-AWS JS corpus acceptance.
- `cli`: `rayito sandbox create|connect|exec|metrics` and the interactive terminal bridge.
- `process-lifecycle`: `CommandHandle.wait` accepts E2B's `on_pty`/`on_stdout`/`on_stderr` callbacks (sync and async, native).

## Impact

- **Code:**
  - `clients/python/src/rayito/e2b/*`, with new modules `_connection.py`, `_client.py`, `_unimplemented.py` and `_types.py`.
  - Native Python: `rayito/_git_base.py`, `sandbox_{sync,async}/git.py`, `exceptions.py`, `_models.py`, `_transport.py`, `_aws.py`, the `sandbox_{sync,async}` `main.py`/`commands.py` pair and the logging call sites.
  - CLI: `rayito/cli/sandbox.py` and a new `rayito/cli/_terminal.py`.
  - TypeScript: a new `clients/typescript/src/e2b/*`, `src/sandbox/git.ts`, `src/sandbox/git-args.ts`, `src/transport/proxy-tunnel.ts`, `src/errors.ts`, the transport and core call options, `package.json`, `tsdown.config.ts` and `scripts/pack-check.mjs`.
  - Infra: `image/Dockerfile` (one pinned `dnf` package), `scripts/check_pins.py` (dnf gate) and `NOTICE` (three copies).
- **Wire:** no `.proto` change, no new RPC, no `rayd` change. Git runs through `ProcessService.Start`, and the headers travel as ordinary gRPC metadata.
- **AWS API:** no new AWS API parameter. The only additions are botocore `Config` options (`retries`, `proxies`, `user_agent_extra`) and AWS SDK for JS client options (`maxAttempts`, `customUserAgent`, `requestHandler`, `abortSignal`), none of which are AWS API parameters.
- **Dependencies:** none new in Python or TypeScript. Git and the proxy tunnel use the standard library and Node built-ins.
- **Image:** `git-core` plus six dependencies (`less`, `libcbor`, `libedit`, `libfido2`, `openssh`, `openssh-clients`). They total 31,452,146 B of RPM installed size, measured in an emulated arm64 build of the pinned base image. Nothing is warmed, so the memory snapshot is unaffected. The real `codeInstallSizeInBytes` delta is recorded at publish.
- **Depends on** (implementation order): `m9-file-transfer`, `m9-server-timeout`, `m9-sandbox-observability`, `m9-egress-policy`, `m9-deno-kernels`. This change consumes their native surfaces and maps their E2B names in the TS shim.
