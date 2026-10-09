## 1. CLI

- [x] 1.1 `artifact_key(payload, run_id)`, `validate_run_id`,
      `RUN_SCOPED_KEY_SEGMENT`, `RUN_ID_PATTERN` in `rayito.cli._publish`.
- [x] 1.2 `UploadedArtifact` through `prepare_build`/`finalize_build`;
      `artifactUploaded` in the summary.
- [x] 1.3 `--artifact-run-id` in `rayito image publish`, validated before any
      AWS call.

## 2. Helpers

- [x] 2.1 `make image-publish*`: `ARTIFACT_RUN_ID` (default
      `RAYITO_E2E_RUN_ID`) adds `--artifact-run-id`.
- [x] 2.2 `tests/e2e/test_m7_cli.py`: reuse with the run's key; assert no
      upload.

## 3. Tests and docs

- [x] 3.1 Unit tests: default key unchanged, run-scoped key, invalid ids,
      `artifactUploaded`, CLI usage error.
- [x] 3.2 `cli.md`, `CONTRIBUTING.md`, `docs/RELEASING.md`, skill reference,
      CHANGELOG.

## 4. Acceptance

- [x] 4.1 Gates (no runtime change: no AWS run needed).
