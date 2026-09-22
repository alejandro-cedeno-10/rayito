## ADDED Requirements

### Requirement: Executions are recorded in a 4 MiB ring and outlive their stream
Every execution SHALL be recorded by `rayd` independently of the `Execute` stream that started it: its counted events (`started`, `stdout`, `stderr`, `result`, `error`, `end`) SHALL be kept in a per-execution ring of at most 4 MiB (cost = text length, sum of a result's mime strings, error value plus traceback; oldest evicted first; an event larger than the ring evicts everything and is not retained), at most 8 subscribers SHALL be attached to an execution, an ended execution SHALL stay available for 30 s of running time with at most 32 ended executions retained (oldest evicted), and the `Execute` stream SHALL be one subscriber among others. The sidecar's events for an execution SHALL be drained into the ring whether or not any subscriber is reading.

#### Scenario: ring replay bounds
- **WHEN** a unit test pushes counted events past 4 MiB into an `ExecuteRing`
- **THEN** `replay_from(oldest_seq)` returns the retained tail, `replay_from(oldest_seq − 1)` and `replay_from(next_seq + 1)` fail with `ReplayOutOfRange`, and `replay_from(0)` returns nothing

#### Scenario: ended execution retained then reaped
- **WHEN** an execution ended 31 s of running time ago and the reaper runs
- **THEN** `Reattach` on it fails with `NOT_FOUND`, and before the reaper it replayed through `end`

### Requirement: Reattach replays and follows an execution
`CodeService.Reattach{context_id, execution_id, from_seq}` SHALL fail with `UNAVAILABLE` while the phase is `Suspending` or `Terminating`, `INVALID_ARGUMENT` for a malformed `execution_id`, `NOT_FOUND` for an unknown, expired or mismatched (`context_id`) execution, `OUT_OF_RANGE` when `from_seq` is evicted or beyond the next `seq`, and `RESOURCE_EXHAUSTED` at the ninth subscriber, all before any message; otherwise it SHALL replay the retained events with `seq >= from_seq` (`0` → none), follow live events without gap or duplicate, emit `keepalive` every 5 s of silence, and end after `end` (immediately after the replay for an ended execution). Dropping a `Reattach` stream SHALL never interrupt the execution.

#### Scenario: reattach after the stream was closed by a suspend
- **WHEN** an integration test has an `Execute` of `sleep 3` closed by `/suspend`, posts `/resume` and opens `Reattach(context_id, execution_id, from_seq: last_seq + 1)`
- **THEN** the stream delivers the remaining events and the `end`, and the fake sidecar never received `interrupt`

#### Scenario: stalled origin, healthy reattach
- **WHEN** the origin `Execute` client of `big 2000` stops reading with `stall_timeout` 1 s while a `Reattach` subscriber keeps reading
- **THEN** the origin stream ends with `error{OutputTruncated}` + `end{0}` and the `Reattach` subscriber receives every chunk and the real `end`

#### Scenario: unknown execution from the SDK
- **WHEN** the SDK's `run_code` reattaches to an execution the agent no longer retains
- **THEN** the agent answers `NOT_FOUND` and the SDK raises `SandboxException`

## MODIFIED Requirements

### Requirement: Server-enforced execution timeout interrupts, then restarts
When `timeout_ms > 0`, `rayd` SHALL start the clock when it accepts the request and, at the deadline measured on the running clock (suspended time excluded), send `interrupt` for that execution; from then on kernel `error` events for it SHALL be dropped and the `end` SHALL be preceded by exactly one `error{name: "ExecutionTimeout", value: "execution exceeded <timeout_ms> ms", traceback: []}`. If no `end` arrives within 5 s of running time after the interrupt, `rayd` SHALL send `restart_context` for that context; the timed-out execution SHALL still end with `ExecutionTimeout` and any other in-flight execution of that context with `error{KernelRestarted}` followed by `end{execution_count: 0}`. A context interrupted in time SHALL keep its state. `timeout_ms = 0` SHALL mean no server limit; values above 28 800 000 SHALL be clamped. The deadlines SHALL be owned by the execution recorder, not by the `Execute` stream, so they keep running after the stream is closed.

#### Scenario: sleep interrupted, state intact
- **WHEN** the SDK runs `import time; time.sleep(10)` with `timeout=2` after `x = 42` in the same context
- **THEN** within 8 s the execution ends with `error.name == "ExecutionTimeout"` and `"2000"` in `error.value`, and `run_code("x").text == "42"` afterwards

#### Scenario: hung cell restarted
- **WHEN** an integration test executes the fake sidecar's `hang 5` with `timeout_ms=500`
- **THEN** the fake receives `interrupt` and then `restart_context`, and the stream ends with `ExecutionTimeout` followed by `end{execution_count: 0}`

#### Scenario: execution deadline survives a simulated pause
- **WHEN** an integration test executes `sleep 5` with `timeout_ms=1500`, posts `/suspend` at 0.5 s, sleeps 2 s and posts `/resume`
- **THEN** the fake receives `interrupt` about 1 s after the `/resume`, not at the `/resume`

### Requirement: Cancelling the stream interrupts the execution
When the client drops or cancels the originating `Execute` stream before its `end`, `rayd` SHALL send `interrupt{context_id, execution_id}` to the sidecar and the execution's later events SHALL be recorded but no longer delivered to that subscriber; a queued execution that is interrupted SHALL be removed from the queue. An `Execute` stream closed by `/suspend` SHALL be detached instead: no `interrupt` is sent and the execution keeps running and being recorded. Dropping a `Reattach` stream SHALL never interrupt.

#### Scenario: client drops mid-execution
- **WHEN** an integration test opens `Execute` on `sleep 5` and drops the stream after `started`
- **THEN** the fake sidecar receives `interrupt` for that execution within 1 s

#### Scenario: suspend does not interrupt
- **WHEN** an `Execute` on `sleep 3` is open and `/suspend` is posted
- **THEN** the stream ends with `UNAVAILABLE suspending`, the fake sidecar receives `quiesce` but no `interrupt`, and after `/resume` a `Reattach` delivers the `end`

### Requirement: Output backpressure and truncation
Each subscriber of an execution (`Execute` or `Reattach`) SHALL buffer at most 256 events ahead of its client; when the buffer is full the recorder SHALL NOT wait: it SHALL detach that subscriber alone, immediately, with `error{OutputTruncated}` and `end{execution_count: 0}` while the execution continues, keeps being recorded in its 4 MiB ring (from which the client may `Reattach` at `last_seq + 1`) and keeps being delivered to the other subscribers. The recorder SHALL never await a subscriber and SHALL always drain the sidecar dispatcher's channel for its execution, so a slow client never parks the recorder, the sidecar's stdout or the ops issued meanwhile. The sidecar SHALL bound the inbox between a kernel's ZMQ channels and its running cell (64 channel messages; the `died`/`abort` sentinels never wait). Sidecar op timeouts that expire while the dispatcher is parked SHALL NOT count towards the consecutive-timeouts kill switch. The recorder SHALL consume the events already queued for an execution before it consults the timeout timers, so an `end` that arrived inside the timeout is delivered unchanged however late a client reads it.

#### Scenario: stalled client
- **WHEN** an integration test executes `big 2000` (2000 chunks of 64 KiB) with a client that stops reading after `started`
- **THEN** the stream ends with `OutputTruncated` followed by `end{0}` once the client has fallen a full queue behind, the fake sidecar keeps running and was never parked, and the ring retains the last 4 MiB of chunks

#### Scenario: a stuck subscriber costs nobody else anything
- **WHEN** a unit test leaves one of two subscribers unread while the fake sidecar emits far more events than a queue holds and a `create_context` is issued meanwhile
- **THEN** the other subscriber receives every event and the `end`, the op is answered, `dispatch_blocked` stays false and no time is spent waiting on the stuck subscriber

#### Scenario: a queued end is not rewritten by a late read
- **WHEN** a unit test queues `started` and `end` for an execution with a 100 ms timeout and polls the subscriber only after the restart grace has elapsed
- **THEN** the stream yields `started` and `end` without `ExecutionTimeout` and sends neither `interrupt` nor `restart_context`

#### Scenario: a blocked emit keeps the sidecar inbox bounded
- **WHEN** a unit test pumps 100 `stream` messages into a context whose `emit` blocks after `started`
- **THEN** the inbox depth never exceeds its capacity and every chunk is emitted once the client resumes

### Requirement: Unary status mapping and Reattach
`CreateContext`, `ListContexts`, `DestroyContext` and `RestartContext` SHALL use gRPC statuses: `INVALID_ARGUMENT` (language, cwd, id syntax, envs), `NOT_FOUND` (context), `FAILED_PRECONDITION` (default context destroy), `RESOURCE_EXHAUSTED` (8 contexts), `UNAVAILABLE` with a message starting with `kernel not ready` (no sidecar, relaunching, context not ready within 30 s, sidecar op timeout) or with the phase name (suspending/terminating), `ABORTED` (concurrent restart/destroy of the same context), `INTERNAL` (protocol fault, message without payloads). `Reattach` SHALL be served with the statuses of its own requirement (`NOT_FOUND`, `INVALID_ARGUMENT`, `OUT_OF_RANGE`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`). Error messages SHALL never contain code, output, envs, cwd or tracebacks.

#### Scenario: execute while suspending
- **WHEN** `/suspend` has been acknowledged and `Execute` is called
- **THEN** the RPC fails with `UNAVAILABLE` and the SDK, once the reconnection poll fails, raises `SandboxStateException`

#### Scenario: kernel gate in the SDK
- **WHEN** `rayd` answers `UNAVAILABLE` with details `kernel not ready: no sidecar configured`
- **THEN** the SDK raises `SandboxException` without probing `Health`

#### Scenario: reattach on an unknown id
- **WHEN** `Reattach{context_id: "default", execution_id: "exec-0000000000000000", from_seq: 0}` is called
- **THEN** the RPC fails with `NOT_FOUND` before any message
