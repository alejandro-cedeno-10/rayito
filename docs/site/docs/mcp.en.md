# MCP server

`rayito.mcp` (Python) and the `rayito-mcp` executable of the npm package
(TypeScript) are a [Model Context Protocol](https://modelcontextprotocol.io)
server with the same six tools, the same environment variables and the same
behavior (the tools' contract is in `testdata/mcp/tools.json`, and both test
suites check it). It gives any MCP host (Claude Code, Claude Desktop, Cursor, VS
Code, the Inspector) a Rayito sandbox with one start command and one
environment variable. Each server **process** owns **one** sandbox: it is
created on the first tool that needs it, AWS suspends it when
`RAYITO_MCP_IDLE_SECONDS` pass without traffic (the next call resumes it
on its own) and it is destroyed (`terminate-microvm`) when the host closes
the server. The cost is that of a running MicroVM while it is in use and
that of a snapshot per suspension cycle ([cost model](cost.md)); nothing
stays alive beyond `RAYITO_MCP_TIMEOUT_SECONDS`.

For frameworks that talk to their own tools and not to MCP (LangChain,
Vercel AI SDK) there are two fifty-line adapters:
[LangChain and Vercel AI](guias/langchain-y-vercel-ai.md).

This page is the reverse case of
[Agent in the sandbox](guias/agente-en-el-sandbox.md): here the agent lives
outside (your MCP host) and uses the sandbox as a tool. To give MCP servers
to an agent that runs **inside** the sandbox (`sbx.agent`), use
`AgentSpec(mcp={"name": McpLocal(...) | McpRemote(...)})`
([Subagents, MCP servers and permissions](guias/agente-en-el-sandbox.md#subagentes-servidores-mcp-y-permisos)).

## Installation { #instalacion }

=== "Python"

    ```bash
    pip install "rayito[mcp]"      # or: uv add "rayito[mcp]"
    python -m rayito.mcp --help     # equivalent to the rayito-mcp script
    ```

    The extra installs the official `mcp` 2.x SDK (`pip install "rayito[mcp]"`,
    the package is on PyPI). From a repository checkout:

    ```bash
    uv run --project <repo>/clients/python --extra mcp rayito-mcp
    ```

    Without the extra, `rayito-mcp` and `python -m rayito.mcp` print a
    one-line notice with the install command and exit with 2 (no traceback),
    just like the `rayito` CLI without `rayito[cli]`; `import rayito` does not
    change. Accessing a `rayito.mcp` name directly (`McpSettings`,
    `build_server`, …) without the extra fails with a `ModuleNotFoundError`
    that gives the same command.

=== "TypeScript"

    The `rayito-mcp` executable ships in the `rayito` package. The official MCP
    SDK (`@modelcontextprotocol/server`) and `zod` are **optional peers**: they
    are not installed with `rayito`, just like Python's `mcp` extra. Without
    installing anything:

    ```bash
    npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp --help
    ```

    Or in a project:

    ```bash
    npm install rayito @modelcontextprotocol/server zod
    npx rayito-mcp --help
    ```

    If either peer is missing, `rayito-mcp` prints the exact `npx` command above
    and exits with 2, with no traceback; `import "rayito"` does not change.

## Environment variables { #variables-de-entorno }

All configuration goes through the environment (MCP hosts configure servers
with an `env` block); no command-line flag duplicates it.

| Variable | Meaning | Default / validation |
|---|---|---|
| `RAYITO_TEMPLATE` | Image name or ARN (the same variable `Sandbox.create` reads) | Required for the tools that touch the sandbox; not validated at startup, the first call that needs it returns an error that names it |
| `RAYITO_TEMPLATE_VERSION` | Image version | Latest `ACTIVE` |
| `RAYITO_EXECUTION_ROLE_ARN` | The MicroVM's execution role; with a role, `logging="cloudwatch"` | Without a role → `logging="disabled"` |
| `AWS_REGION` / `AWS_PROFILE` and the credential variables | The SDK's region and credentials (`AWS_REGION`; if missing, `AWS_DEFAULT_REGION` or the profile); the server passes no region or session of its own | boto3 defaults |
| `RAYITO_MCP_TIMEOUT_SECONDS` | Maximum lifetime of the sandbox (running + suspended) | `3600`; integer in `60..=28800`, otherwise the server exits with code 2 |
| `RAYITO_MCP_IDLE_SECONDS` | Seconds without traffic before AWS suspends the sandbox; `0` turns auto-suspend off | `300`; `0` or an integer `>= 60` and less than `RAYITO_MCP_TIMEOUT_SECONDS` (with a timeout below 300 s you have to lower the idle or set it to `0`), otherwise the server exits with code 2 |
| `RAYITO_MCP_LOG_LEVEL` | Log level (always on stderr) | `INFO`; one of `DEBUG INFO WARNING ERROR CRITICAL` |

## Tools { #herramientas }

Tool and argument names are in English and stable; descriptions are in
Spanish. Text outputs are cut at 100,000 characters with the suffix
`… [truncado: <n> caracteres más]` (that is, "truncated: n more
characters") and `truncated: true`.

| Tool | Arguments | Returns |
|---|---|---|
| `run_code` | `code: str` (Python), `timeout: int = 300` (1-3600 s) | Content blocks: a JSON text (below), then one image per `png`/`jpeg` and one embedded resource per `svg` |
| `run_command` | `cmd: str` (`/bin/sh -c` as the sandbox user), `timeout: int = 60`, `cwd: str \| None` | `{stdout, stderr, exit_code, truncated}`; a non-zero exit code is data, not an error |
| `read_file` | `path: str` | `{path, content, size, truncated}` (UTF-8 text) |
| `write_file` | `path: str`, `content: str` | `{path, size}`; overwrites |
| `list_files` | `path: str = "/home/user"`, `depth: int = 1` (1-5) | `{path, entries: [{name, path, type, size, modified_time}]}` |
| `list_sandboxes` | — | `{sandboxes: [{sandbox_id, state, template, template_version, started_at, current}]}`; never creates a sandbox |

The first block of `run_code` is always a JSON with this shape:

```json
{
  "text": "42",
  "stdout": "hello\n",
  "stderr": "",
  "error": null,
  "execution_count": 3,
  "results": [{"index": 0, "mime_types": ["text/plain"]}],
  "truncated": false
}
```

`text` is the `text/plain` of the main result (`null` if there is none);
`error` is `{"name", "value", "traceback"}` when the cell raised an
exception (or `ExecutionTimeout` when the agent interrupted it) and arrives
with `is_error: false`: the cell ran and the model needs the traceback.
`results` lists the mime types of each result even when they are not
attached (`text/html`, `e2b/chart`...); only PNG, JPEG and SVG travel as
blocks.

Failures the model can fix arrive as a tool error (`is_error: true`) with a
message in Spanish: `RAYITO_TEMPLATE` undefined, a failed `create` (`no se
pudo crear el sandbox: ...`; the next call retries), a `run_command`
timeout, a file that does not exist, a sandbox in the middle of suspending
or resuming (`está en transición ...; reintenta en unos segundos`: the
sandbox is still the same one) and a sandbox that expired (`ya no existe
...; la siguiente llamada crea uno nuevo`).

## Claude Code { #claude-code }

=== "Python"

    ```bash
    claude mcp add rayito \
      -e RAYITO_TEMPLATE=rayito-base -e AWS_REGION=us-east-1 -e AWS_PROFILE=<your-profile> \
      -- uvx --from "rayito[mcp]" rayito-mcp
    ```

    From a checkout, replace the command with
    `uv run --project <repo>/clients/python --extra mcp rayito-mcp`.

=== "TypeScript"

    ```bash
    claude mcp add rayito \
      -e RAYITO_TEMPLATE=rayito-base -e AWS_REGION=us-east-1 -e AWS_PROFILE=<your-profile> \
      -- npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp
    ```

## Claude Desktop { #claude-desktop }

`claude_desktop_config.json` (`~/Library/Application Support/Claude/` on
macOS, `%APPDATA%\Claude\` on Windows):

=== "Python"

    ```json
    {
      "mcpServers": {
        "rayito": {
          "command": "uvx",
          "args": ["--from", "rayito[mcp]", "rayito-mcp"],
          "env": {
            "RAYITO_TEMPLATE": "rayito-base",
            "AWS_REGION": "us-east-1",
            "AWS_PROFILE": "<your-profile>"
          }
        }
      }
    }
    ```

=== "TypeScript"

    ```json
    {
      "mcpServers": {
        "rayito": {
          "command": "npx",
          "args": ["-y", "-p", "rayito", "-p", "@modelcontextprotocol/server", "-p", "zod", "rayito-mcp"],
          "env": {
            "RAYITO_TEMPLATE": "rayito-base",
            "AWS_REGION": "us-east-1",
            "AWS_PROFILE": "<your-profile>"
          }
        }
      }
    }
    ```

Claude Desktop only rereads the file at startup: quit it completely (not
just the window) and open it again. The sandbox is destroyed when Claude
Desktop closes the server.

## Cursor and VS Code { #cursor-y-vs-code }

Cursor, `.cursor/mcp.json`:

=== "Python"

    ```json
    {
      "mcpServers": {
        "rayito": {
          "command": "uvx",
          "args": ["--from", "rayito[mcp]", "rayito-mcp"],
          "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<your-profile>"}
        }
      }
    }
    ```

=== "TypeScript"

    ```json
    {
      "mcpServers": {
        "rayito": {
          "command": "npx",
          "args": ["-y", "-p", "rayito", "-p", "@modelcontextprotocol/server", "-p", "zod", "rayito-mcp"],
          "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<your-profile>"}
        }
      }
    }
    ```

VS Code, `.vscode/mcp.json`:

=== "Python"

    ```json
    {
      "servers": {
        "rayito": {
          "type": "stdio",
          "command": "uvx",
          "args": ["--from", "rayito[mcp]", "rayito-mcp"],
          "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<your-profile>"}
        }
      }
    }
    ```

=== "TypeScript"

    ```json
    {
      "servers": {
        "rayito": {
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "-p", "rayito", "-p", "@modelcontextprotocol/server", "-p", "zod", "rayito-mcp"],
          "env": {"RAYITO_TEMPLATE": "rayito-base", "AWS_REGION": "us-east-1", "AWS_PROFILE": "<your-profile>"}
        }
      }
    }
    ```

## HTTP mode { #modo-http }

=== "Python"

    ```bash
    python -m rayito.mcp --http                    # http://127.0.0.1:8000/mcp
    python -m rayito.mcp --http --host 127.0.0.1 --port 8000
    ```

=== "TypeScript"

    ```bash
    npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp --http   # http://127.0.0.1:8000/mcp
    npx rayito-mcp --http --host 127.0.0.1 --port 8000
    ```

    It serves the official SDK's HTTP transport over `node:http`, with no
    additional dependencies.

It serves streamable HTTP for hosts and tools that only speak HTTP (the
Inspector in URL mode, Cursor's or Claude Code's remote entries):

```bash
claude mcp add --transport http rayito-http http://127.0.0.1:8000/mcp
```

!!! warning "No authentication"
    HTTP mode authenticates no one. Listening on loopback does **not**
    prevent a browser from reaching the server: a web page can resolve its
    own domain to 127.0.0.x and talk to it (DNS rebinding), so the server
    always requires `Host` and `Origin` to be exactly the `host:port` it
    started with: connect with the same spelling you passed in `--host`
    (`127.0.0.1:8000`, not `localhost:8000`). All clients that reach the
    process share **the same sandbox**. That is why `--host` has to be the
    specific address clients arrive at: a wildcard (`0.0.0.0`, `::`) binds
    all interfaces but is not what anyone types in `Host`, so it is rejected
    with a usage error (exit 2). With a `--host` that is not loopback (a
    specific IP on your network) the server warns (`sin autenticación:
    cualquier cliente que alcance <host>:<port> controla el sandbox`, that
    is, "no authentication: any client that reaches <host>:<port> controls
    the sandbox") and continues; it is for your own machine, not for
    exposing it.

## Inspector { #inspector }

=== "Python"

    ```bash
    RAYITO_TEMPLATE=rayito-base AWS_REGION=us-east-1 AWS_PROFILE=<your-profile> \
      pnpm dlx @modelcontextprotocol/inspector \
      uv run --project <repo>/clients/python --extra mcp rayito-mcp
    ```

=== "TypeScript"

    ```bash
    RAYITO_TEMPLATE=rayito-base AWS_REGION=us-east-1 AWS_PROFILE=<your-profile> \
      pnpm dlx @modelcontextprotocol/inspector \
      npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp
    ```

The six tools appear in the *Tools* tab; `run_command` with `echo hello`
creates the sandbox (the first call takes as long as the MicroVM startup
takes) and `run_code` with
`import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()` returns
the JSON and the rendered PNG. When you close the Inspector the sandbox is
destroyed. In URL mode, start `python -m rayito.mcp --http` (or `rayito-mcp --http`) and connect the
Inspector to `http://127.0.0.1:8000/mcp`.

## Costs and limits { #costes-y-limites }

- **One sandbox per process.** Over stdio each host connection is a
  process, so "one sandbox per session" holds exactly; over HTTP all the
  clients of a process share the sandbox.
- **Default `timeout` 3600 s.** If the host kills the process without
  closing it (`SIGKILL`, forced close), the shutdown's `terminate-microvm`
  does not run and the MicroVM keeps billing until
  `RAYITO_MCP_TIMEOUT_SECONDS`. To find and kill it: `list_sandboxes` from
  any server with the same image, `Sandbox.kill(sandbox_id)` (`Sandbox.kill(sandboxId)` in TypeScript), or
  `aws lambda-microvms terminate-microvm --microvm-identifier <id>`.
- **Outputs truncated** at 100,000 characters (`stdout`, `stderr`,
  `content`, `text`), with `truncated: true`; images travel as the sidecar
  delivered them (which discards values over 8 MiB).
- **Suspension mid-cell.** Inactivity counts bytes that cross the endpoint:
  a cell that prints nothing for `RAYITO_MCP_IDLE_SECONDS` can be suspended
  in the middle of running; the SDK waits for the resume and reattaches,
  but if your cells are long and silent raise the value or turn it off with
  `RAYITO_MCP_IDLE_SECONDS=0`.
- **No authentication on HTTP**, no per-client sandboxes, no pools or
  persistence: each process starts empty and ends by destroying its
  sandbox.

## See also { #ver-tambien }

- [Agent in the sandbox](guias/agente-en-el-sandbox.md): the agent loop
  running inside the sandbox, instead of on your MCP host
- [LangChain and Vercel AI](guias/langchain-y-vercel-ai.md)
