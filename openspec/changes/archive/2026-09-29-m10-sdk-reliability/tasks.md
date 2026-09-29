## 1. Reconnect after a proxy stream reset (Python fix, TypeScript tests)

- [x] 1.1 Read `_transport.py`'s current classification and reproduce why `CANCELLED "Stream removed"` (AWS_API_NOTES.md #33) is not reconnected today
- [x] 1.2 Read `@connectrpc/connect-node`'s installed `node-error.ts` to confirm TypeScript's `isStreamReset` already classifies every RST_STREAM code correctly under `Canceled`
- [x] 1.3 Add `"Stream removed"` to `STREAM_RESET_MARKERS` and widen `is_stream_reset`'s code check to `(INTERNAL, CANCELLED)`, keeping a marker-less `CANCELLED` (a local `call.cancel()`) non-reconnectable
- [x] 1.4 Unit test: truth table in `test_transport.py` covers `CANCELLED` with and without the marker
- [x] 1.5 Unit test (sync + async): a live command stream cut with `CANCELLED "Stream removed"` reconnects through `Connect(pid, from_seq)` (`test_commands_sync.py`, `test_commands_async.py`)
- [x] 1.6 Unit test: a `CANCELLED` without the marker (`"Locally cancelled by application!"`) is never reconnected, in both trees
- [x] 1.7 Unit test: repeated `CANCELLED "Stream removed"` cuts exhaust the same three-reconnect budget as an `UNAVAILABLE` cut (M2 classification)
- [x] 1.8 Unit test: `disconnect()` wins the race even when the cut that follows is shaped exactly like a reconnectable `CANCELLED "Stream removed"` reset, in both trees
- [x] 1.9 TypeScript unit tests in `reconnect.test.ts`: a live command reconnects through `Connect` after `process.cut(Code.Canceled, "http/2 stream closed with error code CANCEL (0x8)")`, three such cuts hit the M2 classification, `disconnect()` wins the race against one — no production TypeScript change
- [x] 1.10 `docs/site/docs/concepts.md` "Streams y reconexión": name the RST_STREAM/`CANCELLED` case and the own-cancel guarantee
- [x] 1.11 Both `CHANGELOG.md`s: `[Unreleased]` entry

## 2. `rayito-mcp` without the extra

- [x] 2.1 Reproduce the current uncaught traceback (`runpy.run_module("rayito.mcp", run_name="__main__")` with `mcp` blocked) and confirm it fails during `rayito/mcp/__init__.py`'s eager import, before `__main__.py`'s own code runs
- [x] 2.2 `rayito/mcp/__init__.py`: resolve `McpSettings`, `SandboxLease`, `build_server`, `main` lazily via module `__getattr__` (PEP 562); `import rayito.mcp` alone no longer needs the extra, only accessing one of the four names does, with the same message as before
- [x] 2.3 `rayito/mcp/__main__.py`: catch `ModuleNotFoundError` around the deferred `from rayito.mcp._cli import main`, print a one-line message with the install command to stderr, return 2 — mirrors `rayito.cli.__main__`'s `missing_typer` pattern
- [x] 2.4 `pyproject.toml`: add `src/rayito/mcp/__main__.py` to the ruff `T20` (no-`print`) per-file-ignore, matching `src/rayito/cli/**`
- [x] 2.5 Unit tests in `test_mcp_main.py`: `__main__.main()` returns 2 with the friendly message and no stdout when `mcp` (and every already-cached `mcp.*` submodule) is blocked; `import rayito.mcp` itself still succeeds without the extra; a real subprocess running `python -m rayito.mcp` with `mcp` blocked exits 2 with no `Traceback` in stderr
- [x] 2.6 `docs/site/docs/mcp.md`: describe the new entry-point behaviour (friendly exit) versus direct attribute access (still `ModuleNotFoundError`)
- [x] 2.7 Python `CHANGELOG.md`: `[Unreleased]` entry

## 3. e2e upload budget

- [x] 3.1 Confirm `UPLOAD_BUDGET_SECONDS` (20 s fixed for 8 MB) is the only fixed-throughput assertion in `test_m3_filesystem.py`
- [x] 3.2 Read `m9-transfer.e2e.test.ts` end to end to check whether the TypeScript mirror has the same problem
- [x] 3.3 `test_m3_filesystem.py`: replace the fixed budget with `measure_upload_budget` (300 KB probe, written and removed under `BASE`, 3x slack, clamped to `[5 s, 120 s]`); `check_write_big` takes the computed budget
- [x] 3.4 Confirm the probe's removal keeps `check_list`'s exact-set assertion (`{"big.bin", "hola.txt", "many"}`) unaffected
- [x] 3.5 `--collect-only` and `mypy`/`ruff` over the e2e file (no real AWS run)
- [x] 3.6 TypeScript: no change needed (`routedFloor`/`MIN_GZIP_SPEEDUP` already measure a same-run baseline); documented as verified, not skipped
- [x] 3.7 Python `CHANGELOG.md`: `[Unreleased]` entry

## 4. Spec deltas and gates

- [x] 4.1 `specs/suspend-resume/spec.md`: MODIFIED "Reconnection contract on stream cuts and unary failures" — name the `CANCELLED "Stream removed"` case
- [x] 4.2 `specs/typescript-sdk/spec.md`: MODIFIED "Reconnection contract on stream cuts and unary failures" — add the new stream-level scenario, no `SHALL` text change
- [x] 4.3 `specs/mcp-server/spec.md`: MODIFIED "MCP server package behind the optional extra" (lazy `__getattr__`, updated scenario) and "Transports and command line" (friendly exit without the extra, new scenario)
- [x] 4.4 `openspec validate m10-sdk-reliability --strict` passes
- [x] 4.5 Python: `uv run pytest tests/unit` (full), `uv run pytest ../../scripts/tests`, `ruff check .`, `ruff format --check .`, `mypy src tests`
- [x] 4.6 TypeScript: `pnpm install --frozen-lockfile`, `pnpm lint`, `pnpm typecheck`, `pnpm build`, `pnpm test`, `pnpm pack:check`
- [ ] 4.7 CI green on the PR (GitHub Actions; not runnable locally)
