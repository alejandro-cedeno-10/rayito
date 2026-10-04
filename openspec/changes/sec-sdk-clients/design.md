## Context

Client-side fixes for nine confirmed findings of the SDK-clients security
sweep. Hexagonal layering is kept: pure helpers (`_authority.py`,
`_git_base.py`, `git-args.ts`, `DecodedStream`) carry the rules; the
adapters (`cli/_proxy.py`, `sandbox_*/git.py`, `git.ts`, `templates/build.ts`)
apply them. Python and TypeScript stay in parity wherever both SDKs have the
surface (the proxy and the terminal sanitizer are CLI-only, so Python-only).

## Decisions

- **D1 Proxy Host allowlist.** Allowed: `127.0.0.1`, `localhost`, `[::1]`
  with the local port; the `--bind` address unless it is a wildcard;
  `<sandbox-id>.localhost` when the bind is loopback and the id is a DNS
  label; each `--allowed-host` (without a port it matches bare and with the
  local port; with a port, exactly). A bare loopback name matches only when
  the local port is 80 (RFC 9110 §4.2.1). Missing or other `Host` → `421`
  with `Connection: close`, upstream never opened.
- **D2 Origin.** A present `Origin` must equal `http://<allowed authority>`
  or an `--allow-origin` (`http(s)://host[:port]`, normalised lower-case
  without trailing slash). `null` is rejected. Absent `Origin` passes (curl,
  same-origin navigations). → `403`.
- **D3 Wildcard bind.** `--allow-remote` with `0.0.0.0`/`::` requires
  `--allowed-host` (usage error); a concrete non-loopback address is itself
  allowed, so it needs nothing more. This narrows the finding's suggestion
  ("any non-loopback bind") because a concrete bind address is already a
  valid, non-rebindable authority.
- **D4 Connection cap.** `asyncio.Semaphore(--max-connections)`, default
  `MAX_CONCURRENT_CONNECTIONS_1_VCPU` (8, AWS_API_NOTES.md §7); checked after
  the Host/Origin checks and before minting/connecting; full → `503`.
- **D5 Cookies.** `Cookie` is forwarded unchanged (stripping it breaks the
  guest app's own sessions). The isolated `<id>.localhost` URL is announced
  and documented; no `--strip-cookies` option.
- **D6 Symlinks.** One rule in both SDKs (the image zip refuses links
  instead, per `sec-supply-chain-followups`): a symlink found
  inside a copied directory is skipped (file or directory), as Docker and
  `listFilesRecursively` do. A top-level `CopyStep.src` that is itself a
  symlink is still resolved and containment-checked.
- **D7 Git cleanup.** The credentialed `set-url` and the operation share one
  `try`; on failure the restore is attempted and any `Exception` from it is
  suppressed; on success a restore failure propagates. Every failed restore
  logs `CREDENTIALS_MAY_REMAIN_MESSAGE` (logger `rayito.git`, TS: the
  injected `logger.warn`) with only the action. A clone that fails with
  anything other than a git exit tries the strip (git already removes what
  it created on a non-zero exit, and the destination may be someone else's).
- **D8 Git isolation.** Credentialed invocations get
  `-c core.hooksPath=/dev/null -c credential.helper=` and a preflight
  `git config --get-regexp '^url\..*\.(push)?insteadof$'` (exit 1 = none;
  output = refuse with `GitAuthException`/`GitAuthError` before sending the
  token; any other exit propagates). Not done (needs a product decision):
  `GIT_CONFIG_GLOBAL=/dev/null` (drops `user.name/email` that `pull` may need
  for a merge commit) and running git without the login shell (changes the
  process environment `rayd` gives the command; needs AWS verification).
  Neither would stop a same-uid process watching `/proc`, which is why the
  docs now say to treat the token as revealed.
- **D9 Terminal output.** `visible_controls()` escapes C0 (except tab and
  newline), DEL and C1 as `\xNN`; `echo()` applies it only when the stream
  is a TTY. JSON output is already escaped. `exec`/`connect` passthrough is
  untouched.
- **D10 Logs fallback.** Only names matching
  `^\d{4}/\d{2}/\d{2}\[<version>\]<id>$` with a day not before the
  sandbox's UTC start; the command warns on stderr whenever the result is
  not exactly the expected stream.
- **D11 AWS errors.** Python stacks: `raise _wrap(exc) from
  sanitize_aws_error(exc)`. `submit_build`: non-quota `ClientError` →
  `BuildException(reason="aws_error")` from the summary. TS: `wrap()` uses
  the summary as `cause`; every SDK call of `AwsBuildClients` goes through
  `sanitizedCall` (the summary keeps `name`, so `awsCode` still works);
  `submitBuild` mirrors Python. `client_error_message` redacts.
- **D12 Output cap.** `DecodedStream` keeps a deque of `(text, bytes)` and
  trims from the front to `max_bytes` (received bytes), counting
  `dropped_bytes`; `0` keeps nothing. `COMMAND_OUTPUT_MAX_BYTES` = 64 MiB in
  `limits.json`. `CommandResult.truncated` (Python field, TS optional `true`)
  and `CommandExitException/CommandExitError.truncated`. PTY handles keep
  the default cap (their `stdout` is a public property, so not removed).
  `run_code` output (`Execution.logs`) is out of scope.
- **D13 Tokens.** `ACCESS_TOKEN_MIN_BYTES` = 16 checked in
  `decode_access_token`/`decodeAccessToken` after the canonical-base64url
  check, without echoing the token. `ProxyToken.jwe` is `field(repr=False)`
  / `defineHidden`; `DoctorContext.minted_token` is `repr=False`.
- **D14 One message per proxied connection.** Pure framing rules in
  `cli/_http_framing.py` (RFC 9112 §6.3); `_forward` relays the first
  request's body exactly (`Content-Length`, or the `chunked` framing chunk
  by chunk) and then stops reading the client. Non-upgrade responses are
  piped until the upstream closes (it got `Connection: close`). Upgrade
  responses are parsed: `1xx` pass, `101` tunnels, anything else is
  forwarded with `Connection: close` and a delimited body, then both sides
  close. Ambiguous request framing is `400` (the proxy is an intermediary
  and must not pick one interpretation). Re-parsing and re-rewriting each
  pipelined request was rejected: more code for a client pattern browsers
  do not use with `Connection: close`.
- **D15 `.dockerignore`.** Docker (moby `patternmatcher`) semantics in a
  pure module per SDK, matched by segments without regular expressions
  (polynomial even for hostile patterns; consecutive `**` collapse). A
  `**` glued to other characters inside a segment is a `*`, as in
  `.gitignore` (moby's regex would let it cross `/`; documented edge).
  `*` no longer crossing `/` is a behaviour change that could package a
  nested file an old pattern excluded, so the SDKs warn when likely secrets
  are packaged; the warning never excludes (the user's `.dockerignore`
  stays the only source of truth). Shared vectors keep the SDKs in parity.

