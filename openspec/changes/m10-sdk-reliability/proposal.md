## Why

M9 closed on 2026-09-24 with three items deferred with a written reason
(`MILESTONES.md` "M9 — Paridad con E2B", "Diferido con razón escrita") that
M10 ("mantenimiento y deuda de M9") now picks up, all inside SDK reliability
and UX, none touching `rayd`:

- **Reconnection after a proxy stream reset.** The regression saw a
  `RST_STREAM` from the AWS MicroVM proxy once, not reproduced in four
  retries, and it was not reconnected. `AWS_API_NOTES.md` #33 explains why:
  a `RST_STREAM(CANCEL)` on a client-stream `Write` measured 2026-09-15
  lands in grpcio as `CANCELLED` with details `"Stream removed"`, and
  `clients/python/src/rayito/_transport.py`'s `is_stream_reset` only checks
  `UNAVAILABLE` and `INTERNAL` — never `CANCELLED` — so this reset is never
  classified as reconnectable, regardless of which of the four stream kinds
  (`Connect(pid, from_seq)`, `Pty.Connect`, `WatchDir`, `run_code`'s
  `Reattach`) hits it: `is_reconnectable` gates all of them the same way. A
  `CANCELLED` is also what the SDK's own `call.cancel()` produces
  (`CommandHandle.disconnect()`), so the fix has to tell the two apart, not
  just widen the switch. The TypeScript SDK's `isStreamReset`
  (`clients/typescript/src/transport/errors.ts`) already classifies a
  `Canceled` RST_STREAM correctly for every stream kind (verified by
  reading `@connectrpc/connect-node`'s `node-error.ts`: every HTTP/2
  `RST_STREAM` maps deterministically to a `ConnectError` whose `rawMessage`
  always starts with `http/2 stream closed`, distinct from the
  `AbortSignal`-driven `"This operation was aborted"`), so this proposal is
  a Python fix with symmetric test coverage added to the TypeScript SDK, not
  a change to both SDKs' production code.
- **`rayito-mcp` without the extra.** `rayito.mcp/__init__.py` eagerly
  imports `mcp`-dependent submodules at module load, so importing anything
  under `rayito.mcp` — including `rayito.mcp.__main__`, which is what the
  `rayito-mcp` console script and `python -m rayito.mcp` import before
  their own code runs — raises during package initialisation, before any
  `try`/`except` in `__main__.py` can catch it. The result is an uncaught
  traceback (confirmed by reproducing it: `runpy.run_module("rayito.mcp",
  run_name="__main__")` with `mcp` blocked fails inside
  `rayito/mcp/__init__.py`, one frame before `__main__.py`'s own code
  starts), not the one-line friendly message the `rayito` CLI already
  prints for its own extra (`rayito.cli.__main__`).
- **The e2e upload budget.** `clients/python/tests/e2e/test_m3_filesystem.py`
  asserts an 8 MB `files.write` finishes inside a fixed 20 s
  (`UPLOAD_BUDGET_SECONDS`, ⇒ 0.4 MB/s floor), derived from one measurement
  at 0.66 MB/s (`AWS_API_NOTES.md` §16 Q32); uplinks measured since at
  0.25–0.44 MB/s make that assertion about the tester's network, not about
  `rayd`. The TypeScript mirror, `clients/typescript/tests/e2e/
  m9-transfer.e2e.test.ts`, already derives its S3-vs-gRPC and gzip-vs-plain
  budgets from a baseline measured in the same run (`routedFloor`,
  `MIN_GZIP_SPEEDUP`); reading it end to end found no fixed-seconds
  assertion to fix, so this item is Python-only, confirmed by inspection.

## What Changes

- **Python `_transport.py`**: `is_stream_reset` also checks `CANCELLED`
  against `STREAM_RESET_MARKERS` (now including `"Stream removed"`), so a
  proxy-caused `RST_STREAM` reconnects like a `Socket closed` `UNAVAILABLE`
  while a local `call.cancel()` (whose message never carries a reset
  marker) still does not — and the existing `disconnected`/`stopped` checks
  in every consumer (`CommandHandle`, `PtyHandle`, `WatchHandle`,
  `ExecutionFeed`) still run before `_is_reconnectable`, so a handle
  cancelled by its caller never reconnects even if the cut that follows
  looks like a reset. No proto or `rayd` change; this is purely client-side
  classification, and it is the same function all four stream kinds share.
- **Python `rayito.mcp`**: `__init__.py` resolves its four public names
  (`McpSettings`, `SandboxLease`, `build_server`, `main`) lazily through
  module `__getattr__` (PEP 562) instead of importing them eagerly, so
  `import rayito.mcp` itself (and therefore `python -m rayito.mcp` /
  `rayito-mcp`, which import it as a parent package before their own code
  runs) no longer fails when the extra is missing; only accessing one of
  those four names does, with the same message as before. `__main__.py`
  gains the `rayito.cli.__main__` pattern: a `try`/`except
  ModuleNotFoundError` around the deferred import, printing a one-line
  message with the install command to stderr and returning 2, no traceback.
- **Python `tests/e2e/test_m3_filesystem.py`**: `check_write_big` takes a
  measured budget instead of the fixed `UPLOAD_BUDGET_SECONDS`. A new
  `measure_upload_budget` writes and removes a 300 KB probe, scales its
  measured rate to the 8 MB payload with a 3x slack multiplier, and clamps
  the result to `[5 s, 120 s]` so neither an anomalously fast nor an
  anomalously slow probe produces an unusable budget. The test now checks a
  throughput regression against this run's own network, not a fixed number
  tuned for one measurement.
- **TypeScript `tests/unit/reconnect.test.ts`**: three new tests give the
  already-correct `isStreamReset`/`isReconnectable` explicit regression
  coverage at the stream level (not just the classification-function level
  `transport-errors.test.ts` already had): a live command reconnects
  through `Connect(pid, from_seq)` after a `Canceled` RST_STREAM cut
  (`process.cut(Code.Canceled, "http/2 stream closed with error code
  CANCEL (0x8)")`), three such cuts end the handle with the M2
  classification (same reconnect budget as an `Unavailable` cut), and
  `disconnect()` still wins the race when a cut shaped exactly like that
  reset arrives right after it. No production TypeScript changes.
- **Docs**: `docs/site/docs/mcp.md` describes the new entry-point behaviour;
  `docs/site/docs/concepts.md`'s "Streams y reconexión" section names the
  RST_STREAM/`CANCELLED` case and the own-cancel-wins-the-race guarantee;
  both `CHANGELOG.md`s get `[Unreleased]` entries.

## Non-goals

- No change to `rayd`, the `.proto`, or any published image: this is
  entirely client-side classification, packaging and test methodology.
- No fix for the egress DNS option A, the CI `arm` netns job, the
  `--remap-path-prefix` rustflags, or any other M9-deferred item: those are
  out of this change's scope (SDK reliability and UX only).
- No TypeScript production code change for the RST_STREAM classification:
  it was already correct, confirmed by reading `@connectrpc/connect-node`'s
  source and the existing `transport-errors.test.ts` truth table.

## Impact

- Affected specs: `suspend-resume` (Python reconnection contract text),
  `typescript-sdk` (reconnection contract requirement gains a scenario, no
  text change to the `SHALL` sentences), `mcp-server` (extra-missing
  behaviour for the entry points, and the package-import scenario updated
  for the lazy `__getattr__`).
- Affected code: `clients/python/src/rayito/_transport.py`,
  `clients/python/src/rayito/mcp/__init__.py`,
  `clients/python/src/rayito/mcp/__main__.py`,
  `clients/python/pyproject.toml` (ruff per-file ignore),
  `clients/python/tests/unit/test_transport.py`,
  `clients/python/tests/unit/test_commands_sync.py`,
  `clients/python/tests/unit/test_commands_async.py`,
  `clients/python/tests/unit/test_mcp_main.py`,
  `clients/python/tests/e2e/test_m3_filesystem.py`,
  `clients/typescript/tests/unit/reconnect.test.ts`, both `CHANGELOG.md`s,
  `docs/site/docs/mcp.md`, `docs/site/docs/concepts.md`.
- No breaking change for any caller: the reconnect contract only widens
  (strictly more cases now recover instead of raising), the MCP entry
  points only change from a traceback to a clean exit, and the e2e budget
  change affects nothing outside the test file. No version bump forced by
  this change alone.
