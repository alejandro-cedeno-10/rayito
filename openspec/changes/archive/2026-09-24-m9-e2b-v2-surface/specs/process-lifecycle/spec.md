## ADDED Requirements

### Requirement: CommandHandle.wait accepts E2B output callbacks
The native `CommandHandle.wait` SHALL accept optional keyword callbacks, and so SHALL `PtyHandle`, which inherits it. The signature SHALL be `wait(on_pty: Callable[[bytes], Any] | None = None, on_stdout: Callable[[str], Any] | None = None, on_stderr: Callable[[str], Any] | None = None) -> CommandResult`. `AsyncCommandHandle.wait` SHALL take the same keywords, accepting sync or `async` callbacks and awaiting results that are awaitable.

While `wait()` consumes the stream, each decoded `(stdout, stderr, pty)` chunk SHALL be passed to the matching callback, after any `on_stdout`/`on_stderr` given at `run()` time. Chunks consumed before `wait()` was called SHALL NOT be replayed. The returned `CommandResult`, the exceptions (`CommandExitException`, `TimeoutException`, `SandboxException` for `output_truncated`) and the idempotence of `wait()` SHALL be unchanged.

#### Scenario: callbacks receive the chunks
- **WHEN** the unit test starts `commands.run("echo out; echo err >&2", background=True)` against the fake `rayd` and calls `handle.wait(on_stdout=out.append, on_stderr=err.append)`
- **THEN** `out == ["out\n"]`, `err == ["err\n"]`, and the result has `stdout == "out\n"` and `exit_code == 0`

#### Scenario: async callbacks are awaited
- **WHEN** the async unit test calls `await handle.wait(on_stdout=async_append)`, where `async_append` is a coroutine function
- **THEN** every stdout chunk has been appended by the time `wait()` returns

#### Scenario: PTY output reaches on_pty
- **WHEN** the unit test creates a PTY on the fake, sends `echo hola\n` and calls `handle.wait(on_pty=chunks.append)` until the fake exits
- **THEN** the concatenated `chunks` contain `b"hola"`
