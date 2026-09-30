## ADDED Requirements

### Requirement: The E2B Secret shim implements E2B 2.51.0 over SecretStore
`rayito.e2b` SHALL export `Secret`, `AsyncSecret`, `SecretInfo`, `SecretPaginator`, `AsyncSecretPaginator`, `SecretException` and `SecretNotFoundException` (TS `rayito/e2b`: `Secret`, `SecretPaginator`, type `SecretInfo`, `SecretError`, `SecretNotFoundError`) with the names, parameters, arity and results of `e2b` 2.51.0 (`e2b/secret/`, PyPI and npm), implemented over `SecretStore`. Names SHALL be validated like E2B's API (1–128 `[A-Za-z0-9_-]`, lowercased, `sec_` reserved) before any AWS call; `secret_id` SHALL be the Secrets Manager ARN; `fill(name)` SHALL return the literal `${e2b.secrets.<name>}` without calling AWS and no SDK path SHALL resolve placeholders inside `envs`; `iam_token` SHALL keep raising `UnimplementedError` with the `iam` reason; E2B connection kwargs SHALL warn with `RayitoCompatWarning` and be ignored; `E2B(...).Secret`/`.AsyncSecret` SHALL be bound to the client's region and session. Every divergence SHALL be written in `docs/site/docs/e2b-compat.md`.

#### Scenario: every E2B method exists with E2B's signature
- **WHEN** the shim's `Secret`/`AsyncSecret` members are compared to the signature table copied from `e2b` 2.51.0
- **THEN** every method exists with the same parameter names and kinds (TS: the same `Function.length`), and `SecretInfo` has the same fields

#### Scenario: fill makes no call and nothing resolves it
- **WHEN** `Secret.fill("openai-key")` is called
- **THEN** it returns `${e2b.secrets.openai-key}` and no Secrets Manager call is made

### Requirement: The parity ledger reflects the secrets shim without overstating it
`docs/site/docs/e2b-parity.md` row 56 SHALL say `.Secret` works; row 80 SHALL read "divergente" naming the ARN `secret_id`, the 2048-character metadata cap, the missing 100-secret cap and that `fill()` is not resolved; row 90 SHALL read "divergente" because `SecretException` extends `SandboxException` and the `Volume*Exception` half has no API; the "qué hacer" footer and the status counts SHALL be updated so `scripts/tests/test_m9_docs.py` stays green (113 rows, valid statuses).

#### Scenario: the ledger test still passes
- **WHEN** `scripts/tests/test_m9_docs.py::test_the_parity_page_has_every_ledger_row` runs
- **THEN** it passes and the status table reads 72 implementado, 20 divergente, 10 fuera por SPEC, 11 imposible
