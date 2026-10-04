## ADDED Requirements

### Requirement: Checkpoint and Restore stay inside the scope the accepted run bound
The `runHookPayload` MAY carry `"persist": {"bucket": "<bucket>", "key_prefix": "<base prefix>"}`; `rayd` SHALL parse it with the same D2 rules as a request's location (both keys required, unknown keys inside ignored, a violation makes the payload invalid without quoting either value) and SHALL store it once in `SandboxSession` at the accepted `/run`; a later `/run` SHALL NOT change it. While a scope is bound, `resolve_location` SHALL answer `OutsideBinding` (gRPC `PERMISSION_DENIED`, `StreamError` code `permission_denied`, the fixed message `la ubicación queda fuera del prefijo de persistencia ligado al sandbox`) to any `Checkpoint` or `Restore` whose bucket differs from the bound one or whose key prefix is neither the bound base nor below it at a `/` boundary, before the lease, the credential probe or any object-store call. Without a bound scope every valid location SHALL resolve as before.

#### Scenario: another tenant's home is out of reach
- **WHEN** a session whose `/run` payload bound `{"bucket": "my-bucket", "key_prefix": "rayito/mine"}` receives `Restore` for `rayito/other`, `Checkpoint` for `rayito/other` and `Checkpoint` for `rayito/mine-evil`
- **THEN** all three answer `PERMISSION_DENIED` before the store records any operation, and a `Checkpoint` for `rayito/mine/agent-7` completes

#### Scenario: an unbound session keeps the old behaviour
- **WHEN** a session whose payload has no `persist` resolves `rayito/other`
- **THEN** the location resolves
