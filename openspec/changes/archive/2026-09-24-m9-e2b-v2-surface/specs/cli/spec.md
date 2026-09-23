## ADDED Requirements

### Requirement: rayito sandbox create, connect, exec and metrics
The CLI SHALL add four commands to the `sandbox` group. Only `rayito/cli/sandbox.py` imports `typer`. `rayito/cli/_terminal.py` and `rayito/cli/_tokens.py` SHALL import only standard-library modules and the SDK, and SHALL import without the extra.

**Access token.** The token SHALL come from `--token-file PATH` (the file content, whitespace stripped) or from the environment variable `RAYITO_ACCESS_TOKEN`. When both are present, `--token-file` SHALL win. The token SHALL never be accepted in argv, printed, or logged. A token file SHALL be created with `O_WRONLY | O_CREAT | O_EXCL` and mode `0600`, and an existing file SHALL exit 2 without launching anything.

**`rayito sandbox create [TEMPLATE] [--timeout SECONDS] [--metadata K=V]... [--env K=V]... [--detach] [--token-file PATH] [--user U] [--cwd DIR]`:**

- It SHALL use `RAYITO_ACCESS_TOKEN` or a freshly generated token.
- **With `--detach`:**
  - When the token was generated and `--token-file` is absent, it SHALL exit 2 without calling `run-microvm`.
  - It SHALL write the token file before `run-microvm`.
  - It SHALL print the `sandbox_id`, or with `--json` the document `{"sandbox_id", "endpoint", "template", "template_version", "expires_at"}`, which contains no token.
  - It SHALL exit 0.
- **Without `--detach`:** it SHALL run the terminal bridge, kill the sandbox when the terminal ends, and exit with the shell's exit code.

**`rayito sandbox connect ID [--user U] [--cwd DIR] [--env K=V]... [--token-file PATH]`** SHALL call `Sandbox.connect(ID, access_token=...)`, which resumes a suspended sandbox, and SHALL run the terminal bridge without killing the sandbox. It SHALL exit with the shell's exit code, or 2 when no token is available.

**`rayito sandbox exec ID [--background] [--cwd DIR] [--user U] [--env K=V]... [--timeout SECONDS] [--token-file PATH] -- CMD...`:**

- It SHALL run `shlex.join(CMD)` through `commands.run`. `--timeout 0`, the default, means no server deadline.
- It SHALL stream stdout and stderr to the local stdout and stderr as chunks arrive.
- It SHALL exit with the remote exit code, and with 124 on a server timeout.
- `--background` SHALL print the pid and exit 0.

**`rayito sandbox metrics ID [--follow] [--interval SECONDS] [--token-file PATH]`:**

- It SHALL print one metrics snapshot (`timestamp`, `cpu_used_pct`, `cpu_count`, `mem_used`, `mem_total`, `disk_used`, `disk_total`, and `mem_cache` when present) as a table, or with `--json` as one JSON object.
- `--follow` SHALL repeat every `--interval` seconds (default 5), printing one JSON object per line in JSON mode.
- `--follow` SHALL exit 0 on Ctrl-C and 1 when the sandbox disappears.

A malformed `K=V` pair SHALL exit 2. `SandboxNotFoundException` and AWS errors SHALL exit 1, following the existing global translation.

#### Scenario: detach needs somewhere to keep the token
- **WHEN** `rayito sandbox create --detach` runs with `RAYITO_ACCESS_TOKEN` unset and without `--token-file`
- **THEN** the exit code is 2, stderr names `--token-file`, and the fake control plane records no `run-microvm`

#### Scenario: detached create writes the token file first and never prints the token
- **WHEN** `rayito sandbox create --detach --token-file <tmp>/t --json` runs against the fake control plane
- **THEN** the file exists (mode `0600` on POSIX) before `run-microvm` is recorded, stdout is a JSON document with the `sandbox_id` and without the token, and neither stdout nor stderr contains the token

#### Scenario: exec passes the remote exit code through
- **WHEN** `rayito sandbox exec microvm-x --token-file <tmp>/t -- sh -c 'echo hi; exit 3'` runs against the fake `rayd`
- **THEN** stdout contains `hi`, the recorded `Start` command equals `shlex.join(["sh", "-c", "echo hi; exit 3"])`, and the exit code is 3

#### Scenario: metrics in JSON
- **WHEN** `rayito sandbox metrics microvm-x --token-file <tmp>/t --json` runs against the fake `rayd`
- **THEN** stdout is one JSON object with `cpu_count` and `mem_total` equal to the fake's values, and the exit code is 0

### Requirement: Interactive terminal bridge
`rayito.cli._terminal.run_terminal(sandbox, *, user, cwd, envs, stdin, stdout) -> int` SHALL open a PTY with `sandbox.pty.create`. The PTY size SHALL be the local terminal size, or 80x24 when stdin is not a terminal. `timeout` SHALL be `None`, and `on_data` SHALL write the PTY bytes to `stdout` and flush them.

Input SHALL be forwarded with `send_input` from a daemon reader thread, depending on the kind of stdin:

- **POSIX terminal:** in raw mode (`tty.setraw`, with the saved `termios` attributes restored in `finally`), with a `SIGWINCH` handler that calls `resize`, and with Ctrl-C forwarded as `\x03`.
- **Windows console:** through `msvcrt.getwch`, with the extended arrow, Home, End and Delete codes translated to their VT sequences, and the size polled every second.
- **Non-terminal stdin:** as read, sending `\x04` on EOF.

It SHALL return the PTY's exit code, and 124 when the PTY ends on a server timeout.

#### Scenario: pipe mode round-trip
- **WHEN** `run_terminal` runs against the fake `PtyService` with a non-terminal stdin containing `b"echo hola\nexit\n"`, and the fake echoes input and exits 0 after `exit`
- **THEN** the fake records the piped bytes followed by `\x04`, `stdout` receives the echoed `hola`, and the function returns 0

#### Scenario: terminal restored on POSIX
- **WHEN** `run_terminal` runs on POSIX with a pseudo-terminal as stdin and the PTY ends
- **THEN** the terminal attributes after the call equal those before it
