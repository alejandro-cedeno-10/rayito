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
