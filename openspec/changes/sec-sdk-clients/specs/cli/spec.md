## ADDED Requirements

### Requirement: The local proxy forwards only allowlisted Host and Origin values
`rayito sandbox proxy` SHALL, after parsing a request head and before minting or using the JWE or opening the upstream, compare the request's `Host` with an allowlist made of `127.0.0.1`, `localhost` and `[::1]` with the local port, the `--bind` address with the local port unless it is a wildcard (`0.0.0.0`/`::`), `<sandbox-id>.localhost` with the local port when the bind is loopback and the id is a DNS label, and each repeatable `--allowed-host` value (without a port it matches bare and with the local port; with a port it matches exactly); a bare loopback name SHALL match only when the local port is 80. A missing or non-allowlisted `Host` SHALL be answered `421 Misdirected Request` with `Connection: close`. A present `Origin` SHALL equal `http://` plus an allowlisted authority or a repeatable `--allow-origin` value (`http(s)://host[:port]`), case-insensitively and ignoring a trailing slash; any other value, including `null`, SHALL be answered `403 Forbidden` with `Connection: close`. `--allow-remote` with a wildcard `--bind` SHALL require at least one `--allowed-host` (usage error otherwise). The stderr line for a rejection SHALL never contain the received value.

#### Scenario: DNS rebinding is refused
- **WHEN** a request arrives with `Host: attacker.example:<local-port>`
- **THEN** the proxy answers `421`, closes the connection and never opens the upstream connection

#### Scenario: a cross-origin WebSocket upgrade is refused
- **WHEN** an upgrade request arrives with a loopback `Host` and `Origin: http://attacker.example`
- **THEN** the proxy answers `403` and never opens the upstream connection

#### Scenario: a same-origin request passes
- **WHEN** a request arrives with `Host: localhost:<local-port>` and `Origin: http://localhost:<local-port>`
- **THEN** it is rewritten and forwarded as before

#### Scenario: a wildcard bind without an allowed host
- **WHEN** the operator runs `rayito sandbox proxy <id> --port 8080 --bind 0.0.0.0 --allow-remote`
- **THEN** the command exits with the usage code naming `--allowed-host`

### Requirement: The local proxy caps concurrent forwarded connections and announces an isolated host
`rayito sandbox proxy` SHALL forward at most `--max-connections` connections at a time (default `MAX_CONCURRENT_CONNECTIONS_1_VCPU`, 8); a connection that passes the Host/Origin checks while the budget is full SHALL be answered `503 Service Unavailable` with `Connection: close` without opening the upstream, and a finished connection SHALL release its slot. `--max-connections` below 1 SHALL fail before any AWS call. With a loopback bind, the start-up announcement SHALL also print `http://<sandbox-id>.localhost:<local-port>`, whose cookie jar is separate from `127.0.0.1`/`localhost`.

#### Scenario: the budget is full
- **WHEN** every slot is taken and another allowlisted request arrives
- **THEN** it is answered `503` and the upstream is not opened

### Requirement: CLI output neutralises terminal control characters
`rayito.cli._console.echo` SHALL, when its output stream is a terminal, render every C0 control character except tab and newline, DEL and every C1 control character as a visible `\xNN` escape before printing; output redirected to a file or a pipe SHALL be unchanged. Interactive `exec`/`connect` passthrough SHALL NOT go through this path.

#### Scenario: an OSC 52 sequence in a log line
- **WHEN** `rayito sandbox logs` prints an event containing `ESC ] 52 ; c ; … BEL` to a terminal
- **THEN** the terminal receives the literal text `\x1b]52;c;…\x07` and no escape sequence

### Requirement: The sandbox logs fallback accepts only the exact stream shape and warns
When the expected stream `YYYY/MM/DD[<imageVersion>]<id>` does not exist, `find_streams` SHALL accept from its bounded scan only stream names that match that exact shape with the sandbox's image version and id and a day not before the sandbox's UTC start date; a name that merely ends in `]<id>` SHALL be ignored. `rayito sandbox logs` SHALL print a warning on stderr naming the expected and the used streams whenever the streams it reads are not exactly the expected one.

#### Scenario: a forged stream name
- **WHEN** the group holds `forged]<id>` and a later-day stream with the right shape
- **THEN** only the later-day stream is returned and the command warns on stderr

### Requirement: CLI error output redacts AWS messages
`client_error_message` SHALL return the AWS `Message` passed through `redact_aws_text`, so the CLI's top-level `ClientError` handler, `rayito doctor` and `rayito prune` never print a signature error's canonical string, session token or access key id.

#### Scenario: a signature error reaches the top-level handler
- **WHEN** a command fails with `InvalidSignatureException` whose `Message` contains the canonical string
- **THEN** stderr shows `AWS error InvalidSignatureException:` and neither the session token nor the access key id
