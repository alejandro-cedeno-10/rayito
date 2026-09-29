## MODIFIED Requirements

### Requirement: Health.kernel_ready reflects the default kernel
`HealthResponse.kernel_ready` SHALL be true only while the sidecar is running, has reported `ready`, the default context is not being restarted and no relaunch is in progress; it SHALL be false during boot, during the `/run` rotation, after a sidecar exit and with `--no-sidecar`. `kernel_state_lost` SHALL stay false in M4. The SDK's `create()` and `connect()` SHALL wait for `agent_ready and kernel_ready` before returning; `is_running()` SHALL keep meaning `agent_ready`.

The `/run` rotation SHALL be visible synchronously: the instant `/run` is confirmed installed (before any `await`, in particular before egress enforcement), `rayd` SHALL mark the default kernel `Rotating` if it currently reads `Ready`, so no readiness probe racing the rest of `/run` can observe the previous sandbox's kernel as ready. The kernel's actual restart request SHALL still be free to run later (after egress enforcement settles, so the restarted kernel picks up the settled proxy environment); the eager mark SHALL NOT cause the restart to be skipped.

#### Scenario: ready right after create
- **WHEN** `Sandbox.create()` returns against the M4 image
- **THEN** a `Health` call reports `kernel_ready == True` and the elapsed time from `run-microvm` is at most 15 s (logged)

#### Scenario: not ready while rotating
- **WHEN** an integration test observes `Health` right after `/run` was acknowledged and before the fake sidecar answered `restart_context`
- **THEN** `kernel_ready` is false and becomes true after the reply

#### Scenario: the rotation window is closed synchronously
- **WHEN** a unit test calls `SidecarSupervisor::request_rotation` on a supervisor whose default kernel currently reads `Ready`, and asserts on `state()` with nothing awaited in between
- **THEN** the state already reads `Rotating` (and `kernel_ready()` already `false`), and a later `await` still lets the real restart request reach the sidecar (`restart_context` is sent)
