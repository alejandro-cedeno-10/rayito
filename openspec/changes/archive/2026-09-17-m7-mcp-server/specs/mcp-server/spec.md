## ADDED Requirements

### Requirement: MCP server package behind the optional extra
The Python distribution `rayito` SHALL ship the subpackage `rayito.mcp`, enabled by the optional extra `mcp` whose only requirement is `mcp>=2.2,<3` (the official Model Context Protocol Python SDK, v2 line). `import rayito` SHALL NOT import `mcp`; `import rayito.mcp` in an environment without the extra SHALL raise `ModuleNotFoundError` whose message names `pip install "rayito[mcp]"`. The package SHALL expose `rayito.mcp.McpSettings`, `rayito.mcp.SandboxLease`, `rayito.mcp.build_server(settings, *, control_plane=None, transport=None)` returning an `mcp.server.MCPServer` named `rayito` with `version` equal to `rayito.__version__`, and `rayito.mcp.main(argv=None) -> int`.

#### Scenario: extra missing
- **WHEN** `python -c "import rayito.mcp"` runs in an environment with `rayito` but without the `mcp` extra
- **THEN** it exits non-zero with a `ModuleNotFoundError` whose message contains `rayito[mcp]`, and `python -c "import rayito"` in the same environment exits 0

#### Scenario: server identity
- **WHEN** an MCP client connects to `build_server(McpSettings(...))` in memory and reads `server_info`
- **THEN** `name` is `rayito` and `version` equals `rayito.__version__`

### Requirement: Transports and command line
`python -m rayito.mcp` and the console script `rayito-mcp` SHALL start the server on **stdio** by default and SHALL write nothing to stdout except protocol messages (all logging goes to stderr through the root logger that `MCPServer(log_level=)` configures; `print` is forbidden under `src/rayito` by ruff rule `T20`). With `--http` they SHALL serve **streamable HTTP** via `MCPServer.run(transport="streamable-http", host=<--host>, port=<--port>)` with defaults `127.0.0.1` and `8000` (endpoint `http://<host>:<port>/mcp`, every other transport option left at the SDK default). `--host` or `--port` without `--http` SHALL be a usage error (exit 2). HTTP mode SHALL have no authentication; when `--host` is not a loopback address the server SHALL log a `WARNING` stating that any client reaching the address controls the sandbox, and continue. A malformed environment (see "Configuration through the environment") SHALL make `main` print the Spanish message to stderr and return 2 before any server starts.

#### Scenario: stdio by default
- **WHEN** `parse_args([])` is evaluated and `run(server, options, runner=recorder)` is called
- **THEN** `options.http` is `False` and the recorder was invoked once with no arguments

#### Scenario: streamable HTTP
- **WHEN** `parse_args(["--http", "--host", "0.0.0.0", "--port", "9000"])` is evaluated and `run(server, options, runner=recorder)` is called
- **THEN** the recorder was invoked once with exactly `transport="streamable-http", host="0.0.0.0", port=9000`, and a `WARNING` about the missing authentication was logged

#### Scenario: transport flags without --http
- **WHEN** `parse_args(["--port", "1"])` is evaluated
- **THEN** it raises `SystemExit(2)`

#### Scenario: HTTP round trip
- **WHEN** `build_server(...).streamable_http_app()` is served by uvicorn on a free loopback port and `Client("http://127.0.0.1:<port>/mcp")` lists tools and calls `run_command` with `{"cmd": "echo http"}` against the fake `rayd`
- **THEN** six tools are listed, the call returns `stdout == "http\n"` and `exit_code == 0`, and after the uvicorn server is told to exit `terminate_microvm` has been called once

### Requirement: Configuration through the environment
`McpSettings.from_env(environ)` SHALL read `RAYITO_TEMPLATE` (image name or ARN; may be absent), `RAYITO_TEMPLATE_VERSION` (default `None`), `RAYITO_EXECUTION_ROLE_ARN` (default `None`; when set the sandbox is created with `logging="cloudwatch"`, otherwise `"disabled"`), `RAYITO_MCP_TIMEOUT_SECONDS` (default `3600`; integer in `60..=28800`), `RAYITO_MCP_IDLE_SECONDS` (default `300`; `0` disables auto-suspend, otherwise integer `>= 60` and strictly less than `RAYITO_MCP_TIMEOUT_SECONDS`, the constraint `resolve_idle_policy` enforces at creation) and `RAYITO_MCP_LOG_LEVEL` (default `INFO`; one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). AWS region, profile and credentials SHALL come from boto3's own environment handling; the server SHALL pass no region or session of its own. A malformed value SHALL raise `ValueError` with a Spanish message naming the variable; an idle value that is neither `0` nor below the timeout SHALL raise `ValueError` naming both variables at startup, never at the first tool call. `settings.idle_policy` SHALL be `IdlePolicy(max_idle_seconds=<idle>, auto_resume=True)` or `None` when idle is `0`. No command-line flag SHALL duplicate these variables.

#### Scenario: defaults
- **WHEN** `McpSettings.from_env({})` is evaluated
- **THEN** `template is None`, `timeout_seconds == 3600`, `idle_seconds == 300`, `log_level == "INFO"`, `logging == "disabled"` and `idle_policy == IdlePolicy(max_idle_seconds=300, auto_resume=True)`

#### Scenario: idle disabled
- **WHEN** `McpSettings.from_env({"RAYITO_MCP_IDLE_SECONDS": "0"})` is evaluated
- **THEN** `idle_policy is None`

#### Scenario: malformed value
- **WHEN** `McpSettings.from_env({"RAYITO_MCP_IDLE_SECONDS": "abc"})` or `{"RAYITO_MCP_TIMEOUT_SECONDS": "30"}` or `{"RAYITO_MCP_IDLE_SECONDS": "10"}` or `{"RAYITO_MCP_LOG_LEVEL": "LOUD"}` or `{"RAYITO_MCP_TIMEOUT_SECONDS": "120"}` (default idle `300` is not below it) is evaluated
- **THEN** each raises `ValueError` whose message contains the offending variable name, and `main([])` under that environment returns 2 with the message on stderr

### Requirement: One sandbox per server process, lazily created, idle-suspended, killed at shutdown
The server SHALL own at most one `AsyncSandbox` per process, held by a `SandboxLease` yielded by the `MCPServer` lifespan. The sandbox SHALL be created on the first call of `run_code`, `run_command`, `read_file`, `write_file` or `list_files` with `AsyncSandbox.create(settings.template, template_version=settings.template_version, timeout=settings.timeout_seconds, idle=settings.idle_policy, execution_role_arn=settings.execution_role_arn, logging=settings.logging, ingress=["ALL_INGRESS"])` (no `egress`, no `metadata`, no `envs`), under an `asyncio.Lock` so that concurrent first calls create exactly one MicroVM, and reused by every later call. `list_sandboxes` SHALL never create a sandbox. Idle suspension SHALL be delegated to AWS's `idlePolicy` (no timer in the server); the SDK's reconnect logic resumes a suspended sandbox on the next call. When the server stops (stdin EOF on stdio, uvicorn shutdown on HTTP, or the in-memory client context closing) the lifespan SHALL call `kill()` (`terminate-microvm`, `SandboxNotFoundException` suppressed) and `close()` exactly once if a sandbox exists, and nothing otherwise. A failed creation SHALL leave the lease empty so that the next call retries. After a `SandboxNotFoundException` (the SDK's only terminal signal: the sandbox hit `timeout` or was killed externally) the lease SHALL be reset by releasing the SDK handle without any `terminate-microvm` call, so the next call creates a new sandbox. A `SandboxStateException` (suspend/resume in progress, `ConflictException`) SHALL NOT reset the lease nor terminate the sandbox: the next call reuses it.

#### Scenario: lazy creation and reuse
- **WHEN** an in-memory client calls `run_command` twice with `{"cmd": "echo hola"}` against the fake `rayd` with one `run_microvm` and one `create_microvm_auth_token` response queued
- **THEN** both calls return `stdout == "hola\n"`, the Stubber reports no pending responses and no second `run_microvm` was attempted

#### Scenario: concurrent first calls
- **WHEN** two `run_command` calls are issued concurrently with `asyncio.gather` on a fresh server with one `run_microvm` response queued
- **THEN** both succeed and exactly one MicroVM was created

#### Scenario: list without a sandbox
- **WHEN** the first call on a fresh server is `list_sandboxes` with a `list_microvms` response queued and no `run_microvm` queued
- **THEN** the call succeeds, every returned item has `current == false`, and the Stubber reports no pending responses

#### Scenario: killed at session end
- **WHEN** a `terminate_microvm` response is queued and the in-memory client context exits after a `run_command` call created the sandbox
- **THEN** `terminate_microvm` was called exactly once with the sandbox id, and on a server whose client never called a sandbox tool no `terminate_microvm` call occurs

#### Scenario: creation failure is retried
- **WHEN** `run_microvm` is stubbed to fail on the first call and to succeed on the second
- **THEN** the first tool call returns `is_error == true` with text starting `Error executing tool` and containing `no se pudo crear el sandbox`, and the second tool call succeeds

### Requirement: The six tools and their results
`tools/list` SHALL return exactly `run_code`, `run_command`, `read_file`, `write_file`, `list_files` and `list_sandboxes`, with English tool and argument names and Spanish descriptions, and:

- `run_code(code: str, timeout: int = 300 [1..3600])` SHALL execute Python in the sandbox's default kernel context via `run_code(code, timeout=timeout)` and return content blocks: first a `TextContent` whose text is a JSON object with keys `text` (main result `text/plain` or `null`), `stdout`, `stderr` (joined logs), `error` (`{"name", "value", "traceback"}` or `null`), `execution_count`, `results` (list of `{"index", "mime_types"}` for every result, `mime_types` being the keys of `Result.raw`) and `truncated`; then one `ImageContent` (`mime_type` `image/png` / `image/jpeg`, `data` the SDK's base64 string unchanged) per result carrying `png` / `jpeg`; then one `EmbeddedResource` with `TextResourceContents(uri="rayito://<sandbox_id>/results/<execution_count>/<index>.svg", mime_type="image/svg+xml")` per result carrying `svg`. Other mime types SHALL be listed in `mime_types` and not attached. A kernel error or `ExecutionTimeout` SHALL be reported in `error` with `is_error == false`. No output schema SHALL be published for this tool. Annotations: `read_only_hint=False`, `destructive_hint=False`, `open_world_hint=True`.
- `run_command(cmd: str, timeout: int = 60 [1..3600], cwd: str | None = None)` SHALL run `cmd` through `commands.run(cmd, timeout=timeout, cwd=cwd)` and return structured output `{"stdout", "stderr", "exit_code", "truncated"}`; a `CommandExitException` SHALL become the same shape with its `exit_code`, `stdout` and `stderr` and `is_error == false`. Annotations: `read_only_hint=False`, `destructive_hint=False`, `open_world_hint=True`.
- `read_file(path: str)` SHALL return `{"path", "content", "size", "truncated"}` from `files.read(path)` (UTF-8 text). Annotations: `read_only_hint=True`, `idempotent_hint=True`.
- `write_file(path: str, content: str)` SHALL write UTF-8 text with `files.write(path, content)` and return `{"path", "size"}` from the returned `EntryInfo`. Annotations: `read_only_hint=False`, `destructive_hint=True`, `idempotent_hint=True`.
- `list_files(path: str = "/home/user", depth: int = 1 [1..5])` SHALL return `{"path", "entries": [{"name", "path", "type", "size", "modified_time"}]}` from `files.list(path, depth=depth)` with `type` the `FileType` value or `null` and `modified_time` in ISO 8601. Annotations: `read_only_hint=True`, `idempotent_hint=True`.
- `list_sandboxes()` SHALL return `{"sandboxes": [{"sandbox_id", "state", "template", "template_version", "started_at", "current"}]}` from `AsyncSandbox.list(template=settings.template)` with `current == true` only for this process's sandbox. Annotations: `read_only_hint=True`, `idempotent_hint=True`.

The five structured tools SHALL publish an output schema. Text fields `stdout`, `stderr`, `content` and `run_code`'s `text`/`stdout`/`stderr` SHALL be cut to the first 100 000 characters with the suffix `\n… [truncado: <n> caracteres más]` and the `truncated` flag set to `true`; images SHALL not be capped by the server.

#### Scenario: tool list
- **WHEN** an in-memory client calls `list_tools`
- **THEN** the names are exactly the six above, `run_code` has no `output_schema`, the other five have one, and each tool's annotations match the values above

#### Scenario: run_code text, image and error
- **WHEN** the client calls `run_code` with `{"code": "1+1"}`, then `{"code": "plot"}`, then `{"code": "raise ValueError('x')"}` against the fake `rayd`
- **THEN** the first result's first block parses as JSON with `"text": "2"` and `"error": null`; the second has a first JSON block whose `results[0].mime_types` contains `image/png` and a second block of type `image` with `mime_type == "image/png"` and `data` equal to the fake's base64; the third has `is_error == false` and JSON `error.name == "ValueError"`

#### Scenario: run_command exit codes as data
- **WHEN** the client calls `run_command` with `{"cmd": "exit 3"}` and then `{"cmd": "nosuchcmd"}`
- **THEN** both results have `is_error == false`, `structured_content.exit_code` is `3` and `127` respectively, and the second `stderr` contains `command not found`

#### Scenario: files round trip
- **WHEN** the client calls `write_file` with `{"path": "/home/user/a.txt", "content": "hola"}`, then `read_file` with the same path, then `list_files` with `{"path": "/home/user"}`
- **THEN** `write_file` returns `size == 4`, `read_file` returns `content == "hola"` and `truncated == false`, and `list_files.entries` contains an entry named `a.txt` with `type == "file"`

#### Scenario: truncated output
- **WHEN** `execution_to_blocks` receives an `Execution` whose joined stdout has 100 010 characters
- **THEN** the JSON block's `stdout` ends with `… [truncado: 10 caracteres más]` and `truncated` is `true`

### Requirement: Error translation
Failures the model can act on SHALL surface as `ToolError` (`CallToolResult.is_error == true`, Spanish message): a missing `RAYITO_TEMPLATE` (message names the variable; no AWS call is made), a failed sandbox creation (message prefixed `no se pudo crear el sandbox: ` plus the SDK message), `TimeoutException` from `run_command`, `FileNotFoundException`, `InvalidArgumentException`, `DiskFullException`, `RateLimitException`, `SandboxStateException` (message `el sandbox está en transición (<reason>); reintenta en unos segundos`; the lease is untouched and no `terminate-microvm` is issued), and `SandboxNotFoundException` (message says the next call creates a new sandbox, and the lease is reset). Any other exception SHALL propagate to the SDK, which reports a generic error to the client and logs the traceback on the server. Non-zero exit codes and kernel errors are data, never `ToolError`.

#### Scenario: missing template
- **WHEN** the server is built with `McpSettings(template=None)` and the client calls `run_command` with `{"cmd": "echo x"}`
- **THEN** the result has `is_error == true`, its text contains `RAYITO_TEMPLATE`, and the Stubber saw no AWS call

#### Scenario: missing file
- **WHEN** the client calls `read_file` with `{"path": "/home/user/missing.txt"}`
- **THEN** the result has `is_error == true` and its text contains the SDK's `FileNotFoundException` message

### Requirement: Logging hygiene
The server SHALL log only tool names, argument sizes, durations, the sandbox id, exit codes and exception class names. It SHALL never log `code`, `cmd`, `content`, file contents, stdout, stderr, access tokens or the proxy JWE, at any log level.

#### Scenario: payloads never logged
- **WHEN** at `RAYITO_MCP_LOG_LEVEL=DEBUG` the client calls `run_code` with a code string containing the marker `MARKER_CODE_7f3a` and `write_file` with a content containing `MARKER_FILE_9c1d`
- **THEN** no captured log record contains either marker

### Requirement: Acceptance e2e over stdio against real AWS
`clients/python/tests/e2e/test_m7_mcp.py` (marker `e2e`, guarded by `RAYITO_E2E=1` and `RAYITO_TEMPLATE` like every e2e) SHALL spawn `python -m rayito.mcp` as a subprocess through the SDK's `Client(StdioServerParameters(command=sys.executable, args=["-m", "rayito.mcp"], env=...))` with an environment containing every `AWS_*` and `RAYITO_*` variable of the test process plus `RAYITO_MCP_TIMEOUT_SECONDS=900`, `RAYITO_MCP_IDLE_SECONDS=0` and `RAYITO_MCP_LOG_LEVEL=DEBUG`, and SHALL assert: the six tool names; `run_command` `{"cmd": "echo hola"}` → `stdout == "hola\n"`, `exit_code == 0` (printing the first-call seconds as `mcp first call (create) <s>`); `write_file` → `read_file` round trip on `/home/user/mcp.txt` and `list_files("/home/user")` listing it; `run_code` of a matplotlib plot → an `image/png` block whose base64 decodes to bytes starting with the PNG signature; `run_code("1/0")` → JSON `error.name == "ZeroDivisionError"` with `is_error == false`; `list_sandboxes` → exactly one item with `current == true`; and, after leaving the client context, `get_microvm(<that id>).state` is `TERMINATING` or `TERMINATED` within 30 s.

#### Scenario: green run against AWS
- **WHEN** `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e/test_m7_mcp.py -m e2e -v -s` runs with valid credentials
- **THEN** the test passes and `AsyncSandbox.list(template=<arn>)` afterwards contains no `RUNNING` or `SUSPENDED` sandbox created by the run

### Requirement: Documentation page with host configuration
`docs/site/docs/mcp.md` SHALL exist, be listed in `docs/site/mkdocs.yml` as `- Servidor MCP: mcp.md` after `Compatibilidad con E2B`, and contain, in order: what the server is and its cost model; installation (`pip install "rayito[mcp]"`, `uv add "rayito[mcp]"`, and the pre-PyPI `uv run --project <repo>/clients/python --extra mcp rayito-mcp`); the environment-variable table; the tool table with the `run_code` JSON example; a Claude Code `claude mcp add rayito -e RAYITO_TEMPLATE=... -- uvx --from "rayito[mcp]" rayito-mcp` command; a Claude Desktop `claude_desktop_config.json` block under `mcpServers` with `command`, `args` and `env`, plus the config file locations; a Cursor `.cursor/mcp.json` block under `mcpServers`; a VS Code `.vscode/mcp.json` block under `servers` with `"type": "stdio"`; the HTTP mode with its loopback/no-auth warning; how to use the MCP Inspector; and the costs/limits section (one sandbox per process, `timeout` default, leaked VM after a killed host, `list_sandboxes` and `terminate-microvm` to clean up, the 100 000-character truncation, idle suspension mid-cell and `RAYITO_MCP_IDLE_SECONDS=0`). `README.md` SHALL mention the MCP server in one line linking to the page. The strict mkdocs build SHALL pass.

#### Scenario: strict docs build
- **WHEN** `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` runs
- **THEN** it exits 0 and `docs/site/_build/mcp/index.html` exists

#### Scenario: host snippets present
- **WHEN** `docs/site/docs/mcp.md` is read
- **THEN** it contains `claude mcp add`, `claude_desktop_config.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `--http` and `RAYITO_TEMPLATE`
