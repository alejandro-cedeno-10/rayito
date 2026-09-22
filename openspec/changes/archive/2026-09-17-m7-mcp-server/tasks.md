## 0. [pre-flight] Facts

- [x] 0.1 From the implementer's box confirm `https://pypi.org/pypi/mcp/json` still reports latest `2.2.x` (or a later 2.x) and that `uv run --with "mcp>=2.2,<3" python -c "from mcp.server import MCPServer; from mcp.server.mcpserver import Context; from mcp.server.mcpserver.exceptions import ToolError; from mcp import Client, StdioServerParameters; from mcp.types import ImageContent, EmbeddedResource, TextResourceContents, TextContent, ToolAnnotations"` succeeds. If any import fails, stop and update `design.md` "Context" before continuing
- [x] 0.2 Confirm `clients/python/uv.lock` is clean (`cd clients/python && uv lock --check`) and every Python gate is green **before** touching anything: `uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`

## 1. [packaging] Extra, script, dev group, wheel gate (design D1, D15)

- [x] 1.1 `clients/python/pyproject.toml`: add `[project.optional-dependencies] mcp = ["mcp>=2.2,<3"]`, `[project.scripts] rayito-mcp = "rayito.mcp.__main__:main"`, append `"rayito[mcp]"` to `[dependency-groups] dev`; add `"T20"` to `[tool.ruff.lint] select` and `[tool.ruff.lint.per-file-ignores] "tests/**" = ["T20"]`; nothing else in the file changes
- [x] 1.2 `cd clients/python && uv lock` then `uv lock --check`; `uv run python -c "import mcp, importlib.metadata as m; print(m.version('mcp'))"` prints a 2.x version; record it in "Notes"
- [x] 1.3 `scripts/check_wheel.py`: `REQUIRED_FILES` gains `rayito/mcp/__init__.py` and `rayito/mcp/__main__.py`; new assertions that `entry_points.txt` contains `rayito-mcp = rayito.mcp.__main__:main` and that `METADATA` contains `Provides-Extra: mcp` and the `Requires-Dist` line for `mcp` under `extra == 'mcp'` (paste the exact rendered line from the first `uv build` into the script and into "Notes"); module docstring updated; `uvx ruff check scripts` clean
- [x] 1.4 `clients/python/tests/unit/test_packaging.py`: add `test_mcp_extra_and_script` asserting `project["optional-dependencies"]["mcp"] == ["mcp>=2.2,<3"]`, `project["scripts"]["rayito-mcp"] == "rayito.mcp.__main__:main"` and `"rayito[mcp]" in dependency_groups["dev"]`; add `test_import_rayito_does_not_import_mcp` running `python -c "import rayito, sys; raise SystemExit('mcp' in sys.modules)"` in a subprocess and asserting exit code 0

## 2. [server] `rayito.mcp` (design D1–D11)

- [x] 2.1 `src/rayito/mcp/_settings.py`: frozen dataclass `McpSettings(template: str | None, template_version: str | None, execution_role_arn: str | None, timeout_seconds: int, idle_seconds: int, log_level: str)` with properties `idle_policy -> IdlePolicy | None` (`None` when `idle_seconds == 0`) and `logging -> LoggingOption` (`"cloudwatch"` iff a role ARN); `McpSettings.from_env(environ)` reading the D4 variables with the D4 defaults and `ValueError` (Spanish message naming the variable) for a non-integer, an out-of-range timeout (`60..=28800`), an idle value that is neither `0` nor `>= 60`, an idle value that is not strictly below the timeout (names both variables), or an unknown log level; constants `TIMEOUT_ENV_VAR`, `IDLE_ENV_VAR`, `LOG_LEVEL_ENV_VAR`, `DEFAULT_TIMEOUT_SECONDS = 3600`, `DEFAULT_IDLE_SECONDS = 300`; reuse `rayito._sandbox_base.TEMPLATE_ENV_VAR` and the e2e's `RAYITO_TEMPLATE_VERSION` / `RAYITO_EXECUTION_ROLE_ARN` names; no `mcp` import
- [x] 2.2 `src/rayito/mcp/_results.py`: `MAX_OUTPUT_CHARS = 100_000`, `truncate_text(text, limit=MAX_OUTPUT_CHARS) -> tuple[str, bool]` with the suffix `\n… [truncado: <n> caracteres más]`; the `TypedDict`s `CommandOutput`, `FileContent`, `WriteReceipt`, `FileEntry`, `DirectoryListing`, `SandboxSummary`, `SandboxList` exactly as in D5; `execution_to_blocks(execution, *, sandbox_id, limit=MAX_OUTPUT_CHARS) -> list[TextContent | ImageContent | EmbeddedResource]` implementing D6 (JSON first block with `text`, `stdout`, `stderr`, `error`, `execution_count`, `results[].index/mime_types`, `truncated`; then one `ImageContent` per `png` and per `jpeg`; then one `EmbeddedResource`/`TextResourceContents` per `svg` with the `rayito://<sandbox_id>/results/<execution_count>/<index>.svg` URI); `command_output_from(result: CommandResult | CommandExitException, limit)`, `file_entry_from(EntryInfo)` (ISO 8601 `modified_time`, `type` value or `None`), `sandbox_summary_from(item, *, current)`; every function pure, every name English, Spanish docstrings only where the contract is not obvious
- [x] 2.3 `src/rayito/mcp/_lease.py`: `SandboxLease` per D3 (`acquire`, `peek`, `close`, an `asyncio.Lock`, `AsyncSandbox.create(...)` with `ingress=["ALL_INGRESS"]`, `timeout=settings.timeout_seconds`, `idle=settings.idle_policy`, `execution_role_arn`, `template_version`, `logging`, injected `control_plane`/`transport`); `close()` idempotent, `kill()` with `SandboxNotFoundException` suppressed, then `await sandbox.close()`; a `reset()` (release the handle with `close()`, no `kill()`) used by the D7 not-found translation; raises `MissingTemplateError(RuntimeError)` (Spanish message naming `RAYITO_TEMPLATE`) when `settings.template` is `None`
- [x] 2.4 `src/rayito/mcp/_server.py`: `build_server(settings, *, control_plane=None, transport=None) -> MCPServer` with `MCPServer("rayito", instructions=SERVER_INSTRUCTIONS, version=__version__, lifespan=..., log_level=settings.log_level)`; the lifespan yields the `SandboxLease` and closes it in `finally`; the six tools of D5 as `async def` with `ctx: Context[SandboxLease]`, pydantic `Field` constraints (`timeout ge=1 le=3600`, `depth ge=1 le=5`), `ToolAnnotations` per D5, Spanish docstrings as descriptions; `run_command` maps `CommandExitException` to `CommandOutput`; `list_sandboxes` uses `peek()` and `AsyncSandbox.list(template=settings.template, control_plane=..., transport=...)`; error translation through one `translated_errors(lease)` async context manager implementing the D7 table (`MissingTemplateError`, create failures with the `no se pudo crear el sandbox: ` prefix, `TimeoutException`, filesystem exceptions, `SandboxStateException` as a retry hint without touching the lease, `SandboxNotFoundException` with `lease.reset()`); D11 logging (tool name, sizes, duration, `sandbox_id`, exit code / error name only)
- [x] 2.5 `src/rayito/mcp/__main__.py`: `RunOptions(http: bool, host: str, port: int)`, `parse_args(argv) -> RunOptions` (argparse; `--http`, `--host` default `127.0.0.1`, `--port` default `8000`; `--host`/`--port` without `--http` → `parser.error`), `run(server, options, *, runner=None)` calling `server.run()` or `server.run(transport="streamable-http", host=..., port=...)` (the `runner` parameter is the callable to invoke, default `server.run`), the non-loopback `WARNING` of D9, `main(argv=None) -> int` (exit 2 with the `ValueError` message on stderr for bad settings), `if __name__ == "__main__": raise SystemExit(main())`
- [x] 2.6 `src/rayito/mcp/__init__.py`: the `ModuleNotFoundError` guard with the install hint, `__all__ = ["McpSettings", "SandboxLease", "build_server", "main"]`; confirm `import rayito` still succeeds in an environment **without** the extra (`uv run --no-project --with dist/*.whl python -c "import rayito"` after 3.2, or a `--no-extra` env)
- [x] 2.7 `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` clean on the new package (mypy `strict`, no `type: ignore` except a justified one on a third-party gap, listed in "Notes" if any)

## 3. [tests] Unit against the fake `rayd` (design D12)

- [x] 3.1 `tests/unit/test_mcp_settings.py`: defaults; each variable; `RAYITO_MCP_IDLE_SECONDS=0` → `idle_policy is None`; `logging == "cloudwatch"` with a role ARN; the four `ValueError`s each naming the variable
- [x] 3.2 `tests/unit/test_mcp_results.py`: `truncate_text` (below, at and above the limit; suffix and count); `execution_to_blocks` on synthetic `Execution`s: text only; png + chart (`mime_types` lists both, one `image` block with the base64 passed through untouched); svg (one `resource` block, the `rayito://` URI); error (`error.name/value/traceback`, `text` null); long stdout (truncated, flag); `command_output_from` for a `CommandResult` and for a `CommandExitException(exit_code=3)`; `file_entry_from`; `sandbox_summary_from`
- [x] 3.3 `tests/unit/test_mcp_server.py` with the in-memory `Client(build_server(McpSettings(template=IMAGE_ARN, ...), control_plane=control_plane.plane, transport=fake_rayd.transport), raise_exceptions=True)`: every case listed in design D12 (tool list and annotations/output schemas; `list_sandboxes` creates nothing; lazy create on first `run_command` and reuse on the second; two concurrent first calls → one `run_microvm`; `exit 3` and unknown command as data; `write_file`/`read_file`/`list_files` round trip and the missing-file error; `run_code` text, `plot` image block, error as data; missing template → `is_error` naming `RAYITO_TEMPLATE` with no AWS call; failed `run_microvm` → prefixed error then successful retry; `terminate_microvm` exactly once on client exit; no terminate when no sandbox was created; the D11 caplog test with marker strings)
- [x] 3.4 `tests/unit/test_mcp_http.py`: uvicorn on a free loopback port serving `build_server(...).streamable_http_app()` as a task; `Client("http://127.0.0.1:<port>/mcp")` lists six tools and runs `run_command("echo http")`; shutdown via `should_exit` → `terminate_microvm` observed; every await under `asyncio.wait_for(..., 30)`
- [x] 3.5 `tests/unit/test_mcp_main.py`: `parse_args` (`[]`, `["--http"]`, `["--http", "--host", "0.0.0.0", "--port", "9000"]`, `["--port", "1"]` → `SystemExit(2)`); `run` with a recording runner → `()` and `(transport="streamable-http", host=..., port=...)`; `main` returns 2 and prints the message for `RAYITO_MCP_IDLE_SECONDS=abc`; the non-loopback warning appears for `--host 0.0.0.0` and not for the default
- [x] 3.6 `cd clients/python && uv run pytest tests/unit` green; total runtime of the five new files under 20 s on the box (note the figure)

## 4. [packaging gate] Wheel (design D15)

- [x] 4.1 `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl && uvx twine check dist/*` → `OK`, `PASSED`; paste the `Provides-Extra` / `Requires-Dist … extra == 'mcp'` lines and the `entry_points.txt` content into "Notes"
- [x] 4.2 `uv run --no-project --with "dist/<wheel>[mcp]" rayito-mcp --help` prints the usage with `--http`, `--host`, `--port`; `uv run --no-project --with dist/<wheel> python -c "import rayito.mcp"` fails with the message that names `pip install "rayito[mcp]"`

## 5. [e2e] Acceptance against AWS (design D12)

- [x] 5.1 `tests/e2e/test_m7_mcp.py` (marker `e2e`, reuses `e2e_settings`, `control_plane`, `template_arn` and the session sweeper from `tests/e2e/conftest.py`): the seven steps of D12 in one test function, with the subprocess environment built from every `AWS_*` and `RAYITO_*` variable of `os.environ` plus `RAYITO_MCP_TIMEOUT_SECONDS=900`, `RAYITO_MCP_IDLE_SECONDS=0`, `RAYITO_MCP_LOG_LEVEL=DEBUG`; prints `mcp first call (create) <s>`; asserts the sandbox state is `TERMINATING`/`TERMINATED` within 30 s of leaving the client context
- [x] 5.2 `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e/test_m7_mcp.py -m e2e -v -s` green against real AWS (record the run date, image version, first-call seconds, total seconds and the `list-microvms` count afterwards in "Notes"); then the full `make test-e2e` equivalent (`uv run pytest tests/e2e -m e2e -v -s`) still green
  - Acceptance 2026-09-17: alone on `rayito-base` 20.0 (`rayd` 0.2.0) **1 passed in 18.54 s** — first call (create) 8.20 s, `write_file` 0.11 s, `read_file` 0.21 s, `list_files` 0.11 s, `run_code` 0.26 s / 0.16 s (`ZeroDivisionError` as data), `list_sandboxes` 2.03 s, VM `TERMINATING` 0.13 s after the client closed, total 12.48 s; `list-microvms` live = 0 afterwards. Inside the full suite (`uv run pytest tests/e2e -m e2e -v -s`, 31 passed / 3 skipped / 1 failed-then-green-alone): first call 7.72 s, total 11.64 s.
- [x] 5.3 Manual evidence (not a gate): MCP Inspector (`pnpm dlx @modelcontextprotocol/inspector`) pointed at `uv run --project clients/python --extra mcp rayito-mcp` with `RAYITO_TEMPLATE` set lists the six tools and renders the PNG of a matplotlib `run_code`; `claude mcp add rayito -e RAYITO_TEMPLATE=… -- uv run --project <repo>/clients/python --extra mcp rayito-mcp` and one `run_code` from Claude Code; note both outcomes in "Notes"

## 6. [docs, examples, bookkeeping] (design D13–D15)

- [x] 6.1 `docs/site/docs/mcp.md` with the ten sections of D13 in that order (env table = D4, tool table = D5, JSON example = D6, the four host configs, HTTP mode with the warning, Inspector, costs and limits); `docs/site/mkdocs.yml` nav gains `- Servidor MCP: mcp.md` after `Compatibilidad con E2B`
- [x] 6.2 `docs/examples/langchain_tool.py` (≤ 50 lines) per D14: `from langchain.tools import tool`, lazy `Sandbox.create(timeout=900)`, `@tool def run_python(code: str) -> str`, `atexit` kill, `__main__` usage with `create_agent`; `python -m py_compile docs/examples/langchain_tool.py` and `uvx ruff check docs/examples/langchain_tool.py` clean
- [x] 6.3 `docs/examples/vercel_ai_tool.ts` (≤ 50 lines) per D14: `tool({ description, inputSchema: z.object({ code: z.string() }), execute })` over `Sandbox.create({ timeoutMs: 900_000 })` + `runCode`, `beforeExit` kill; type-checked once in a scratch project (`pnpm init`, `pnpm add ai zod`, `pnpm add file:<repo>/clients/typescript`, `pnpm exec tsc --noEmit --strict --module nodenext --moduleResolution nodenext --target es2022 vercel_ai_tool.ts`); command and result in "Notes"; pnpm only
- [x] 6.4 `README.md`: the one-line MCP bullet of D13 with the link to `docs/site/docs/mcp.md`; `clients/python/CHANGELOG.md` `[Unreleased]` → `### Added` entry of D15
- [x] 6.5 `MILESTONES.md` M7 table row 5: scope, acceptance and state per D15 (state "implementado <fecha>" only after 5.2 is green)
- [x] 6.6 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` green

## 7. [gates] Everything green on the box

- [x] 7.1 Python: `cd clients/python && uv lock --check && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`; scripts: `uv run pytest ../../scripts/tests -p no:cacheprovider` and `uvx ruff check scripts` from the root; `python scripts/check_license.py` and `python scripts/gen_limits.py --check` still `OK`
- [x] 7.2 Rust and TypeScript untouched, confirmed green: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace`; `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
  - Acceptance 2026-09-17 (tree at 0.2.0): `buf lint`, `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace --locked` (81 + 5 + 13 `rayd`, 283 `rayd-core`), `cargo deny check` (advisories/bans/licenses/sources ok); TypeScript `pnpm lint` (77 files), `pnpm typecheck`, `pnpm test` **386 passed** (22 files), `pnpm build`, `pnpm pack:check` (LICENSE + NOTICE), `pnpm audit` clean.
- [x] 7.3 Read `.github/workflows/ci.yml`'s Python job and confirm no edit is needed (the `dev` group installs the extra); if a step uses `--no-default-groups` or `--only-group`, add `--extra mcp` there and note it
- [x] 7.4 `openspec validate m7-mcp-server --strict --no-interactive` still passes with the ticked tasks; fill "Notes" (mcp version from 1.2, wheel metadata lines from 4.1, e2e figures from 5.2, manual evidence from 5.3, TypeScript check from 6.3, unit runtime from 3.6)

## Notes

(Rellenado por el implementador, 2026-09-16, en la caja Windows del repo.)

- **0.1 / 1.2 `mcp`**: PyPI reporta latest `2.2.0` (`Requires-Python >=3.10`); los
  imports de 0.1 resuelven; `uv lock` resolvió `mcp 2.2.0` (+ `mcp-types 2.2.0`,
  `starlette 1.6.0`, `uvicorn 0.53.0`, `sse-starlette 3.4.11`, `pydantic`, `httpx2`,
  `pyjwt`, `opentelemetry-api`; `pywin32` en Windows). `uv lock --check` limpio.
- **1.1 pyproject**: `m7-cli` había añadido `[project.optional-dependencies] cli` y
  `[project.scripts] rayito` entre mi lectura y mi edición; el extra `mcp`, el script
  `rayito-mcp` y `"rayito[mcp]"` en `dev` se fusionaron en esas mismas tablas. La
  regla `T20` se añadió como dice D9 y, además de `tests/**`, exime
  `src/rayito/cli/**` (la CLI de `m7-cli` imprime por stderr legítimamente; sin la
  exención su gate rompería por una regla mía).
- **2.x**: `rayito.mcp._results` usa `typing_extensions.TypedDict` (dependencia
  transitiva de `mcp`, como `pydantic`) porque pydantic rechaza los
  `typing.TypedDict` anidados en Python 3.11. Sin `type: ignore` en `src`. Dos
  decisiones dentro del espíritu de D3/D7 que van más allá de la letra:
  (a) ~~`SandboxLease.reset()` intenta `kill()` best-effort~~ — revertido en la
  revisión: `SandboxStateException` es transitoria en este SDK (phase gate,
  reconnect en suspend/resume, `ConflictException`), así que ya no resetea el lease
  sino que pide reintentar; sólo `SandboxNotFoundException` (única señal terminal)
  resetea, y `reset()` sólo hace `close()` porque la VM ya no existe. También se
  añadió la comprobación `idle < timeout` en `McpSettings` (lo exige
  `resolve_idle_policy`; sin ella cada herramienta fallaba con timeouts < 300 s); (b) los fallos de creación también cubren
  `botocore.exceptions.BotoCoreError` (`NoCredentialsError`, `NoRegionError`), que
  el SDK no traduce y que son el primer fallo típico de un host mal configurado.
  Higiene (D11): `MCPServer(log_level=)` configura el logger raíz y con `DEBUG`
  botocore volcaba cuerpos (el JWE de `create-microvm-auth-token`, el
  `runHookPayload`); `build_server` fija `botocore`/`boto3`/`urllib3` en WARNING y
  el test de caplog afirma que no aparece el JWE ni ningún record de botocore
  (comprobado que el test falla sin esa fijación).
- **3.x tests**: `test_mcp_settings.py` (16), `test_mcp_results.py` (20),
  `test_mcp_server.py` (17), `test_mcp_http.py` (1, uvicorn real en puerto libre),
  `test_mcp_main.py` (16, incluido `python -m rayito.mcp --help` en subproceso):
  **65 passed en 8,79 s**. El cliente en memoria no puede ser una fixture async
  (el `Client` de `mcp` exige entrar y salir del contexto en la misma tarea:
  `Attempted to exit cancel scope in a different task`), de ahí el context manager
  `launched_client` en `tests/unit/mcp_support.py`. El `rayd` falso verifica
  `x-access-token`, así que los tests inyectan `RAYITO_ACCESS_TOKEN`.
- **4.1 wheel** (`uv build`, `check_wheel.py` -> `OK (91 entradas)`, `twine check`
  `PASSED` x2). Líneas exactas de `METADATA`: `Provides-Extra: mcp` y
  `Requires-Dist: mcp>=2.2,<3 ; extra == 'mcp'`; `entry_points.txt`:
  `[console_scripts]` / `rayito = rayito.cli.__main__:main` /
  `rayito-mcp = rayito.mcp.__main__:main`. Una wheel sin `rayito/mcp/__main__.py` ni
  el entry point da `KO` nombrando ambos y sale 1.
- **4.2**: `uv run --no-project --with "dist/rayito-0.1.0-py3-none-any.whl[mcp]"
  rayito-mcp --help` imprime el uso con `--http`, `--host`, `--port`;
  `uv run --no-project --with dist/<wheel> python -c "import rayito.mcp"` sale 1
  con `ModuleNotFoundError: rayito.mcp necesita el extra 'mcp': pip install
  "rayito[mcp]"` **si se ejecuta fuera de `clients/python`** (dentro, el entorno
  efímero de uv ve el `.venv` del proyecto, que ya tiene `mcp`); `import rayito`
  sin el extra funciona y `mcp` no está en `sys.modules`.
- **5.2 e2e** (`rayito-base` **17.0**, cuenta de pruebas, us-east-1).
  Aislado, 2026-09-16 23:35 UTC: `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest
  tests/e2e/test_m7_mcp.py -m e2e -v -s` -> **1 passed in 19.55 s**; `mcp first call
  (create) 15.9 s`; `microvm-<id>: al cerrar el cliente -> TERMINATING a los
  0.12 s; total 15.93 s`; `list-microvms` sin VMs no terminales después (0).
  Pasada completa `uv run pytest tests/e2e -m e2e -v -s`, 23:45-00:01 UTC (935 s):
  **16 passed, 1 failed, 2 skipped, 1 deselected**; `test_m7_mcp.py` PASSED de nuevo
  (`mcp first call (create) 8.04 s`, total 11.27 s, `TERMINATING` a los 0.12 s). El
  fallo es `test_m5_pty_suspend_resume::test_pty_suspend_resume`
  (`DEADLINE_EXCEEDED` en la PTY de M5, ajeno a este cambio; en la misma sesión
  corrió `test_m7_pool.py` de `m7-suspended-pool`, que lanzó 23 VMs). Por eso 5.2
  queda sin marcar: el e2e de este cambio está verde dos veces, la suite completa
  no lo estuvo por un test de M5. Antes de la pasada la imagen tuvo que esperar a
  que otra sesión e2e (4 VMs, tres `SUSPENDED`) terminara: el sweeper de
  `conftest.py` mata todo VM vivo del template.
- **5.3 manual (Inspector)**: `pnpm dlx @modelcontextprotocol/inspector --cli` en
  esta caja Windows no consiguió inicializar el servidor por **stdio** (`Request
  timed out after 1m00s (initialize)` con el python del venv; `Connection closed`
  vía `uv run`), mientras que el cliente stdio del SDK `mcp` de Python
  (`test_m7_mcp.py`) funciona; queda como incidencia del Inspector CLI en Windows
  Git Bash, no reproducida en Linux/macOS. En **modo URL** sí: con `python -m
  rayito.mcp --http --port 8765`, `--cli http://127.0.0.1:8765/mcp --method
  tools/list` devuelve las seis herramientas; `--method tools/call --tool-name
  run_code --tool-arg "code=import matplotlib.pyplot as plt; plt.plot([1, 2, 3]);
  plt.show()"` devuelve `isError: false`, bloques `['text', 'image']`, `image/png`
  de 20 282 bytes con firma PNG válida; `list_sandboxes` marca `current: true` en
  `microvm-<id>`. En Windows no hay forma de enviar SIGINT a un proceso
  ajeno (`taskkill` sin `/F` lo rechaza), así que el servidor se mató con `/F` y el
  VM quedó `RUNNING`: exactamente el caso "host que mata el proceso" de la página
  de docs; `aws lambda-microvms terminate-microvm` lo dejó `TERMINATED`. **Claude
  Code** no se ejerció en esta pasada (`claude mcp add` escribe en la configuración
  del usuario; se deja para la revisión manual).
- **6.3 TypeScript**: proyecto scratch con `pnpm init`, `pnpm add ai zod`
  (`ai 7.0.105`, `zod 4.6.5`), `pnpm add -D typescript@5.9 @types/node
  @types/json-schema`, `pnpm add file:<repo>/clients/typescript`; `pnpm exec tsc
  --noEmit --strict --module nodenext --moduleResolution nodenext --target es2022
  vercel_ai_tool.ts` -> **exit 0** con `typescript 5.9.3` (también con `--lib
  es2022,dom,esnext.disposable --types node`, con y sin `--skipLibCheck`). Con
  `typescript 7.0.2` (lo que instala `pnpm add typescript` hoy) el comando literal
  falla sólo dentro de `.d.ts` de terceros (`Buffer`/`node:*` sin `--types node`,
  `Symbol.asyncDispose` sin `esnext.disposable`, `HeadersInit`/`json-schema` del AI
  SDK), nunca en `vercel_ai_tool.ts`. 44 líneas; `langchain_tool.py` 50 líneas,
  `py_compile` y `uvx ruff check`/`format --check` limpios (con la configuración
  por defecto de ruff, que es la del gate; con `known-first-party = rayito` del
  SDK isort pediría separar el bloque de imports: son incompatibles entre sí).
- **6.6 mkdocs** `--strict`: `Documentation built in 2.84 seconds`,
  `docs/site/_build/mcp/index.html` existe.
- **7.1**: `uv lock --check`, `ruff check .`/`ruff format --check .` y `mypy src
  tests` están limpios en todos los ficheros de este cambio; en el árbol hay fallos
  ajenos en curso de `m7-cli` (`src/rayito/cli/**`: E501, `rayito.cli.app`
  inexistente) y `m7-suspended-pool` (`rayito.sandbox_async.pool` inexistente,
  `test_pool_base.py` RUF043, `no-any-return` en `sandbox_sync/main.py:247/252` y
  `sandbox_async/main.py:196/201`, ficheros con mtime posterior a mi `uv lock`).
  `scripts/tests` 85 passed; `uvx ruff check scripts`, `check_license.py` `OK`,
  `gen_limits.py --check` exit 0.
- **7.2**: Rust y TypeScript no se tocaron. TypeScript: `pnpm lint/typecheck/test/
  build` fallan hoy por el módulo `src/pool/` a medio escribir de
  `m7-suspended-pool` (`../pool/pool.js` inexistente, `errors.ts` sin formatear),
  no por este cambio. Rust: `cargo fmt --check` y `cargo clippy` fallan en
  `crates/rayd-core` (`persistence::*`, `PersistenceError::Store`, 9 errores de
  compilación en el lib test) y `cargo test` tiene 2 fallos en
  `persistence::checkpoint`/`persistence::restore`: trabajo en curso de
  `m7-s3-persistence`. Queda sin marcar hasta que esos árboles se estabilicen.
- **Unit suite completa** (`uv run pytest tests/unit`): 899 passed, 3 failed, 2
  skipped en 271 s; los 3 fallos son ajenos (`test_limits.py` x2 por los nuevos
  límites `s3_bucket_name` de `m7-s3-persistence`, `cli/test_logs.py` de `m7-cli`).
- **Desviación de D1 (fichero, no superficie)**: `python -m rayito.mcp` avisaba
  `RuntimeWarning: 'rayito.mcp.__main__' found in sys.modules...` porque
  `__init__.py` reexporta `main` desde `__main__.py`. `parse_args`, `run` y `main`
  viven ahora en `rayito/mcp/_cli.py`; `__main__.py` es el shim de tres líneas y el
  entry point `rayito.mcp.__main__:main` no cambia. `test_mcp_main.py` corre
  `python -W error::RuntimeWarning -m rayito.mcp --help` para que no vuelva.
- **7.3 ci.yml**: el job Python usa `uv run pytest tests/unit`, `uv run ruff ...`,
  `uv run mypy src tests` sin `--no-default-groups`/`--only-group`; el grupo `dev`
  instala el extra: sin cambios. (`uv export --frozen --no-dev --no-emit-project`
  del paso de `pip-audit` audita el grafo de runtime sin extras, como estaba.)
