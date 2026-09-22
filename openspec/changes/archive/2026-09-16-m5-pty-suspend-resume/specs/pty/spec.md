## ADDED Requirements

### Requirement: PtyService.Create opens a login shell on a real controlling terminal
`PtyService.Create` SHALL allocate a pseudo-terminal with `openpty`, set its initial window size from `PtyStart.size` (absent → 80×24), and spawn `<shell> -i -l` where `<shell>` is `PtyStart.shell` when present (an absolute path, `INVALID_ARGUMENT` otherwise) or the user's login shell from the image's user database (`/bin/sh` when empty). The child SHALL, in `pre_exec` and in this order, call `setsid`, make the slave its controlling terminal with `TIOCSCTTY`, apply the M2 resource limits and drop privileges to the resolved identity (`user`, uid 1000 by default; root refused unless `RAYITO_ALLOW_ROOT=1`); the slave SHALL be owned by that identity (`fchown`, mode `0620`) before the spawn; stdin, stdout and stderr of the child SHALL all be the slave. The environment SHALL be built from scratch in the order identity variables (`PATH`, `HOME`, `USER`, `LOGNAME`), then `TERM=xterm-256color`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, `SHELL=<shell>`, then the `/run` payload envs, then `PtyStart.envs`, the last definition winning; `cwd` SHALL follow the M2 resolution rules. The first message of the stream SHALL be `started{pid}`.

#### Scenario: echo through the PTY
- **WHEN** the SDK calls `pty.create(size=PtySize(cols=100, rows=30), timeout=None)` and then `pty.send_input(pid, "echo hola\n")`
- **THEN** the stream started with `started{pid}` and, within 5 s, the concatenated `data` bytes contain `hola\r\n`

#### Scenario: identity, terminal and environment inside the shell
- **WHEN** the SDK sends `id -u; tty\n` and `echo $TERM $LANG\n` to a PTY created without `user`
- **THEN** the output contains `1000`, a `/dev/pts/` device name and `xterm-256color C.UTF-8`

#### Scenario: relative shell refused
- **WHEN** `Create` is called with `shell: "bash"`
- **THEN** the RPC fails with `INVALID_ARGUMENT` before any message and nothing is spawned

#### Scenario: no pty devices in the guest
- **WHEN** `/dev/ptmx` does not exist where `rayd` runs
- **THEN** `Create` fails with `FAILED_PRECONDITION` ("pty devices unavailable") before any message

### Requirement: Window size is validated and Resize applies TIOCSWINSZ
`PtyStart.size` and `ResizeRequest.size` SHALL be accepted only when `1 <= cols <= 4096` and `1 <= rows <= 4096`; otherwise the RPC SHALL fail with `INVALID_ARGUMENT`. `Resize{pid, size}` SHALL issue `TIOCSWINSZ` on the master so the foreground process group receives `SIGWINCH`, and SHALL answer `NOT_FOUND` for an unknown or ended pid. The SDK SHALL expose `PtySize(cols=80, rows=24)` with the same validation.

#### Scenario: stty sees the resize
- **WHEN** a PTY created with `PtySize(cols=100, rows=30)` runs `stty size`, then the SDK calls `pty.resize(pid, PtySize(cols=120, rows=40))` and runs `stty size` again
- **THEN** the outputs contain `30 100` and then `40 120`

#### Scenario: invalid size
- **WHEN** `Create` is called with `size{cols: 0, rows: 24}` or `size{cols: 5000, rows: 24}`
- **THEN** the RPC fails with `INVALID_ARGUMENT` and the SDK raises `InvalidArgumentException` from `PtySize(cols=0, rows=24)` before calling

### Requirement: PTY output is streamed in 16 KiB chunks with a per-PTY sequence
`rayd` SHALL read the master in chunks of at most 16 KiB and send each as `PtyServerMessage{data, seq}` with `seq` numbered `1, 2, 3…` per PTY; `started`, `exited` and `keepalive` SHALL carry `seq = 0`. Output SHALL be pushed into the PTY's 1 MiB ring before being fanned out to subscribers, in one order for every subscriber. `rayd` SHALL never decode, inspect or log PTY bytes.

#### Scenario: large output respects the chunk size
- **WHEN** an integration test sends `head -c 100000 /dev/zero | tr '\0' x\n` to a PTY
- **THEN** every `data` message is at most 16 KiB, the `seq` values are strictly increasing by one, and the concatenated output contains 100 000 `x`

### Requirement: PTYs share the process registry, its cap, its retention and its subscriber rules
A PTY SHALL be registered in the same registry as processes with `kind = PROCESS_KIND_PTY`, `config{cmd: <shell>, args: ["-i","-l"], envs: <request envs>, cwd}` and no tag; it SHALL count towards the 256 live entries; `ProcessService.List` SHALL list it with its kind; after it ends its `started`, ring and `exited` SHALL stay available to `Connect` for 30 s of running time (at most 256 ended entries retained). At most 8 subscribers SHALL be attached per PTY; a subscriber whose channel stays full for 30 s SHALL be detached alone with `exited{exited:false, status:"output_truncated", error:{code:"output_truncated"}}` while the PTY keeps running. `ProcessService.Connect`, `SendInput` and `CloseStdin` SHALL refuse a PTY pid with `FAILED_PRECONDITION`, and `PtyService.Connect`, `SendInput`, `Resize` and `Kill` SHALL refuse a non-PTY pid with `FAILED_PRECONDITION`; `ProcessService.SendSignal` SHALL accept both kinds.

#### Scenario: listed as a PTY and refused by the process RPCs
- **WHEN** the SDK creates a PTY and calls `commands.list()` and then `commands.send_stdin(pid, "x")` on its pid
- **THEN** the list contains an item with that `pid` and `kind == "pty"`, and `send_stdin` raises `InvalidArgumentException`

#### Scenario: second subscriber stalls alone
- **WHEN** an integration test attaches a second `Connect` that never reads, with `stall_timeout` 1 s, while the first keeps reading
- **THEN** the second stream ends with `exited{status:"output_truncated"}` and the first keeps receiving `data`

### Requirement: Connect re-attaches with gap-free replay
`PtyService.Connect{pid, from_seq}` SHALL fail with `FAILED_PRECONDITION` for a non-PTY pid, `NOT_FOUND` for an unknown or expired pid and `OUT_OF_RANGE` when `from_seq` is below the oldest retained `seq` or above the next one, all before any message; otherwise it SHALL start with `started{pid}`, replay the retained `data` with `seq >= from_seq` (`from_seq = 0` → none), continue with live output without gap or duplicate, and end with the retained `exited` when the PTY already ended. The SDK SHALL expose `pty.connect(pid, *, from_seq=0, on_data=None, timeout=None, request_timeout=None) -> PtyHandle` and `PtyHandle.last_seq`.

#### Scenario: replay from the first byte
- **WHEN** an integration test sends `echo hola\n` to a PTY and then opens `Connect{pid, from_seq: 1}`
- **THEN** the new stream starts with `started`, replays every `data` message including the one containing `hola`, and then follows live output

#### Scenario: evicted sequence
- **WHEN** `Connect{pid, from_seq}` names a `seq` beyond the next one
- **THEN** the RPC fails with `OUT_OF_RANGE` and the SDK raises `NotFoundException`

### Requirement: SendInput writes to the terminal and Kill signals the group
`SendInput{pid, data}` SHALL write and flush `data` to the master in order and answer afterwards; on an ended or unknown pid it SHALL answer `NOT_FOUND`. `Kill{pid}` SHALL send `SIGKILL` to the PTY's process group (the shell is a session leader with `pgid == pid`), answer `NOT_FOUND` for an ended or unknown pid, and the stream SHALL end with `exited{exited:true, status:"signaled", exit_code:137, signal:9}`. The SDK SHALL expose `pty.send_input(pid, data)` (`str` encoded as UTF-8, alias `send_stdin`), `pty.kill(pid) -> bool` (`False` on `NOT_FOUND`) and the same methods on `PtyHandle`.

#### Scenario: kill from the SDK
- **WHEN** the SDK calls `pty.kill(pid)` on a live PTY and then again
- **THEN** the first call returns `True`, `handle.wait()` raises `CommandExitException` with `exit_code == 137`, and the second call returns `False`

#### Scenario: input after exit
- **WHEN** the shell exited and the SDK calls `pty.send_input(pid, "x")`
- **THEN** `NotFoundException` is raised

### Requirement: PtyExited mirrors EndEvent, including the server timeout
When the shell exits the stream SHALL end with `exited{exited:true, status:"exited", exit_code}`; when it dies by a signal, `exited{exited:true, status:"signaled", exit_code:128+signal, signal}`. When `PtyStart.timeout_ms > 0`, `rayd` SHALL send `SIGTERM` to the group when the deadline expires on the running clock (suspended time excluded), `SIGKILL` 5 s later if still alive, and end with `exited{exited:false, status:"timeout", exit_code:128+signal, signal, error:{code:"deadline_exceeded"}}`. `timeout_ms = 0` SHALL mean no limit. The SDK's `pty.create(timeout=60.0)` SHALL send `timeout_ms = timeout * 1000` (`None`/`0` → no limit) and raise `TimeoutException` from `wait()` on the `timeout` status; `PtyHandle.wait()` SHALL return `CommandResult(stdout=<decoded terminal output>, stderr="", exit_code=0)` on exit 0 and raise `CommandExitException` otherwise. If a child of the shell still holds the slave when the shell exits, the stream SHALL end at most 500 ms after the shell's exit.

#### Scenario: exit code through the PTY
- **WHEN** an integration test sends `exit 3\n` to a PTY
- **THEN** the stream ends with `exited{exited:true, status:"exited", exit_code:3}` and `Connect` within 30 s replays `started`, the ring and that `exited`

#### Scenario: idle shell timed out
- **WHEN** an integration test creates a PTY with `timeout_ms: 1500` whose shell traps `TERM` and prints it
- **THEN** the output contains the trap's text and the stream ends with `exited{status:"timeout", error.code:"deadline_exceeded"}` about 1.5 s after creation

#### Scenario: background child holding the slave
- **WHEN** an integration test sends `sleep 30 &\n` and then `exit\n` to a PTY
- **THEN** the stream ends within 1.5 s of the `exit`

### Requirement: PTY streams are kept alive and gated by the session phase
`Create` and `Connect` streams SHALL emit `keepalive` after 30 s without any other message, repeatedly while idle; SDK iteration SHALL ignore them. `Create` and `Connect` SHALL fail with `UNAVAILABLE` while the session phase is `Suspending` or `Terminating`. Every `PtyService` RPC SHALL require `x-access-token`.

#### Scenario: keepalive cadence
- **WHEN** an integration test configures a 200 ms keepalive interval and leaves a PTY silent for 1 s
- **THEN** at least three `keepalive` messages arrive, each with `seq 0`

#### Scenario: create while suspending
- **WHEN** `/suspend` has been acknowledged and `Create` is called
- **THEN** the RPC fails with `UNAVAILABLE` with details `suspending`

### Requirement: SDK PTY surface, sync and async
The SDK SHALL expose `sbx.pty` with `create(*, size=None, user=None, cwd=None, envs=None, shell=None, on_data=None, timeout=60.0, request_timeout=None) -> PtyHandle`, `connect(pid, *, from_seq=0, on_data=None, timeout=None, request_timeout=None) -> PtyHandle`, `send_input(pid, data)` (alias `send_stdin`), `resize(pid, size)` and `kill(pid) -> bool`. `PtyHandle` SHALL be the `CommandHandle` implementation (`pid`, `last_seq`, `stdout`, `stderr == ""`, `exit_code`, `error`, `wait()`, `kill()`, `disconnect()`, iteration) plus `send_input(data)` (alias `send_stdin`), `resize(size)` and `reconnects`; iteration SHALL yield `(None, None, bytes)` per `data` message and call `on_data(bytes)` when given; `stdout` SHALL be the terminal output decoded as UTF-8 with replacement. `Create` and `Connect` SHALL use the sandbox's stream channel; the gRPC deadline SHALL be `timeout + 5 s` (none for `None`/`0`). `AsyncSandbox.pty` SHALL offer the same names as coroutines and `async for`.

#### Scenario: on_data and iteration
- **WHEN** the SDK creates a PTY with `on_data=chunks.append`, sends `echo hola\n` and iterates the handle until `hola` appears
- **THEN** every element of `chunks` is `bytes`, the iterated tuples have `stdout is None`, `stderr is None` and `pty` bytes, and `handle.stdout` contains `hola`

#### Scenario: async parity
- **WHEN** `AsyncSandbox.connect(...)` creates a PTY, sends `echo async-pty\n`, reads until it appears and kills it
- **THEN** the bytes contain `async-pty` and `await a.pty.kill(pid)` returns `True`

### Requirement: PTY logging hygiene
`rayd` SHALL log for a PTY only `pid`, `cols`, `rows`, `live_processes`, `status`, `exit_code`, `signal`, `duration_ms`, `subscribers`, `seq`/`from_seq` and error names; never the input or output bytes, the envs, the cwd or the shell's arguments beyond the program path. The SDK SHALL never log PTY bytes.

#### Scenario: no bytes in the log
- **WHEN** an integration test sends `echo SECRET-M5\n` to a PTY with the log captured
- **THEN** no log line contains `SECRET-M5`
