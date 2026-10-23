## ADDED Requirements

### Requirement: Deadline changes of a fleet VM are ordered and follow failures
A fleet SHALL order the `set_timeout` calls it makes on each VM (one turn per VM in both SDKs, sync and async): the invocation hold and the scale-in after a release SHALL never be in flight at the same time on the same VM, and the scale-in SHALL be sent only if the VM has no invocation in flight when its turn comes. Every failed invocation (a `429` until the queue gives up, an agent error, a transport failure or any other error, except a lost VM and a cancellation) SHALL release the VM like a successful one, so an extra VM goes back to `scale_in_after_seconds`.

#### Scenario: a release does not cut a hold that started meanwhile
- **WHEN** the release of an invocation on an extra VM has decided to shorten it and, before its `set_timeout` lands, another invocation leases the VM
- **THEN** the hold of the new invocation is the last deadline the VM receives before that invocation ends

#### Scenario: a failed invocation gives the scale-in deadline back
- **WHEN** an invocation on an extra VM fails with a host error or keeps answering `429` until `CapacityException`
- **THEN** the last deadline the VM received is `scale_in_after_seconds`

### Requirement: A shortened VM promoted to base gets its whole life back even without a known cap
When a VM this process shortened becomes a base VM, the fleet SHALL ask for everything up to `rayd`'s cap. With an unknown cap it SHALL ask for `MAX_DURATION_SECONDS` and, when `rayd` rejects it reporting its cap, retry once with that cap. When nothing can be asked (the cap is too close) or `rayd` reports no cap, the VM SHALL stop counting as shortened. The pure function (`restore_seconds` / `restoreMs`) SHALL be tested in both SDKs with the `restoreSeconds` vectors of `testdata/agent-fleet/lifetime.json`.

#### Scenario: unknown cap
- **WHEN** a shortened VM whose cap the fleet does not know is promoted to base and invoked
- **THEN** the VM first rejects `MAX_DURATION_SECONDS` and then receives a deadline at most its cap minus the safety margin, and a later invocation sends nothing more

#### Scenario: shared restore vectors
- **WHEN** every case of `restoreSeconds` is evaluated by each SDK
- **THEN** the result equals `expected` (seconds in Python, milliseconds in TypeScript)
