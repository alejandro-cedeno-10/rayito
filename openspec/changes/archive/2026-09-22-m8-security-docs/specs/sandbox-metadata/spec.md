## MODIFIED Requirements

### Requirement: Metadata is not secret and is never logged
Metadata SHALL be documented as non-secret labels (it travels in `runHookPayload` like `envs` and is readable through `Health` by any principal able to mint a proxy JWE for the MicroVM). Neither `rayd` nor the SDK SHALL write metadata keys or values to logs, exceptions or `debug_error_string`; `SECURITY.md` T4 and T9 SHALL mention `metadata` next to `envs`. `SECURITY.md` T4 and `docs/site/docs/security.md` SHALL additionally name the reader closest to the data — the sandbox workload itself: the gRPC listener is `0.0.0.0:8080` and `Health` is the only anonymous RPC (ADR-004), so a process at uid 1000 inside the MicroVM reads `sandbox_id` and the whole `metadata` map with no proxy JWE, no IAM permission and no access token. Both texts SHALL present that as accepted rather than fixed (splitting the `Health` response would break the readiness probe and the 0.2.0 `.proto` contract), so the only control is what the operator decides to put in `metadata`.

#### Scenario: logging allowlist
- **WHEN** the `rayd` logging allowlist test runs after a `/run` with metadata
- **THEN** no log line contains a metadata key or value, and the `/run` line reports `metadata_keys` as a count

#### Scenario: the in-VM reader is documented
- **WHEN** `scripts/tests/test_security_docs.py::test_metadata_is_readable_from_inside_the_vm` reads the T4 row of `SECURITY.md` and the section "Qué no poner en `envs` ni en `metadata`" of `docs/site/docs/security.md`
- **THEN** both state that the code running inside the sandbox reads `metadata` through the anonymous `Health` with no credential, and neither presents the readers as IAM principals only
