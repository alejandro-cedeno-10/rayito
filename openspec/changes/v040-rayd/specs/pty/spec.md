## MODIFIED Requirements

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
- **THEN** `Create` fails with `FAILED_PRECONDITION` ("no hay dispositivos PTY disponibles") before any message
