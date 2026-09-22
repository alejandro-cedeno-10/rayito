# guest-isolation Specification

## Purpose
TBD - created by archiving change m6-hardening. Update Purpose after archive.
## Requirements
### Requirement: rayd reports its capabilities and the cgroup2 root at boot
At startup `rayd` SHALL parse `CapEff` from `/proc/self/status` and log one `capabilities` event with `net_admin`, `sys_admin`, `sys_resource`, `sys_ptrace` (booleans) and `cgroup2_root` (whether `/sys/fs/cgroup/cgroup.controllers` is readable). cgroup2 slices SHALL NOT be implemented or emulated in this change: if `cgroup2_root` is `false` on the default image the fact is recorded as a platform limit in `SECURITY.md` T7 and `AWS_API_NOTES.md` Q47 (the design's Q43; the §16 numbers were taken by parallel tracks and reassigned at write time); if it is `true` on the capabilities variant the fact is recorded and slices are left to a later change.

#### Scenario: default image has no net_admin
- **WHEN** the acceptance reads the build log of `rayito-base` 11.0
- **THEN** the `capabilities` line shows `net_admin: false`, `sys_admin: false`, `sys_resource: false` and `cgroup2_root: false` (Q20 re-measured), and the M0 mask parses the same way in a host test

### Requirement: IMDS is blocked for uid 1000 when CAP_NET_ADMIN is present
When `CapEff` includes `CAP_NET_ADMIN`, `rayd` SHALL install a policy route before the listeners start (so it is part of the memory snapshot): `ip -4 rule add uidrange 1000-65535 lookup 100 priority 100` (only when `ip -4 rule show` does not list it) and `ip -4 route replace blackhole 169.254.169.254/32 table 100`, plus the same pair for `fd00:ec2::254/128` as best effort. The mechanism replaces the `iptables -m owner` rule of the design because the guest kernel has no `xt_owner` match (measured, Q48) and the uid range starts at 1000 because the platform's in-VM agent owns sockets as uids 991-994 with its own connection to `169.254.169.254:80` (a blackhole for every non-root uid made the image build's `/validate` time out, measured, Q48). `rayd` SHALL re-check both halves with `ip -4 rule show` / `ip -4 route show table 100` at the first accepted `/run` (re-installing them if absent) and at every `/resume` (clearing `imds_blocked` and logging `imds_rule_missing` if they vanished), and SHALL verify after `/run`, in a background task bounded by 10 s, that a TCP connect to `169.254.169.254:80` succeeds as root (connect only, no request) and fails as uid 1000 through the process spawner. `imds_blocked` SHALL be `true` only when the route is present, the root connect succeeded and the uid-1000 connect failed; the result SHALL be logged as `imds_probe` with `rule_present`, `root_reachable`, `user_reachable`, `imds_blocked`. `ip` output and the probes' output SHALL never be logged; `rayd` SHALL never read credentials from IMDS.

#### Scenario: credentials unreachable from the sandbox
- **WHEN** the e2e creates a sandbox from the capabilities variant with an execution role and waits for readiness
- **THEN** `get_health().imds_blocked` is `True` within 10 s, a `PUT /latest/api/token` from `commands.run` as uid 1000 fails (non-zero exit within 5 s), and `rayd`'s `imds_probe` line in CloudWatch shows `root_reachable: true`

#### Scenario: default image is fail-open
- **WHEN** the same probe runs on a sandbox from the default image
- **THEN** `imds_blocked` is `False`, the PUT succeeds (exit 0) and `rayd` logged `imds_block_unavailable` at boot

#### Scenario: rule installation fails
- **WHEN** `ip` exits non-zero or is not on the PATH (nor at `/usr/sbin/ip`) while `CAP_NET_ADMIN` is present
- **THEN** `rayd` logs `imds_block_unavailable` with the step and exit code as `reason` (`ip -4 rule add exit 2`, `ip not found`), keeps serving, and `imds_blocked` stays `false`

### Requirement: Health exposes imds_blocked and the SDK warns when a role is exposed
`HealthService.Health` SHALL report `imds_blocked` (`HealthResponse` field 9, `bool`, `false` until verified). The SDK SHALL expose it as `SandboxHealth.imds_blocked` and SHALL log a warning once when `imds_blocked` is `False`, the sandbox was created with an `execution_role_arn`, and the verification window of `rayd` has elapsed: the warning SHALL be evaluated only on a `Health` whose `uptime_ms` is at least 10 000 ms (`IMDS_VERIFY_BUDGET_MS`, mirroring `rayd`'s `IMDS_VERIFY_BUDGET`) past the `uptime_ms` of the first `Health` the SDK recorded (the readiness one, which arrives after the `/run` that starts the verification). Before that a `False` means "not verified yet" (measured on the capabilities image: `imds_blocked=True` 0.10 s after `kernel_ready`), so the readiness poll SHALL never produce the warning.

#### Scenario: warning only with a role and after the window
- **WHEN** the unit fake reports `imds_blocked: false` to a sandbox created without a role and to one created with a role, first with the readiness `uptime_ms` and then with an `uptime_ms` 10 000 ms past it
- **THEN** neither sandbox logs the warning while the uptime is inside the window, only the second sandbox logs it once the window elapsed, exactly once across repeated `get_health()` calls, and a later `imds_blocked: true` adds no warning

#### Scenario: verified inside the window never warns
- **WHEN** the unit fake flips `imds_blocked` to `true` 100 ms after the readiness `Health` of a sandbox created with a role, and `get_health()` is called again past the window
- **THEN** no warning is logged

### Requirement: The capabilities image variant is published under its own name
`scripts/publish_image.py --os-capabilities ALL` SHALL add exactly `"additionalOsCapabilities": ["ALL"]` to the image configuration (the only value the service model accepts) and nothing else; the Makefile target `image-publish-caps` SHALL publish it as `rayito-base-caps` from the same Dockerfile and artifact so `rayito-base`'s version history stays homogeneous. If the variant never becomes launchable or a MicroVM from it never reaches `agent_ready` within 90 s, the acceptance SHALL record the `stateReason`/`Health` outcome as Q48 (the design's Q44), keep `rayito-base` as the only supported image and leave `SECURITY.md` T1 open with that measurement.

#### Scenario: configuration delta
- **WHEN** the scripts unit test builds `desired_configuration` with and without `--os-capabilities ALL`
- **THEN** the two dicts differ only by the key `additionalOsCapabilities` with value `["ALL"]`

