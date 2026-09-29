## MODIFIED Requirements

### Requirement: MCP server package behind the optional extra
The Python distribution `rayito` SHALL ship the subpackage `rayito.mcp`, enabled by the optional extra `mcp` whose only requirement is `mcp>=2.2,<3` (the official Model Context Protocol Python SDK, v2 line). `import rayito` SHALL NOT import `mcp`. `import rayito.mcp` itself SHALL succeed even without the extra: the package SHALL resolve its four public names lazily through module `__getattr__` (PEP 562) instead of importing them eagerly, so that importing `rayito.mcp` as a parent package (which `python -m rayito.mcp` and the console script `rayito-mcp` both do before their own code runs) never fails on its own. Only accessing one of `rayito.mcp.McpSettings`, `rayito.mcp.SandboxLease`, `rayito.mcp.build_server(settings, *, control_plane=None, transport=None)` (returning an `mcp.server.MCPServer` named `rayito` with `version` equal to `rayito.__version__`) or `rayito.mcp.main(argv=None) -> int` in an environment without the extra SHALL raise `ModuleNotFoundError` whose message names `pip install "rayito[mcp]"`.

#### Scenario: extra missing
- **WHEN** `python -c "import rayito.mcp; rayito.mcp.build_server"` runs in an environment with `rayito` but without the `mcp` extra
- **THEN** it exits non-zero with a `ModuleNotFoundError` whose message contains `rayito[mcp]`, and `python -c "import rayito"` in the same environment exits 0

#### Scenario: bare package import survives without the extra
- **WHEN** `python -c "import rayito.mcp"` runs in an environment with `rayito` but without the `mcp` extra
- **THEN** it exits 0; only accessing one of the package's four names raises

#### Scenario: server identity
- **WHEN** an MCP client connects to `build_server(McpSettings(...))` in memory and reads `server_info`
- **THEN** `name` is `rayito` and `version` equals `rayito.__version__`

### Requirement: Transports and command line
`python -m rayito.mcp` and the console script `rayito-mcp` SHALL start the server on **stdio** by default and SHALL write nothing to stdout except protocol messages (all logging goes to stderr through the root logger that `MCPServer(log_level=)` configures; `print` is forbidden under `src/rayito` by ruff rule `T20`, except in `rayito.mcp.__main__`'s own extra-missing message, mirroring `rayito.cli.__main__`). With `--http` they SHALL serve **streamable HTTP** via `MCPServer.run(transport="streamable-http", host=<--host>, port=<--port>, transport_security=<settings>)` with defaults `127.0.0.1` and `8000` (endpoint `http://<host>:<port>/mcp`, every other transport option left at the SDK default). The settings SHALL always be built by Rayito and never left to the SDK's default, because `mcp` auto-enables its DNS-rebinding protection only when the host string is literally `127.0.0.1`, `localhost` or `::1` and silently disables `Host`/`Origin` validation for every other spelling, including the rest of 127.0.0.0/8: they SHALL be `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[<authority>], allowed_origins=["http://" + <authority>])`, where `<authority>` is `host:port` with an IPv6 literal bracketed (`[::1]:8000`) so it matches what a client actually sends. A client therefore SHALL use the same host spelling the server was started with, and `--host` SHALL be the address clients actually reach — a wildcard bind address (`0.0.0.0`, `::`, any unspecified address) SHALL be a usage error (exit 2) whose message says to pass the concrete address, because that spelling never appears in a client's `Host` header and the middleware would answer 421 to every request. `--host` or `--port` without `--http` SHALL be a usage error (exit 2). HTTP mode SHALL have no authentication; when `--host` is not a loopback address the server SHALL log a `WARNING` stating that any client reaching the address controls the sandbox, and continue. A malformed environment (see "Configuration through the environment") SHALL make `main` print the Spanish message to stderr and return 2 before any server starts. Without the `mcp` extra installed, both entry points SHALL print a single line to stderr naming the install command (`uv pip install "rayito[mcp]"` or `pip install "rayito[mcp]"`) and return 2, with no traceback — never the raw `ModuleNotFoundError` that a missing extra produced before this requirement, and never anything on stdout.

#### Scenario: stdio by default
- **WHEN** `parse_args([])` is evaluated and `run(server, options, runner=recorder)` is called
- **THEN** `options.http` is `False` and the recorder was invoked once with no arguments

#### Scenario: streamable HTTP
- **WHEN** `parse_args(["--http", "--host", "192.168.1.5", "--port", "9000"])` is evaluated and `run(server, options, runner=recorder)` is called
- **THEN** the recorder was invoked once with `transport="streamable-http", host="192.168.1.5", port=9000` and a `transport_security` whose `allowed_hosts` is `["192.168.1.5:9000"]`, and a `WARNING` about the missing authentication was logged

#### Scenario: a wildcard --host is a usage error
- **WHEN** `parse_args(["--http", "--host", "0.0.0.0"])` or `parse_args(["--http", "--host", "::"])` is evaluated
- **THEN** each raises `SystemExit(2)` and stderr explains that `--host` must be the concrete address clients put in their `Host` header

#### Scenario: an exotic loopback address is protected too
- **WHEN** `run()` is called for `--host 127.0.0.2 --port 8000` and for `--host ::1 --port 8000`
- **THEN** both invocations carry `enable_dns_rebinding_protection=True` with `allowed_hosts` `["127.0.0.2:8000"]` and `["[::1]:8000"]` and the matching `http://…` origins, so a page that resolves its own domain to that address is rejected on the `Host` header

#### Scenario: transport flags without --http
- **WHEN** `parse_args(["--port", "1"])` is evaluated
- **THEN** it raises `SystemExit(2)`

#### Scenario: HTTP round trip
- **WHEN** `build_server(...).streamable_http_app()` is served by uvicorn on a free loopback port and `Client("http://127.0.0.1:<port>/mcp")` lists tools and calls `run_command` with `{"cmd": "echo http"}` against the fake `rayd`
- **THEN** six tools are listed, the call returns `stdout == "http\n"` and `exit_code == 0`, and after the uvicorn server is told to exit `terminate_microvm` has been called once

#### Scenario: friendly exit without the extra, for both entry points
- **WHEN** `mcp` is not installed and, separately, `rayito.mcp.__main__.main()` is called directly and `python -m rayito.mcp` runs in a fresh interpreter
- **THEN** both print exactly one line to stderr containing `rayito[mcp]`, write nothing to stdout, contain no `Traceback` in stderr, and exit with status 2
