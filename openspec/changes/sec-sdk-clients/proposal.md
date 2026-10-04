## Why

A security sweep of the SDK clients (Python `rayito`, TypeScript `rayito`,
the CLI) confirmed eleven findings, two medium and nine low/info. None needs
a change to `rayd`, the `.proto` or any AWS resource; all are client-side.

- **CLI-PROXY-01 (medium)**: `rayito sandbox proxy` attached the operator's
  proxy token to every request and accepted any `Host`/`Origin`, so a web
  page open in the operator's browser could reach the proxied guest service
  (DNS rebinding, cross-site POSTs, cross-origin WebSockets). No cap on
  concurrent connections either.
- **CLI-PROXY-02 (low)**: content served through the proxy shares the
  `127.0.0.1`/`localhost` cookie jar and "site" with other local apps.
- **TPL-IGNORE-01 (medium)**: `.dockerignore` patterns in Docker's `**/`
  form (the `docker init` defaults `**/.env`, `**/.git`, …) never matched
  root-level paths in either SDK, so a root `.env` or `.git/` ended up in
  the image, readable by any sandbox code.
- **CLI-PROXY-03 (info)**: after the first request the proxy piped client
  bytes verbatim; for an upgrade the guest refused with a keep-alive
  response, later requests reached the upstream unrewritten (their own
  `x-aws-proxy-*` kept) over an authenticated connection.
- **TPL-SYMLINK-01 (low)**: the Python build-context walk (and the image
  zip) followed symlinks inside copied directories, so a link to a local
  file could end up in the uploaded artifact and the image (TypeScript and
  Docker skip them: parity break).
- **GIT-CREDS-01 (low)**: credentialed `git clone/push/pull` cleanup only
  tolerated a git exit error, so a timeout could leave the token in
  `.git/config` and mask the original error; the docs understated how
  exposed the token is to code already running in the sandbox.
- **CLI-TERM-01 (low)**: the CLI printed CloudWatch/build log text raw to
  the terminal (escape-sequence injection) and `sandbox logs` fell back to
  any stream whose name merely ended in the sandbox id.
- **AWS-ERR-01 (low)**: stacks, `Template.build` and the CLI's top-level
  handler/doctor/prune bypassed the AWS error sanitizer.
- **OUT-DOS-01 (low)**: command and PTY output was accumulated in client
  memory without a cap.
- **REPR-01 (info)**: `ProxyToken`/`DoctorContext` showed the JWE in
  `repr`/`util.inspect`.
- **TOKEN-ENTROPY-01 (info)**: no minimum length for caller-supplied access
  tokens.

## What Changes

- `rayito sandbox proxy`: `Host` allowlist (`421`), `Origin` check (`403`),
  new `--allowed-host`, `--allow-origin`, `--max-connections` (default 8,
  `503` beyond), a wildcard `--bind` requires `--allowed-host`, and the
  announcement offers `http://<id>.localhost:<port>` (separate cookie jar).
  `http_authority`/`is_wildcard` move to a shared stdlib-only module.
- Python build context and image zip never follow symlinks inside a copied
  directory (TypeScript gains tests pinning the same rule); each context
  file is re-checked and opened with `O_NOFOLLOW` before reading.
- `.dockerignore` follows Docker's semantics in both SDKs (shared vectors);
  both SDKs warn when likely secrets are packaged.
- The proxy forwards one message per connection (`400` on ambiguous body
  framing; a refused upgrade is answered with `Connection: close`).
- Git: the clean URL is always restored (operation error wins, a warning
  without URL/remote/path when the token may remain), a clone that fails
  other than by a git exit still strips origin, credentialed invocations
  run with `-c core.hooksPath=/dev/null -c credential.helper=`, and they are
  refused when the effective config rewrites URLs (`url.*.insteadOf`).
- CLI: `echo()` renders control characters visibly on a terminal; the logs
  fallback accepts only the exact stream-name shape and warns.
- AWS errors: stacks chain the sanitized summary; `Template.build` raises
  `BuildException`/`BuildError` with the new reason `aws_error`; the TS build
  adapter sanitizes every SDK call; `client_error_message` redacts.
- Output: per-stream cap `COMMAND_OUTPUT_MAX_BYTES` (64 MiB, `limits.json`),
  `truncated` on `CommandResult`/`CommandExitException`/`CommandExitError`,
  `max_output_bytes`/`maxOutputBytes` on `commands.run`/`connect`; PTYs use
  the default cap; `rayito sandbox exec` keeps nothing.
- `ACCESS_TOKEN_MIN_BYTES` (16, `limits.json`) enforced when decoding a
  caller token; the JWE is hidden from `repr`/`inspect`.
- `SECURITY.md` (T3, T4, T7, T9, T18, T28, logging hygiene), `security.md`,
  `proxy-local.md`, `cli.md`, `git.md`, `templates.md`, `comandos.md`,
  `variables-de-entorno.md`, both CHANGELOGs.

## Impact

- Breaking (documented under Security in both CHANGELOGs): a proxy client
  that sends a non-loopback `Host` needs `--allowed-host`; a wildcard bind
  needs `--allowed-host`; access tokens shorter than 16 bytes are rejected;
  `Template.build` raises `BuildException(reason="aws_error")` instead of a
  raw `ClientError`; credentialed git commands no longer run hooks or
  credential helpers; in `.dockerignore`, `*` and `?` no longer cross `/`;
  the proxy answers `400` to ambiguous body framing.
- No runtime (`rayd`), proto, infra or IAM change; no AWS call added; no
  cost. Acceptance is the local gates.
