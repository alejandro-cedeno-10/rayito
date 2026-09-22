## Why

Every agent host that matters today (Claude Code, Claude Desktop, Cursor,
VS Code, Strands, LangChain, the Vercel AI SDK) can drive a Model Context
Protocol server, and E2B's own MCP server is how most people first try a
code sandbox from an agent. Rayito has a complete Python SDK (M1–M6, accepted
on real AWS) but no way to use it from any of those hosts without writing
glue. `docs/research/2026-09-m7-oss-readiness.md` §3(f) calls an MCP server
"the cheapest high-leverage integration" because one server plugs into every
host at once, and §5 lists `m7-mcp-server` as item 5 of M7 (size S).

The official Python SDK is `mcp` on PyPI, verified on 2026-09-16: latest
**2.2.0** (released 2026-09-07, `Requires-Python >=3.10`, the v2 line that
implements the 2026-07-28 revision of the protocol; 1.30.0 is the last 1.x).
Its server class is `MCPServer` (`from mcp.server import MCPServer`), tools
are plain typed functions under `@mcp.tool()`, and the same `Client` object
connects over stdio, streamable HTTP or in memory, which is what makes an
honest unit suite and a scripted e2e possible without the Inspector GUI.

## What Changes

Track 5 of M7, decided in full by `design.md`:

- **`rayito.mcp` subpackage** inside the existing `rayito` wheel, enabled by
  the optional extra `rayito[mcp]` (`mcp>=2.2,<3`), started with
  `python -m rayito.mcp` (stdio, the default) or `python -m rayito.mcp --http`
  (streamable HTTP on `127.0.0.1:8000/mcp`), plus a console script
  `rayito-mcp` for host configs. Importing `rayito.mcp` without the extra
  fails with a message naming `pip install "rayito[mcp]"`.
- **Six tools**: `run_code` (Python in the stateful kernel; text, stdout,
  stderr and the kernel error as a JSON text block, PNG/JPEG results as
  base64 `ImageContent` blocks, SVG as an embedded resource), `run_command`
  (`stdout`, `stderr`, `exit_code` as structured output; a non-zero exit is
  data, not an error), `read_file`, `write_file`, `list_files` and
  `list_sandboxes`.
- **One sandbox per server process**, created lazily on the first tool call
  that needs it, auto-suspended by AWS's `idlePolicy` after
  `RAYITO_MCP_IDLE_SECONDS` (default 300) of no traffic and auto-resumed by
  the SDK on the next call, and killed (`terminate-microvm`) when the server
  stops (stdin EOF on stdio, SIGINT/shutdown on HTTP). The process is the
  session: on the 2026-07-28 protocol streamable HTTP is sessionless, so a
  finer unit of ownership is not expressible.
- **Configuration only through the environment**: `RAYITO_TEMPLATE`
  (required, same variable the SDK already reads), `RAYITO_TEMPLATE_VERSION`,
  `RAYITO_EXECUTION_ROLE_ARN`, `AWS_REGION`/`AWS_PROFILE` (boto3),
  `RAYITO_MCP_TIMEOUT_SECONDS`, `RAYITO_MCP_IDLE_SECONDS`,
  `RAYITO_MCP_LOG_LEVEL`.
- **Tests**: unit tests against the fake `rayd` and the Stubber control plane
  through the in-memory `Client(server)` and through a real streamable HTTP
  round trip on an ephemeral port; the acceptance e2e
  `clients/python/tests/e2e/test_m7_mcp.py` spawns `python -m rayito.mcp` as a
  subprocess over stdio with the SDK's `Client(StdioServerParameters(...))`
  (what the MCP Inspector does, scripted) against a real MicroVM, guarded by
  the existing `RAYITO_E2E=1` + `RAYITO_TEMPLATE` gate.
- **Docs**: `docs/site/docs/mcp.md` (nav entry "Servidor MCP") with the
  environment table, the tool table and copy-paste config for Claude Code,
  Claude Desktop, Cursor and VS Code, plus the HTTP mode and the Inspector.
- **Framework adapters as examples, not packages**:
  `docs/examples/langchain_tool.py` (LangChain `@tool` over the sync SDK) and
  `docs/examples/vercel_ai_tool.ts` (AI SDK `tool()` over the TypeScript SDK),
  fifty lines each.
- **Bookkeeping**: `pyproject.toml` extra + script + dev group, `uv.lock`,
  `scripts/check_wheel.py` asserts the subpackage and the entry point, unit
  test in `test_packaging.py`, `CHANGELOG.md` (Python), `README.md` one-liner,
  `MILESTONES.md` row 5.

Nothing changes in `rayd`, the kernel sidecar, the image, the `.proto` or the
TypeScript SDK.

## Capabilities

### New Capabilities

- `mcp-server`: the `rayito.mcp` server — package and extra, transports and
  CLI, the six tools and their result shapes, the sandbox lease lifecycle,
  environment configuration, logging hygiene, unit and e2e coverage, and the
  docs page with host configuration.
- `framework-adapters`: the two example adapters under `docs/examples/`
  (LangChain, Vercel AI SDK), their size limit and how they are checked.

### Modified Capabilities

- `python-release`: gains a requirement for the optional extra `mcp`, the
  `rayito-mcp` console script, the `dev` dependency group including
  `rayito[mcp]` and the corresponding wheel assertions. Written as an
  **added** requirement so it does not collide with the requirement texts
  that `m7-oss-hygiene` and `m7-supply-chain` (both still open) modify in the
  same spec.

## Impact

- `clients/python/pyproject.toml`, `clients/python/uv.lock`,
  `clients/python/src/rayito/mcp/` (new), `clients/python/tests/unit/test_mcp_*.py`
  (new), `clients/python/tests/e2e/test_m7_mcp.py` (new),
  `clients/python/tests/unit/test_packaging.py`, `clients/python/CHANGELOG.md`.
- `scripts/check_wheel.py`.
- `docs/site/docs/mcp.md` (new), `docs/site/mkdocs.yml` (nav),
  `docs/examples/langchain_tool.py` (new), `docs/examples/vercel_ai_tool.ts`
  (new), `README.md`, `MILESTONES.md`.
- New runtime dependency **only under the extra**: `mcp` 2.x and what it
  pulls (`pydantic`, `starlette`, `uvicorn`, `sse-starlette`, `httpx2`,
  `anyio`, `jsonschema`, `pyjwt`, `opentelemetry-api`). The base wheel's
  three runtime dependencies are unchanged. `uv run` on the dev box installs
  the extra through the `dev` group, so every existing gate command keeps
  working as written.
- AWS: the server creates one MicroVM per process with the SDK's defaults
  (`ALL_INGRESS`, egress inherited from the image version, no execution role
  unless `RAYITO_EXECUTION_ROLE_ARN`). An orphaned process still bills until
  `RAYITO_MCP_TIMEOUT_SECONDS` (default 3600 s); the docs say so.
- Gates unchanged in name: `cargo` set untouched; Python `uv run pytest
  tests/unit`, `ruff check`, `ruff format --check`, `mypy src tests`, wheel
  build + `check_wheel.py` + `twine check`; mkdocs `--strict`; `make test-e2e`
  now also collects `test_m7_mcp.py`.
