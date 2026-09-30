## 1. Contract (`AWS_API_NOTES.md` §19, hard rule 1)

- [x] 1.1 §19 "Secrets Manager (M13a, contrato de parámetros)": the seven operations and only their used parameters, output fields read, IAM per operation, error codes, limits, prices; verified offline against botocore 1.43.103 and `@aws-sdk/client-secrets-manager` 3.1140.0.
- [x] 1.2 SEC-9 marked "A MEDIR" (recreate after forced delete, token clash, `name` filter semantics) and measured by the e2e.
- [x] 1.3 `test_secrets_store.py::test_every_operation_used_by_both_sdks_is_documented_in_section_19` greps the operations used in `_secrets.py` and the `…Command`s in `src/secrets/*.ts` against §19.

## 2. Native API (Python `_secrets.py`, TypeScript `src/secrets/*`)

- [x] 2.1 `SecretRef`, name resolution (`arn:` as-is, `prefix + name`), version token and metadata codecs (pure).
- [x] 2.2 `SecretStore` CRUD, lazy client, errors by code without names or values, bounded create retry, one-time update-frequency warning.
- [x] 2.3 `SecretCache`: TTL 1..86400 (300), key (region, credentials, SecretId, version), single flight (threading lock per key + `aget` in `to_thread`; shared promise in TS), no negative caching, masked `repr`/`toJSON`/`inspect`.
- [x] 2.4 `SecretException`/`SecretNotFoundException` (TS `SecretError`/`SecretNotFoundError`); `RayitoCompatWarning` moved to `rayito.exceptions`.
- [x] 2.5 TS: optional peerDependency + devDependency + `tsdown` external; loaded only through `loadOptionalPeer`.
- [x] 2.6 Unit tests: `test_secrets_store.py`, `test_secrets_cache.py`; `secrets-store.test.ts`, `secrets-cache.test.ts`, `secrets-adapter.test.ts`.

## 3. Injection (`secrets=` / `secret_cache=`)

- [x] 3.1 Python sync/async: `create()` (and `pool=`), `connect()` both forms, `SandboxPool.take()`, `commands.run`, `pty.create`, `run_code` (Python only), `create_code_context`; `LaunchOptions.secrets` (refs only) for `reincarnate()`.
- [x] 3.2 TypeScript: the same surface through `SecretEnvs` shared by `Commands`, `Pty` and `CodeClient`; `LaunchContext` keeps refs for `reincarnate()`; `Sandbox.attachSecrets` for the pool.
- [x] 3.3 "Coste y activación" block on every option's docstring/TSDoc with a runnable example.
- [x] 3.4 Unit tests: `test_secrets_inject_sync.py`, `test_secrets_inject_async.py`, `test_secrets_pool.py`, `secrets-inject.test.ts` (values reach the four request types, 3 commands = 1 read, env conflicts, non-Python `run_code`, clean `runHookPayload`, `LaunchOptions`/slot records without values, no client/import without secrets, first-use warning).
- [ ] 3.5 e2e `clients/python/tests/e2e/test_secrets_e2e.py` against real AWS (`RAYITO_E2E=1`): CRUD through the shim → `create(secrets=)` → `printenv` ×3 with one `GetSecretValue` → destroy; SEC-9 and SEC-10 recorded. **Gate for archive; not run in this branch (no real AWS).**

## 4. Infra (`infra/secrets-access.yaml`)

- [x] 4.1 Template with `RayitoSecretsReader`/`RayitoSecretsAdmin`, KMS behind `HasKmsKey` + `kms:ViaService`.
- [x] 4.2 `infra/README.md` section (deploy, `$0`, delete) and `make infra-lint` lists the template.
- [x] 4.3 `scripts/tests/test_secrets_template.py`; `cfn-lint` 1.56.3 clean.

## 5. E2B shim

- [x] 5.1 Downloaded `e2b==2.51.0` (PyPI wheel) and `e2b@2.51.0` (npm) and read `e2b/secret/` / `src/secret.ts`; version recorded in `e2b-compat.md`.
- [x] 5.2 Python `e2b/_secret.py` (`Secret`, `AsyncSecret`, paginators), `_unimplemented.py` (Secret removed, `iam` kept), `_client.py` (bound `.Secret`/`.AsyncSecret`), `exceptions.py`, `__init__.py`.
- [x] 5.3 TypeScript `e2b/secret.ts`, `resources.ts` (Secret removed), `unimplemented.ts`, `client.ts`, `index.ts`.
- [x] 5.4 Unit tests: `test_e2b_secret_shim.py`, `secrets-e2b-shim.test.ts` (signature/arity table from the package, fill without calls, not found, name validation, paginator, `iam_token`, ignored kwargs, bound client); updated export/unimplemented tables in the existing e2b tests.

## 6. Docs

- [x] 6.1 `docs/site/docs/secrets.md` + nav entry.
- [x] 6.2 `optional-features.md` rows 1–2 → "disponible (0.5.0)" with examples.
- [x] 6.3 `e2b-parity.md` rows 56, 80, 90, footer and counts; `e2b-compat.md` divergence table.
- [x] 6.4 `SECURITY.md` T18, `docs/site/docs/security.md`, `api.md`, READMEs, CHANGELOGs.
- [x] 6.5 `mkdocs build --strict` clean; `scripts/tests` green (`test_optional_features_docs.py`, `test_m9_docs.py`, `test_security_docs.py`).

## 7. Gates

- [x] 7.1 Python: `uv run pytest tests/unit -q`, `ruff check`, `ruff format --check`, `mypy src tests`.
- [x] 7.2 TypeScript: `pnpm install --frozen-lockfile`, `lint`, `typecheck`, `build`, `test`, `pack:check`.
- [x] 7.3 `uv run pytest ../../scripts/tests -q`, `python3 scripts/check_pins.py`, `python3 scripts/check_hygiene.py`.
- [x] 7.4 `openspec validate m13-secrets --strict`.
