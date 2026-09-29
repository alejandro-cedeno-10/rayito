## Context

Three items from `MILESTONES.md`'s M9 "Diferido con razón escrita" list,
all inside `clients/python` and `clients/typescript`, none requiring a
`rayd`/proto change or a republished image. Investigated by reading the
current classification code, the grpcio and `@connectrpc/connect-node`
sources actually installed in this repo's lockfiles, and
`AWS_API_NOTES.md`'s existing measurements, before writing any code.

## Decision 1 — classify `CANCELLED "Stream removed"` by message, not by widening the code alone

`clients/python/src/rayito/_transport.py::is_stream_reset` handled two
codes:

```python
if code is grpc.StatusCode.UNAVAILABLE:
    return not (is_phase_gate(exc) or is_kernel_gate(exc))
if code is not grpc.StatusCode.INTERNAL:
    return False
text = rpc_details(exc) + rpc_debug_string(exc)
return any(marker in text for marker in STREAM_RESET_MARKERS)
```

`AWS_API_NOTES.md` #33 (measured 2026-09-15, `rayd` 4.0) recorded a real
`RST_STREAM(CANCEL)` from the proxy landing in grpcio as `CANCELLED` with
details `"Stream removed"`, error code 8 — a code this function never
checked, so `is_reconnectable` returned `False` and the four stream
consumers (`CommandHandle`, `PtyHandle`, `WatchHandle`, `ExecutionFeed`, all
gated by the same `Sandbox._is_reconnectable`) raised instead of
reconnecting.

The obvious fix — add `CANCELLED` to the code check — is not safe alone:
`CANCELLED` is also exactly what a local `call.cancel()` produces (used by
`CommandHandle.disconnect()`, `WatchHandle.stop()`, `ExecutionFeed`'s
cleanup), and retrying a stream the caller itself cancelled would violate
"never retry a stream cancelled by the user's own cancel." The two cases
need to be told apart by the same signal: the exact text grpc-core leaves
in `details`/`debug_error_string`. Reproduced with the C-core strings
actually shipped (`strings` on `cygrpc*.so`): a `RST_STREAM` the transport
receives from the peer is always described as `"Stream removed"`; a local
`grpc_call_cancel` is described as `"call cancelled by v2 filter stack"` (or
similar locally-cancelled text) — never `"Stream removed"`. So the fix
widens the code check to `(INTERNAL, CANCELLED)` and adds `"Stream removed"`
to `STREAM_RESET_MARKERS`, keeping the existing marker-matching path
unchanged for `INTERNAL`. A `CANCELLED` without any marker (a genuine local
cancel) still returns `False`.

This is defence in depth, not the only guard: every stream consumer already
checks its own `disconnected`/`stopped` flag *before* calling
`_is_reconnectable` (`CommandHandle._consume_stream`: `if
progress.disconnected: return` comes first), set synchronously by
`disconnect()`/`stop()` before the local `call.cancel()` ever runs. So even
if a cut arrived disguised as `"Stream removed"` right after a caller-issued
cancel, the disconnected/stopped check already short-circuits before the
classification is ever consulted. Both layers are tested: the truth table
in `test_transport.py` covers the classification alone, and
`test_commands_sync.py`/`test_commands_async.py` add a
`disconnect()`-then-inject-a-matching-cut test proving the ordering holds
end to end.

Bounded retries and no duplicate output need no new code: they were already
enforced generically by `ReconnectBudget` (three reconnects without a new
generation end the handle with the M2 classification) and `from_seq =
last_seq + 1` on every `Connect`/`Pty.Connect`/`Reattach` re-subscribe,
regardless of which reset shape triggered the reconnect. The new tests
exercise both with the `CANCELLED "Stream removed"` shape specifically,
mirroring the existing `UNAVAILABLE "Socket closed"` coverage.

## Decision 2 — TypeScript needs tests, not a production fix

Before touching `clients/typescript/src/transport/errors.ts`, its
`isStreamReset` was read against the actual RST_STREAM-to-`ConnectError`
mapping in `@connectrpc/connect-node`'s installed `node-error.ts`
(`connectErrorFromH2ResetCode`): every HTTP/2 error code an `RST_STREAM` can
carry maps deterministically to a `ConnectError` whose `rawMessage` always
starts with `"http/2 stream closed"`, for every one of `Code.Internal`,
`Code.Unavailable` and `Code.Canceled` (`CANCEL` (0x8) specifically maps to
`Code.Canceled` with that exact prefix). `isStreamReset`'s `Canceled` branch
already checks precisely that prefix, and the SDK's own `AbortSignal` path
produces `"This operation was aborted"` through a completely different code
path (`AbortController.abort()`, never through connect-node's H2-reset
mapping), so there is no message grpc-python's ambiguous `"Stream removed"`
has that the Node stack can produce under `Canceled` without that prefix.
The existing `transport-errors.test.ts` truth table already asserts this
(`"RST_STREAM CANCEL"` reconnectable, `"own abort"` not).

What was missing was coverage at the stream level: nothing in
`reconnect.test.ts` cut a live command/PTY/watch stream with `Code.Canceled`
specifically (every existing `process.cut(...)` call used
`Code.Unavailable`). Three tests were added to close that gap and give this
SDK the same explicit regression coverage the Python fix needed: a live
reconnect through `Connect`, the three-cut budget exhaustion, and
`disconnect()` winning the race — using the fake `rayd`'s existing
`FakeProcess.cut(code, message)` hook, no new test infrastructure.

## Decision 3 — measured-baseline upload budget, not a bigger fixed number

Bumping `UPLOAD_BUDGET_SECONDS` to some larger fixed value only moves the
flake threshold; it does not make the assertion mean what it is meant to
mean ("did throughput regress on this run", not "is the tester's uplink
above N MB/s"). The fix times a small write first (300 KB — big enough that
per-call overhead does not dominate, small enough to add well under a
second even on the slowest uplinks measured) and scales that rate to the 8
MB payload with a 3x slack multiplier (covering HTTP/2 window ramp-up being
different at 8 MB than at 300 KB, and general noise), clamped to `[5 s,
120 s]` so a probe that happens to run anomalously fast does not produce an
unreasonably tight budget, and one that runs anomalously slow does not let
the test hang for minutes. The probe writes and removes a dotfile under the
same `BASE` directory used throughout the test, so the later exact-set
assertion in `check_list` (`{"big.bin", "hola.txt", "many"}`) is unaffected.

The TypeScript mirror (`m9-transfer.e2e.test.ts`) was read end to end
looking for the same pattern; every assertion there (`routedFloor`,
`MIN_GZIP_SPEEDUP`) already compares against a rate measured in the same
run, so nothing needed to change on that side — confirmed by inspection,
not assumed.

## Risks / Trade-offs

- The `CANCELLED` marker match is a string comparison against a grpc-core
  message that is not part of any public API contract and could change in
  a future grpcio release. This is the same trade-off the existing
  `INTERNAL` marker matching already accepted (`"RST_STREAM"`, `"GOAWAY"`,
  `"Socket closed"`, `"Connection reset"` are exactly as unstable); no new
  category of risk, and the existing `is_stream_reset` truth-table test
  will catch a message-format change immediately if a grpcio upgrade ever
  breaks it.
- The upload budget's 3x slack and `[5 s, 120 s]` clamp are chosen, not
  measured against a large sample; if they prove too tight or too loose in
  practice, the constants are isolated at the top of
  `test_m3_filesystem.py` for a one-line adjustment next cycle.

## Migration Plan

None: no public API changes, no data migration, no image republish. Ships
in the next patch/minor release of both SDK packages.
