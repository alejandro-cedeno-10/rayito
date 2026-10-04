## MODIFIED Requirements

### Requirement: SECURITY.md names CallerPolicy as the publisher policy
The IAM section of `SECURITY.md` SHALL introduce `SandboxLauncherPolicy` as the policy of the service that launches sandboxes (runtime verbs, image reads, `iam:PassRole` on the execution role only, and never an image write, the build role or an artifact upload), `ImagePublisherPolicy` as the policy of the **publisher**, and `CallerPolicy` as their union, kept for existing setups: it publishes images with `rayito image publish` / `prune` and launches sandboxes, and SHALL NOT be attached to an application server, which gets `SandboxLauncherPolicy`. The `CallerPolicy` bullet SHALL NOT enumerate individual image verbs, so that removing `lambda:DeleteMicrovmImage` from `infra/iam.yaml` needs no further documentation change.

#### Scenario: the IAM bullet says publisher and offers the runtime-only policy
- **WHEN** `scripts/tests/test_security_docs.py::test_caller_policy_is_the_publisher_policy` reads the IAM section of `SECURITY.md`
- **THEN** the `CallerPolicy` bullet says it is the union, names `rayito image publish` / `prune`, warns against attaching it to an application server and names `SandboxLauncherPolicy`, and the `SandboxLauncherPolicy` bullet says it never creates or updates images

## ADDED Requirements

### Requirement: SECURITY.md T22 says only the MAC authenticates a lifecycle event
`SECURITY.md` SHALL carry a T22 row for lifecycle events and webhook delivery that states the log stream name is not proof of identity (holders of the execution role or the build role can write any stream), that the MAC is verified before anything is parsed, that events must be fresh, that one bad line never drops its batch, and that delivery dedupe is per sandbox.

#### Scenario: the T22 row is pinned
- **WHEN** `scripts/tests/test_security_docs.py::test_t22_says_only_the_mac_authenticates_an_event` reads the T22 row
- **THEN** it holds those statements and not the retired "double identity check" wording

### Requirement: SECURITY.md carries the M15 threat rows and does not overstate them
`SECURITY.md` SHALL carry exactly one row each for T20 (S3 mounts), T23 (OTLP export), T26 (templates) and T27 (size cost guard). T22 SHALL say that `paused`/`resumed` are advisory (guest code can make `rayd` emit them through the loopback-reachable hooks), how admission bounds them, that webhooks are stack-wide, that receivers dedupe on `(sandbox_id, event_id)`, and that a `rayito-signature` header exists. T27 SHALL name the image-publishing Deny and the `imageVersion` residual.

#### Scenario: the rows are pinned
- **WHEN** `scripts/tests/test_security_docs.py` reads `SECURITY.md`
- **THEN** each of T20, T23, T26 and T27 appears once, and T22 and T27 hold those statements
