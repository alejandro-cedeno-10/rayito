## ADDED Requirements

### Requirement: Client-side command and PTY output is bounded
Each output stream of a command or PTY SHALL keep at most `max_output_bytes`/`maxOutputBytes` received bytes in client memory (default `COMMAND_OUTPUT_MAX_BYTES`, 64 MiB, generated from `limits.json`), dropping the oldest output first; `0` SHALL keep nothing; callbacks SHALL receive all output regardless. `CommandResult.truncated` and `CommandExitException.truncated`/`CommandExitError.truncated` SHALL be true when output was dropped (TypeScript `CommandResult` carries `truncated: true` only then). `commands.run` and `commands.connect` SHALL accept the option in both SDKs and reject a negative or non-integer value before any RPC. PTY handles SHALL use the default cap. `rayito sandbox exec` SHALL keep no output in memory.

#### Scenario: a command writes more than the cap
- **WHEN** `commands.run` with `max_output_bytes=N` produces 3·N bytes of stdout
- **THEN** `result.stdout` holds the last N bytes, `result.truncated` is true and the `on_stdout` callback received all 3·N bytes

### Requirement: Caller-supplied access tokens have a minimum length
`decode_access_token`/`decodeAccessToken` SHALL reject, with `InvalidArgumentException`/`InvalidArgumentError` and without echoing the token, a canonical base64url token that decodes to fewer than `ACCESS_TOKEN_MIN_BYTES` (16, from `limits.json`) bytes, so `access_token=`, `RAYITO_ACCESS_TOKEN` and `--token-file` all enforce it. Generated tokens (32 bytes) SHALL be unaffected.

#### Scenario: a hand-picked short token
- **WHEN** `RAYITO_ACCESS_TOKEN` holds the base64url of 15 bytes
- **THEN** `create`/`connect` fail with `InvalidArgumentException` naming the minimum and not containing the token

### Requirement: Objects holding a proxy JWE do not reveal it
Python `ProxyToken.jwe` and the CLI's `DoctorContext.minted_token` SHALL be excluded from `repr`, and TypeScript `ProxyToken.jwe` SHALL be non-enumerable so `util.inspect`, `console.log` and `JSON.stringify` do not show it, while direct access keeps working.

#### Scenario: a debug print of a token
- **WHEN** a `ProxyToken` is printed with `repr()` or `util.inspect`
- **THEN** the JWE does not appear
