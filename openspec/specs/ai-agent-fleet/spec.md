# ai-agent-fleet Specification

## Purpose
TBD - created by archiving change agent-zero-config. Update Purpose after archive.

## Requirements

### Requirement: ScalingOptions describes how many MicroVMs and how sessions share them
`ScalingOptions` SHALL hold `min_vms`, `max_vms`, `warm_vms`, `sessions` (`shared` or `dedicated`) and `scale_in_after_seconds` (`minVms`, `maxVms`, `warmVms`, `sessions`, `scaleInAfterMs` in TypeScript). Defaults SHALL be one VM, no warm VM and `shared`. It SHALL raise `InvalidArgumentException` for `min_vms > max_vms`, `warm_vms > max_vms - min_vms`, `max_vms` above the pool size limit and `scale_in_after_seconds` below its minimum. Every default and bound SHALL be a named constant from `limits.json`.

#### Scenario: defaults equal a single VM
- **WHEN** `ScalingOptions()` is built
- **THEN** it has one minimum VM, one maximum VM, no warm VM and `shared` sessions

#### Scenario: bounds
- **WHEN** `warm_vms` exceeds `max_vms - min_vms`
- **THEN** construction raises `InvalidArgumentException`

### Requirement: ServeOptions bounds the session cap and adds a queue
`ServeOptions.max_concurrency` SHALL be at most the connection cap of the VM size minus the reserved connections, taken from `limits.json`. `queue_max` and `queue_timeout_seconds` SHALL default to `0` and the documented default, keeping the previous behaviour of answering 429 above the cap. With `queue_max > 0`, the host SHALL hold up to that many invocations in a bounded FIFO for up to the timeout and then answer 429 with `Retry-After`.

#### Scenario: over the cap
- **WHEN** `max_concurrency` is above the cap for the memory size
- **THEN** construction raises `InvalidArgumentException` naming the cap

#### Scenario: queue then 429
- **WHEN** `max_concurrency=1`, `queue_max=1` and three invocations arrive at once
- **THEN** one runs, one waits and the third gets 429

### Requirement: A fleet routes sessions on the client
`deploy_agent()` and `AgentProject.deploy()` with `ScalingOptions` of more than one VM SHALL return an `AgentFleet` (`AsyncAgentFleet`, TypeScript `AgentFleet`) with `invoke`, `stream`, `status`, `scale`, `reconcile`, `stop` and `connect`. With `max_vms > 1`, `AgentResult.session_id` SHALL be a handle `rs1.<sandbox_id>.<inner_session_id>` that routes to that VM without a lookup, and the host SHALL only ever see the inner id. With `max_vms == 1` the session id SHALL be unchanged. No server of Rayito's own SHALL be involved.

#### Scenario: handle routes back
- **WHEN** a caller invokes twice passing the handle returned by the first call
- **THEN** both calls reach the same VM

#### Scenario: single VM is unchanged
- **WHEN** `max_vms` is 1
- **THEN** the returned session id has no prefix

### Requirement: Shared sessions fill a VM and dedicated sessions never share one
In `shared` mode a new session SHALL go to a VM with free capacity (from `/ping`, cached for the named TTL), then to a warm VM, then to a new VM below `max_vms`, and otherwise queue up to the timeout and raise `CapacityException(code="agent_capacity")`. In `dedicated` mode every new session SHALL get its own MicroVM and a VM SHALL never serve two session ids.

#### Scenario: second VM
- **WHEN** `max_concurrency=1`, `max_vms=2` and two sessions start at once in `shared` mode
- **THEN** they run on two different VMs

#### Scenario: dedicated
- **WHEN** two sessions start in `dedicated` mode
- **THEN** their handles name two different sandbox ids

### Requirement: Fleet membership is the project's image
Members of a fleet SHALL be the non-terminated MicroVMs of the project's image, found by listing, and suspended members SHALL be found without resuming them. `AgentFleet.connect(name, access_token=, region=)` SHALL rebuild membership from that listing in a new process.

#### Scenario: reconnect from another process
- **WHEN** a second process calls `connect` with the token of a running fleet
- **THEN** a handle from the first process routes to the same VM

### Requirement: A fleet never counts or terminates a MicroVM that is not an agent
The image of a fleet MAY also hold MicroVMs that are not agent deployments. A `RUNNING` MicroVM SHALL count as a member only when its labels prove it is an agent deployment of Rayito (read with a tokenless `Health`, which wakes nobody). When the image has a `RUNNING` MicroVM without those labels, no MicroVM that cannot be probed without resuming it (suspended or starting) SHALL count either. `AgentFleet.stop()`, `reconcile()` and `rayito agent stop <name>` SHALL only terminate members.

#### Scenario: a foreign running VM
- **WHEN** the image has one labelled VM and one `RUNNING` VM without the agent labels, and `connect(name).stop()` runs
- **THEN** only the labelled VM is terminated

#### Scenario: a shared image with suspended VMs
- **WHEN** the image has a `RUNNING` VM without the agent labels and a suspended VM
- **THEN** neither is a member and neither is terminated

### Requirement: Project env and network reach every launched MicroVM
`AgentProject.env` SHALL be the `envs` and `AgentProject.network` the `network` of `Sandbox.create()` for `run()`, `deploy()` and the warm pool slots, and nothing is sent when they are empty.

#### Scenario: egress policy is applied
- **WHEN** a project sets `network` with `deny_out` and is deployed
- **THEN** `Sandbox.create()` receives that `network`

### Requirement: Warm VMs and scale-in reuse existing features
Warm VMs SHALL be slots of the existing pool, started with the agent host running, and `PoolConfig` SHALL gain an optional `access_token` whose absence keeps one token per slot. VMs above `min_vms` SHALL carry a sandbox timeout of `scale_in_after_seconds` that the fleet extends with `set_timeout` on use, so an idle extra VM ends by self-termination.

#### Scenario: claim a warm slot
- **WHEN** every running VM is busy and a warm slot exists
- **THEN** the router claims the slot instead of creating a VM

#### Scenario: pool default unchanged
- **WHEN** a pool is built without `access_token`
- **THEN** each slot has its own token as before

### Requirement: An unhealthy VM is replaced
After the named number of consecutive failed pings the fleet SHALL terminate that VM and replace it up to `min_vms`; sessions in flight on it SHALL fail with `SandboxException(code="agent_vm_lost")`.

#### Scenario: lost VM
- **WHEN** a VM fails the named number of pings
- **THEN** it is terminated, a replacement is created and in-flight calls fail with `agent_vm_lost`

### Requirement: The fleet declares its cost and makes no call until it is used
`deploy_agent`, `deployAgent`, `rayito agent deploy` and `AgentFleet` SHALL carry the "Coste y activación" block (running VMs, parked VMs, image build and snapshot storage, optional base publish, optional secret and optional session index). `detect`, `load` and `--dry-run` SHALL make no AWS call and load no AWS peer, with a test in each SDK.

#### Scenario: dry run
- **WHEN** `rayito agent deploy PATH --dry-run` runs with fake AWS clients that fail on any call
- **THEN** it prints the detection, the plan and a cost estimate and exits 0
