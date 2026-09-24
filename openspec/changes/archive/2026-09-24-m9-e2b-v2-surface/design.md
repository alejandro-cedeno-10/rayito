## Context

`rayito.e2b` (M6, `openspec/specs/e2b-compat`) is a composition shim over the native `rayito.Sandbox`. It already covers E2B 1.x:

- `create`, `run_code`, native `commands`, `files` and `pty` wrappers
- `get_info` and `list` with metadata
- `pause`, `connect` and `kill`
- `UnimplementedError` for what AWS cannot do

The research digest (`e2b-inventory`) enumerated E2B 2.51.0 from source, by AST dump of `e2b/__init__.py` and the JS `index.ts` at commit `ccaf9fc`. It compared that against Rayito 0.2.0:

- Several 2.x names already exist natively and only need wiring: code contexts, `write_files`, `include_entry`, `pty.connect`, `pause() -> bool`, the exceptions.
- Several fail with `TypeError`/`AttributeError`, which violates constitution rule 7.
- The TypeScript SDK has no E2B entry point.

Facts this design relies on, all verified:

- **E2B 2.51 signatures.** The E2B clone at `ccaf9fc` has these signatures (Python sync unless noted):
  - `Commands.run(cmd, background=None, envs=None, user=None, cwd=None, on_stdout=None, on_stderr=None, stdin=None, timeout=60, request_timeout=None)`
  - `Commands.connect(pid, timeout=60, request_timeout=None)`; async adds `on_stdout=None, on_stderr=None`
  - `CommandHandle.wait(on_pty=None, on_stdout=None, on_stderr=None)`
  - sync `Filesystem.watch_dir(path, user=None, request_timeout=None, recursive=False, include_entry=False, allow_network_mounts=False)`
  - async `watch_dir(path, on_event, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False, include_entry=False, allow_network_mounts=False)`
  - sync `Pty.create(size, user=None, cwd=None, envs=None, timeout=60, request_timeout=None)` and `Pty.connect(pid, timeout=60, request_timeout=None)`; async `create(size, on_data, user=None, ...)` and `connect(pid, on_data, timeout=60, request_timeout=None)`
  - `Sandbox.create(template=None, timeout=None, metadata=None, envs=None, secure=None, allow_internet_access=None, mcp=None, network=None, iam=None, lifecycle=None, volume_mounts=None, logger=None, **ApiParams)`
  - instance `connect(timeout=None, *, on_resume='restore', **ApiParams)`, `pause(keep_memory=None, **ApiParams) -> bool`, `is_running(request_timeout=None)`
- **E2B 2.51 connection types:**
  - `ApiParams` = `request_timeout, retries, headers, api_headers, api_key, validate_api_key, domain, api_url, debug, proxy, sandbox_url`.
  - `ConnectionConfig.set_integration(cls, integration)` appends to the User-Agent.
  - `merge_api_params`: per-call params win and a per-call `None` falls back to the bound value.
  - `E2B(**ApiParams)` binds by generating subclasses that carry `_bound_api_params`.
- **E2B 2.51 exception tree:**
  - `GitAuthException(AuthenticationException)`, `GitUpstreamException(SandboxException)`
  - `ServiceBusyException(Exception)`
  - `BuildException(Exception)` and `FileUploadException(BuildException)`, where E2B raises the latter only from template builds
  - `NotEnoughSpaceException(SandboxException)`
- **E2B 2.51 aliases:** `Username = str`, `Stdout = str`, `Stderr = str`, `PtyOutput = bytes`, `ALL_TRAFFIC = "0.0.0.0/0"`, `RunCodeLanguage = Literal["python","javascript","typescript","r","java","bash"] | str`, `OutputHandler = Callable[[T], Any]`, `MIMEType(str)`.
- **E2B 2.51 git module:** a pure wrapper over `commands.run` with `GIT_TERMINAL_PROMPT=0` merged into `envs`. Every command is built as a shell-quoted `git [-C path] args...`, and `status` is `git status --porcelain=1 -b`. The module is marked deprecated upstream. E2B is Apache-2.0.
- **E2B JS 2.51:**
  - `ConnectionOpts` = `requestTimeoutMs, retries, logger, headers, proxy, signal` plus the ignored `apiKey/domain/apiUrl/sandboxUrl/apiHeaders/debug`.
  - `SandboxInfo` keys: `sandboxId, templateId, name, metadata, startedAt, endAt, state, cpuCount, memoryMB, envdVersion, allowInternetAccess, network, lifecycle, volumeMounts, sandboxDomain`.
  - `pty.create({cols, rows, onData, user, cwd, envs, timeoutMs})`.
  - `files.watchDir(path, onEvent, opts)`.
  - `ConnectionConfig.setIntegration(integration)`.
  - Error classes: `NotEnoughSpaceError`, `GitAuthError extends AuthenticationError`, `GitUpstreamError`, `ServiceBusyError extends Error`, `BuildError`, `FileUploadError extends BuildError`, `TemplateError`.
- **E2B CLI 2.51:**
  - `sandbox create [template] [-d|--detach] [--timeout s] [-u user] [-c cwd] [-e K=V]`: without `--detach` it connects a terminal and, on exit, shortens the timeout to 1 s.
  - `sandbox exec <id> <command...> [-b] [-c] [-u] [-e]`
  - `sandbox connect <id> [-u] [-c] [-e]`
  - `sandbox metrics <id> [-f]`
- **Rayito native Python:**
  - `pause(*, wait=True) -> bool` already returns False when the sandbox is already suspended.
  - `Commands.run/connect/kill/send_stdin/close_stdin` take keyword-only arguments after the first.
  - `CommandHandle.wait()` takes no arguments.
  - `Pty.connect(pid, *, from_seq, on_data, timeout, request_timeout)` exists.
  - `list/remove/restart_code_context` exist.
  - `is_running()` takes no arguments.
  - `get_host(port)` returns a `HostAccess(str)` whose `.headers` hold `x-aws-proxy-auth`.
  - `TransportSettings(channel_credentials, port, options)` opens every channel.
  - `ProxyAuthPlugin` adds the four proxy headers.
  - `client_config()` = `Config(retries={"mode":"standard","total_max_attempts":5}, connect_timeout=5, read_timeout=60, user_agent_extra="rayito/<ver>")`.
  - `shared_control_plane(session, region=)` caches one plane per `(session, region)`.
  - `translate_client_error` maps `InsufficientCapacityException` to `CapacityException`, and `DiskFullException` is raised for `disk_reserve`/`disk_full`.
  - `UnimplementedError` lives in `rayito/e2b/exceptions.py`.
- **Rayito native TypeScript:**
  - `RequestOptions = { requestTimeoutMs }` has no `signal`, and every stream owns an `AbortController`.
  - `translateConnectError` maps `ResourceExhausted` to `RateLimitError`, with no disk-full distinction.
  - `getHost` is `async` and returns `HostAccess`.
  - `pty.create({ size: {cols, rows}, ... })`.
  - `WatchOptions.onEvent` lives inside the options object.
- **Image:** `image/Dockerfile` has one `dnf install` line (no git). `scripts/check_pins.py` gates only workflow actions and `uvx`.
- **Measured for this design:** in an emulated arm64 run of the pinned base image `al2023-minimal@sha256:05cb9b38…`, `dnf install -y --setopt=install_weak_deps=0 git-core` resolves to the seven packages below. `git --version` prints `git version 2.50.1`.

  | Package | RPM installed size (B) |
  |---|---|
  | `git-core-2.50.1-1.amzn2023.0.1` | 24,588,809 |
  | `less-608-2.amzn2023.0.2` | 814,145 |
  | `libcbor-0.7.0-3.amzn2023.0.2` | 231,079 |
  | `libedit-3.1-38.20210714cvs.amzn2023.0.2` | 285,646 |
  | `libfido2-1.10.0-2.amzn2023.0.2` | 347,542 |
  | `openssh-8.7p1-8.amzn2023.0.18` | 2,180,631 |
  | `openssh-clients-8.7p1-8.amzn2023.0.18` | 3,004,294 |
  | **Total** | **31,452,146** |

## Goals / Non-Goals

**Goals**
- An E2B 2.51 program (Python sync, Python async, JS) runs on Rayito by changing only its import. The real-AWS corpus proves it.
- Every E2B 2.51 public name that Lambda MicroVMs cannot provide fails with `UnimplementedError` that names its reason. None fails with `TypeError` or `AttributeError`.
- A TypeScript `rayito/e2b` entry point with the same contract as the Python shim.
- A git module that works in the default image.
- `rayito sandbox create|connect|exec|metrics`.
- `e2b-compat.md` becomes the single parity ledger for all of M9.

**Non-Goals**
- Any `.proto` or `rayd` change.
- Re-implementing what sibling changes deliver: upload/download URLs, the deadline, lifecycle, egress, metrics history, pagination, and the JS/TS kernels. This change only wires their E2B names in the TS shim, where no sibling owns it, and reads their data into `SandboxInfo`.
- Template, Volume, Secret, snapshots, fork, MCP gateway and workload identity. These are explicit `UnimplementedError`.
- The E2B CLI's `auth`, `template`, `snapshots` and `fork`.
- SSH access: `rayito sandbox connect` is the terminal.

## Decisions

### D1. Ownership, sibling contract and archive order

Each M9 feature change maps its **own** E2B names in the Python shim, as the scope decided:

- `m9-file-transfer`: `upload_url`, `download_url`, gzip, metadata.
- `m9-server-timeout`: `timeout`, `lifecycle`, `max_lifetime`, `set_timeout`, `connect(timeout=)`, `beta_create(auto_pause=)`.
- `m9-egress-policy`: `allow_internet_access`, `network.allow_out/deny_out/egress_proxy/https_ports`, `update_network`.
- `m9-sandbox-observability`: `get_metrics(start, end)`, `list` pagination/order/filters, `SandboxQuery`, `mem_cache`.
- `m9-deno-kernels`: the `javascript`/`typescript` languages and their `js`/`ts` aliases.

This change owns everything else:

- the cross-cutting Python shim surface
- the **whole** TypeScript shim, including the TS mapping of the siblings' features onto the native TS methods they add
- the git module and the CLI
- the docs ledger

Native surfaces this change consumes. The native names are the ones the scope fixed. If a sibling landed a different spelling, only `rayito/e2b/_compat.py::info_from_native`, the shim method bodies and `src/e2b/compat.ts` adapt; the specified behaviour does not change.

| Sibling | Native surface consumed |
|---|---|
| `m9-server-timeout` | Python `create(timeout=, max_lifetime=, on_timeout=)`, `sbx.set_timeout(s)` (EXACT), `Sandbox.set_timeout(id, s, access_token=)`, the class `connect(id, timeout=)`, and the **native instance** `sbx.connect(*, timeout=None, request_timeout=None) -> Self`. The instance connect does, in order: `get-microvm`, `resume-microvm` when suspended without auto-resume, the readiness poll, then `SetTimeout(AT_LEAST)`. The logical deadline appears in `SandboxInfo.expires_at`, and the lifecycle state (`on_timeout`, `auto_resume`, `phase`) in native `SandboxInfo.lifecycle`. TS gets the camelCase mirror: instance `connect({ timeoutMs, requestTimeoutMs })` returns `this`, and static `setTimeout`. The shim's instance `connect()` is this change's (server-timeout design, "The shim's instance `sbx.connect()` is `m9-e2b-v2-surface`'s") and delegates to that native instance method. |
| `m9-egress-policy` | `create(allow_internet_access=, network=)`, `update_network`, native `SandboxInfo.network` (a `NetworkState` with `allow_out`, `deny_out`, `enforcement`), `rayito.ALL_TRAFFIC`. TS gets the mirror and `ALL_TRAFFIC`. |
| `m9-sandbox-observability` | `get_metrics_history(start, end, max_points)`, static `Sandbox.get_metrics(id, access_token=)`, the `list()` paginator, native `SandboxInfo.cpu_count` / `memory_mb` / `agent_version`, `SandboxMetrics.mem_cache_bytes`. TS gets `getMetricsHistory`, static `getMetrics` and the `SandboxPaginator`. |
| `m9-file-transfer` | `files.upload_url/download_url`, `UploadTicket(str)`, `DownloadLink(str)` and the gzip/metadata kwargs. Exceptions: `rayito.exceptions.TransferException(SandboxException)` with `code` and `reason`, `FileUploadException(TransferException)`, and the native `rayito.exceptions.UnimplementedError(feature, reason, doc=None)`. TS gets `files.uploadUrl/downloadUrl`, `read({format:'blob'})`, `TransferError extends SandboxError`, `FileUploadError extends TransferError`, `DiskFullError extends SandboxError` and `UnimplementedError extends Error`. |
| `m9-deno-kernels` | `SUPPORTED_LANGUAGES` gains `typescript`, with the shim aliases `js`→`javascript` and `ts`→`typescript`. |

**Archive order.** This change is archived **after** the other five. Its MODIFIED blocks under `specs/e2b-compat/spec.md` are the integrated final text of those requirements. They already carry the siblings' requirement text and scenarios as they stand in their drafts:

- `E2B create kwargs map to Rayito or warn`: from `m9-server-timeout` and `m9-egress-policy`.
- `E2B features without an AWS primitive raise UnimplementedError`: from all five siblings.
- `E2B-shaped models on the instance`: from `m9-sandbox-observability` and `m9-server-timeout`. The one requirement the siblings modify and this change does not touch is `E2B-shaped listing`, which is owned by `m9-sandbox-observability`. `tasks.md` §10.3 re-diffs every MODIFIED block against the then-current `openspec/specs/e2b-compat/spec.md` before archiving. Any sibling wording that differs gets merged in, so a sibling's archived text is never silently lost.

**Shared exception classes.** `m9-file-transfer` defines them. This change only re-exports them and checks the contract with a unit test:

- `rayito.exceptions.FileUploadException(TransferException)`, where `TransferException(SandboxException)` carries `code: str` and `reason: str`. It is raised by `UploadTicket.wait()` and by large writes when the import ends `FAILED` with `failed_precondition`, `unavailable`, `cancelled`, `internal` or an unknown code.
- TS equivalent: `FileUploadError extends TransferError extends SandboxError`.

`UnimplementedError` is a single native class: `rayito.exceptions.UnimplementedError(NotImplementedError)`, with `feature`, `reason` and an optional `doc`. It is defined by `m9-file-transfer` (or by whichever sibling landed first), and `rayito.e2b.UnimplementedError` is a **re-export** of it. The shim builds every instance with `doc=COMPAT_DOC_PATH`, so shim messages still point at `docs/site/docs/e2b-compat.md`.

`m9-egress-policy`'s design drafted the other form: a shim subclass that keeps the E2B message. If that form landed, task 5.3 collapses it into the re-export, so `rayito.e2b.UnimplementedError is rayito.UnimplementedError` holds and one `except` catches every site. Its native raises keep working unchanged, because they already raise the native class. The same rule applies to the TS `UnimplementedError`.

### D2. Proto and `rayd` delta: none

> **Status: checked by the Contract step (2026-09-22).** Nothing to apply: every RPC this change maps onto now exists in `proto/rayito/v1/` (transfer RPCs in `filesystem.proto`, `LifecycleService`, `NetworkService`, `HealthService.MetricsHistory`, `HealthResponse` 12-15).

- **Proto delta: none.** No message, field, enum, RPC or comment changes. Git runs as `ProcessService.Start` commands, headers are plain gRPC metadata, and every other surface maps onto RPCs that exist after the sibling changes.
- **`rayd` delta: none** in the original plan; the image rebuild bundles the existing `rayd`. Amended by D26 (2026-09-23): one watch-translation fix found by the real-AWS corpus, no proto change.
- **Sidecar delta: none.**

The only image change is D16.

### D3. Python module layout

Shim, under `clients/python/src/rayito/e2b/`:

| File | Content |
|---|---|
| `__init__.py` | The 2.x docstring and `__all__` (D4). |
| `_sync.py` | `Sandbox`, plus the wrappers `Commands`, `Filesystem`, `Pty`. The native `Git` is exposed as-is. |
| `_async.py` | `AsyncSandbox`, plus the wrappers `AsyncCommands`, `AsyncFilesystem`, `AsyncPty`. |
| `_compat.py` | Pure. Existing helpers (including `m9-egress-policy`'s `map_network`), plus `map_create_kwargs` for 2.x, `info_from_native` for 2.x, `reject_callable_in_user_slot`, `native_stdin`. |
| `_connection.py` | **New, pure.** `ConnectionConfig`, `ConnectionSettings`, `split_api_params`, `validate_extra_headers`, `validate_proxy_url`, `resolve_retries`, `merge_bound_params`, `connection_overrides`, `IGNORED_API_PARAMS`. |
| `_client.py` | **New.** `E2B`, `bind_class`. |
| `_unimplemented.py` | **New, pure.** `UNIMPLEMENTED_REASONS` (D14) and `unimplemented(feature)`. |
| `_types.py` | **New.** Type aliases (D4). |
| `_models.py` | `SandboxInfo` for 2.x (D10), and the existing `PtySize`, `SandboxState` and paginators. `SandboxQuery` and `SandboxMetrics` stay as `m9-sandbox-observability` leaves them. |
| `exceptions.py` | D13. |

Native Python, under `clients/python/src/rayito/`:

| File | Change |
|---|---|
| `_git_base.py` | **New, pure.** D15. |
| `sandbox_sync/git.py`, `sandbox_async/git.py` | **New.** `Git` and `AsyncGit`. |
| `sandbox_{sync,async}/main.py` | Add a `git` property, `logger=` on `create`/`connect` (D6), and `is_running(*, request_timeout=None)`. The instance `connect(timeout=)` comes from `m9-server-timeout`. |
| `sandbox_{sync,async}/commands.py` | Add the `wait()` callbacks (D11). |
| `_transport.py` | Add `TransportSettings.extra_metadata`, `TransportSettings.http_proxy`, and the `ProxyAuthPlugin(extra=...)` parameter. |
| `_aws.py` | Add `ClientSettings`, `client_config(settings=None)`, `from_session(..., settings=None)`, and `shared_control_plane(..., settings=None)`. |
| `_models.py` | Add `Logs.to_json`, `ExecutionError.to_json`, `CodeContext.from_json`. |
| `exceptions.py` | Add `GitAuthException` and `GitUpstreamException`. `UnimplementedError`, `TransferException` and `FileUploadException` come from `m9-file-transfer` (D1). |
| `__init__.py` | Export `Git`, `AsyncGit`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`, `GitAuthException`, `GitUpstreamException`, `UnimplementedError`, `ClientSettings`. |

CLI, under `clients/python/src/rayito/cli/`:

- `sandbox.py`: four new commands.
- `_terminal.py`: new; stdlib only, no `typer`.
- `_tokens.py`: new; stdlib only. Token file read and write.

### D4. The 2.x contract and exports

The `rayito/e2b/__init__.py` docstring changes to "Shim de compatibilidad con el SDK Python de E2B 2.x (contrato: `e2b` 2.51.0, `e2b-code-interpreter` 2.10.0)". It lists the import lines for `e2b`, `e2b_code_interpreter` and `e2b.exceptions`.

`__all__` is a superset of the M6 list plus the following additions:

- `ALL_TRAFFIC`
- `AsyncCommandHandle`, `AsyncWatchHandle`
- `AsyncSecret`, `AsyncTemplate`, `AsyncVolume`
- `BuildException`
- `Chart2D`
- `ConnectionConfig`
- `E2B`
- `FileNotFoundException`, `FileUploadException`
- `Git`, `GitAuthException`, `GitBranches`, `GitFileStatus`, `GitResetMode`, `GitStatus`, `GitUpstreamException`
- `MIMEType`, `OutputHandler`, `PtyOutput`, `RunCodeLanguage`
- `SandboxNotFoundException`
- `Secret`
- `ServiceBusyException`
- `Stderr`, `Stdout`
- `Template`
- `Username`
- `Volume`
- `get_signature`

The names the sibling changes add, such as ticket types, are kept too.

`rayito/e2b/exceptions.py` `__all__` adds `BuildException`, `FileNotFoundException`, `FileUploadException`, `GitAuthException`, `GitUpstreamException`, `SandboxNotFoundException` and `ServiceBusyException`.

`_types.py` defines the aliases exactly as follows. `T` is a module `TypeVar`.

```python
Username: TypeAlias = str
Stdout: TypeAlias = str
Stderr: TypeAlias = str
PtyOutput: TypeAlias = bytes
MIMEType: TypeAlias = str
RunCodeLanguage: TypeAlias = Literal["python", "javascript", "typescript", "r", "java", "bash"] | str
OutputHandler: TypeAlias = Callable[[T], Any]
```

Re-exports with no copies: `Chart2D` from `rayito._charts`, `ALL_TRAFFIC` from `rayito` (defined by `m9-egress-policy`), `AsyncCommandHandle` and `AsyncWatchHandle` from `rayito`, `Git` from `rayito`. The Rayito extension `AsyncGit` is exported from `rayito` only, because E2B names both classes `Git`.

`Template`, `AsyncTemplate`, `Volume`, `AsyncVolume`, `Secret` and `AsyncSecret` are classes whose `__new__` and every public classmethod E2B defines raise `unimplemented(...)`:

- `Template`: `build`, `build_in_background`, `get_build_status`, `exists`, `alias_exists`, `assign_tags`, `remove_tags`, `get_tags`, `to_json`, `to_dockerfile`
- `Volume`: `create`, `connect`, `destroy`, `list`, `get_info`
- `Secret`: `create`, `update`, `get_info`, `list`, `exists`, `destroy`, `fill`, `iam_token`

They are generated by one helper, `unimplemented_resource(name, feature, methods)`, so `Template.build(...)` raises `UnimplementedError` and never `AttributeError`. `get_signature(*args, **kwargs) -> NoReturn` raises `unimplemented("get_signature")`.

### D5. `create` / `connect` signatures and the kwarg mapping (Python)

**Signatures.**

```python
@classmethod
def create(
    cls,
    template: str | None = None,
    timeout: int | None = None,
    metadata: Mapping[str, str] | None = None,
    envs: Mapping[str, str] | None = None,
    secure: bool | None = None,
    allow_internet_access: bool | None = None,
    mcp: Any | None = None,
    network: Mapping[str, Any] | None = None,
    iam: Any | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    volume_mounts: Any | None = None,
    logger: logging.Logger | None = None,
    *,
    max_lifetime: int | None = None,           # m9-server-timeout
    region: str | None = None, session: Any | None = None,
    template_version: str | None = None, execution_role_arn: str | None = None,
    allowed_ports: Sequence[PortLike] | None = None, ingress: Sequence[str] | None = None,
    logging: LoggingOption = "disabled", access_token: str | None = None,
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    keep_on_failure: bool = False, control_plane: Any | None = None, transport: Any | None = None,
    **api_params: Unpack[ApiParams],
) -> Self
```

- `ApiParams` is a `TypedDict(total=False)` in `_connection.py` with E2B's eleven keys. Any other `**` key raises `TypeError: create() got an unexpected keyword argument '<k>'`, the same text Python itself produces, and that keeps `pool=`, `idle=` and `egress=` refused.
- `AsyncSandbox.create` has the identical signature as a coroutine.
- The deprecated constructor `Sandbox(...)` keeps its current 1.x keyword surface, including `sandbox_id` for connect semantics. It gains `logger`, `retries` and `headers` as keyword-only, and runs the same mapping.
- `create` no longer delegates through `cls(*args, **kwargs)`: it calls the mapping and then `cls(_native=...)`.

**Mapping.** `_compat.map_create_kwargs` and `_connection.split_api_params` are pure and unit-tested. The first row of the table is not a single kwarg: it is the set of kwargs that `split_api_params` returns as warning messages and otherwise ignores.

| E2B kwarg | Rayito |
|---|---|
| `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`, `validate_api_key`, `api_headers`, `secure=False` | One `RayitoCompatWarning` each, naming the kwarg and never its value; otherwise ignored. `secure=None/True` produces no warning. |
| `template` | As given; `None` means `RAYITO_TEMPLATE`. |
| `timeout`, `lifecycle`, `max_lifetime` | The `m9-server-timeout` mapping, which this change does not duplicate. The logical deadline is 300 s when `None`. `max_lifetime` defaults to `max(3600, min(timeout + 60, 28800))`, capped at 28800, and becomes `maximumDurationInSeconds`. `lifecycle` follows E2B's validation (`InvalidArgumentException`), and `keep_memory: False` raises `UnimplementedError("lifecycle.on_timeout.keep_memory=False")`. That mapping builds the error with `unimplemented(...)`, so it carries the D14 reason (task 5.5). The older-image gate (terminate, then `UnimplementedError` naming the M9 image) is also server-timeout's. |
| `metadata`, `envs` | As given. |
| `allow_internet_access`, `network` | The `m9-egress-policy` mapping, its pure `_compat.map_network`. It already raises `UnimplementedError` for `rules` (any value), `mask_request_host` (any value) and `allow_public_traffic=True`, accepts `allow_public_traffic=False` as a no-op, applies its `https_ports` measurement rule, and raises `TypeError` naming any other unknown key. This change does not duplicate that logic: `map_network` builds those three errors with `unimplemented(...)` from `_unimplemented.py`, so the reason text is the single D14 string (task 5.5). |
| `mcp`, `iam`, `volume_mounts` | Any value other than `None` raises `UnimplementedError("mcp" / "iam" / "volume_mounts")`. |
| `logger` | Native `create(logger=)` (D6). |
| `request_timeout` | Native `request_timeout`. |
| `retries`, `proxy`, `headers` | D6: `ConnectionSettings` → `transport=` (headers, proxy) and a dedicated `control_plane=` (retries, proxy, integration). |
| native pass-through keywords | As today. `ingress` defaults to `["ALL_INGRESS"]`. |

Every `UnimplementedError` and `TypeError` in the table is raised before any AWS or agent call. That is asserted with the stubbed control plane and the fake `rayd` recording nothing.

**`connect`** becomes a `class_method_variant("_class_connect")`:

- Class form: `Sandbox.connect(sandbox_id, timeout=None, *, on_resume="restore", logger=None, access_token=None, region=None, session=None, ready_timeout=..., reconnect_timeout=..., control_plane=None, transport=None, **api_params)`.
- Instance form: `sbx.connect(timeout=None, *, on_resume="restore", **api_params) -> Self`.
- `on_resume` must be `"restore"` or `"reboot"`. `"reboot"` raises `UnimplementedError("connect(on_resume='reboot')")`, and any other value raises `InvalidArgumentException`.

The class form maps to native `connect(sandbox_id, timeout=, logger=, access_token=, ..., transport=, control_plane=)` with the D6 settings applied.

The instance form validates `on_resume` and warns about ignored `ApiParams`. It then calls the native instance `self._native.connect(timeout=timeout, request_timeout=request_timeout)` from `m9-server-timeout` (D1), which runs `get-microvm`, a resume when needed, the readiness poll and the AT_LEAST extension on the already-bound native object, and returns `self`. It never opens new channels or rebinds the native object. `request_timeout` is taken from `api_params`.

### D6. Connection options plumbing

**Python native changes.** All are additive, with defaults unchanged.

- `TransportSettings` gains `extra_metadata: tuple[tuple[str, str], ...] = ()` and `http_proxy: str | None = None`.
  - `open_channel` and `open_aio_channel` pass `options=list(self.options) + ([("grpc.http_proxy", self.http_proxy)] if self.http_proxy else [])`.
  - Every `ProxyAuthPlugin` built from these settings receives `extra=settings.extra_metadata` and appends those pairs **after** the four reserved keys. That covers the authenticated unary channel, the stream channel and the anonymous `Health` plugin used by `probe_health`/`probe_metadata`.
- `_aws.ClientSettings` is a frozen, hashable dataclass: `retries: int | None = None`, `proxy: str | None = None`, `integration: str | None = None`.
- `client_config(settings: ClientSettings | None = None) -> Config`:
  - `retries={"mode": "standard", "total_max_attempts": 5 if settings.retries is None else settings.retries + 1}`, `connect_timeout=5`, `read_timeout=60`.
  - `user_agent_extra="rayito/<ver>"`, followed by `" " + integration` when an integration is set.
  - `proxies={"http": proxy, "https": proxy}` when a proxy is set.
- `LambdaMicrovmsControlPlane.from_session(session, *, region, settings=None)` and `shared_control_plane(session, *, region, settings=None)`. The cache key becomes `(session, region, settings)`, so N sandboxes with the same options share one set of token buckets, as ARCHITECTURE.md requires.
- `logger`:
  - Native `Sandbox.create(..., logger: logging.Logger | None = None)` and `connect(..., logger=None)`, in both trees. TS already has `logger`.
  - The `Sandbox` stores `self._logger = logger or <module logger "rayito.sandbox">`. It hands that logger to its `Commands`, `Filesystem`, `Pty`, `CodeClient`, `PersistenceClient`, `Git` and `TokenRefresher` (a new `logger=` constructor parameter, defaulting to the module logger).
  - Every record those objects emit goes to that logger.
  - The module-level functions they call take a `log: logging.Logger` parameter: `probe_health`, `wait_for_state`, `terminate_quietly`, and the readiness and reconnect helpers.
  - The `create` classmethod logs `run-microvm aceptado` through `logger or module logger`.
  - The shared control plane keeps its module logger, because it is process-wide.
  - Record content does not change, so the T9 hygiene rules still hold.

**Pure validation** (`_connection.py`, mirrored in TS `src/e2b/compat.ts` and native `src/transport/headers.ts`):

- `validate_extra_headers(headers: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]`:
  - Keys are lower-cased.
  - A key is refused with `InvalidArgumentException("headers: la clave '<key>' está reservada")` when:
    - it is in `RESERVED_METADATA_KEYS = {"x-aws-proxy-auth", "x-aws-proxy-port", "x-aws-proxy-force-h2", "x-access-token", "rayito-compress", "user-agent", "content-type", "te", "host"}`, or
    - it starts with `x-aws-proxy-`, `grpc-` or `:`, or
    - it ends with `-bin`, or
    - it is not an RFC 9110 token.
  - A value outside printable ASCII `0x20..0x7E` is refused with `InvalidArgumentException("headers: el valor de '<key>' no es ASCII imprimible")`.
  - Messages name the key and never the value.
- `validate_proxy_url(proxy: str | None) -> str | None`:
  - `None` passes. Anything that is not a `str` raises `InvalidArgumentException("proxy debe ser una URL http://host:puerto")`.
  - The scheme must be `http` (grpc-core's `grpc.http_proxy` only speaks HTTP CONNECT), and there must be a host and an explicit port.
  - Userinfo is allowed. The URL is never logged, and it is never included in an exception message.
- `resolve_retries(retries: int | None) -> int | None`: `None` passes. A `bool`, a non-`int` or a negative value raises `InvalidArgumentException`.
- `connection_overrides(settings, *, transport, control_plane, session, region) -> tuple[TransportSettings | None, ControlPlane | None]`:
  - With no headers and no proxy, `transport` is returned unchanged. Otherwise it returns `dataclasses.replace(transport or TransportSettings(), extra_metadata=..., http_proxy=...)`.
  - With no retries, no proxy and no integration, the control plane is returned unchanged. If an explicit `control_plane` is combined with any of them: `InvalidArgumentException("retries/proxy/set_integration no se combinan con control_plane=")`, because silently ignoring them would be a lie. Otherwise: `shared_control_plane(session, region=region, settings=ClientSettings(retries, proxy, ConnectionConfig._integration))`.

Every class-variant call (`kill`, `get_info`, `pause`, `beta_pause`, `connect`, `list`, `set_timeout`, `get_metrics`, `update_network`) resolves its settings the same way through `native_call_kwargs(cls, call_kwargs)`. That helper merges `cls._bound_params` (D8), splits `ApiParams`, emits the warnings, and returns native kwargs with `transport` and `control_plane` resolved.

### D7. `ConnectionConfig`, `set_integration`, `sbx.connection_config`

```python
class ConnectionConfig:
    _integration: ClassVar[str | None] = None
    def __init__(self, *, request_timeout: float | None = None, retries: int | None = None,
                 headers: Mapping[str, str] | None = None, proxy: str | None = None,
                 logger: logging.Logger | None = None, region: str | None = None,
                 **ignored: Any) -> None
    @classmethod
    def set_integration(cls, integration: str | None) -> None
    request_timeout: float            # property; 60.0 when None
    retries: int | None               # property
    headers: Mapping[str, str]        # property, MappingProxyType of the validated pairs
    proxy: str | None                 # property
    logger: logging.Logger | None     # property
    region: str | None                # property
    integration: str | None           # property, snapshot of _integration at construction
    def get_request_timeout(self, request_timeout: float | None = None) -> float
```

- The constructor validates with the D6 helpers. `ignored` accepts E2B's ignored keys and warns once per key, as `create` does.
- `set_integration` validates that the value is `None` or a non-empty string of printable ASCII without spaces around `/`, then stores it. Configs and control planes built afterwards read it. A call to `set_integration` does not rebuild existing control planes, as in E2B ("call once at startup").
- `sbx.connection_config` (sync and async; previously `UnimplementedError`) returns a `ConnectionConfig` snapshot of the sandbox's effective settings:
  - the native `region`
  - the native `request_timeout`
  - the `retries`, `headers`, `proxy` and `logger` given at create/connect
  - the `integration` in force at create
  It is built once at bind time and cached.

### D8. The bound `E2B` client

```python
class E2B:
    def __init__(self, *, region: str | None = None, session: Any | None = None,
                 control_plane: Any | None = None, **api_params: Unpack[ApiParams]) -> None
    Sandbox: type[rayito.e2b.Sandbox]          # instance attributes
    AsyncSandbox: type[rayito.e2b.AsyncSandbox]
    Template: NoReturn  # property → raises unimplemented("Template")
    AsyncTemplate, Volume, AsyncVolume, Secret, AsyncSecret: same, feature "Volume" / "Secret" / "Template"
```

- `bind_class(cls, params)` returns `type(cls.__name__, (cls,), {"_bound_params": MappingProxyType(dict(params)), "__module__": cls.__module__})`. The params dict is copied, so later mutation by the caller cannot change the binding.
- `rayito.e2b.Sandbox._bound_params` defaults to an empty `MappingProxyType`.
- `merge_bound_params(bound, call)` returns `{**bound, **{k: v for k, v in call.items() if v is not None}}`, E2B's rule. `headers` is **not** deep-merged: per-call `headers` replace the bound ones, also as in E2B.
- Every classmethod (`create`, `connect`, `list`, `kill`, `get_info`, `pause`, `beta_pause`, `set_timeout`, `get_metrics`, `update_network`, `beta_create`) merges through it. Instances created from a bound class keep the binding for their instance-variant calls.
- Warnings for `api_key` and the other ignored params passed to `E2B(...)` are emitted once, at client construction, not on every call.

### D9. Instance surface

All sync and async; the async forms are coroutines only where E2B's are.

| Member | Behaviour |
|---|---|
| `sandbox_id`, `sandbox_domain` | Unchanged; `sandbox_domain` is the endpoint. |
| `envd_api_url`, `envd_direct_url` | Properties, both `f"https://{native.endpoint}"`. The docstring says every request also needs `get_host(8080).headers`. |
| `traffic_access_token` | Property: `native.get_host(8080).headers["x-aws-proxy-auth"]`, the JWE currently held for rayd's port. It rotates: the refresher re-mints at 45 min and the TTL is at most 60 min (AWS_API_NOTES §3). The docstring says it is a bearer credential for the endpoint and must not be logged. Async: a plain property; the token store is synchronous. |
| `connection_config` | D7. |
| `is_running(request_timeout=None) -> bool` | Native `is_running(request_timeout=request_timeout)`. The native method gains `*, request_timeout: float \| None = None`, used as the Health probe timeout (`min(ReadinessPoll.MAX_RPC_TIMEOUT, request_timeout or self._request_timeout)`). |
| `pause(keep_memory=None, **api_params) -> bool` | `keep_memory is False` raises `UnimplementedError("pause(keep_memory=False)")`. Otherwise it returns native `pause(wait=True)`: True when it suspended the sandbox, False when it was already `SUSPENDING`/`SUSPENDED`. The class form `Sandbox.pause(sandbox_id, keep_memory=None, **api_params) -> bool` is native `_class_pause`. `beta_pause` is the same function. |
| `connect(...)` | D5. |
| `get_host(port) -> HostAccess` | Unchanged; `HostAccess` is a `str` subclass holding the hostname. |
| `git` | Property: the native `Git`/`AsyncGit` of `native.git`. |
| `fork`, `create_snapshot`, `list_snapshots`, `get_mcp_url`, `get_mcp_token` (instance), `Sandbox.fork(id)`, `Sandbox.delete_snapshot(id)`, `Sandbox.list_snapshots(...)` | Raise `unimplemented(...)`, taking `*args, **kwargs` so that any E2B call shape reaches the explicit error. |

### D10. `SandboxInfo` 2.x mapping

`rayito.e2b.SandboxInfo` is a frozen dataclass. The field order is E2B's, and `raw_state` is kept last as a Rayito extension.

```python
sandbox_id: str
sandbox_domain: str | None
template_id: str
name: str | None
metadata: dict[str, str] | None
started_at: datetime
end_at: datetime | None
state: SandboxState
cpu_count: int | None
memory_mb: int | None
envd_version: str | None
allow_internet_access: bool | None = None
network: dict[str, list[str]] | None = None
lifecycle: dict[str, Any] | None = None
volume_mounts: list[dict[str, str]] = field(default_factory=list)
raw_state: str = ""
```

`info_from_native(info)` fills it from native `SandboxInfo` or `SandboxListItem`:

| Field | Source |
|---|---|
| `sandbox_domain` | `info.endpoint`; `None` for list items. |
| `template_id`, `name` | Image ARN and name, as today. |
| `metadata` | As today (`None` when not read). |
| `end_at` | Native `expires_at`, which after `m9-server-timeout` is the logical deadline; `None` on list items. |
| `cpu_count`, `memory_mb` | Native `cpu_count`, `memory_mb` from Health (observability). `None` when not read from the agent (list items without a metadata query, or an agent older than M9). |
| `envd_version` | Native `agent_version`; `None` when unknown. |
| `lifecycle` | `{"on_timeout": "kill" \| "pause", "auto_resume": bool}` from the native lifecycle state. `None` when the phase is `UNMANAGED`, or when Health carries no lifecycle. |
| `network` | `{"allow_out": [...], "deny_out": [...]}` from the native `NetworkState`. `None` when no guest policy was read. |
| `allow_internet_access` | `False` when a guest policy was read whose `deny_out` contains `ALL_TRAFFIC` and whose `allow_out` is empty. `True` when a policy was read that does not deny everything, or when no policy exists and the egress connectors include `INTERNET_EGRESS`. `None` otherwise (list items). |
| `volume_mounts` | Always `[]`. |

`ListedSandbox` stays an alias of `SandboxInfo`. The docs ledger marks each field populated or documented-empty.

### D11. Commands, files and PTY wrappers (E2B 2.x shapes)

**Native addition (process-lifecycle ADDED):**

```python
CommandHandle.wait(self, on_pty: Callable[[bytes], Any] | None = None,
                   on_stdout: Callable[[str], Any] | None = None,
                   on_stderr: Callable[[str], Any] | None = None) -> CommandResult
```

`AsyncCommandHandle.wait(...)` has the same keywords, and its callbacks may be sync or `async`: the result is awaited when it is awaitable. Each `(stdout, stderr, pty)` chunk yielded while `wait()` consumes the stream is passed to the matching callback, **after** any `on_stdout`/`on_stderr` given at `run()` time. Chunks already consumed by earlier iteration are not replayed. The result, exceptions and idempotence are unchanged.

**Shim `Commands`** (sync; `AsyncCommands` has the same shape as coroutines, and its `connect` adds `on_stdout=None, on_stderr=None` after `request_timeout`):

```python
def run(self, cmd: str, background: bool | None = None, envs=None, user=None, cwd=None,
        on_stdout=None, on_stderr=None, stdin: bool | None = None,
        timeout: float | None = 60, request_timeout: float | None = None) -> CommandResult | CommandHandle
def connect(self, pid: int, timeout: float | None = 60, request_timeout: float | None = None) -> CommandHandle
def list(self, request_timeout=None) -> list[ProcessInfo]
def kill(self, pid: int, request_timeout=None) -> bool
def send_stdin(self, pid: int, data: str | bytes, request_timeout=None) -> None
def close_stdin(self, pid: int, request_timeout=None) -> None
```

These delegate to the native keyword-only methods. `background=None` means `False`, and `stdin=None` means `False` (`native_stdin`). The native `tag=` stays reachable through `sbx.native.commands`. The handles returned are the native `CommandHandle` and `AsyncCommandHandle`, so `wait(on_stdout=...)` works.

**Shim `Filesystem`** (sync):

- `write(path, data, user=None, request_timeout=None, **transfer_kwargs)` keeps both forms (`str` path or `Sequence[WriteEntry]`).
- `write_files(files: Sequence[WriteEntry], user=None, request_timeout=None, **transfer_kwargs) -> list[WriteInfo]` maps to native `write_files`. `transfer_kwargs` are `gzip`, `use_octet_stream` and `metadata`, whose semantics are owned by `m9-file-transfer`.
- ```python
  watch_dir(self, path: str, user: str | None = None, request_timeout: float | None = None,
            recursive: bool = False, include_entry: bool = False, allow_network_mounts: bool = False,
            *, on_event: EventCallback | None = None, on_exit: ExitCallback | None = None,
            timeout: float | None = None) -> WatchHandle
  ```
  - `include_entry` passes to native `watch_dir(include_entry=)`.
  - `allow_network_mounts` is accepted and ignored, because the sandbox has no network mounts. That is documented, and no warning is emitted since the value cannot change the behaviour.
  - `timeout=None` means the watch lives until `stop()`, matching E2B 2.x sync polling watchers. An open watch's keepalives cross the endpoint and count as idle activity, and the docstring says so.
  - `reject_callable_in_user_slot(user, "watch_dir")` raises `InvalidArgumentException("watch_dir: on_event es keyword-only en el contrato 2.x (el segundo posicional es user)")` when `user` is callable. That catches 1.x-shim calls loudly.

**Shim `AsyncFilesystem`:** `watch_dir(path, on_event, on_exit=None, user=None, request_timeout=None, timeout=60, recursive=False, include_entry=False, allow_network_mounts=False) -> AsyncWatchHandle`, E2B's async order. The other methods are as in the sync shim.

**Shim `Pty`** (sync):

```python
def create(self, size: PtySize, user=None, cwd=None, envs=None, timeout: float | None = 60,
           request_timeout=None, *, on_data: PtyDataCallback | None = None) -> PtyHandle
def connect(self, pid: int, timeout: float | None = 60, request_timeout=None,
            *, on_data: PtyDataCallback | None = None) -> PtyHandle
def send_stdin(self, pid, data: bytes, request_timeout=None) -> None
def resize(self, pid, size: PtySize, request_timeout=None) -> None
def kill(self, pid, request_timeout=None) -> bool
```

- A callable in `user` raises `InvalidArgumentException("pty.create: on_data es keyword-only en el contrato 2.x")`.
- `connect` is native `pty.connect(pid, from_seq=0, on_data=on_data, timeout=timeout, request_timeout=request_timeout)`.

**Shim `AsyncPty`:** `create(size, on_data, user=None, cwd=None, envs=None, timeout=60, request_timeout=None)` and `connect(pid, on_data, timeout=60, request_timeout=None)`. The rest are as in the sync shim.

### D12. Code contexts and JSON round-trips

- The shim gains `list_code_contexts(request_timeout=None) -> list[Context]`, `remove_code_context(context: Context | str, request_timeout=None) -> None` and `restart_code_context(context: Context | str, request_timeout=None) -> None` (async: coroutines). They delegate to the native methods.
- `create_code_context(cwd=None, language=None, request_timeout=None)` is unchanged. Its language normalisation (`js`→`javascript`, `ts`→`typescript`) is owned by `m9-deno-kernels`.
- Native `_models.py` additions:
  - `Logs.to_json(self) -> str` = `json.dumps({"stdout": list(self.stdout), "stderr": list(self.stderr)})`.
  - `ExecutionError.to_json(self) -> str` = `json.dumps({"name": ..., "value": ..., "traceback": ...})`.
  - `CodeContext.from_json(cls, data: Mapping[str, str]) -> CodeContext` = `cls(id=data["id"], language=data["language"], cwd=data["cwd"])`. A missing key raises `InvalidArgumentException` naming the key.
- `Execution.to_json` already exists natively. A unit test asserts `json.loads(execution.to_json())["logs"] == logs.to_json()`, E2B's nesting, which serialises logs as a string.

### D13. Exceptions

| Name in `rayito.e2b.exceptions` | Class | Raised by Rayito |
|---|---|---|
| `SandboxException`, `TimeoutException`, `InvalidArgumentException`, `NotFoundException`, `AuthenticationException`, `RateLimitException`, `CommandExitException` | native, unchanged | yes |
| `FileNotFoundException`, `SandboxNotFoundException` | native re-exports (**new**) | yes |
| `NotEnoughSpaceException` | `= rayito.DiskFullException` (**alias**; was a separate never-raised class) | yes, for `disk_reserve`/`disk_full` |
| `ServiceBusyException` | `= rayito.CapacityException` (alias) | yes, for `InsufficientCapacityException` |
| `FileUploadException` | native `FileUploadException(TransferException)`, with `code` and `reason` (D1, `m9-file-transfer`) | yes, when a transfer import ends `FAILED` |
| `GitAuthException` | native `GitAuthException(AuthenticationException)` | yes (D15) |
| `GitUpstreamException` | native `GitUpstreamException(SandboxException)` | yes (D15) |
| `TemplateException` | `TemplateException(SandboxException)`, unchanged | never |
| `BuildException` | `BuildException(Exception)` (**new**), docstring "never raised: there is no template build API" | never |
| `UnimplementedError` | native `rayito.exceptions.UnimplementedError(NotImplementedError)`, re-exported | yes |
| `RayitoCompatWarning` | unchanged | warning |

Divergence, documented: in E2B, `FileUploadException` subclasses `BuildException`, because E2B only raises it from template builds. In Rayito it subclasses `SandboxException`, because it is only raised for transfer imports.

### D14. `UnimplementedError` table

`rayito/e2b/_unimplemented.py` holds `UNIMPLEMENTED_REASONS: Final[Mapping[str, str]]`. Python feature keys are on the left, TS feature keys in the middle. Each reason string is verbatim and appears once per SDK: Python `_unimplemented.py`, TS `src/e2b/unimplemented.ts`.

| Python feature | TS feature | Reason (verbatim, user-facing Spanish) |
|---|---|---|
| `fork`, `create_snapshot`, `list_snapshots`, `delete_snapshot` | `fork`, `createSnapshot`, `listSnapshots`, `deleteSnapshot` | `ninguna operación de Lambda MicroVMs copia la memoria de un MicroVM en marcha (AWS_API_NOTES.md §1 y §15); el análogo de sólo ficheros es checkpoint_files() + create(persist=)` |
| `connect(on_resume='reboot')` | `connect({ onResume: 'reboot' })` | `resume-microvm siempre restaura memoria y disco (AWS_API_NOTES.md §5); el análogo es reincarnate(), con un id nuevo` |
| `pause(keep_memory=False)` | `pause({ keepMemory: false })` | `suspend-microvm siempre guarda memoria y disco (AWS_API_NOTES.md §5); el análogo es checkpoint_files() + kill()` |
| `lifecycle.on_timeout.keep_memory=False` | `lifecycle.onTimeout.keepMemory=false` | same as the previous row |
| `network.rules` | `network.rules` | `no hay un proxy de egress fuera del VM donde inyectar cabeceras: el proxy de Lambda MicroVMs sólo gestiona el ingress (AWS_API_NOTES.md §7)` |
| `network.mask_request_host` | `network.maskRequestHost` | `el proxy de Lambda MicroVMs siempre reenvía Host: <endpoint> y no lo reescribe (AWS_API_NOTES.md §7)` |
| `network.allow_public_traffic=True` | `network.allowPublicTraffic=true` | `no existe acceso sin autenticar: toda petición al endpoint exige X-aws-proxy-auth (AWS_API_NOTES.md §3 y §7); allow_public_traffic=False es el comportamiento permanente` |
| `iam` | `iam` | `los MicroVMs no emiten tokens con audiencia: la única identidad es el execution role por IMDSv2 (AWS_API_NOTES.md §9)` |
| `mcp`, `get_mcp_url`, `get_mcp_token` | `mcp`, `getMcpUrl`, `getMcpToken` | `cada petición al endpoint necesita además un JWE en cabecera con TTL de 60 min como máximo (AWS_API_NOTES.md §3 y §7), así que una URL con token fijo no sirve; usa el servidor rayito-mcp` |
| `volume_mounts`, `Volume` | `volumeMounts`, `Volume` | `SPEC.md §4 deja fuera EFS y los montajes compartidos; usa persist= (S3) o upload_url/download_url` |
| `get_signature` | `getSignature` | `una firma de envd no autentica en el proxy: el JWE sólo viaja en cabecera o en el subprotocolo WebSocket (AWS_API_NOTES.md §7); usa upload_url/download_url, que firman en S3` |
| `Secret` | `Secret` | `necesita un almacén de secretos en un plano de control y un inyector de egress fuera del VM (SPEC.md §4; AWS_API_NOTES.md §7)` |
| `Template` | `Template` | `SPEC.md §4 deja fuera los templates declarativos; construye la imagen con un Dockerfile y rayito image publish` |

The `lifecycle` and `network` rows are raised inside the sibling mappings (`m9-server-timeout`'s lifecycle validation, `m9-egress-policy`'s `map_network`). Those mappings take their reason strings from this table.

Two rows stay as they are today and are not new:

- `run_code(language=...)` and `create_code_context(language=...)` with a language outside python, bash, javascript/js and typescript/ts. The reason is `AVAILABLE_KERNELS_REASON`, updated by `m9-deno-kernels` to name typescript.
- `list(query=SandboxQuery(metadata=...))` combined with a PAUSED state.

The sibling-owned raises keep their own reasons and are not duplicated here:

- a shim `create` against a pre-M9 image (`m9-server-timeout`)
- `allow_internet_access=False` or a network policy on an image without enforcement, where the VM is terminated and then the error raised (`m9-egress-policy`)
- a static `get_metrics` without a token (`m9-sandbox-observability`)
- `upload_url`/`download_url` without staging (`m9-file-transfer`)

`unimplemented(feature)` builds `UnimplementedError(feature, UNIMPLEMENTED_REASONS[feature])`. The feature key is also the `feature` attribute, and a `KeyError` in `unimplemented` is a programming error that the unit test prevents.

### D15. Git module

**Pure base:** `rayito/_git_base.py` (Python) and `src/sandbox/git-args.ts` (TS). It is a port of E2B's `sandbox/_git/{args,auth,parse,types,config}.py` behaviour, adapted from Apache-2.0 source. One `NOTICE` line records it: "Git argument builders, porcelain parsers and failure snippets in `clients/python/src/rayito/_git_base.py` and `clients/typescript/src/sandbox/git-args.ts` are adapted from the E2B SDK (github.com/e2b-dev/E2B, Apache License 2.0)". The line goes in all three `NOTICE` copies and is checked by `check_license.py`'s copy equality.

Contents:

- `shell_quote(value) = "'" + value.replace("'", "'\"'\"'") + "'"` and `git_command(args, repo_path=None) = " ".join(shell_quote(p) for p in ["git", *(["-C", repo_path] if repo_path else []), *args])`.
- `GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}`, merged under the caller's `envs` (caller keys win).
- Argument builders, **exactly E2B's argv**:

  | Operation | argv |
  |---|---|
  | status | `status --porcelain=1 -b` |
  | branches | `branch --format=%(refname:short)\t%(HEAD)` |
  | create branch | `checkout -b <b>` |
  | checkout branch | `checkout <b>` |
  | delete branch | `branch -d\|-D <b>` |
  | add | `add -A\|.` when no files are given, else `add -- <files...>` |
  | commit | `[-c user.name=… -c user.email=…] commit -m <msg> [--allow-empty]` |
  | reset | `reset [--<mode>] [<target>] [-- <paths...>]`, mode ∈ `soft\|mixed\|hard\|merge\|keep` |
  | restore | `restore [--worktree] [--staged] [--source <s>] -- <paths...>`, with E2B's defaulting of `staged`/`worktree` |
  | init | `init [--bare] [--initial-branch <b>] <path>` |
  | remote add | `remote add [-f] <name> <url>`, with the `\|\| git remote set-url` fallback when `overwrite=True` |
  | remote get | `remote get-url <name> \|\| true` |
  | push | `push [--set-upstream] [<remote>] [<branch>]` |
  | pull | `pull [<remote>] [<branch>]` |
  | config | `config --global\|--local\|--system <key> [<value>]`, with `--local` requiring `path`: `resolve_config_scope` |
  | clone | `clone <url> [--branch <b> --single-branch] [--depth <n>] [<path>]` |
  | credential | `printf %s '<protocol/host/username/password lines>' \| git credential approve` |

- Parsers: `parse_git_status(stdout) -> GitStatus` (porcelain v1 with `-b`: branch line `## <branch>[...<upstream>] [ahead N, behind M]`, `No commits yet on`, `HEAD (no branch)`, rename `R  old -> new`, conflict codes `DD AU UD UA DU AA UU`), `parse_git_branches(stdout) -> GitBranches`, `derive_repo_dir_from_url(url)`.
- Models, frozen dataclasses:
  - `GitFileStatus(name, status, index_status, working_tree_status, staged, renamed_from=None)`
  - `GitStatus(current_branch, upstream, ahead, behind, detached, file_status)` with the properties `is_clean`, `has_changes`, `has_staged`, `has_untracked`, `has_conflicts`, `total_count`, `staged_count`, `unstaged_count`, `untracked_count`, `conflict_count`
  - `GitBranches(branches, current_branch)`
  - `GitResetMode = Literal["soft", "mixed", "hard", "merge", "keep"]`
- Credentials:
  - `with_credentials(url, username, password)`: http(s) URLs only; both values are required, otherwise `InvalidArgumentException`. The URL user and password are percent-encoded with `urllib.parse.quote(value, safe="")`.
  - `strip_credentials(url)`.
  - `redact(text, secrets) -> str` replaces each non-empty secret, and its percent-encoded form, with `***`.
- Classification: `is_auth_failure(exc)` and `is_missing_upstream(exc)` match E2B's lower-cased snippet lists against `stderr + "\n" + stdout` of a `CommandExitException`.

**Sync `rayito.Git(commands: Commands)`** and **async `rayito.AsyncGit(commands: AsyncCommands)`**. They have identical method sets, with E2B's Python signatures and every parameter keyword-able in E2B's order:

- `clone(url, path=None, branch=None, depth=None, username=None, password=None, envs=None, user=None, cwd=None, timeout=None, request_timeout=None, dangerously_store_credentials=False) -> CommandResult`
- `init(path, bare=False, initial_branch=None, ...)`
- `remote_add(path, name, url, fetch=False, overwrite=False, ...)`
- `remote_get(path, name, ...) -> str | None`
- `status(path, ...) -> GitStatus`
- `branches(path, ...) -> GitBranches`
- `create_branch(path, branch, ...)`, `checkout_branch(path, branch, ...)`, `delete_branch(path, branch, force=False, ...)`
- `add(path, files=None, all=True, ...)`
- `commit(path, message, author_name=None, author_email=None, allow_empty=False, ...)`
- `reset(path, mode=None, target=None, paths=None, ...)`
- `restore(path, paths, staged=None, worktree=None, source=None, ...)`
- `push(path, remote=None, branch=None, set_upstream=True, username=None, password=None, ...)`
- `pull(path, remote=None, branch=None, username=None, password=None, ...)`
- `set_config(key, value, scope="global", path=None, ...)`, `get_config(key, scope="global", path=None, ...) -> str | None`
- `dangerously_authenticate(username, password, host="github.com", protocol="https", ...)`
- `configure_user(name, email, scope="global", path=None, ...)`

Where `...` appears, it stands for `envs=None, user=None, cwd=None, timeout=None, request_timeout=None`. Every method runs `commands.run(git_command(...), envs={**GIT_ENV, **(envs or {})}, user=user, cwd=cwd, timeout=timeout, request_timeout=request_timeout)` in the foreground. `timeout=None` means no server deadline, as in E2B, whose git module passes `None`. Native `Sandbox.git` and `AsyncSandbox.git` are lazily built properties.

**Credentials** (E2B semantics, never logged, never echoed):

- **clone with username and password.** The clone runs against the URL with credentials. Unless `dangerously_store_credentials=True`, it is followed by `git -C <repo> remote set-url origin <stripped url>`. The repo path is `path`, or else `derive_repo_dir_from_url(url)`; when neither exists, `InvalidArgumentException` is raised before running anything.
- **push/pull with username and password:**
  1. `remote get-url <remote>`, where the remote defaults to the configured upstream remote or `origin`.
  2. `remote set-url <remote> <url with credentials>`.
  3. Run the command.
  4. Always `remote set-url <remote> <original url>` in `finally`, even when the command failed.
  A password without a username raises `InvalidArgumentException` before running anything.
- **dangerously_authenticate:**
  1. `set_config("credential.helper", "store", scope="global")`.
  2. `git credential approve` with the four lines piped through `printf %s '<quoted>'`.
  The credentials land in `~/.git-credentials` (uid 1000, mode 0600 by git), readable by any process of the sandbox user. The docstring and `git.md` say so in bold, exactly as E2B does.

**Errors:**

- On `CommandExitException` from any git command:
  - `is_auth_failure` → `GitAuthException("git <action> necesita credenciales para repositorios privados")`, or `"... necesita un password/token ..."` when a username was given without a password.
  - `is_missing_upstream` (push/pull only) → `GitUpstreamException` with E2B's guidance text translated to Spanish.
  - Otherwise the same `CommandExitException` is re-raised, with `stdout`, `stderr`, `error` and its message passed through `redact(..., {password, quote(password)})` whenever the command carried credentials.
  - The originals are chained with `from None` whenever redaction applied, so the unredacted text is not reachable from `__cause__`/`__context__`.
- Argument validation errors (`InvalidArgumentException`) are raised before any RPC.
- `GitAuthException` messages never contain the URL.

**No logging:** the git module emits no log records. The command string, which contains credentials when they are passed, travels only inside `StartRequest.cmd`. `rayd` never logs commands (T9), and the SDK never logs commands.

**TypeScript `Git`** (`src/sandbox/git.ts`): the same behaviour, with E2B JS signatures:

- `clone(url, opts?)`, `init(path, opts?)`, `remoteAdd(path, name, url, opts?)`, `remoteGet(path, name, opts?)`
- `status(path, opts?)`, `branches(path, opts?)`
- `createBranch(path, branch, opts?)`, `checkoutBranch(...)`, `deleteBranch(path, branch, opts?)`
- `add(path, opts?)`, `commit(path, message, opts?)`, `reset(path, opts?)`, `restore(path, opts)`
- `push(path, opts?)`, `pull(path, opts?)`
- `setConfig(key, value, opts?)`, `getConfig(key, opts?)`
- `dangerouslyAuthenticate(opts)`, `configureUser(name, email, opts?)`

The option keys are camelCase (`initialBranch`, `setUpstream`, `authorName`, `allowEmpty`, `dangerouslyStoreCredentials`, `timeoutMs`, `requestTimeoutMs`, `user`, `cwd`, `envs`). The native TS `Sandbox` gains `readonly git: Git`. TS errors: `GitAuthError extends AuthenticationError` and `GitUpstreamError extends SandboxError`, defined natively in `src/errors.ts` and re-exported by the shim.

### D16. Image: `git-core` in rayito-base

`image/Dockerfile`: the existing first `RUN dnf install -y --setopt=install_weak_deps=0 \` line gains the explicit package `git-core-2.50.1-1.amzn2023.0.1` (the NEVRA measured above). A comment line above the `RUN` records the reason (the git module, M9) and the measured size (7 packages, 31,452,146 B RPM installed size). It is the same layer, so all four variants (`rayito-base`, `-slim`, `-caps`, `-poly`) get git. The acceptance tooling list in the `for tool in ...` check gains `git`, so a missing binary fails the build rather than the e2e.

`scripts/check_pins.py` gains a third gate, "dnf":

- `PINNED_DNF_PACKAGES = frozenset({"git-core"})`.
- For every `dnf install` command in `image/Dockerfile`, which joins into `DEFAULT_PATHS`, each token whose name (the part before the first `-<digit>`) is in that set must match `^<name>-\d[^\s]*-[^\s]+$`, i.e. carry the version and the release.
- A bare `git-core` is a finding: `(line, token, "el paquete dnf no lleva -<versión>-<release>")`.
- The other packages of the line keep their current, unpinned status: the M1 baseline, recorded in T10.

Tests in `scripts/tests/test_check_pins.py`: a bare `git-core` → one finding; the pinned NEVRA → none; an unrelated unpinned package → none.

**Measurement to record at publish** (`AWS_API_NOTES.md` §16, new row "git-core en rayito-base"):

- `codeInstallSizeInBytes` and `memorySnapshotSizeInBytes` of the M9 `rayito-base` version, against the previous published version, with the git-core share taken from the RPM sum above.
- Snapshot memory is expected unchanged: nothing is warmed.
- The row uses placeholders only.

### D17. TypeScript `rayito/e2b` entry point

**Packaging:**

- `package.json` `exports` gains `"./e2b": { "types": "./dist/e2b.d.mts", "import": "./dist/e2b.mjs", "require": { "types": "./dist/e2b.d.cts", "default": "./dist/e2b.cjs" } }`.
- `tsdown.config.ts` `entry` becomes `{ index: "src/index.ts", e2b: "src/e2b/index.ts" }`.
- `scripts/pack-check.mjs` `REQUIRED_ENTRIES` gains `package/dist/e2b.mjs`, `package/dist/e2b.cjs`, `package/dist/e2b.d.mts` and `package/dist/e2b.d.cts`.
- `sideEffects: false` stays. `src/e2b/index.ts` repeats the `Symbol.asyncDispose` polyfill line of `src/index.ts` (the entries are independent).

**Layout** (`src/e2b/`):

| File | Content |
|---|---|
| `index.ts` | Exports (below); default export `Sandbox`. |
| `sandbox.ts` | `class Sandbox` by composition over the native `Sandbox` (`readonly native`). |
| `filesystem.ts`, `pty.ts` | Wrappers. `commands` is the native `Commands`: its camelCase shape already matches. |
| `compat.ts` | Pure: `mapCreateOptions`, `splitConnectionOpts`, `IGNORED_CONNECTION_OPTS`, `rejectUnsupportedNetworkKeys`, `infoFromNative`, `emitCompatWarning`. |
| `unimplemented.ts` | The D14 table and `unimplemented(feature)`. |
| `connection.ts` | `ConnectionConfig` with `static setIntegration(integration: string \| undefined): void`, and `ConnectionOpts`. |
| `client.ts` | `class E2B { constructor(opts?: E2BClientOpts); readonly Sandbox: typeof Sandbox; Template/Volume/Secret getters throw }`. |
| `resources.ts` | `Template`, `Volume`, `Secret` classes whose statics throw `unimplemented(...)`; `getSignature()`. |

**Exports:**

- Values: `Sandbox` (named and default), `E2B`, `ConnectionConfig`, `ALL_TRAFFIC`, `FileType`, `FilesystemEventType`, `Git`, `getSignature`, `Template`, `Volume`, `Secret`.
- Errors: `SandboxError`, `TimeoutError`, `InvalidArgumentError`, `NotEnoughSpaceError` (= native `DiskFullError`), `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `AuthenticationError`, `GitAuthError`, `GitUpstreamError`, `TemplateError`, `RateLimitError`, `ServiceBusyError` (= native `CapacityError`), `BuildError`, `FileUploadError`, `CommandExitError`, `UnimplementedError`.
- Types: `ConnectionOpts`, `SandboxOpts`, `SandboxConnectOpts`, `SandboxInfo`, `SandboxState`, `SandboxMetrics`, `SandboxPaginator`, `Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`, `Context`, `CommandResult`, `CommandHandle`, `EntryInfo`, `WriteInfo`, `FilesystemEvent`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`, `Logger`, `Username`.

Aliases are re-exported bindings (`export { DiskFullError as NotEnoughSpaceError }`), so `instanceof` holds across entries. `TemplateError extends SandboxError` and `BuildError extends Error` are defined in `src/e2b/errors.ts` and never thrown.

**Surface:**

```ts
interface ConnectionOpts {
  requestTimeoutMs?: number; retries?: number; logger?: Logger;
  headers?: Record<string, string>; proxy?: string; signal?: AbortSignal;
  apiKey?: string; domain?: string; debug?: boolean; apiUrl?: string; sandboxUrl?: string;
  apiHeaders?: Record<string, string>;                    // accepted, warned, ignored
  region?: string; controlPlane?: ControlPlane; accessToken?: string;
  transport?: Partial<TransportSettings>;                // Rayito binding
}
interface SandboxOpts extends ConnectionOpts {
  metadata?; envs?; timeoutMs?; secure?; allowInternetAccess?; mcp?; network?; iam?;
  volumeMounts?; lifecycle?; maxLifetimeMs?;             // maxLifetimeMs: m9-server-timeout
  templateVersion?; executionRoleArn?; allowedPorts?; ingress?; logging?;
  readyTimeoutMs?; reconnectTimeoutMs?; keepOnFailure?;
}
class Sandbox implements AsyncDisposable {
  static create(opts?: SandboxOpts): Promise<Sandbox>;
  static create(template: string, opts?: SandboxOpts): Promise<Sandbox>;
  static connect(sandboxId: string, opts?: SandboxConnectOpts): Promise<Sandbox>; // timeoutMs, onResume
  static kill(sandboxId: string, opts?: ConnectionOpts): Promise<boolean>;
  static getInfo(sandboxId: string, opts?: ConnectionOpts): Promise<SandboxInfo>;
  static getFullInfo(sandboxId: string, opts?: ConnectionOpts): Promise<SandboxInfo>; // same as getInfo
  static isRunning(sandboxId: string, opts?: ConnectionOpts): Promise<boolean>;   // get-microvm state RUNNING
  static pause(sandboxId: string, opts?: ConnectionOpts & { keepMemory?: boolean }): Promise<boolean>;
  static betaPause: typeof Sandbox.pause;
  static setTimeout(sandboxId: string, timeoutMs: number, opts?: ConnectionOpts): Promise<void>;  // m9-server-timeout
  static getMetrics(sandboxId: string, opts?: ConnectionOpts & { start?: Date; end?: Date }): Promise<SandboxMetrics[]>; // observability
  static list(opts?: SandboxListOpts): SandboxPaginator;                          // observability
  static updateNetwork(sandboxId: string, network: SandboxNetworkUpdate, opts?: ConnectionOpts): Promise<void>; // egress
  static fork(...args: unknown[]): Promise<never>;
  static deleteSnapshot(...args: unknown[]): Promise<never>;
  static listSnapshots(...args: unknown[]): never;
  readonly native: NativeSandbox; readonly files: Filesystem; readonly commands: Commands;
  readonly pty: Pty; readonly git: Git;
  get sandboxId(): string; get sandboxDomain(): string; get trafficAccessToken(): string | undefined;
  getHost(port: number): string;                           // endpoint hostname, synchronous
  getHostHeaders(port: number): Promise<Record<string, string>>; // native getHost(port).headers
  kill(opts?): Promise<boolean>; getInfo(opts?): Promise<SandboxInfo>; isRunning(opts?): Promise<boolean>;
  setTimeout(timeoutMs: number, opts?): Promise<void>; pause(opts?): Promise<boolean>; betaPause(opts?): Promise<boolean>;
  connect(opts?: { timeoutMs?: number; onResume?: "restore" | "reboot" } & ConnectionOpts): Promise<this>;
  getMetrics(opts?: { start?: Date; end?: Date } & ConnectionOpts): Promise<SandboxMetrics[]>;
  uploadUrl(path?: string, opts?): Promise<string>; downloadUrl(path: string, opts?): Promise<string>;  // file-transfer
  updateNetwork(network, opts?): Promise<void>;
  runCode(code: string, opts?: RunCodeOptions & { signal?: AbortSignal }): Promise<Execution>;
  createCodeContext(opts?); listCodeContexts(opts?); removeCodeContext(ctx, opts?); restartCodeContext(ctx, opts?);
  fork(...a): Promise<never>; createSnapshot(...a): Promise<never>; listSnapshots(...a): never;
  getMcpUrl(): never; getMcpToken(): Promise<never>;
  [Symbol.asyncDispose](): Promise<void>;                  // kill
}
```

Notes:

- `uploadUrl()` without a path throws `InvalidArgumentError` (divergence 8 of `m9-file-transfer`: S3 carries no filename).
- `trafficAccessToken` reads native `currentProxyToken(8080)` (D18).
- `getHost` validates the port with the native rules (refuses 9000) and returns `native.endpoint`.
- Static `isRunning` is control-plane only (`get-microvm` state is `RUNNING`). The instance `isRunning` probes `Health` like the native one, with `requestTimeoutMs` as the probe timeout.
- Warnings for ignored options: `process.emitWarning("<name> ignorado: <reason>", { type: "RayitoCompatWarning" })`, one per option, naming it and never its value.
- `UnimplementedError`: async methods return a rejected promise; synchronous members (`listSnapshots`, `getMcpUrl`, resource statics, `E2B.prototype.Template`) throw.
- The create mapping (`mapCreateOptions`) is the camelCase mirror of D5: E2B's `timeoutMs` default is 300,000 ms, and `maxLifetimeMs` follows server-timeout's rule `max(3_600_000, min(timeoutMs + 60_000, 28_800_000))`.

**Wrappers:**

- `Filesystem.watchDir(path: string, onEvent: (e: FilesystemEvent) => void | Promise<void>, opts?: { onExit?; recursive?; includeEntry?; allowNetworkMounts?; user?; timeoutMs?: number /* default 60_000 */; requestTimeoutMs?; signal? }): Promise<WatchHandle>` maps to native `watchDir(path, { onEvent, ... })`. The other `files` methods are native. `read` gains `format: "blob"` through `m9-file-transfer`.
- `Pty.create(opts: { cols: number; rows: number; onData: (d: Uint8Array) => void | Promise<void>; user?; cwd?; envs?; timeoutMs?: number /* 60_000 */; requestTimeoutMs?; signal? }): Promise<PtyHandle>` maps to native `create({ size: { cols, rows }, onData, ... })`.
- `Pty.connect(pid: number, opts: { onData; timeoutMs?; requestTimeoutMs?; signal? })`.
- `Pty.sendInput(pid, data, opts?)`, `resize(pid, { cols, rows }, opts?)`, `kill(pid, opts?)`.

### D18. TypeScript native additions

- `RequestOptions` gains `signal?: AbortSignal`.
  - Unary calls pass it in Connect `CallOptions.signal`.
  - `SandboxCore.openStream` links it to the stream's own `AbortController`: an abort makes `controller.abort(signal.reason)`, and the listener is removed when the stream ends.
  - An abort rejects the pending promise, or the iterator, with `signal.reason`. That is a `DOMException("AbortError")` by default, the `fetch` convention, and no `SandboxError` wrapping happens.
  - `CodeClient.runCode` honours it, so cancelling the stream interrupts the execution per the existing "Cancelling the stream interrupts the execution" requirement. So do `Commands.run`, `Filesystem.*` and `Pty.*`.
  - `SandboxCreateOptions.signal` aborts the readiness poll and every control-plane `send` (AWS SDK v3 `send(command, { abortSignal })`, an SDK option and not an API parameter). A launched VM whose create was aborted is terminated unless `keepOnFailure`, matching the existing readiness-failure path.
- `TransportSettings` gains `extraHeaders?: Readonly<Record<string, string>>`, validated by `src/transport/headers.ts::validateExtraHeaders` (D6 rules). The interceptor sets them after the four reserved headers on every unary and streaming request.
- `TransportSettings` gains `proxy?: string`, validated with the D6 URL rules.
  - `openTransport` builds the `Http2SessionManager` with `createConnection: () => tls.connect({ socket: new ProxyTunnelSocket(proxyUrl, host, port), servername: host, ALPNProtocols: ["h2"] })`.
  - `src/transport/proxy-tunnel.ts` exports `ProxyTunnelSocket extends Duplex` and `ProxyTunnelAgent extends https.Agent`. `ProxyTunnelSocket` behaves as follows:
    - It opens `net.connect(proxyPort, proxyHost)` and writes `CONNECT host:port HTTP/1.1\r\nHost: host:port\r\n[Proxy-Authorization: Basic <b64>]\r\n\r\n`.
    - It buffers `_write` calls until it reads a status line `HTTP/1.x 200`, then forwards bytes both ways.
    - Any other status destroys the socket with `SandboxError("el proxy rechazó CONNECT con HTTP <status>")`, and the message never includes the URL or credentials.
  - `ProxyTunnelAgent` overrides `createConnection(options, cb)` with the same handshake followed by `tls.connect`.
  - The control plane client gets `requestHandler: new NodeHttpHandler({ httpsAgent: new ProxyTunnelAgent(proxy) })`.
- `ControlPlaneOptions` gains `retries?: number`, which sets the client option `maxAttempts: retries + 1`, and `integration?: string`, which sets the client option `customUserAgent: [["rayito-integration", integration]]`. A dedicated `LambdaMicrovmsControlPlane` is built when any of `retries`, `proxy` or `integration` is set. The shim's `ConnectionConfig.setIntegration` value feeds `integration`.
- `errors.ts` gains:
  - `DiskFullError extends SandboxError`. `translateConnectError` returns it for `ResourceExhausted` whose details contain `disk_reserve` or `disk_full` (the Python `DISK_FULL_DETAILS`); other `ResourceExhausted` stays `RateLimitError`.
  - `UnimplementedError extends Error` with `feature` and `reason`, and a message identical in format to Python's.
  - `GitAuthError` and `GitUpstreamError`.

  `DiskFullError`, `UnimplementedError`, `TransferError` and `FileUploadError` are defined by `m9-file-transfer` for transfer errors (D1). This change adds only the `translateConnectError` rule for `Write`'s `disk_reserve`/`disk_full`, and the Git classes.
  All are exported from `src/index.ts`.
- `Sandbox.currentProxyToken(port = 8080): string | undefined` is a synchronous read of the `TokenStore`. `Sandbox.isRunning(opts?: RequestOptions)` uses `opts.requestTimeoutMs` as the probe timeout. `Sandbox.git` is added (D15).

### D19. CLI: `rayito sandbox create | connect | exec | metrics`

These go in `rayito/cli/sandbox.py`, which is the only module importing `typer`. `_terminal.py` and `_tokens.py` import the standard library only, and the "library modules import without typer" test is extended to them.

**Token handling** (`_tokens.py`):

- The token source is `--token-file PATH` (the file content is the token, whitespace stripped) or the environment variable `RAYITO_ACCESS_TOKEN`. When both are present, `--token-file` wins.
- The token is never accepted in argv, because argv is visible in `ps`. It is never printed and never logged.
- `write_token_file(path, token)` uses `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)`, so an existing file is an error with exit code 2 and the message `el fichero de token ya existe: <path>`.
- On Windows the mode argument is ignored by the OS. The docs say so, and the file lands in the user's profile directory.

**`rayito sandbox create [TEMPLATE] [--timeout SECONDS] [--metadata K=V]... [--env K=V]... [--detach] [--token-file PATH] [--user U] [--cwd DIR]`:**

1. The token is resolved: `RAYITO_ACCESS_TOKEN`, else a new `generate_access_token()`.
2. With `--detach`:
   - If the token was generated, `--token-file` is required: without it, exit 2 with `sin RAYITO_ACCESS_TOKEN, --detach necesita --token-file para poder volver a conectarte`.
   - The file is written **before** `run-microvm`, so a write error never leaves an orphan VM.
   - Then `Sandbox.create(template, timeout=, metadata=, envs=, access_token=token, control_plane=clients.control_plane)`, with the native defaults otherwise.
   - Output is `sandbox_id` on stdout, or `--json` `{"sandbox_id", "endpoint", "template", "template_version", "expires_at"}`, which contains no token.
   - Exit 0.
3. Without `--detach`: create, run the terminal bridge, and `kill()` in `finally`. That is E2B's "close the terminal ⇒ the sandbox ends" behaviour. The exit code is the shell's exit code.

`--timeout` is the native `timeout` (default 3600). `--metadata` and `--env` are parsed by `parse_pairs`, which refuses a missing `=` with exit 2.

**`rayito sandbox connect ID [--user U] [--cwd DIR] [--env K=V]... [--token-file PATH]`:**

- `Sandbox.connect(ID, access_token=token)`, which auto-resumes a suspended VM, then the terminal bridge.
- The sandbox is not killed.
- The exit code is the shell's exit code. No token → exit 2 with `falta el access token: --token-file o RAYITO_ACCESS_TOKEN`.

**`rayito sandbox exec ID [--background] [--cwd DIR] [--user U] [--env K=V]... [--timeout SECONDS=0] [--token-file PATH] -- CMD...`:**

- `cmd = shlex.join(CMD)`, then `commands.run(cmd, background=, envs=, user=, cwd=, timeout=None if 0 else timeout)`.
- `on_stdout` and `on_stderr` write each chunk to `sys.stdout` and `sys.stderr` as it arrives, UTF-8 with `errors="replace"`, and flush.
- The foreground exit code is the remote exit code: `CommandExitException.exit_code`, with output already streamed. A server timeout gives exit 124 and the message `timeout del comando` on stderr.
- `--background` prints the `pid` and exits 0.

**`rayito sandbox metrics ID [--follow] [--interval SECONDS=5] [--token-file PATH]`:**

- `Sandbox.connect(ID, access_token=token)`, then `get_metrics()` (the snapshot RPC).
- Human output is a table of `timestamp`, `cpu_used_pct`, `cpu_count`, `mem_used`, `mem_total`, `disk_used`, `disk_total`, plus `mem_cache` when present. `--json` gives one JSON object, and one JSON object per line with `--follow`.
- `--follow` repeats every `--interval` until Ctrl-C (exit 0) or until the sandbox disappears (exit 1, `el sandbox ya no existe`).
- The docs note that `connect` wakes a suspended sandbox.

**Terminal bridge** (`_terminal.py`, `run_terminal(sandbox, *, user, cwd, envs, stdin, stdout) -> int`):

1. Start the PTY with `sandbox.pty.create(size=<current terminal size or 80x24>, user=, cwd=, envs=, on_data=<write bytes to stdout.buffer and flush>, timeout=None)`.
2. Read input by mode:
   - **POSIX TTY** (`stdin.isatty()` and `sys.platform != "win32"`): `termios` attributes saved, `tty.setraw`, restored in `finally`. A `SIGWINCH` handler calls `pty.resize`. A reader thread does `os.read(fd, 4096)` and sends each chunk with `handle.send_input`. Ctrl-C is sent to the remote as `\x03`, never as a local SIGINT.
   - **Windows console TTY:** a reader thread uses `msvcrt.getwch()`. Characters are encoded as UTF-8. The two-code sequences `\xe0`/`\x00` + `H,P,K,M,G,O,S` map to `ESC[A`, `ESC[B`, `ESC[D`, `ESC[C`, `ESC[H`, `ESC[F`, `ESC[3~`. The size is polled every 1 s via `shutil.get_terminal_size()`, and a resize is sent on change.
   - **Non-TTY stdin** (pipes, any OS): chunks from `stdin.buffer.read1(4096)` are forwarded as-is. EOF sends `\x04`.
3. The main thread waits for `handle.wait()` and returns its exit code. `CommandExitException` gives its exit code, and `TimeoutException` gives 124.
4. The reader thread is a daemon; it stops on exit and never blocks the return.

Exit codes follow `m7-cli`: 0 success; the remote code for `exec`, `connect` and `create`-attached; 1 operation failure (`SandboxNotFoundException`, AWS error); 2 usage or environment.

### D20. Error mapping (all surfaces)

| Condition | Python | TypeScript | CLI exit |
|---|---|---|---|
| Unsupported E2B 2.x feature (D14) | `UnimplementedError` (before any call) | `UnimplementedError` | n/a |
| Unknown `network` key | `TypeError` | `InvalidArgumentError` (TS has no kwargs `TypeError`) | n/a |
| Reserved or invalid `headers` key, bad value | `InvalidArgumentException` | `InvalidArgumentError` | n/a |
| Invalid `proxy` URL, negative `retries` | `InvalidArgumentException` | `InvalidArgumentError` | n/a |
| `retries/proxy/set_integration` with an explicit control plane | `InvalidArgumentException` | `InvalidArgumentError` | n/a |
| Callable in the 2.x `user` slot (sync `watch_dir`, sync `pty.create`) | `InvalidArgumentException` | n/a | n/a |
| Proxy refuses CONNECT | grpc `UNAVAILABLE` → existing translation (`SandboxException`) | `SandboxError("el proxy rechazó CONNECT con HTTP <n>")` | 1 |
| `InsufficientCapacityException` | `CapacityException` = `ServiceBusyException` | `CapacityError` = `ServiceBusyError` | 1 |
| Write `disk_reserve`/`disk_full` | `DiskFullException` = `NotEnoughSpaceException` | `DiskFullError` = `NotEnoughSpaceError` | 1 |
| Transfer import ends `FAILED` | `FileUploadException(code)` | `FileUploadError` | n/a |
| git authentication failure | `GitAuthException` | `GitAuthError` | n/a |
| git missing upstream on push/pull | `GitUpstreamException` | `GitUpstreamError` | n/a |
| Other git failure | `CommandExitException` (redacted) | `CommandExitError` (redacted) | n/a |
| Aborted `signal` | n/a | rejects with `signal.reason` | n/a |
| CLI missing token, bad pair, existing token file | n/a | n/a | 2 |
| Remote command or shell exit N | n/a | n/a | N (124 on server timeout) |

### D21. Logging and secret hygiene

- No new log record may contain any of: header values, proxy URLs, the proxy JWE, access tokens, git URLs with credentials, git passwords, command strings, PTY bytes or file contents.
- A new unit test runs a create with `headers={"x-trace": "valor-secreto"}`, `proxy="http://u:clave@127.0.0.1:3128"` and `git.clone(url_with_creds)`, captures every `rayito.*` record and the custom `logger`, and asserts that none of `valor-secreto`, `clave` and `u:clave` appears.
- The TS mirror asserts the same over the `Logger` spy.
- The CLI never prints the token: the unit test scans `stdout` and `stderr` of `create --detach --json`.
- `traffic_access_token` / `trafficAccessToken` deliberately return the JWE. `SECURITY.md` T3 gains the sentence that this accessor exists for E2B parity and that the value is a bearer credential valid for at most 60 minutes.

### D22. Tests that fail without the change

Every test below fails on today's tree. Python paths are under `clients/python/tests/unit/`; TS paths are under `clients/typescript/tests/unit/`.

**Python:**

- `test_e2b_v2_exports.py`:
  - Every D4 name imports from `rayito.e2b`, and the new exception names from `rayito.e2b.exceptions`.
  - `NotEnoughSpaceException is DiskFullException`, `ServiceBusyException is CapacityException`, `issubclass(GitAuthException, AuthenticationException)`, `issubclass(GitUpstreamException, SandboxException)`, `issubclass(BuildException, Exception)`, `rayito.e2b.UnimplementedError is rayito.UnimplementedError`.
  - `OutputHandler[str]` is subscriptable.
  - No name contains `Pool`.
- `test_e2b_v2_base.py` (pure):
  - Parametrised over every D14 key: `unimplemented(key)` is an `UnimplementedError` whose `reason` equals the table string and contains `AWS_API_NOTES.md §` or `SPEC.md §4`.
  - `validate_extra_headers`: each reserved key, each reserved prefix, `-bin`, a non-token key and a non-ASCII value each raise, and `X-Trace` becomes `x-trace`.
  - `validate_proxy_url`: `https://`, no port and a non-str are refused; `http://u:p@h:3128` is accepted, and the error messages never contain `p`.
  - `resolve_retries` bounds.
  - `merge_bound_params`: per-call `None` falls back and per-call headers replace.
  - `split_api_params`: eight warnings for the eight ignored params, none containing a value.
  - `map_create_kwargs` for the 2.x positional order.
  - The `map_network` rejections carry the D14 reason strings for `network.rules`, `network.mask_request_host` and `network.allow_public_traffic=True`.
  - `info_from_native` for each D10 row, including `volume_mounts == []` and the `allow_internet_access` truth table.
  - `Logs.to_json`/`ExecutionError.to_json`/`CodeContext.from_json` round-trips, and `CodeContext.from_json` with a missing key.
- `test_e2b_v2_sync.py` and `test_e2b_v2_async.py`, against `FakeControlPlane` and the fake `rayd`:
  - Every D14 trigger raises `UnimplementedError`, not `TypeError`/`AttributeError`, with the fakes recording **zero** requests: instance and class `fork`, snapshots, `connect(on_resume='reboot')`, `pause(keep_memory=False)`, `lifecycle` with `keep_memory` false, the three network keys, `iam`, `mcp`, `get_mcp_url`, `get_mcp_token`, `volume_mounts`, `get_signature`, `Template.build`, `Volume.create`, `Secret.list`, and `E2B().Template` / `.Volume` / `.Secret`.
  - `commands.run("echo hi", True)` returns a `CommandHandle`, and `handle.wait(on_stdout=cb)` calls `cb` with `"hi\n"`.
  - `commands.run("x", False, {"K": "1"})` puts `K` on the wire.
  - `files.write_files([...])` returns two `WriteInfo`.
  - `watch_dir(path, include_entry=True)` puts `include_entry` on the wire, and a callable second positional raises `InvalidArgumentException`.
  - `pty.create(PtySize(24, 80))` followed by `pty.connect(pid)` re-attaches on the fake `PtyService`.
  - The three code-context methods.
  - `pause()` gives `True`, then `False` when the stub answers `SUSPENDED`.
  - The instance `connect()` on a `SUSPENDED` stub calls `resume-microvm` and returns `self`, while `connect(timeout=120)` calls the AT_LEAST extension.
  - `is_running(request_timeout=0.5)` bounds the Health deadline.
  - `envd_api_url == "https://" + endpoint`, and `traffic_access_token` equals the minted JWE.
  - `connection_config.headers == {"x-trace": "1"}`.
  - The logger routing check: a `logging.Logger` given as `logger=` receives the `run-microvm aceptado` record and the readiness record, and `rayito.sandbox` receives none of them for that sandbox.
  - `headers={"x-trace": "1"}` reaches the fake `rayd` on `Health`, on a unary and on a stream. `headers={"x-access-token": "x"}` and `{"X-AWS-Proxy-Port": "1"}` raise before `run-microvm`.
  - `proxy="http://127.0.0.1:3128"` adds `("grpc.http_proxy", ...)` to the transport options, which the test reads from the `TransportSettings` passed to the native `create`.
  - `retries=2` and `set_integration("acme/1.0")` produce a control plane whose botocore client config has `retries["total_max_attempts"] == 3`, `proxies == {"http": ..., "https": ...}` and a `user_agent_extra` ending in `acme/1.0`, read from `client.meta.config`.
  - `E2B(region="us-east-1", control_plane=fake).Sandbox.create()` uses the fake, and a per-call `region` wins.
- `test_git_base.py`:
  - The argv table of D15, element by element.
  - `shell_quote` with `'`.
  - `parse_git_status` fixtures: clean, ahead/behind, detached, no commits yet, rename, conflict, untracked.
  - `parse_git_branches`.
  - The `is_auth_failure`/`is_missing_upstream` snippet tables.
  - `with_credentials` percent-encoding.
  - `redact` of both raw and encoded passwords.
  - The clone plan when no path can be derived.
- `test_git_sync.py` and `test_git_async.py`, against the fake `ProcessService`:
  - The exact `cmd` strings and `GIT_TERMINAL_PROMPT=0` in envs.
  - Push with credentials issues `get-url`, `set-url(creds)`, `push`, `set-url(original)` in order, even when `push` exits 128.
  - A 128 exit with `could not read Username` becomes `GitAuthException`, and one with `has no upstream branch` becomes `GitUpstreamException`.
  - A failing clone with credentials raises a `CommandExitException` whose `stderr`, message and `__cause__` chain do not contain the password.
  - `dangerously_authenticate` runs the two commands.
  - `git` emits no log records.
- `test_commands_sync.py` and `test_commands_async.py`: `wait(on_stdout=, on_stderr=)` receives the chunks, and an `async` callback is awaited.
- `test_transport.py`: `extra_metadata` is appended after the reserved keys on the authenticated and the anonymous plugin, and `http_proxy` becomes a channel option.
- `test_aws.py`: `client_config(ClientSettings(2, "http://h:1", "acme/1"))` fields, and a `shared_control_plane` cache key per settings.
- `test_exceptions.py`: `FileUploadException.code`, and the `GitAuthException` MRO.
- `cli/test_sandbox_interactive.py`:
  - `create --detach` without the env variable and without `--token-file` exits 2 and makes no `run-microvm`.
  - With `--token-file`, the file is created `0600` (POSIX) before `run-microvm`, and stdout contains the id but not the token.
  - An existing token file exits 2.
  - `exec` streams output and exits with the fake's code 3.
  - `metrics --json` prints the snapshot fields.
  - `connect` in pipe mode forwards the piped bytes to the fake PTY and exits with its code.
  - `_terminal.py` and `_tokens.py` import without `typer`.
- `scripts/tests/test_check_pins.py`: the dnf gate (D16).

**TypeScript:**

- `e2b-exports.test.ts`: every export resolves; `NotEnoughSpaceError === DiskFullError`; `ServiceBusyError === CapacityError`; `new GitAuthError("x") instanceof AuthenticationError`.
- `e2b-compat.test.ts` (pure): `mapCreateOptions` for `create(template, opts)` and `create(opts)`; the ignored options give one warning each (spied `process.emitWarning`) without values; `rejectUnsupportedNetworkKeys`; `infoFromNative`; the D14 table.
- `e2b-shim.test.ts`, against the fake `rayd` and the fake control plane:
  - Every D14 member throws or rejects `UnimplementedError` with zero requests.
  - `getHost(3000)` returns the endpoint synchronously, and `getHost(9000)` throws.
  - `watchDir(path, onEvent)` delivers an event.
  - `pty.create({cols: 80, rows: 24, onData})` records size 80x24.
  - `pause()` gives true, then false.
  - Static `kill`, `getInfo` and `isRunning`.
  - `E2B({controlPlane}).Sandbox.create()`.
- `abort.test.ts`: aborting during a fake `Execute` rejects with the signal's reason, and the fake saw the cancellation. An already-aborted signal rejects before any request. Aborting a unary rejects.
- `headers.test.ts`: `extraHeaders` reach unary and stream requests, and reserved keys throw before any request.
- `proxy-tunnel.test.ts`, against a local `net` server acting as a CONNECT proxy:
  - The request line is `CONNECT h:443 HTTP/1.1`, and `Proxy-Authorization` is sent for `http://u:p@...`.
  - Bytes pass through after a 200.
  - A 407 destroys the socket with a message lacking `p`.
  - `openTransport` supplies `createConnection` when `proxy` is set, and the control plane uses `ProxyTunnelAgent`.
- `errors.test.ts`: `ResourceExhausted` with `disk_reserve` becomes `DiskFullError`, and without it stays `RateLimitError`.
- `git.test.ts`: the command strings, credential restore order and error classification, mirroring Python.
- `aws.test.ts`: `retries: 2` gives `maxAttempts === 3`, and the integration gives `customUserAgent`.

### D23. Real-AWS acceptance

All tests use env-driven configuration with placeholders only in tracked files.

**Python (`clients/python/tests/e2e/test_m9_e2b_v2.py`):**

- It runs each program of the corpus as a subprocess with `sys.executable`. The environment carries `RAYITO_TEMPLATE` (the M9 `rayito-base`), `RAYITO_ACCESS_TOKEN` (a fresh token, so `Sandbox.connect(id)` works unmodified), `AWS_REGION` and `AWS_PROFILE`. Each program must exit 0.
- The corpus lives in `clients/python/tests/e2e/e2b_corpus/`, split into `sync/` and `async/`, with one program per item. Each file starts with a comment naming the docs.e2b.dev page it follows, and its only Rayito-specific line is the `rayito.e2b` import. The programs are:
  1. `hello_run_code`: `Sandbox.create()`, `run_code("x = 1; x + 1").text == "2"`, `kill()`.
  2. `commands`: `commands.run("echo hi")`, the positional background `commands.run("sleep 1; echo done", True)` then `handle.wait(on_stdout=...)`, `commands.list()`.
  3. `files`: `files.write_files([...])`, `read`, `list`, `exists`, `remove`.
  4. `watch`: sync `watch_dir(path, include_entry=True)` + write + `get_new_events()` holding an event whose `entry` is set; async `watch_dir(path, on_event, include_entry=True)`.
  5. `pty`: `pty.create(PtySize(24, 80))`, `send_stdin(b"echo hola\n")`, `disconnect`, `pty.connect(pid)`, output contains `hola`, `kill`.
  6. `contexts`: `create_code_context()`, `list_code_contexts()`, `restart_code_context`, `remove_code_context`.
  7. `info_list`: `get_info()` with every 2.x field populated or documented-empty (`volume_mounts == []`), and a `Sandbox.list()` paginator (`next_items`, `has_next`).
  8. `pause_connect`: `pause() is True`, `pause() is False` (a second call via `Sandbox.pause(id)`), `Sandbox.connect(id)` → `run_code` works, the instance `connect()` returns `self`, `kill()`.
  9. `bound_client`: `E2B().Sandbox.create()` → `run_code`, `kill()`.
- The test also asserts that `sbx.fork()` raises `UnimplementedError` against the live sandbox before any agent call. It prints `corpus_ok=<n>`, `kernel_ready_s` and per-program seconds.
- `test_m9_git.py` (Python):
  - `sbx.git.clone(REPO, "/home/user/hello", depth=1)`. `REPO` defaults to the public `https://github.com/octocat/Hello-World.git`, and env `RAYITO_E2E_GIT_REPO` overrides it.
  - `configure_user`; `status().is_clean`; `branches()`; `create_branch("feature")`; `checkout_branch`.
  - Write a file, then `status().has_untracked`, `add`, `status().has_staged`, `commit`, `reset(mode="soft", target="HEAD~1")` with `staged_count == 1`, `restore(paths=[f], staged=True)`.
  - `set_config("core.editor", "true", scope="local", path=repo)` and `get_config(... ) == "true"`.
  - Bare remote:
    1. `init("/home/user/remote.git", bare=True)`.
    2. `init(work, initial_branch="main")`, a commit, `remote_add(work, "origin", "/home/user/remote.git")`, `push(work, remote="origin", branch="main")`.
    3. `clone` the remote to `/home/user/copy`.
    4. A second commit, then `push`.
    5. `pull(copy)` contains the new commit (checked with `git log`).
  - `pull` on a new branch without upstream raises `GitUpstreamException`.
  - `push("/home/user/hello")` without credentials raises `GitAuthException` (`GIT_TERMINAL_PROMPT=0` → "could not read Username").
  - `dangerously_authenticate("e2e", <random secret>, host="example.invalid")`, then `commands.run("cat ~/.git-credentials")` contains the host line (the documented visibility). The SDK `caplog` does not contain the secret.
  - It prints `git_version` and per-operation seconds.
- `test_m9_cli_sandbox.py` (Python), running `sys.executable -m rayito.cli` as subprocesses:
  - `sandbox create --detach --token-file <tmp> --json` gives an id.
  - `sandbox exec <id> --token-file <tmp> -- echo hi` gives stdout `hi\n` and exit 0. `-- sh -c 'exit 3'` gives exit 3.
  - `sandbox connect <id> --token-file <tmp>`:
    - **POSIX:** driven through `pty.fork()`: write `echo hola\r`, read until `hola` appears in the echo **and** the output, write `exit\r`, exit 0.
    - **Every OS:** pipe mode with `stdin=b"echo hola\nexit\n"` gives `hola` in stdout.
    - On win32 the POSIX scenario is skipped with an explicit reason. Acceptance requires one run on Linux: the e2e workflow on `ubuntu-24.04-arm`, or WSL.
  - `sandbox metrics <id> --token-file <tmp> --json` gives `cpu_count >= 1` and `mem_total > 0`.
  - Teardown: `sandbox kill <id>`.
- **TS** (`clients/typescript/tests/e2e/m9-e2b.e2e.test.ts`): it runs `pnpm build` in `globalSetup`, then executes each `.mjs` program in `clients/typescript/tests/e2e/e2b-corpus/` with `node`. Package self-reference resolves `import { Sandbox } from "rayito/e2b"` to `dist/e2b.mjs`. The programs are:
  - `create(template, opts)`, `runCode`, and static `getInfo`/`kill`/`list`
  - `getHost(port)` returning the endpoint string synchronously
  - `watchDir(path, onEvent)` receiving an event
  - `pty.create({cols, rows, onData})` + `sendInput`
  - `git.clone` + `status`
  - `pause()` true then false
  - an `AbortController` aborting `runCode("import time; time.sleep(60)")` after 2 s, which rejects in under 10 s with the abort reason; a follow-up `runCode("1+1")` on the same sandbox answers `2`
  - `new E2B().Sandbox.create()`
- **Recorded:** the `AWS_API_NOTES.md` §16 git-core row (D16), and a `MILESTONES.md` M9 line with the corpus counts. Placeholders only.

### D24. Docs to touch

| File | Change |
|---|---|
| `docs/site/docs/e2b-compat.md` | Rewritten against E2B 2.51. It covers: Python and JS import tables; the "funciona sin cambios", "se mapea, con nota" and "`UnimplementedError`" tables with the D14 reasons; the **full parity ledger** (all 110 scope rows, one table, columns `API de E2B`, `Estado tras M9`, `Cambio`, `Nota`); a divergences section per M9 change (transfer, timeout, egress, kernels, observability, this change); and the migration notes from the 1.x shim. |
| `docs/site/docs/git.md` (new) + `docs/site/mkdocs.yml` nav | Usage, credentials in bold ("visibles para el código del sandbox"), errors, and the deprecation note inherited from E2B. |
| `docs/site/docs/cli.md` | The four commands, token file, exit codes, Windows notes. |
| `docs/site/docs/api.md` | `::: rayito.Git`, `::: rayito.AsyncGit`. |
| `clients/python/README.md`, `clients/typescript/README.md` | E2B 2.x sections, `rayito/e2b`. |
| `clients/python/CHANGELOG.md`, `clients/typescript/CHANGELOG.md` | Unreleased entries, including the breaking changes. |
| `SPEC.md` | §3 rows for the E2B 2.x shim (Python and TS), git and the CLI commands. §4 CLI bullet: `rayito sandbox create/connect/exec/metrics` are operational commands; E2B's `auth/template/snapshots/fork` stay out. |
| `SECURITY.md` | T3 (`traffic_access_token`); T9 (git credentials in argv inside `StartRequest.cmd`, never logged; `dangerously_authenticate` writes `~/.git-credentials`, readable by sandbox code; `headers=` cannot override the proxy or access-token keys). |
| `AWS_API_NOTES.md` | §16 git-core row after the publish. |
| `MILESTONES.md` | M9 status line for this change. |
| `NOTICE`, `clients/python/NOTICE`, `clients/typescript/NOTICE` | E2B git attribution line (D15). |
| `ARCHITECTURE.md` | One sentence in the client SDK layer naming the two E2B shims (`rayito.e2b`, `rayito/e2b`) as composition layers over the native SDKs. No ADR: no architectural decision changes. |

### D25. Gates

- `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings` and `cargo test --workspace --locked` stay green, with no Rust change.
- `cd clients/python && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`.
- `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm pack:check`; the pack listing shows the four `dist/e2b.*` entries.
- `buf lint`, which is unchanged.
- `python scripts/check_pins.py`, with the new dnf gate.
- `python scripts/check_license.py`, which checks the three `NOTICE` copies are identical.
- `python scripts/check_hygiene.py`.
- `openspec validate m9-e2b-v2-surface --strict --no-interactive`.

### D26. A `files.write` into a watched directory surfaces as `WRITE`

> **Status: added 2026-09-23 after the real-AWS corpus run on `rayito-base` 23.0.** The `watch` programs (D23 item 4) received only `FilesystemEvent(name='note.txt', type=RENAME, entry=None)` after `files.write`.

- **Root cause.** `Write` lands each file as `.rayito-tmp-<random>` + `rename(2)` over the destination (M3 D6). inotify reports `IN_CREATE`/`IN_MODIFY`/`IN_MOVED_FROM` on the temporary (dropped by the translator) and `IN_MOVED_TO` on the destination, which M3 mapped to `RENAME`. `entry` is only resolved for `CREATE`, `WRITE` and `CHMOD`, so it was `None` too. M3 recorded this as "the honest consequence of atomic writes"; M9 D23 item 4 asks for an event with `entry` after a write, which that rule can never satisfy.
- **What E2B does.** envd writes in place (`open(O_CREAT|O_TRUNC)` + copy), so its fsnotify watcher reports `CREATE` for a new file and `WRITE` for the data (plus `CHMOD` from the `chown`); an overwrite reports `WRITE`. The watch example on docs.e2b.dev (`filesystem/watch`, Python and JS) calls `files.write` and then checks `event.type == WRITE`. Code ported unchanged saw nothing on Rayito.
- **Decision.** Fix the agent, not the program: the translator pairs the temporary's `RenameFrom` with the destination's `RenameTo` by the inotify cookie (`RawWatchEvent.cookie`, from notify's `tracker()`), and the landing becomes a single `WRITE` of the destination, so `include_entry` fills `entry` by the existing rule. `WRITE` is the one type E2B reports for both a new file and an overwrite; a `CREATE` would be wrong for an overwrite, and the watcher cannot tell the two apart after a rename. Every other rename (a `mv` inside or into the directory, `files.rename`) stays `RENAME`, so the M3 rename semantics and their tests are unchanged. Unmatched temporary renames are bounded at 64 per watch (renames in different directories of a recursive watch can interleave).
- **Not possible in the SDK.** On the wire a landing and a genuine move-in are both `(name, RENAME)`; only the agent sees the cookie.
- **Spec.** MODIFIED `filesystem` requirement "Watch events carry relative names and the E2B event types" (`specs/filesystem/spec.md` of this change); the corpus programs stay as written. Needs a `rayd` republish of `rayito-base` before the corpus `watch` programs can pass on AWS.
- **Tests red without the fix.** `rayd-core` `filesystem::events::tests::{an_atomic_write_landing_is_one_write_of_the_destination, interleaved_landings_pair_by_cookie_and_plain_moves_stay_renames, pending_landings_are_bounded}`; `rayd` `adapters::notify_watcher::unix::tests::a_rename_reaches_the_sink_with_one_cookie_on_both_halves` (real inotify); integration `m3_filesystem::write_rpc_into_a_watched_directory_surfaces_as_one_write_with_its_entry` (was `..._as_one_rename`). The M3 Python e2e `check_watch_callback_and_entries` now expects `WRITE` with `entry`.

## Risks / Trade-offs

- **The integrated MODIFIED blocks can drift from sibling wording.** Mitigation: archive last, plus the re-diff task (§10.3).
- **Breaking positional changes for 1.x-shim users** (sync `watch_dir`, sync `pty.create`, `pause() -> bool`). Mitigation: callables in the new `user` slot raise a descriptive `InvalidArgumentException` rather than misbehaving, and the CHANGELOG and compat doc carry a migration table. Following 2.51 is the goal of this change.
- **`logger=` routing touches many native call sites.** Mitigation: records keep their text, and a unit test asserts routing for create, readiness, reconnect and refresher.
- **The TS HTTP CONNECT tunnel is new code on the TLS path.** It is small and uses Node built-ins only. TLS verification is unchanged (`tls.connect` with `servername`), and the tunnel is tested against a local proxy. It only engages when `proxy` is set.
- **Git credentials are visible inside the sandbox** (argv during the command, `~/.git-credentials` with `dangerously_authenticate`). That is the E2B contract, documented, and never logged by the SDK.
- **`openssh-clients` enters the default image with git-core.** Egress is already open by default (T8), and ssh adds no inbound surface: no daemon, no port. It is recorded in the Dockerfile comment and T10.
- **The `connect` e2e needs a POSIX pseudo-terminal.** The pipe-mode scenario runs everywhere, and the pty scenario is required on the Linux e2e run.
- **The corpus is written in E2B-docs style rather than copied verbatim.** Each file cites its docs page, and the only Rayito line is the import, so anything else would be caught in review.

## Migration Plan

Changes for programs written against the 1.x `rayito.e2b` shim:

| Before (1.x shim) | After (2.x shim) |
|---|---|
| `sbx.pause()` returned the sandbox id | returns `bool` (`Sandbox.pause(id)` likewise) |
| sync `files.watch_dir(path, on_event)` | `watch_dir(path, on_event=cb)`; the second positional is `user`; default lifetime until `stop()` |
| sync `pty.create(size, on_data)` | `pty.create(size, on_data=cb)`; the second positional is `user` |
| `proxy=` warned and was ignored | honoured (`http://` only) |
| `NotEnoughSpaceException` never raised | alias of `DiskFullException`, raised on a full disk |
| `sbx.connection_config` raised `UnimplementedError` | returns a `ConnectionConfig` |
| `mcp=`, `network=`, `lifecycle=` etc. were `TypeError` | mapped, or `UnimplementedError` (D14) |
| TS disk full was `RateLimitError` | `DiskFullError` |

Native Python and TS changes are additive, with defaults unchanged. Rollback is a plain revert: there is no wire or data migration.

## Facts to record in acceptance (no decision depends on them)

- The real `codeInstallSizeInBytes` / `memorySnapshotSizeInBytes` delta of the M9 `rayito-base` version, with the git-core share. The emulated RPM sum is 31,452,146 B.
- Corpus wall time per program (Python sync, Python async, JS), and git operation latencies.
- The `rayito sandbox connect` round-trip for `echo hola` (keypress to echo, p50 over 20 keystrokes) on the Linux e2e run.
