## Context

State on 2026-09-16: M0–M6 accepted on real AWS and archived; `rayito` 0.1.0
(Python) has the full E2B-style surface in sync (`Sandbox`) and async
(`AsyncSandbox`) trees over shared pure helpers; `m7-oss-hygiene` and
`m7-supply-chain` are implemented and still open. No git repository exists on
the development box. The M7 research report
(`docs/research/2026-09-m7-oss-readiness.md`) §3(f) and §5 row 5 are the
brief for this change.

Facts verified for this design (2026-09-16, on the box unless noted):

- **PyPI `mcp`** (`https://pypi.org/pypi/mcp/json`): latest `2.2.0`, uploaded
  2026-09-07; `1.30.0` uploaded the same day is the last 1.x; `Requires-Python
  >=3.10`; runtime deps `anyio`, `httpx2>=2.5.0`, `jsonschema>=4.20.0`,
  `mcp-types==2.2.0`, `opentelemetry-api>=1.28.0`, `pydantic>=2.12.0`,
  `pyjwt[crypto]>=2.10.1`, `python-multipart>=0.0.9`, `sse-starlette>=3.0.0`,
  `starlette`, `typing-extensions>=4.13.0`, `uvicorn>=0.31.1`, `pywin32>=311`
  (Windows only); extras `cli` (`typer`, `python-dotenv`) and `rich`. Streamable
  HTTP therefore needs **no** extra: `starlette` and `uvicorn` are core deps.
- **v2 API** (docs at `https://py.sdk.modelcontextprotocol.io/`, migration
  guide and `llms-full.txt`; imports and signatures checked with
  `uv run --with mcp==2.2.0`): `from mcp.server import MCPServer`;
  `MCPServer(name, ..., instructions=, version=, lifespan=, log_level=)`
  (transport options are **not** constructor arguments);
  `@mcp.tool(name=, title=, description=, annotations=ToolAnnotations(...),
  structured_output=)`; `from mcp.server.mcpserver import Context`;
  `ctx.request_context.lifespan_context` is the object the lifespan yielded
  (`Context[T]` types it); `from mcp.server.mcpserver.exceptions import
  ToolError` → `CallToolResult(is_error=True)` with the message in `content`;
  any other exception is sanitised to a generic error; `MCPError` becomes a
  JSON-RPC error. Return-type rules: `str` → `TextContent` + `structured_content
  {"result": ...}`; a `TypedDict`/`BaseModel`/dataclass → JSON `TextContent` +
  `structured_content` of the object itself; a `list[TextContent | ImageContent
  | EmbeddedResource]` → those blocks, `structured_content=None`.
  `mcp.types` fields are snake_case (`ImageContent(type="image", data=<base64>,
  mime_type=)`, `ToolAnnotations(read_only_hint=, destructive_hint=,
  idempotent_hint=, open_world_hint=)`, `CallToolResult.is_error`,
  `Tool.input_schema`/`output_schema`). Probe on the box: a tool returning
  `[TextContent, ImageContent, EmbeddedResource]` arrives with content types
  `['text', 'image', 'resource']`; a `BaseModel` return arrives as
  `structured_content` plus pretty JSON text; `ToolError("x")` arrives as
  `is_error=True`, text `Error executing tool <name>: x`.
- **Transports**: `mcp.run()` defaults to stdio; `mcp.run(transport=
  "streamable-http", host="127.0.0.1", port=8000, streamable_http_path="/mcp",
  json_response=False, stateless_http=False, session_idle_timeout=1800,
  max_sessions=10000, max_request_body_size=4 MiB, transport_security=None)`
  builds a Starlette app and serves it with uvicorn (the same app is
  `mcp.streamable_http_app(**same)`). `MCPServer(log_level=)` calls
  `logging.basicConfig` on the root logger, which writes to **stderr**; stdout
  is the protocol channel on stdio.
- **Sessions**: the lifespan runs **once per server run** (entered before the
  first request, exited when the server stops), shared by every connection.
  A 2026-07-28 client over streamable HTTP is **sessionless** (no
  `Mcp-Session-Id`; `stateless_http` is a legacy-only knob); a legacy client
  gets an `Mcp-Session-Id` held in one process. `ServerSession` is a fresh
  proxy per message and the per-connection `Connection` object is "not
  currently reachable from `ctx`" (migration guide). On stdio there is one
  connection per process. Probe on the box: with the in-memory
  `Client(server)` the lifespan is entered when the client context opens and
  exited when it closes; with `streamable_http_app()` under uvicorn on an
  ephemeral port, it is entered at server start ("StreamableHTTP session
  manager started") and exited on `server.should_exit = True`.
- **Client**: `from mcp import Client, StdioServerParameters`;
  `Client(server_object, raise_exceptions=True)` connects in memory;
  `Client(StdioServerParameters(command=, args=, env=, cwd=))` spawns a
  subprocess with an **allow-listed** environment (`DEFAULT_INHERITED_ENV_VARS`
  on Windows: `APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT
  PROCESSOR_ARCHITECTURE SYSTEMDRIVE SYSTEMROOT TEMP USERNAME USERPROFILE`;
  on POSIX `HOME LOGNAME PATH SHELL TERM USER`) plus whatever `env=` adds;
  `Client("http://127.0.0.1:<port>/mcp")` connects over streamable HTTP;
  `await client.list_tools()` → `.tools[*].name/.annotations/.output_schema`;
  `await client.call_tool(name, arguments, read_timeout_seconds=)` →
  `CallToolResult(content, structured_content, is_error)`.
- **uv** 0.7.21 on the box: a dependency group may reference the project's
  own extra (`dev = ["rayito[mcp]"]`); probed with a throwaway `uv_build`
  project, `uv lock` resolves and `uv run` installs the extra. So every
  existing gate command (`uv run pytest tests/unit`, `uv run mypy src tests`,
  …) sees `mcp` without new flags.
- **Rayito SDK surface used** (`clients/python/src/rayito`): `AsyncSandbox.create(
  template, *, template_version, timeout, idle, execution_role_arn, ingress,
  logging, control_plane, transport, …)` resolves `template=None` from
  `RAYITO_TEMPLATE` (`_sandbox_base.TEMPLATE_ENV_VAR`); `IdlePolicy(
  max_idle_seconds ≥ 60, suspended_duration_seconds=None → timeout −
  max_idle_seconds, auto_resume=True)` mirrors AWS's `idlePolicy` (a suspended
  VM auto-resumes on the next request in 1.1–1.6 s, `AWS_API_NOTES.md` §5;
  idle counts only bytes crossing the endpoint); `await sandbox.run_code(code,
  timeout=)` → `Execution(results: list[Result], logs: Logs(stdout, stderr:
  list[str]), error: ExecutionError(name, value, traceback) | None,
  execution_count)` where `Result.png`/`.jpeg` are **base64 strings as
  received**, `.svg`/`.text`/`.html`/… are strings, `.chart` is a `Chart`;
  `await sandbox.commands.run(cmd, timeout=)` → `CommandResult(stdout, stderr,
  exit_code)` or raises `CommandExitException(exit_code, stdout, stderr)` on a
  non-zero exit and `TimeoutException` on the agent timeout;
  `await sandbox.files.read(path)` (UTF-8 strict text; a directory or symlink
  is `InvalidArgumentException`; a missing file `FileNotFoundException`);
  `await sandbox.files.write(path, data)` → `EntryInfo`;
  `await sandbox.files.list(path, depth=)` → `list[EntryInfo(name, type,
  path, size, modified_time, …)]`; `await AsyncSandbox.list(template=,
  control_plane=, transport=)` → `list[SandboxListItem(sandbox_id, state,
  template, template_version, started_at)]` (omits `TERMINATING|TERMINATED`);
  `await sandbox.kill()`; `sandbox.sandbox_id`. The e2e conftest already uses
  `RAYITO_TEMPLATE`, `RAYITO_TEMPLATE_VERSION`, `RAYITO_EXECUTION_ROLE_ARN`
  and `ingress=["ALL_INGRESS"]`; the quickstart creates with SDK defaults.
- **Unit fixtures**: `tests/unit/conftest.py` gives `fake_rayd` (a full fake
  `rayd` on loopback: `Health`, `Process`, `Filesystem`, `Code`, `Pty`) and
  `control_plane` (boto3 Stubber for `lambda-microvms` + `sts`); the
  `stub_launch(control_plane, fake_rayd)` pattern queues `run_microvm` and
  `create_microvm_auth_token`; the fake `CodeService` answers `plot` with a
  one-pixel PNG plus a chart, `raise ZeroDivisionError('…')`-style cells with
  an error, `print(x)`/arithmetic with text; the fake `ProcessService` runs
  `echo`, `err`, `exit <n>`, `cat`, `pwd`, `whoami`, unknown → exit 127.
- **LangChain** (`docs.langchain.com/oss/python/langchain/tools`, fetched):
  `from langchain.tools import tool`; the docstring is the description, type
  hints are the schema, `async def` is supported, `create_agent(model,
  tools=[...])`. **Vercel AI SDK** (`ai-sdk.dev/docs/reference/ai-sdk-core/tool`,
  AI SDK 7.x): `import { tool } from "ai"`, `tool({ description, inputSchema:
  z.object(...), execute: async (input) => ... })` (the field is `inputSchema`,
  not `parameters`).

## Goals / Non-Goals

Goals:

- Any MCP host runs a Rayito sandbox with one launch command and one
  environment variable (`RAYITO_TEMPLATE`), no code.
- The six tools cover the E2B MCP server's `run_code` plus the operations an
  agent needs around it (shell, files, discovery), with results a model can
  read (text/JSON, images it can see) and a client application can parse
  (`structured_content` where the shape is data).
- Cost hygiene by construction: one MicroVM per process, lazily created,
  suspended by AWS when idle, killed when the process ends, hard-capped by
  `timeout`.
- The whole behaviour is testable without AWS (fake `rayd` + Stubber through
  the SDK's in-memory and HTTP clients) and accepted against AWS with a
  scripted stdio client, the same path the Inspector and Claude Code use.
- Adapters for LangChain and the AI SDK exist as fifty-line examples so the
  docs can say "any framework" honestly, without adding packages to maintain.

Non-goals (report §5 and the brief):

- A separate `rayito-mcp` distribution on PyPI (the subpackage ships inside
  `rayito`; the console script is named `rayito-mcp`).
- Multi-tenant or authenticated HTTP (`--http` binds loopback for a single
  local user; OAuth, `transport_security`, per-client sandboxes are out).
- Per-session sandboxes over streamable HTTP, sandbox pools (ADR-008,
  `m7-suspended-pool`), S3 persistence (`m7-s3-persistence`), `set_timeout`.
- PTY, background commands, `stdin`, file watching, `write_files`, binary
  file transfer, uploads/downloads, resources or prompts on the MCP surface.
- Non-Python languages in `run_code` (`m7-poly-kernels`), the `rayito` CLI
  (`m7-cli`), TypeScript MCP server, publishing anything.
- Changes to `rayd`, the sidecar, the image, the `.proto`, the TypeScript SDK
  or the SDK's public Python surface.

## Decisions

### D1. Package shape: `rayito.mcp` subpackage behind the extra `rayito[mcp]`

The server lives at `clients/python/src/rayito/mcp/` inside the `rayito`
wheel, not as a second distribution: one version, one changelog, one
release pipeline (`m7-supply-chain` D9 lockstep), and the SDK internals it
needs (`AsyncSandbox`, `IdlePolicy`, exceptions, `_models`) stay private.

`clients/python/pyproject.toml`:

```toml
[project.optional-dependencies]
mcp = ["mcp>=2.2,<3"]

[project.scripts]
rayito-mcp = "rayito.mcp.__main__:main"

[dependency-groups]
dev = [
    ...existing entries...,
    "rayito[mcp]",
]
```

`mcp>=2.2,<3` pins the v2 line (2.2.0 is the first version this design was
checked against; the 1.x API has a different server class and constructor
signature and must not resolve). The `dev` group's self-reference keeps every
gate command unchanged (Context). `[project.scripts]` is a console entry
point; `uv_build` writes it to `entry_points.txt` in the wheel, so a host
config can use `uvx --from "rayito[mcp]" rayito-mcp` once the package is on
PyPI, and `uv run --project <repo>/clients/python --extra mcp rayito-mcp`
before that. `python -m rayito.mcp` is equivalent and is the form the docs
lead with.

Modules (all English identifiers, Spanish docstrings/messages as in the rest
of the SDK, no inline comments inside function bodies):

| Module | Content |
|---|---|
| `rayito/mcp/__init__.py` | Imports `mcp` inside `try/except ModuleNotFoundError` and re-raises `ModuleNotFoundError("rayito.mcp necesita el extra 'mcp': pip install \"rayito[mcp]\"")` from the original; re-exports `McpSettings`, `SandboxLease`, `build_server`, `main` (from `_cli`) |
| `rayito/mcp/_settings.py` | `McpSettings` frozen dataclass + `McpSettings.from_env(environ)` (D4); pure, no `mcp` import |
| `rayito/mcp/_results.py` | Pure mapping helpers and the `TypedDict` result shapes (D5, D6, D8); imports `mcp.types` only for the content-block classes |
| `rayito/mcp/_lease.py` | `SandboxLease` (D3); imports `rayito` only |
| `rayito/mcp/_server.py` | `build_server(settings, *, control_plane=None, transport=None) -> MCPServer`: the lifespan, the six tools, the error translation (D5, D7) |
| `rayito/mcp/_cli.py` | `parse_args(argv)`, `main(argv=None)`, `run(server, options)` (D9) |
| `rayito/mcp/__main__.py` | Shim de `python -m rayito.mcp`: importa `main` de `_cli` y lo ejecuta; separado para que el paquete reexporte `main` sin el doble import de `__main__` |

`rayito/__init__.py` does **not** import `rayito.mcp`; `import rayito` keeps
working without the extra.

### D2. SDK usage: `MCPServer`, typed lifespan, `ToolError`, content blocks

`build_server` returns
`MCPServer("rayito", instructions=SERVER_INSTRUCTIONS, version=rayito.__version__,
lifespan=<lease lifespan>, log_level=settings.log_level)`. `SERVER_INSTRUCTIONS`
is a short Spanish paragraph telling the model that all tools share one
sandbox (Linux, uid 1000, `/home/user`, Python kernel with state between
`run_code` calls, internet egress unless the image says otherwise), that the
sandbox appears on the first call and is destroyed when the server stops, and
that a non-zero `exit_code` or a kernel `error` is data to read, not a failure
to retry blindly. Tool descriptions are the tools' docstrings, in Spanish like
every docstring in the SDK (switching the language is a one-file edit of
`_server.py`; tool and argument **names** are English and stable).

Every tool is `async def` and receives `ctx: Context[SandboxLease]`; it reads
the lease from `ctx.request_context.lifespan_context`. Tools never construct
`CallToolResult`: they return a `TypedDict` (structured output) or a list of
content blocks (`run_code`), exactly the two return shapes the SDK documents.
Failures the model can act on raise `ToolError` (D7). Argument constraints use
`Annotated[..., Field(...)]` from pydantic (a dependency of `mcp`, so
acceptable inside `rayito.mcp` only).

Images are built as `ImageContent(type="image", data=result.png,
mime_type="image/png")` directly from the SDK's base64 string; the `Image`
helper is not used because it takes raw bytes and would decode and re-encode
what the sidecar already encoded.

### D3. Ownership: one sandbox per server process, lazy, idle-suspended, killed at shutdown

The unit of ownership is the **server process**. On stdio a host launches
one process per MCP connection, so "one sandbox per MCP session" holds
exactly. Over streamable HTTP the 2026-07-28 protocol has no session
(Context), the SDK does not expose the connection object to handlers, and a
legacy `Mcp-Session-Id` would tie behaviour to the client's protocol era;
keying on the process is the only honest option, and HTTP mode is declared a
single-user local transport (D9).

`SandboxLease` (`_lease.py`):

```python
@dataclass
class SandboxLease:
    settings: McpSettings
    control_plane: ControlPlane | None = None
    transport: TransportSettings | None = None
    _sandbox: AsyncSandbox | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> AsyncSandbox: ...   # creates on first call
    async def peek(self) -> AsyncSandbox | None: ...  # never creates
    async def close(self) -> None: ...             # kill + close, idempotent
```

- `acquire()` takes the lock; if `_sandbox` is `None` it calls
  `AsyncSandbox.create(settings.template, template_version=..., timeout=
  settings.timeout_seconds, idle=settings.idle_policy, execution_role_arn=
  settings.execution_role_arn, logging=settings.logging, ingress=
  ["ALL_INGRESS"], control_plane=..., transport=...)` and stores it. Egress is
  not passed (the MicroVM inherits the image version's connector,
  `AWS_API_NOTES.md` Q44). Concurrent first calls wait on the lock and share
  the result; one MicroVM is ever created. A failed create leaves `_sandbox`
  `None` so the next call retries (the SDK already terminated the VM,
  `keep_on_failure=False`).
- Idle: no timer in the server. `settings.idle_policy` is
  `IdlePolicy(max_idle_seconds=RAYITO_MCP_IDLE_SECONDS, auto_resume=True)`
  (`suspended_duration_seconds=None` → `timeout − max_idle`), so AWS suspends
  the VM when no bytes cross the endpoint for that long and the SDK's
  reconnect logic resumes it transparently on the next tool call. `0`
  disables auto-suspend (`idle=None`). A suspended VM stops billing compute;
  a cycle costs ≈ $0.005 at 2 GB (`IdlePolicy` docstring), which is why the
  default stays 300 s and the floor is the API's 60 s.
- Shutdown: the lifespan is
  `async with lease_lifespan(server): ... finally: await lease.close()`.
  `close()` calls `kill()` (`terminate-microvm`, `SandboxNotFoundException`
  suppressed) then `close()` on the SDK object, once. On stdio the server
  returns when the host closes stdin, on HTTP when uvicorn shuts down
  (SIGINT/SIGTERM); both run the `finally`. A `SIGKILL`ed process leaks the
  VM until `timeout` — documented, and the reason `RAYITO_MCP_TIMEOUT_SECONDS`
  defaults to one hour and not eight.
- `list_sandboxes` and every other tool that does not need the sandbox
  (`list_sandboxes` only) use `peek()`; the five others use `acquire()`.

### D4. Configuration: environment only

`McpSettings.from_env(environ: Mapping[str, str]) -> McpSettings` reads:

| Variable | Meaning | Default / validation |
|---|---|---|
| `RAYITO_TEMPLATE` | image name or ARN, the same variable the SDK's `create()` reads | required for the sandbox tools; **not** validated at startup (D7) |
| `RAYITO_TEMPLATE_VERSION` | `template_version` | `None` (latest ACTIVE) |
| `RAYITO_EXECUTION_ROLE_ARN` | `execution_role_arn`; when set `logging="cloudwatch"` (as in `tests/e2e/conftest.py`) | `None` → `logging="disabled"` |
| `AWS_REGION` / `AWS_DEFAULT_REGION` / `AWS_PROFILE` and the credential variables | read by boto3 through `resolve_control_plane`; the server passes nothing | boto3 defaults |
| `RAYITO_MCP_TIMEOUT_SECONDS` | `timeout` (maximum life, running + suspended) | `3600`; integer in `60..=28800`, else `ValueError` at startup |
| `RAYITO_MCP_IDLE_SECONDS` | `IdlePolicy.max_idle_seconds`; `0` disables auto-suspend | `300`; `0` or integer `>= 60` **and strictly less than `RAYITO_MCP_TIMEOUT_SECONDS`** (what `resolve_idle_policy` enforces at `create`), else `ValueError` at startup naming both variables |
| `RAYITO_MCP_LOG_LEVEL` | `MCPServer(log_level=)` | `INFO`; one of `DEBUG INFO WARNING ERROR CRITICAL` |

`from_env` raises `ValueError` with a Spanish message naming the variable for
a malformed numeric or level value; `main()` prints it to stderr and exits 2.
No CLI flags duplicate these variables: hosts configure servers through
`env`, and one source of truth avoids precedence rules. `--host`/`--port`
exist only because they describe the transport, not the sandbox (D9).

### D5. The six tools

All tool and argument names are English and final. `ctx` is invisible in the
schemas. Sizes are characters of the decoded text (D8).

| Tool | Arguments (schema) | Returns | Annotations |
|---|---|---|---|
| `run_code` | `code: str` (Python), `timeout: int = 300` (`ge=1, le=3600`, seconds of wall clock enforced by the agent) | `list[TextContent \| ImageContent \| EmbeddedResource]` (D6) | `read_only_hint=False, destructive_hint=False, open_world_hint=True` |
| `run_command` | `cmd: str` (run by `/bin/sh -c` as the sandbox user, cwd `/home/user`), `timeout: int = 60` (`ge=1, le=3600`), `cwd: str \| None = None` | `CommandOutput = TypedDict(stdout: str, stderr: str, exit_code: int, truncated: bool)` | `read_only_hint=False, destructive_hint=False, open_world_hint=True` |
| `read_file` | `path: str` (absolute or relative to `/home/user`) | `FileContent = TypedDict(path: str, content: str, size: int, truncated: bool)`; `format="text"` (UTF-8 strict) | `read_only_hint=True, idempotent_hint=True` |
| `write_file` | `path: str`, `content: str` (UTF-8) | `WriteReceipt = TypedDict(path: str, size: int)` from the returned `EntryInfo` | `read_only_hint=False, destructive_hint=True` (overwrites), `idempotent_hint=True` |
| `list_files` | `path: str = "/home/user"`, `depth: int = 1` (`ge=1, le=5`) | `DirectoryListing = TypedDict(path: str, entries: list[FileEntry])`, `FileEntry = TypedDict(name: str, path: str, type: str \| None, size: int, modified_time: str)` (`type` is `FileType` value or `None`, `modified_time` ISO 8601) | `read_only_hint=True, idempotent_hint=True` |
| `list_sandboxes` | none | `SandboxList = TypedDict(sandboxes: list[SandboxSummary])`, `SandboxSummary = TypedDict(sandbox_id: str, state: str, template: str, template_version: str, started_at: str, current: bool)`; lists `AsyncSandbox.list(template=settings.template)` (all states the SDK returns by default), `current` is `True` for this process's sandbox | `read_only_hint=True, idempotent_hint=True` |

`run_command` maps `CommandExitException` to a normal `CommandOutput` with the
exception's `exit_code`, `stdout`, `stderr`: a failing command is information
for the model (E2B and every shell tool behave this way). `list_sandboxes`
needs `RAYITO_TEMPLATE` too (it lists that template) and never creates a
sandbox. `write_file` is the only tool with `destructive_hint=True` because it
replaces an existing file; `run_code`/`run_command` can destroy anything
inside the sandbox but the sandbox is disposable, and marking them
destructive would make hosts prompt on every call. Nothing on the surface
reaches outside the MicroVM.

### D6. `run_code` result: one JSON text block, then images, then resources

The first block is always a `TextContent` whose text is a JSON document
(`json.dumps(..., ensure_ascii=False, indent=2)`):

```json
{
  "text": "42",
  "stdout": "hola\n",
  "stderr": "",
  "error": null,
  "execution_count": 3,
  "results": [{"index": 0, "mime_types": ["text/plain"]}],
  "truncated": false
}
```

- `text` is `execution.text` (the main result's `text/plain`), `null` when
  there is none; `stdout`/`stderr` are `"".join(execution.logs.stdout)` and
  the same for stderr; `error` is `{"name", "value", "traceback"}` or `null`;
  `results[i].mime_types` lists the keys of `Result.raw` for every result in
  order (so the model knows a chart or HTML existed even when it is not
  attached).
- For each `Result` with `png` an `ImageContent(mime_type="image/png",
  data=result.png)` follows, then `jpeg` as `image/jpeg`; for each `svg` an
  `EmbeddedResource(resource=TextResourceContents(uri=
  "rayito://<sandbox_id>/results/<execution_count>/<index>.svg", mime_type=
  "image/svg+xml", text=result.svg))`. `html`, `markdown`, `latex`, `json`,
  `data`, `chart` and `pdf` are **not** attached in this change: they are
  listed in `mime_types`, and the model can `read_file` or re-run to get them
  as text. That keeps results bounded and the mapping small.
- A kernel exception is `error`, `is_error=False`: the cell ran and the model
  needs the traceback. `ExecutionTimeout` (the agent's timeout) arrives the
  same way (`error.name == "ExecutionTimeout"`), as the SDK documents.
- The block list is the return type (`list[TextContent | ImageContent |
  EmbeddedResource]`), so `structured_content` is `None` and no output schema
  is published, which the SDK documents for content-block returns. The
  mapping `execution_to_blocks(execution, *, sandbox_id, limit)` lives in
  `_results.py` and is unit-tested on synthetic `Execution` objects
  independently of the server.

### D7. Errors: `ToolError` for what the model can act on, data for outcomes

| Situation | Behaviour |
|---|---|
| `RAYITO_TEMPLATE` unset and a tool needs the sandbox or the template | `ToolError("RAYITO_TEMPLATE no está definido: configura la imagen (nombre o ARN) en el entorno del servidor")` |
| `AsyncSandbox.create` fails (`SandboxException` and subclasses: `AuthenticationException`, `CapacityException`, `QuotaExceededException`, `SandboxNotReadyException`, boto3 `ClientError` translated by the SDK, …) | `ToolError` with the exception's message prefixed `no se pudo crear el sandbox: `; the lease stays empty so the next call retries |
| Kernel error in `run_code`; non-zero exit in `run_command` | data (D5, D6) |
| `TimeoutException` from `run_command` (agent timeout) | `ToolError("el comando superó el timeout de <n> s")` |
| `FileNotFoundException`, `InvalidArgumentException` (directory, symlink, bad path, non-UTF-8 content), `DiskFullException`, `RateLimitException` (> 10 000 entries) | `ToolError` with the SDK message |
| `SandboxStateException` (transient: the phase gate while AWS suspends/resumes, the reconnect deadline expiring mid-suspend, a `ConflictException`) | `ToolError("el sandbox está en transición (<reason>); reintenta en unos segundos")`; the lease is untouched, no `terminate-microvm` |
| `SandboxNotFoundException` after the sandbox died (hit `timeout`, killed externally; the SDK's only terminal signal — `SandboxLifetimeException` is raised only by `validate_timeout` before `create`) | `ToolError("el sandbox <id> ya no existe (<reason>); la siguiente llamada crea uno nuevo")` and the lease is reset to `None` by `close()` on the SDK handle, without `kill()` (the VM is already terminal) |
| Anything else (`grpc` internals, bugs) | propagates; the SDK sanitises it to a generic error for the client and logs the traceback on the server |

The translation is one helper, `tool_error_from(exc)`, used by every tool
through a single `async with translated_errors():` context manager, so no
tool body has `try/except` ladders.

### D8. Output bounds

`MAX_OUTPUT_CHARS = 100_000` (a module constant in `_results.py`, not in
`_limits.py`: it is a presentation limit of this server, not an SDK or agent
limit, so `gen_limits.py --check` is untouched). `run_command.stdout/stderr`,
`read_file.content`, and `run_code`'s `stdout`/`stderr`/`text` are each cut
to the first `MAX_OUTPUT_CHARS` characters with the suffix
`\n… [truncado: <n> caracteres más]`, and the result's `truncated` flag is
set. Images are attached as the sidecar delivered them (it already drops
values above 8 MiB, `Result` docstring); no additional image cap.
`write_file.content` is not limited by the server (the SDK's write limits
apply). The helper `truncate_text(text, limit) -> tuple[str, bool]` is pure
and unit-tested.

### D9. Transports and the `python -m rayito.mcp` CLI

`__main__.py` uses `argparse` (stdlib; no `mcp[cli]`/typer):

```
python -m rayito.mcp                      # stdio (default)
python -m rayito.mcp --http               # streamable HTTP on 127.0.0.1:8000/mcp
python -m rayito.mcp --http --host 127.0.0.1 --port 8000
```

- `parse_args(argv) -> RunOptions(http: bool, host: str, port: int)`; pure,
  unit-tested; `--host`/`--port` without `--http` is a usage error (exit 2).
- `main(argv=None) -> int`: `settings = McpSettings.from_env(os.environ)`
  (exit 2 with the message on `ValueError`), `server = build_server(settings)`,
  then `run(server, options)`: `server.run()` for stdio,
  `server.run(transport="streamable-http", host=options.host, port=options.port)`
  for HTTP (defaults for `streamable_http_path`, `json_response`,
  `stateless_http`, `session_idle_timeout`, `max_sessions`,
  `max_request_body_size`, `transport_security` are left as the SDK's; the
  endpoint is `/mcp`). `run` takes the callable to invoke so the unit test can
  assert the exact keyword arguments without starting a server.
- HTTP has **no authentication** and binds loopback by default; when `--host`
  is not a loopback address `main()` logs a `WARNING` ("sin autenticación:
  cualquier cliente que alcance <host>:<port> controla el sandbox") and
  continues. It exists for hosts and tools that only speak HTTP (the
  Inspector's URL mode, remote-server entries in Cursor/Claude Code) and for
  a developer's own machine, not for exposure.
- stdio: nothing is ever written to stdout except the protocol; all logging
  goes to stderr via the root logger `MCPServer(log_level=)` configured.
  `print` is banned in `src/`: ruff `T20` is added to `[tool.ruff.lint]
  select` (no `print` exists under `src/rayito` today) with
  `per-file-ignores = { "tests/**" = ["T20"] }` for the e2e suites that
  report timings.

### D10. Concurrency

Tools are `async def` on the server's event loop (`anyio.run` with the
asyncio backend); `AsyncSandbox` is used, never the sync tree, so no thread
pool and no blocking of the loop. The lease lock serialises only creation;
after that the shared `AsyncSandbox` serves concurrent tool calls (the SDK's
async tree supports concurrent RPCs; `run_code` cells are serialised by the
kernel itself, one cell at a time per context, which the model experiences as
ordering, not failure). `close()` runs under the same lock so a call racing
shutdown either finishes on the live sandbox or fails with a
`SandboxException` translated by D7.

### D11. Logging hygiene

`SECURITY.md` "Higiene de logging" applies unchanged: the server logs tool
names, argument **sizes** (characters/bytes), durations, the `sandbox_id`,
exit codes and error **names**, never `code`, `cmd`, `content`, file
contents, stdout/stderr, tokens or the JWE. `RAYITO_MCP_LOG_LEVEL=DEBUG`
adds timings and the resolved settings with the template name, still no
payloads. Unit test: a caplog assertion that a `run_code` of a marker string
and a `write_file` of another marker never emit those markers at `DEBUG`.

### D12. Tests

Unit (`clients/python/tests/unit/`, fake `rayd` + Stubber, no network beyond
loopback; every test also passes `ruff`/`mypy strict`):

- `test_mcp_settings.py`: defaults; each variable; the `0` idle case; the
  four `ValueError`s; `logging` derived from the role ARN.
- `test_mcp_results.py`: `truncate_text`; `execution_to_blocks` on synthetic
  `Execution`s (text only; png + chart → JSON lists `image/png` and
  `e2b/chart`, one `ImageContent`; svg → one `EmbeddedResource` with the
  `rayito://` URI; error → `error` object, `text` null; long stdout →
  truncated + flag); `command_output_from(result | exception)`;
  `file_entry_from(EntryInfo)`; `sandbox_summary_from(item, current)`.
- `test_mcp_server.py` (in-memory `Client(build_server(settings,
  control_plane=control_plane.plane, transport=fake_rayd.transport),
  raise_exceptions=True)`):
  - `tools/list` returns exactly the six names with the D5 annotations and
    an `output_schema` for the five structured tools and none for `run_code`;
  - `list_sandboxes` before any other call → `run_microvm` never stubbed, so
    the Stubber proves no sandbox was created; result lists the stubbed
    `list_microvms` items with `current: false`;
  - first `run_command("echo hola")` creates the sandbox (`stub_launch`),
    returns `stdout "hola\n"`, `exit_code 0`; a second call reuses it (no
    second `run_microvm` queued, Stubber would fail);
  - two concurrent first calls (`asyncio.gather`) create one sandbox;
  - `run_command("exit 3")` → `exit_code 3`, `is_error False`;
    `run_command("nosuchcmd")` → `exit_code 127` with the stderr;
  - `write_file` → `read_file` round trip; `read_file` of a missing path →
    `is_error True` with the SDK message; `list_files` shows the entry with
    `type "file"`;
  - `run_code("1+1")` → JSON text `"text": "2"`; `run_code("plot")` → a
    second block of type `image` with `mime_type image/png`;
    `run_code("raise ValueError('x')")` → `error.name "ValueError"`,
    `is_error False`;
  - missing template (`McpSettings(template=None)`) → `run_command` is
    `is_error True` naming `RAYITO_TEMPLATE` and no AWS call is made;
  - `run_microvm` stubbed to raise a service error → `is_error True` with the
    `no se pudo crear el sandbox` prefix, a retry after re-stubbing succeeds;
  - closing the client context → `terminate_microvm` was called exactly once
    (`assert_no_pending_responses` plus a queued `terminate_microvm`), and a
    client that never called a sandbox tool leaves no `terminate_microvm`
    call;
  - the D11 caplog test.
- `test_mcp_http.py`: `build_server(...).streamable_http_app()` served by
  `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=<free port>))`
  as a task on the test loop; `Client(f"http://127.0.0.1:{port}/mcp")` lists
  the tools and runs `run_command("echo http")`; after `server.should_exit =
  True` and awaiting the serve task, `terminate_microvm` was called. Every
  await in the test is bounded by `asyncio.wait_for(..., 30)`.
- `test_mcp_main.py`: `parse_args` cases; `run` invoked with a recording
  fake `server.run` → `()` for stdio and `(transport="streamable-http",
  host=..., port=...)` for HTTP; `main` exit code 2 on a bad
  `RAYITO_MCP_IDLE_SECONDS`; the non-loopback warning.
- `test_packaging.py`: `project.optional-dependencies.mcp == ["mcp>=2.2,<3"]`,
  `project.scripts["rayito-mcp"] == "rayito.mcp.__main__:main"`,
  `"rayito[mcp]" in dependency-groups.dev`; `import rayito` does not import
  `mcp` (`sys.modules` check in a subprocess).

E2E (`clients/python/tests/e2e/test_m7_mcp.py`, marker `e2e`, the existing
`RAYITO_E2E=1` + `RAYITO_TEMPLATE` gate and the session sweeper apply):

1. `Client(StdioServerParameters(command=sys.executable, args=["-m",
   "rayito.mcp"], env={every `AWS_*` and `RAYITO_*` variable of `os.environ`,
   `RAYITO_MCP_TIMEOUT_SECONDS: "900"`, `RAYITO_MCP_IDLE_SECONDS: "0"`,
   `RAYITO_MCP_LOG_LEVEL: "DEBUG"`}))` — the same 900 s cap as every test
   sandbox and no auto-suspend, so the assertions are deterministic;
2. `list_tools` → the six names;
3. `run_command("echo hola")` → `stdout "hola\n"`, `exit_code 0`; the elapsed
   time of this first call is printed as `mcp first call (create) <s>`;
4. `write_file("/home/user/mcp.txt", "hola mcp")` → `read_file` → same
   content; `list_files("/home/user")` contains `mcp.txt`;
5. `run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")`
   → an `image` block with `mime_type image/png` whose base64 decodes to a
   PNG signature; `run_code("1/0")` → JSON `error.name "ZeroDivisionError"`,
   `is_error False`;
6. `list_sandboxes` → exactly one item with `current: true`; its
   `sandbox_id` is remembered;
7. leaving the client context (stdin EOF) → within 30 s
   `control_plane.get_microvm(sandbox_id).state` is `TERMINATING` or
   `TERMINATED` (the acceptance for "killed on session end").

Manual evidence recorded in `tasks.md` "Notes" (not a gate): the MCP
Inspector (`pnpm dlx @modelcontextprotocol/inspector`, pnpm only) pointed at
the launch command `uv run --project clients/python --extra mcp rayito-mcp`
lists the six tools and renders the PNG of `run_code`; Claude Code drives
`run_code` once after `claude mcp add`. `mcp dev` is not used: it imports a
server *file*, and this server is a module with a console script.

### D13. Docs page and host configuration

`docs/site/docs/mcp.md` ("Servidor MCP", Spanish, nav entry after
"Compatibilidad con E2B") with these sections, in order:

1. **Qué es** — one paragraph: one sandbox per server process, created on the
   first call, suspended when idle, destroyed when the host closes the
   server; what it costs (`docs/site/docs/cost.md` link).
2. **Instalación** — `pip install "rayito[mcp]"` / `uv add "rayito[mcp]"`;
   before PyPI, `uv run --project <repo>/clients/python --extra mcp rayito-mcp`.
3. **Variables de entorno** — the D4 table verbatim.
4. **Herramientas** — the D5 table (name, arguments, what it returns) and
   the D6 JSON example.
5. **Claude Code** —
   `claude mcp add rayito -e RAYITO_TEMPLATE=rayito-base -e AWS_REGION=us-east-1 -e AWS_PROFILE=<perfil> -- uvx --from "rayito[mcp]" rayito-mcp`
   plus the `--project` variant.
6. **Claude Desktop** — the `claude_desktop_config.json` block:

   ```json
   {
     "mcpServers": {
       "rayito": {
         "command": "uvx",
         "args": ["--from", "rayito[mcp]", "rayito-mcp"],
         "env": {
           "RAYITO_TEMPLATE": "rayito-base",
           "AWS_REGION": "us-east-1",
           "AWS_PROFILE": "<perfil>"
         }
       }
     }
   }
   ```

   with the file locations (`~/Library/Application Support/Claude/` and
   `%APPDATA%\Claude\`) and the "quit completely" note from the SDK docs.
7. **Cursor** — `.cursor/mcp.json` under `mcpServers`, same `command`/`args`/
   `env`. **VS Code** — `.vscode/mcp.json` under `servers` with
   `"type": "stdio"`.
8. **Modo HTTP** — `python -m rayito.mcp --http`, the URL
   `http://127.0.0.1:8000/mcp`, the no-auth/loopback warning, and a Claude
   Code remote example `claude mcp add --transport http rayito-http
   http://127.0.0.1:8000/mcp`.
9. **Inspector** — how to point the MCP Inspector at the launch command and
   what to expect (the six tools, `run_code` rendering the PNG).
10. **Costes y límites** — one sandbox per process, `timeout` default 3600 s,
    a killed host process leaks the VM until then, `list_sandboxes` to find
    it and `Sandbox.kill` / the CLI-less `aws lambda-microvms
    terminate-microvm` to clean up; the 100 000-character truncation.

`README.md`: one bullet under the existing feature list / "Estado" paragraph:
"Servidor MCP (`rayito[mcp]`, `python -m rayito.mcp`) para Claude Code,
Claude Desktop, Cursor y VS Code: `docs/site/docs/mcp.md`". The mkdocs
`--strict` build is the gate for the page.

### D14. Framework adapters as examples

`docs/examples/langchain_tool.py` (≤ 50 lines, `ruff` clean when checked
with the SDK's config, `python -m py_compile` clean):

- `from langchain.tools import tool`; a module-level `Sandbox` created lazily
  by `sandbox()` (`Sandbox.create(timeout=900)`, template from
  `RAYITO_TEMPLATE`); `@tool def run_python(code: str) -> str` returning the
  D6-style summary as text (text, stdout, stderr, error). The example formats
  those four fields itself in a few lines and does **not** import
  `rayito.mcp` or any private module: it must read as "the SDK plus the
  framework", nothing else. `atexit` kills the sandbox; an
  `if __name__ == "__main__":` block shows `create_agent(model,
  tools=[run_python])` in two lines. Header docstring in Spanish explaining
  `pip install rayito langchain` and the env vars.
- `docs/examples/vercel_ai_tool.ts` (≤ 50 lines): `import { tool } from "ai"`,
  `import { z } from "zod"`, `import { Sandbox } from "rayito"`; a lazily
  created `Sandbox` promise (`Sandbox.create({ timeoutMs: 900_000 })`);
  `export const runPython = tool({ description, inputSchema: z.object({ code:
  z.string() }), execute: async ({ code }) => { const execution = await
  (await sandbox()).runCode(code); return { text: execution.text ?? null,
  stdout: execution.logs.stdout.join(""), stderr: ..., error: execution.error
  ?? null }; } })`; `process.on("beforeExit", () => sandbox.kill())`. Header
  comment in Spanish with `pnpm add ai zod rayito`.
- Neither file is packaged, imported by tests or built by CI. The TypeScript
  example is checked once by hand in a scratch project (`pnpm init`, `pnpm add
  ai zod`, `pnpm add ../../clients/typescript` via `file:`, `pnpm exec tsc
  --noEmit --strict --module nodenext --moduleResolution nodenext
  --target es2022 vercel_ai_tool.ts`) and the command and result are recorded
  in `tasks.md` "Notes". `docs/site/docs/mcp.md` §1 links both files as "para
  frameworks sin MCP".

### D15. Bookkeeping

- `clients/python/CHANGELOG.md` `[Unreleased]` → `### Added`: "Servidor MCP
  (`rayito.mcp`, extra `rayito[mcp]`, script `rayito-mcp`): seis herramientas
  (`run_code`, `run_command`, `read_file`, `write_file`, `list_files`,
  `list_sandboxes`), stdio y streamable HTTP, un sandbox por proceso".
- `MILESTONES.md` M7 table row 5: scope column updated to the final tool
  list and transports, acceptance column to
  "`tests/e2e/test_m7_mcp.py` verde contra AWS (cliente stdio del SDK `mcp`
  sobre `python -m rayito.mcp`) + Inspector/Claude Code a mano", state
  "en curso" when implementation starts, "implementado <fecha>" with the
  evidence when the e2e is green.
- `scripts/check_wheel.py`: `REQUIRED_FILES` gains `rayito/mcp/__init__.py`
  and `rayito/mcp/__main__.py`; a new assertion that `entry_points.txt`
  contains `rayito-mcp = rayito.mcp.__main__:main`; `METADATA` contains
  `Provides-Extra: mcp` and `Requires-Dist: mcp>=2.2,<3; extra == 'mcp'`
  (the exact rendering is confirmed on the first build and pasted into
  `tasks.md` "Notes"; the assertion matches the rendered line).
- `.github/workflows/ci.yml`: no change needed (the `dev` group carries the
  extra); verified by reading the Python job, not assumed.

### D16. Coordination with sibling M7 changes

`m7-cli` (open in parallel) also edits `clients/python/pyproject.toml`
(`[project.optional-dependencies] cli`, `[project.scripts] rayito`, the
`dev` group), `scripts/check_wheel.py`, `tests/unit/test_packaging.py` and
`MILESTONES.md`. Every edit of this change is **additive** to a different key
(`mcp` extra, `rayito-mcp` script, the `"rayito[mcp]"` group entry, the
`rayito/mcp/*` wheel entries, its own test functions, its own milestone row),
so the two land in any order; whichever lands second re-runs `uv lock` and
`uv lock --check`. The self-referencing group entry (`"rayito[mcp]"`) and
`m7-cli`'s duplicated pin (`"typer>=0.15,<1"` in both places) are two valid uv
spellings of "install the extra in dev"; this change keeps the
self-reference because it cannot drift from the extra. `m7-supply-chain` D9's
lockstep release is unaffected: no version changes here.

## Risks / Trade-offs

- **`mcp` v2 is nine days old.** Mitigation: the constraint `>=2.2,<3` and
  every import/signature this design relies on was executed on the box;
  the unit suite exercises the real SDK (in-memory and HTTP), so a
  regression in a 2.x release shows up in `uv lock` bumps, not in a host.
- **Dependency weight of the extra** (pydantic, starlette, uvicorn, OTel
  API). Accepted: it is opt-in, and hosts launch the server in its own
  environment (`uvx`), so the SDK's own footprint is unchanged.
- **One process = one sandbox over HTTP.** Two clients on the same HTTP
  server share a sandbox. Documented and bound to loopback; anything more
  needs authentication first (non-goal).
- **Leaked VM after `SIGKILL`.** Bounded by `timeout` (1 h default); the
  docs page tells how to find and kill it. A future `rayito doctor`
  (`m7-cli`) will list orphans.
- **Spanish tool descriptions.** Consistent with the SDK; models read them
  fine. If adoption data says English, it is one file.
- **`run_code` publishes no output schema.** Content-block returns cannot
  carry `structured_content` in this SDK; the JSON text block is the
  parseable form and its shape is fixed by D6 and tested.
- **Idle suspend during a long `run_code`.** Idle counts bytes crossing the
  endpoint; a cell that prints nothing for `RAYITO_MCP_IDLE_SECONDS` can be
  suspended mid-cell. The SDK already handles it (waits for the resume and
  `Reattach`es), and the default 300 s is generous; documented in `mcp.md`
  §10 with the pointer to `RAYITO_MCP_IDLE_SECONDS=0`.
- **e2e cost**: one MicroVM for ≈ 2 minutes per run, ≈ $0.01 at the M6
  measurements; within the `e2e.yml` budget of `m7-supply-chain` D8.

## Migration Plan

No migration: new, opt-in surface. Users without the extra see no change
(`import rayito` unchanged; `rayito.mcp` raises the install hint). Rollback
is deleting the subpackage, the extra, the script and the docs page; the
`dev` group reference must be removed in the same edit or `uv lock` fails.

## Open Questions

None blocking. Two choices are recorded as deliberate and cheap to revisit:
Spanish tool descriptions (D2) and the set of result mime types attached by
`run_code` (D6: PNG, JPEG, SVG only).

## Acceptance test list

1. `cd clients/python && uv lock --check && uv run pytest tests/unit` green
   including `test_mcp_settings.py`, `test_mcp_results.py`,
   `test_mcp_server.py`, `test_mcp_http.py`, `test_mcp_main.py` and the new
   `test_packaging.py` cases; `uv run ruff check .`, `uv run ruff format
   --check .`, `uv run mypy src tests` clean.
2. `uv build && python ../../scripts/check_wheel.py dist/*.whl && uvx twine
   check dist/*` → `OK`, `PASSED`, with the `mcp` entries asserted.
3. `uv run --no-project --with dist/*.whl python -c "import rayito;
   import sys; assert 'mcp' not in sys.modules"` and `uv run --with
   "dist/<wheel>[mcp]" rayito-mcp --help` prints the usage.
4. `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e/test_m7_mcp.py
   -m e2e -v -s` green against real AWS; zero live MicroVMs afterwards
   (`Sandbox.list` empty for the template).
5. `cd clients/python && uv run --group docs mkdocs build -f
   ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build`
   green with `mcp.md` in the nav.
6. `python -m py_compile docs/examples/langchain_tool.py` and `uvx ruff check
   docs/examples/langchain_tool.py` clean; the TypeScript example type-checks
   in the scratch project (D14), command and output in `tasks.md` "Notes".
7. Every pre-existing gate (Rust, sidecar, TypeScript, `check_license.py`,
   `gen_limits.py --check`) unchanged and green.
8. `openspec validate m7-mcp-server --strict --no-interactive` passes.
