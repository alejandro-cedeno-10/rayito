## MODIFIED Requirements

### Requirement: Default identity is uid 1000 and root is refused unless the image allows it
`rayd` SHALL run every process as the user named by `StartRequest.user.username`, falling back to the `user` field of the `/run` payload and then to `"user"`; the identity (uid, gid, supplementary groups from `getgrouplist`, home) SHALL be resolved from the image's user database before `fork`. The identity gate SHALL be a positive check, not a blacklist of root: after the lookup, `UserPolicy::authorize_identity` SHALL accept an identity only when `uid >= 1000`, `gid >= 1000` and no supplementary group is `0`. `"root"` and any alias of uid 0 SHALL be refused with `PERMISSION_DENIED` and the existing root message unless `rayd`'s own environment contains `RAYITO_ALLOW_ROOT=1`, which is an image-level opt-in read once at boot and never influenced by a request, and which bypasses the whole gate for process spawning, PTYs, the filesystem service and code execution. Every other privileged account — a system uid below 1000, a gid below 1000, or membership of group 0 — SHALL be refused with `PERMISSION_DENIED` and a message that says only unprivileged accounts may run code, never quoting the requested username. The single gate SHALL live in `crates/rayd-core/src/process/identity.rs` so process spawning, PTYs, the filesystem service, code execution and persistence inherit it from one place. Persistence SHALL NOT inherit the image opt-in: `resolve_home_identity` SHALL resolve its identity with `UserPolicy::without_root()` — a four-line helper returning the same policy with `allow_root` cleared — so root and every privileged account are refused there even when the image sets `RAYITO_ALLOW_ROOT=1`, which is what keeps T15's promise that persistence never archives `/root`. `crates/rayd-core/src/persistence/mod.rs` SHALL NOT carry a duplicated `uid == 0` check: with the opt-in dropped the shared gate refuses everything that check refused and, unlike it, every other privileged account too. Unknown usernames SHALL fail with `INVALID_ARGUMENT`. The privilege drop SHALL happen in the child, after the resource limits are set, as `setgroups`, `setgid`, `setuid` in that order.

#### Scenario: default user
- **WHEN** the SDK calls `commands.run("whoami")` and `commands.run("id -u")` without `user`
- **THEN** the outputs are `user` and `1000`

#### Scenario: root refused by default
- **WHEN** the image does not set `RAYITO_ALLOW_ROOT=1` and the SDK calls `commands.run("whoami", user="root")`
- **THEN** `Start` fails with `PERMISSION_DENIED` and the SDK raises `AuthenticationException` with `proxy_rejected == False`

#### Scenario: a system account is refused everywhere
- **WHEN** the user lookup resolves the requested name to uid 11 with gid 0 (a system account such as `operator`), to uid 1 with gid 1 (`bin`), to uid 1000 with gid 0, or to uid 1000 whose supplementary groups contain 0
- **THEN** `plan_spawn`, `plan_pty`, the filesystem identity resolution, the code-execution identity and `resolve_home_identity` all refuse it with the privileged-account error, mapped to `PERMISSION_DENIED` (and to the `permission_denied` stream code for persistence), and the message never contains the username

#### Scenario: the image opt-in still works, except for persistence
- **WHEN** `rayd` runs with `RAYITO_ALLOW_ROOT=1`
- **THEN** process spawning, PTYs, the filesystem service and code execution accept uid 0 and the system accounts above exactly as before this change, while `resolve_home_identity` refuses `root`, an alias of uid 0 and a system account such as `operator` with `RootNotAllowed` / `PrivilegedAccount`, because persistence resolves with `UserPolicy::without_root()`

#### Scenario: rayd started unprivileged
- **WHEN** `rayd` starts with `geteuid() != 0` (developer machine or CI)
- **THEN** it logs one warning at boot, runs processes as its own user, and still applies resource limits clamped to its hard limits

### Requirement: Cross-process connect honours the access token
`Sandbox.connect(sandbox_id, access_token=...)` from another process SHALL be able to run commands; a wrong token SHALL make every `ProcessService` RPC fail with `UNAUTHENTICATED`, surfaced as `AuthenticationException`. `RAYITO_ACCESS_TOKEN` SHALL keep working as the fallback for `connect()` and for `create()`, but the SDK SHALL log exactly one `WARNING` per process, the first time `create()` falls back to that variable, stating that every sandbox created by this process then shares one secret and that passing `access_token=` per call gives each sandbox its own. The warning SHALL name only the variable — never the token, a prefix of it, or any `envs`/`metadata` value — and the `connect()` path SHALL NOT warn, because reading the variable there is its documented purpose.

#### Scenario: right and wrong token
- **WHEN** another process connects with the sandbox's token and runs `echo hola`, and a third connects with a different valid base64url token and runs `true`
- **THEN** the first succeeds and the second raises `AuthenticationException`

#### Scenario: rejection reaches the client as a gRPC status through the proxy
- **WHEN** a request without a valid `x-access-token` arrives through the AWS proxy
- **THEN** `rayd` reads the request body to its end (bounded to 1 MiB / 2 s) before answering, so the client receives `UNAUTHENTICATED` rather than the `CANCELLED` the proxy produces when the app resets a half-open stream

#### Scenario: the shared-token warning fires once
- **WHEN** `RAYITO_ACCESS_TOKEN` is set and the launch plan resolves the access token twice in the same process
- **THEN** both resolutions return the same token and exactly one `WARNING` naming `RAYITO_ACCESS_TOKEN` was logged

#### Scenario: explicit, generated and connect paths stay silent
- **WHEN** the token is passed explicitly, or no variable is set and a fresh 32-byte token is generated, or `connect()` reads the variable
- **THEN** nothing is logged
