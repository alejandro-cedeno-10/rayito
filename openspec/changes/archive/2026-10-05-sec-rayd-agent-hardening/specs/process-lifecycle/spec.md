## MODIFIED Requirements

### Requirement: Default identity is uid 1000 and root is refused unless the image allows it
`rayd` SHALL run every process as the user named by `StartRequest.user.username`, falling back to the `user` field of the `/run` payload and then to `"user"`; the identity (uid, gid, supplementary groups from `getgrouplist`, home) SHALL be resolved from the image's user database before `fork`. The identity gate SHALL be a positive check, not a blacklist of root: after the lookup, `UserPolicy::authorize_identity` SHALL accept an identity only when its uid and gid are both within `MIN_UNPRIVILEGED_ID..=MAX_UNPRIVILEGED_ID` (1000-65535, the range the IMDS blackhole and the egress and DNS `uidrange` rules cover, `SANDBOX_UID_RANGE`, which a unit test pins to the two constants) and no supplementary group is `0`. `"root"` and any alias of uid 0 SHALL be refused with `PERMISSION_DENIED` and the existing root message unless `rayd`'s own environment contains `RAYITO_ALLOW_ROOT=1`, which is an image-level opt-in read once at boot and never influenced by a request, and which bypasses the whole gate for process spawning, PTYs, the filesystem service and code execution. Every other privileged account — a system uid below 1000, a gid below 1000, a uid or gid above 65535, or membership of group 0 — SHALL be refused with `PERMISSION_DENIED` and a message that says only unprivileged accounts may run code, never quoting the requested username. The single gate SHALL live in `crates/rayd-core/src/process/identity.rs` so process spawning, PTYs, the filesystem service, code execution and persistence inherit it from one place. Persistence SHALL NOT inherit the image opt-in: `resolve_home_identity` SHALL resolve its identity with `UserPolicy::without_root()` — a four-line helper returning the same policy with `allow_root` cleared — so root and every privileged account are refused there even when the image sets `RAYITO_ALLOW_ROOT=1`, which is what keeps T15's promise that persistence never archives `/root`. `crates/rayd-core/src/persistence/mod.rs` SHALL NOT carry a duplicated `uid == 0` check: with the opt-in dropped the shared gate refuses everything that check refused and, unlike it, every other privileged account too. Unknown usernames SHALL fail with `INVALID_ARGUMENT`. The privilege drop SHALL happen in the child, after the resource limits are set, as `setgroups`, `setgid`, `setuid` in that order.

#### Scenario: default user
- **WHEN** the SDK calls `commands.run("whoami")` and `commands.run("id -u")` without `user`
- **THEN** the outputs are `user` and `1000`

#### Scenario: root refused by default
- **WHEN** the image does not set `RAYITO_ALLOW_ROOT=1` and the SDK calls `commands.run("whoami", user="root")`
- **THEN** `Start` fails with `PERMISSION_DENIED` and the SDK raises `AuthenticationException` with `proxy_rejected == False`

#### Scenario: a system account is refused everywhere
- **WHEN** the user lookup resolves the requested name to uid 11 with gid 0 (a system account such as `operator`), to uid 1 with gid 1 (`bin`), to uid 1000 with gid 0, or to uid 1000 whose supplementary groups contain 0
- **THEN** `plan_spawn`, `plan_pty`, the filesystem identity resolution, the code-execution identity and `resolve_home_identity` all refuse it with the privileged-account error, mapped to `PERMISSION_DENIED` (and to the `permission_denied` stream code for persistence), and the message never contains the username

#### Scenario: an account above the sandbox range is refused
- **WHEN** the user lookup resolves the requested name to uid 65536, or to uid 1000 with gid 65536
- **THEN** the gate refuses it with the privileged-account error, while uid 65535 is accepted

#### Scenario: the image opt-in still works, except for persistence
- **WHEN** `rayd` runs with `RAYITO_ALLOW_ROOT=1`
- **THEN** process spawning, PTYs, the filesystem service and code execution accept uid 0 and the system accounts above exactly as before this change, while `resolve_home_identity` refuses `root`, an alias of uid 0 and a system account such as `operator` with `RootNotAllowed` / `PrivilegedAccount`, because persistence resolves with `UserPolicy::without_root()`

#### Scenario: rayd started unprivileged
- **WHEN** `rayd` starts with `geteuid() != 0` (developer machine or CI)
- **THEN** it logs one warning at boot, runs processes as its own user, and still applies resource limits clamped to its hard limits
