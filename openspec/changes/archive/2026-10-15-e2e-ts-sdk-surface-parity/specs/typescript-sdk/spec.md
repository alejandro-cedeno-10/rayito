## ADDED Requirements

### Requirement: TypeScript e2e covers the SDK surface that Python covers
The TypeScript e2e suite SHALL contain a test for every behaviour that the Python e2e suite checks and that the TypeScript SDK implements itself: the two-level auth of the transport, `commands` (envs, cwd, callbacks, background with tag, list, kill, `sendStdin`, `connect` with `fromSeq`, large output, long keepalive, `getHost`, metrics, impostor token), `files` (batch write, streaming read, list with types and symlink, remove, `watchDir` events, errors, timeout and recursion), `runCode` (stderr, `onStdout`, timeout with kept state, isolated contexts with `cwd` and `envs`, per-run `envs`, restart and remove with their errors, `KernelDied` recovery), `cpuTimeLimit` and `egress`, the upload size caps, the older-agent gate and the E2B shim lifecycle, language and network gates. Behaviour that belongs to the guest, `rayd` or the image SHALL stay tested once, in Python. Every e2e file SHALL be skipped, with no AWS call, when `RAYITO_E2E` is not `1`, and cases that need an optional resource SHALL be skipped with the variable named in the title.

#### Scenario: no AWS call without the flag
- **WHEN** the e2e project runs without `RAYITO_E2E=1`
- **THEN** every new test is reported as skipped and no AWS client is created

#### Scenario: optional gates name their variable
- **WHEN** `RAYITO_EGRESS_CONNECTOR_ARN` or `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION` is unset
- **THEN** the matching test is reported as skipped and its title names the variable

#### Scenario: a forged proxy token is told apart
- **WHEN** the transport test presents a malformed JWE
- **THEN** the call fails with a proxy 403 that `isProxyForbidden` recognises, and an anonymous `ProcessService.List` fails with `UNAUTHENTICATED`
