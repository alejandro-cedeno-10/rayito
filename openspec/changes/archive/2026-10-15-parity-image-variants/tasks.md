## 1. Image variant catalogue

- [x] 1.1 `limits.json` `officialImageVariants` and `scripts/gen_limits.py`;
      regenerate `_limits.py` and `limits.ts`.
- [x] 1.2 `_role_policy.py` / `role-policy.ts`: closed pattern, `require_efs_for`
      / `requireEfsFor`.
- [x] 1.3 `volumes=` and the shim's `volume_mounts=` use the efs gate.
- [x] 1.4 `testdata/image-variants/vectors.json` read by
      `test_m15_role_policy.py` and `role-policy.test.ts`; existing tests
      updated to the efs image.
- [x] 1.5 Docs: `images.md`, `AWS_API_NOTES.md` §24 (generic text only).

## 2. Parity

- [x] 2.1 `scripts/check_sdk_parity.py` and its test; `make lint` and CI.
- [x] 2.2 TypeScript: `sbx.metadata`, shim exports, exports test, golden
      zero-cost trace test, `events.local.test.ts` (Floci).
- [x] 2.3 Eight docs pages gain their TypeScript tab.
- [x] 2.4 Engineering skill: what is tested once, Python-only and in both.
- [x] 2.5 CHANGELOGs.
