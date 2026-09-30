## Context

Phase 1 of `docs/research/2026-10-e2b-out-of-scope.md` §2 (options 1 + 2),
narrowed by the architect: delivery reuses the per-call `envs` of
`ProcessService.Start` (process.proto:33), `PtyService` (pty.proto:32) and
`CodeService` `ExecuteRequest`/`CreateContextRequest` (code.proto:55/40)
instead of a new `SecretsService` push RPC, so there is no rayd change and
no vault inside the guest.

## Decisions

### D1. Delivery over existing `envs`, no new RPC

The per-call `envs` already travel over TLS to the AWS proxy plus
`x-access-token`, rayd builds the child environment from scratch (hard rule
6) and never logs `envs`. A push RPC would add a vault in rayd (and to the
suspend snapshot) for no gain in phase 1: the value is readable by uid 1000
either way. Consequence: `run_code` can only carry secrets into Python
contexts (rayd rejects per-execution `envs` elsewhere); call-level
`secrets=` on another language is an `InvalidArgumentException` pointing at
`create_code_context(secrets=)`, and handle-level secrets are simply not
added to non-Python cells (string context ids are sent as-is: rayd answers
`INVALID_ARGUMENT` for a non-Python one).

### D2. The handle stores references, the cache stores values

`SecretBinding` (Python) / `SecretEnvs` (TypeScript) keep ENV → `SecretRef`
and the cache; `LaunchOptions.secrets` (Python) / `LaunchContext.secrets`
(TypeScript) keep references for `reincarnate()`. Values live only inside
`SecretCache`, whose `repr`/`toJSON`/`inspect` print `***`.

### D3. Resolve before launching

`create()`/`connect()`/`take()` resolve the handle's secrets first (and fix
the cache in the binding): a missing secret fails before `run-microvm` or
before a pool slot is claimed, and the next commands are cache hits.

### D4. Cache key and single flight

Key = (region, credentials identity — the boto3 session object / the TS
credentials provider object —, resolved `SecretId`, `VersionId` or
`VersionStage`). Python: one `threading.Lock` per key plus a generation
counter so an `invalidate()` during a fetch wins; `aget` returns hits on the
loop and runs misses in `asyncio.to_thread`. TypeScript: one shared promise
per key. Not-found results are never stored.

### D5. Version and metadata encoding

`ClientRequestToken` = `rayito-secret-version-{n:020d}` (42 chars); the
current version is the `VersionId` carrying `AWSCURRENT`. Metadata is
`rayito:v1:` + compact sorted JSON in `Description` (≤ 2048). Values are
always `SecretString`.

### D6. Errors never name the secret

Secret names are confidential selectors (E2B): messages name the env key or
the IAM action, never the name or the value; AWS messages are dropped and
only the sanitized code survives as `__cause__`/`cause`.

### D7. E2B shim divergences are written, not approximated

`secret_id` is the ARN; names validated like E2B's API before any call;
metadata ≤ 2048; `fill()` returns the literal placeholder but nothing
resolves it; `iam_token` stays unimplemented; `SecretException` extends
`SandboxException` (TS: `SandboxError`). Parity row 90 is "divergente", not
"implementado": its Volume half has no API.

## Risks

- Users may treat env injection as safe: first-use `RayitoCompatWarning`,
  docs admonition, T18.
- Values in the suspend snapshot (SEC-5 open).
- SEC-9 (recreate timing, token clash code) is measured in the e2e before
  archive.
