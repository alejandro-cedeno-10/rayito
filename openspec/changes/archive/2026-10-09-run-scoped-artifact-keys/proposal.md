## Why

`rayito image publish` uploads the image zip to a content-addressed key,
`rayito/images/rayd-<12 hex of sha256>.zip`. Two acceptance or e2e runs that
publish the same commit therefore share one S3 object. When one run finished
and cleaned up, it deleted that object while the other run's image version
still pointed at it as its `codeArtifact`. A cleanup cannot tell, from the
key alone, whether its run uploaded the object or found it already there.

## What Changes

- `rayito image publish --artifact-run-id RUN_ID` (optional) uploads the zip
  to `rayito/images/runs/<RUN_ID>/rayd-<sha>.zip`. The key stays under
  `rayito/images/`, so the IAM templates need no change. `RUN_ID` is one key
  segment (letters, digits and hyphens, 1 to 64), validated before any AWS
  call.
- The publish summary (`--json`) gains `artifactUploaded`: `true` when this
  invocation uploaded the object, `false` when it was already there.
- `make image-publish*` pass `--artifact-run-id` when `RAYITO_E2E_RUN_ID`
  (or `ARTIFACT_RUN_ID=`) is set; the m7 CLI e2e reuses the run's key when
  `RAYITO_E2E_RUN_ID` is set.
- Docs: `cli.md`, `CONTRIBUTING.md`, `docs/RELEASING.md` and the
  engineering skill describe the per-run key and the cleanup rule (delete
  only what the run uploaded).

Without the flag nothing changes for users: same key, same reuse check, same
output (only one extra field in the JSON summary).

## Impact

- Python CLI only (`rayito.cli._publish`, `rayito.cli.image`); TypeScript has
  no `image publish`, so there is no parity surface.
- No runtime (`rayd`) change and no AWS call added.
