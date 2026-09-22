## MODIFIED Requirements

### Requirement: Transports and command line
`python -m rayito.mcp` and the console script `rayito-mcp` SHALL start the server on **stdio** by default and SHALL write nothing to stdout except protocol messages (all logging goes to stderr through the root logger that `MCPServer(log_level=)` configures; `print` is forbidden under `src/rayito` by ruff rule `T20`). With `--http` they SHALL serve **streamable HTTP** via `MCPServer.run(transport="streamable-http", host=<--host>, port=<--port>, transport_security=<settings>)` with defaults `127.0.0.1` and `8000` (endpoint `http://<host>:<port>/mcp`, every other transport option left at the SDK default). The settings SHALL always be built by Rayito and never left to the SDK's default, because `mcp` auto-enables its DNS-rebinding protection only when the host string is literally `127.0.0.1`, `localhost` or `::1` and silently disables `Host`/`Origin` validation for every other spelling, including the rest of 127.0.0.0/8: they SHALL be `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[<authority>], allowed_origins=["http://" + <authority>])`, where `<authority>` is `host:port` with an IPv6 literal bracketed (`[::1]:8000`) so it matches what a client actually sends. A client therefore SHALL use the same host spelling the server was started with, and `--host` SHALL be the address clients actually reach — a wildcard bind address (`0.0.0.0`, `::`, any unspecified address) SHALL be a usage error (exit 2) whose message says to pass the concrete address, because that spelling never appears in a client's `Host` header and the middleware would answer 421 to every request. `--host` or `--port` without `--http` SHALL be a usage error (exit 2). HTTP mode SHALL have no authentication; when `--host` is not a loopback address the server SHALL log a `WARNING` stating that any client reaching the address controls the sandbox, and continue. A malformed environment (see "Configuration through the environment") SHALL make `main` print the Spanish message to stderr and return 2 before any server starts.

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

### Requirement: Documentation page with host configuration
`docs/site/docs/mcp.md` SHALL exist, be listed in `docs/site/mkdocs.yml` as `- Servidor MCP: mcp.md` after `Compatibilidad con E2B`, and contain, in order: what the server is and its cost model; installation (`pip install "rayito[mcp]"`, `uv add "rayito[mcp]"`, and the pre-PyPI `uv run --project <repo>/clients/python --extra mcp rayito-mcp`); the environment-variable table; the tool table with the `run_code` JSON example; a Claude Code `claude mcp add rayito -e RAYITO_TEMPLATE=... -- uvx --from "rayito[mcp]" rayito-mcp` command; a Claude Desktop `claude_desktop_config.json` block under `mcpServers` with `command`, `args` and `env`, plus the config file locations; a Cursor `.cursor/mcp.json` block under `mcpServers`; a VS Code `.vscode/mcp.json` block under `servers` with `"type": "stdio"`; the HTTP mode with its no-authentication warning; how to use the MCP Inspector; and the costs/limits section (one sandbox per process, `timeout` default, leaked VM after a killed host, `list_sandboxes` and `terminate-microvm` to clean up, the 100 000-character truncation, idle suspension mid-cell and `RAYITO_MCP_IDLE_SECONDS=0`). The HTTP warning SHALL NOT claim that listening on loopback keeps other parties out: it SHALL say that a web page can reach a loopback server through DNS rebinding, that the server therefore validates `Host` and `Origin` against the exact `host:port` it was started with, that clients must use that same spelling, and that a wildcard `--host` is refused with a usage error. `README.md` SHALL mention the MCP server in one line linking to the page. The strict mkdocs build SHALL pass.

#### Scenario: strict docs build
- **WHEN** `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` runs
- **THEN** it exits 0 and `docs/site/_build/mcp/index.html` exists

#### Scenario: host snippets present
- **WHEN** `docs/site/docs/mcp.md` is read
- **THEN** it contains `claude mcp add`, `claude_desktop_config.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `--http` and `RAYITO_TEMPLATE`

#### Scenario: the HTTP warning names DNS rebinding
- **WHEN** the HTTP admonition of `docs/site/docs/mcp.md` is read
- **THEN** it says a web page can reach a loopback server through DNS rebinding, that the server validates `Host` and `Origin` against the `host:port` it was started with, that the client must use that same spelling and that a wildcard `--host` is refused — and it no longer presents loopback itself as the protection
