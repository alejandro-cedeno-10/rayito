## MODIFIED Requirements

### Requirement: LifecycleService.SetTimeout moves the deadline
`proto/rayito/v1/lifecycle.proto` SHALL define `LifecycleService.SetTimeout(SetTimeoutRequest{timeout_ms, mode}) returns (LifecycleState)`. The RPC SHALL require `x-access-token`, so the sandbox's own code cannot extend itself. The two modes SHALL behave as follows:

- `TIMEOUT_MODE_EXACT` SHALL set `deadline = now + timeout`, and MAY shorten it.
- `TIMEOUT_MODE_AT_LEAST` SHALL set `deadline = max(deadline, now + timeout)` while `ACTIVE`, and `now + timeout` from `RESUME_GRACE` or `EXPIRED`.
- Either mode SHALL move the phase to `ACTIVE` and count one extension when the deadline changed.

Errors, in the order they are judged. The gRPC adapter SHALL only parse the contract (the `mode` enum) and map the domain's errors; every rule on `timeout_ms` SHALL be the domain's (`rayd_core::sandbox_timeout`):

- `mode` `UNSPECIFIED` SHALL fail with `INVALID_ARGUMENT` before any phase check (it is contract parsing).
- A sandbox without a lifecycle SHALL answer `FAILED_PRECONDITION` "lifecycle_unmanaged", whatever `timeout_ms` is (0 included).
- A kill-mode sandbox already terminating SHALL answer `FAILED_PRECONDITION` "sandbox_timeout", whatever `timeout_ms` is (0 included). The deadline gate admits `SetTimeout` in that phase (reported `EXPIRED`), so this answer comes from the domain.
- `timeout_ms` below 1000, 0 included, SHALL fail with `INVALID_ARGUMENT` only after the phase checks, with one message for every value below one second: "el timeout debe ser de al menos 1 s".
- A target beyond the cap SHALL fail with `INVALID_ARGUMENT` "timeout beyond cap; cap_unix_ms=<n>" and change nothing.

#### Scenario: exact can shorten, at-least cannot
- **WHEN** a sandbox with 600 s left receives `SetTimeout(EXACT, 10 000)` and later `SetTimeout(AT_LEAST, 5 000)` with 8 s left
- **THEN** the deadline moves to now + 10 s, the second call leaves it unchanged, and `extensions == 1`

#### Scenario: beyond the cap
- **WHEN** a sandbox launched with `cap_s = 900` receives `SetTimeout(EXACT, 2 000 000)`
- **THEN** the RPC fails with `INVALID_ARGUMENT` whose message starts with `timeout beyond cap` and carries `cap_unix_ms`, and `Health.lifecycle.deadline_unix_ms` is unchanged

#### Scenario: token required
- **WHEN** `SetTimeout` arrives without `x-access-token`
- **THEN** it fails with `UNAUTHENTICATED` before the service runs

#### Scenario: a zero timeout follows the phase rules
- **WHEN** `SetTimeout(EXACT, 0)` reaches a sandbox without a lifecycle, and `SetTimeout(AT_LEAST, 0)` and `SetTimeout(AT_LEAST, 999)` reach an active one
- **THEN** the first fails with `FAILED_PRECONDITION` "lifecycle_unmanaged", the other two fail with `INVALID_ARGUMENT` and the same message, and the deadline and `extensions` are unchanged
