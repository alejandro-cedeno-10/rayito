## MODIFIED Requirements

### Requirement: Watch streams are bounded, kept alive and released on drop
Each watch SHALL queue at most 1024 raw events; when the queue is full `rayd` SHALL stop enqueuing, deliver the buffered events and end the stream with `RESOURCE_EXHAUSTED`. An inotify queue overflow SHALL end the stream the same way; removal of the watched directory SHALL end it with `NOT_FOUND`. A silent watch stream SHALL emit `KeepAlive` every 50 s. Dropping the client stream SHALL remove the inotify watch immediately. At most 64 watches SHALL be live per sandbox; the next `WatchDir` SHALL fail with `RESOURCE_EXHAUSTED`. `WatchDir`, `Read` and `Write` SHALL fail with `UNAVAILABLE` and details `suspending` or `terminating` while the session phase is `Suspending` or `Terminating`. On the first `/suspend` of a cycle every live `WatchDir` and `Read` stream SHALL end with the gRPC status `UNAVAILABLE` and details `suspending` (the inotify watch is removed with the stream), and an in-flight `Write` SHALL be aborted with the same status, its temporary file removed and the destination untouched, draining at most 200 ms of the remaining body.

#### Scenario: stalled client overflows the queue
- **WHEN** the integration test configures a queue capacity of 4, produces more events than that without reading, then reads
- **THEN** the buffered events arrive and the stream ends with `RESOURCE_EXHAUSTED`

#### Scenario: watch closed by a suspend
- **WHEN** a `WatchDir` stream is open and `/suspend` is posted
- **THEN** the stream ends with `UNAVAILABLE` and details `suspending` within 2 s and the live-watch count drops to zero

#### Scenario: write aborted by a suspend
- **WHEN** a `Write` client-stream is mid-body when `/suspend` is posted
- **THEN** the RPC fails with `UNAVAILABLE suspending`, no `.rayito-tmp-*` file remains in the destination directory and the destination file does not exist

### Requirement: SDK WatchHandle contract
`WatchHandle` SHALL consume the stream in a background daemon thread (`asyncio.Task` for `AsyncWatchHandle`), call `on_event(event)` for each event when given (an exception inside the callback is logged and does not stop the watch), otherwise collect events for `get_new_events()`, ignore `keepalive`, and on stream end store the terminal exception (`None` after `stop()`) and call `on_exit(exc)` when given. When the stream ends with `UNAVAILABLE suspending`, a connection reset, `GOAWAY` or EOF while not stopped, the handle SHALL run the sandbox's reconnection poll and, on success, re-issue `WatchDir` with the same `path`, `recursive`, `include_entry`, `user` and the remaining deadline, wait for `WatchStarted`, keep the same event state and continue without calling `on_exit` (`is_running` stays `True`; events raised while suspended are not replayed); when the poll fails the terminal exception is stored as before. `get_new_events()` SHALL return the undelivered events or raise the stored exception once; `stop()` SHALL cancel the RPC, wait for the consumer (≤ 5 s) and be idempotent; `is_running` SHALL be `False` afterwards; `Sandbox.close()` SHALL stop every live handle. A `timeout > 0` SHALL become the gRPC deadline and surface as `TimeoutException`; `timeout` `0` or `None` SHALL mean no deadline.

#### Scenario: stop is clean
- **WHEN** the SDK calls `h.stop()` on a running handle
- **THEN** `h.is_running` is `False` and `h.get_new_events()` returns `[]` without raising

#### Scenario: watch survives a pause
- **WHEN** a `WatchHandle` on `/home/user` is live across `pause()` and `resume()` on real AWS and a file is written afterwards
- **THEN** within 10 s `get_new_events()` contains an event for that file name and `is_running` is `True`

#### Scenario: watch re-issue observed by the fake
- **WHEN** a unit test suspends and resumes the fake `rayd` with a live handle
- **THEN** the fake received two `WatchDir` requests with identical `path`, `recursive`, `include_entry` and `user`, and `on_exit` was not called
