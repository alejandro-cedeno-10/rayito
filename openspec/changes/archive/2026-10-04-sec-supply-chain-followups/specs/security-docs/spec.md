## ADDED Requirements

### Requirement: The docs state what a release signature proves and verify before publishing
`docs/site/docs/verify.md` SHALL contain a section "Qué prueba la firma" stating that a valid signature proves `release.yml` of this repository signed the file in a run started from the `rayd-v<version>` tag and, for releases after 0.6.1, that the run passed the `release` environment's approval before the OIDC token existed, that a dry run signs nothing, and what the signature does not prove (that the tagged code was reviewed on `main`; signing needs both creating a `rayd-v*` tag and approving the `release` environment). It SHALL NOT claim that uploading requires approval as the only gate. The recommended "Desde la release" recipe of `docs/site/docs/primeros-pasos/configurar-aws.md` SHALL download `SHA256SUMS`, run `cosign verify-blob` with the exact identity `release.yml@refs/tags/rayd-v${RAYD_VERSION}` and a `sha256sum -c` before `rayito image publish`, and SHALL NOT present the verification as optional. `scripts/tests/test_security_docs.py` SHALL assert both.

#### Scenario: verification is optional again
- **WHEN** the recipe moves `cosign verify-blob` after `rayito image publish` or reintroduces "Opcional pero recomendable"
- **THEN** `test_the_release_recipe_verifies_before_publishing` fails

#### Scenario: the overclaim comes back
- **WHEN** `verify.md` again says only that uploading requires the maintainer's approval
- **THEN** `test_verify_states_what_a_signature_proves` fails
