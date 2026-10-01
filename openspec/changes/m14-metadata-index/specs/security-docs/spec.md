## ADDED Requirements

### Requirement: The threat model covers the metadata copy at rest (T19)
`SECURITY.md` SHALL have a row T19 for the optional metadata index stating what is copied (only the non-secret metadata and immutable launch facts; never the access token, envs or secrets), that a forged row never creates a phantom sandbox (the join starts from `list-microvms` and checks image and `startedAt`), that a writer can relabel how a real sandbox appears (metadata is not an access control), that the writer (`RayitoIndexWriter`, `PutItem`) and reader (`RayitoIndexReader`, `BatchGetItem`) roles are separate, and that rows expire by TTL and are filtered on read. `docs/site/docs/security.md` SHALL summarise it.

#### Scenario: the T19 row exists
- **WHEN** `scripts/tests/test_metadata_index_template.py::test_security_threat_model_has_t19` runs
- **THEN** exactly one `| T19 |` row exists and it mentions "nunca", "access token", "fantasma", `RayitoIndexWriter` and `RayitoIndexReader`
