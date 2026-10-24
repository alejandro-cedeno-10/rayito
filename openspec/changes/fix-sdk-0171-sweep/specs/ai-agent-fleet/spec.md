## ADDED Requirements

### Requirement: A member whose probe fails while listing is treated as terminating
When a fleet lists its members (`AgentFleet.connect()`, `status()`, `refresh_members()` / `refreshMembers()`, sync, async and TypeScript), each `RUNNING` MicroVM SHALL be probed on its own. A MicroVM whose probe fails with a `SandboxException` / `SandboxError`, or that is no longer `RUNNING` when probed, SHALL NOT be a member, SHALL NOT count for capacity and SHALL NOT make the image count as shared, and the listing SHALL NOT fail because of it; a warning SHALL name its id and the error class only. A rate limit or a timeout of the control plane (`RateLimitException`/`RateLimitError`, `TimeoutException`/`TimeoutError`) and any error outside `SandboxException`/`SandboxError` SHALL still fail the listing. The pure selection SHALL be tested in both SDKs with `testdata/agent-fleet/members.json`.

#### Scenario: one member is being terminated
- **WHEN** a fleet has two `RUNNING` members and a suspended one, and the `Health` of one running member fails
- **THEN** `connect()` and `refresh_members()` succeed, the fleet has the other running member and the suspended one, and `status()` does not list the failing one

#### Scenario: a member left RUNNING before its probe
- **WHEN** a VM listed as `RUNNING` is `TERMINATING` when probed
- **THEN** it is not a member

### Requirement: A fleet never terminates a MicroVM it could not prove is a member
When the probe of a `RUNNING` MicroVM fails while listing, the MicroVMs of that listing that are not `RUNNING` SHALL count as members for capacity but SHALL be unproven: `stop()`, `reconcile()` (trimming) and `rayito agent stop <name>` SHALL NOT terminate an unproven member, SHALL log a warning with its id only, and SHALL leave it to its own deadline and idle policy; `stop()` SHALL return the number of MicroVMs it terminated, and the CLI SHALL print that number. A member created by this process, or proven by a later listing, SHALL be proven. The `RUNNING` MicroVMs SHALL be probed with at most `AGENT_FLEET_PROBE_CONCURRENCY` (8) at a time, in both SDKs (threads in Python sync, the event loop in Python async and TypeScript), and the selected members and the warnings SHALL keep the listing order; when a probe fails the listing, no further probe SHALL start.

#### Scenario: a foreign sandbox's probe fails during stop
- **WHEN** the image has a fleet VM, a foreign `RUNNING` sandbox whose probe fails and a foreign suspended sandbox, and `connect(name).stop()` runs
- **THEN** only the fleet VM is terminated, the suspended sandbox is untouched and a warning names its id

#### Scenario: bounded concurrent probes
- **WHEN** a fleet lists ten `RUNNING` VMs whose probes finish in reverse order
- **THEN** at most eight probes run at once and the warnings follow the listing order

### Requirement: The deploy that asks for key renewal always writes a fresh key
With `renew_gateway_keys=True` / `renewGatewayKeys: true` / `--renew-gateway-keys`, `deploy_agent()` / `deployAgent()` SHALL write a fresh short-term key to the secret of every `bedrock` gateway once, before the first VM, without reading `LastChangedDate`. The renewals before each later VM, and those of `AgentFleet.connect(name, renew_gateway_keys=True)`, SHALL keep the `gatewayKeyRenewAfterSeconds` window.

#### Scenario: a secret created just before the deploy
- **WHEN** a secret changed 10 seconds ago and a deploy with renewal starts
- **THEN** one `PutSecretValue` writes the key before any VM is created and the renewals of the VMs that follow write nothing

### Requirement: A reconnected fleet creates VMs with its members' image version
When a fleet has no pinned image version (no build in this process and no recipe), it SHALL adopt the image version of its newest listed member, in both SDKs, sync and async, and SHALL create new VMs and warm slots with it instead of the image's newest version. A version already pinned SHALL NOT change.

#### Scenario: the newest version of the image is another build
- **WHEN** a fleet reconnected with `project=` and no recipe has members on versions 1 and 2 and the image's newest version is 3, and it grows by one VM
- **THEN** the new VM is created with version 2

### Requirement: Fleet status reports the real state of running members
`AgentFleet.status()` (sync, async and TypeScript) and `rayito agent status <name>` (text and `--json`, both CLIs) SHALL re-read the listing and SHALL ask `GET /ping` of every `RUNNING` member, including right after `connect()`, with at most `AGENT_FLEET_PROBE_CONCURRENCY` (8) at a time. A member this process has not opened SHALL be asked without being kept: its lifetime SHALL NOT be set and its service SHALL NOT be started. A member that is not `RUNNING` (suspended) SHALL be reported as `unknown`, because asking it would resume it; so SHALL a member that cannot be connected or has no service yet. A member whose `/ping` does not answer SHALL be reported as `unavailable`, and that ask SHALL NOT count towards replacing it. No error while asking a member this process has not opened SHALL fail `status()`.

#### Scenario: status right after connect
- **WHEN** a fleet connects to two `RUNNING` members and a suspended one and calls `status()`
- **THEN** the two running members are `healthy`, the suspended one is `unknown` and was not connected, and no `set_timeout` was sent

### Requirement: The missing-lifetime warning only when the fleet can grow
When `rayd` rejects the `set_timeout` of a member because it has no logical lifetime, the fleet SHALL mark the member and SHALL NOT retry, and SHALL log `UNMANAGED_LIFETIME_MESSAGE` only when the fleet can grow (`max_vms > min_vms`). A fixed-size fleet, including one connected with `project=` and no `scaling` (the default limits), SHALL NOT log it, in both SDKs.

#### Scenario: a fixed-size fleet finds a VM without a deadline
- **WHEN** a fleet with `min_vms == max_vms` opens a VM created without a logical lifetime
- **THEN** no warning is logged and the VM's lifetime is not set again
