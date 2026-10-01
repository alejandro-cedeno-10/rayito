## Why

The 0.5.0 acceptance on real AWS (commit `a422b54`, 2026-10-01) found four
problems in the secrets feature added by `m13-secrets`:

1. **Name reuse after a forced delete.** AWS freed the name 19.8, 26.8, 27.9
   and 19.3 s after `DeleteSecret(ForceDeleteWithoutRecovery=true)`. With the
   0.5 s → 8 s backoff, the 30 s create retry budget gave up before the last
   attempt that would have succeeded.
2. **`destroy()` returned `True` for a name that never existed** (both SDKs,
   native and E2B shim). A forced `DeleteSecret` on a missing name does not
   raise `ResourceNotFoundException`, so the documented "`False` if it did
   not exist" (and E2B's semantics) never happened.
3. **SEC-10 e2e captured secret values from botocore.** The test put the
   root logger at DEBUG, so botocore's own request/response bodies (with the
   dummy `SecretString`) reached `caplog`. Rayito itself logged nothing.
4. **`ListSecrets` is eventually consistent**: a just-created or updated
   secret took ~3–5 s to appear. Undocumented, and tests must poll.

## What Changes

- Python `rayito/_secrets.py` and TypeScript `src/secrets/store.ts`: a single
  per-SDK budget constant raised to 60 s (`CREATE_RETRY_BUDGET_SECONDS`,
  `CREATE_RETRY_BUDGET_MS`) with ±25 % jitter (`CREATE_RETRY_JITTER`) on the
  existing exponential backoff; the last sleep is clipped to the deadline so
  the final attempt happens at the budget. Error messages derive the
  seconds from the constant.
- `SecretStore.destroy()` in both SDKs: `DescribeSecret` first; not found or
  scheduled for deletion → `False` without `DeleteSecret`; then the forced
  delete; a concurrent not-found on delete → `False`. The E2B shims delegate
  unchanged. `secretsmanager:DescribeSecret` is already in
  `RayitoSecretsAdmin` (`infra/secrets-access.yaml` unchanged).
- The fakes of both SDKs now mirror AWS: a forced delete of a missing name
  succeeds.
- `clients/python/tests/e2e/test_secrets_e2e.py`: `caplog` scoped to the
  `rayito` logger. The TypeScript e2e already captured only Rayito's logger.
- Docs: `secrets.md` (danger box on AWS SDK DEBUG logging, `destroy`
  semantics, 60 s budget, `list()` eventual consistency), `security.md` and
  `SECURITY.md` T18, `e2b-compat.md`, `optional-features.md`,
  `AWS_API_NOTES.md` §19, the "Coste y activación" blocks, CHANGELOGs.

## Impact

- Specs: `secrets` (the CRUD requirement added by `m13-secrets` is modified:
  60 s jittered budget and `destroy` via `DescribeSecret`; two requirements
  added). Archive after `m13-secrets`.
- Behaviour: `destroy` costs one more call (`DescribeSecret`, $0.05 per
  10 000 calls); `create` on a just-deleted name may wait up to 60 s instead
  of failing at 30 s. No wire, image or `rayd` change.
