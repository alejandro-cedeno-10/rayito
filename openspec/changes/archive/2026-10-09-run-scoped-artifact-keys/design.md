## Context

Acceptance runs publish disposable images from the release tree and delete
what they created at the end. The content-addressed key made the artifact a
shared resource between concurrent runs of the same commit.

## Decisions

- **D1 — run id, not a free prefix.** The flag takes a run id, not an
  arbitrary key prefix. The key is always
  `rayito/images/runs/<run-id>/rayd-<sha>.zip`, so it stays under the
  `rayito/images/*` resources that `infra/iam.yaml` and
  `infra/templates.yaml` grant; a free prefix could leave that namespace and
  fail with `AccessDenied` at upload or at build.
- **D2 — validation.** `RUN_ID_PATTERN` (`[A-Za-z0-9][A-Za-z0-9-]{0,63}`)
  keeps the id a single key segment (no `/`, no `..`). An invalid id is a
  usage error (exit 2) before any AWS call.
- **D3 — content address kept inside the run.** The basename is still
  `rayd-<sha>.zip`, so a re-publish within the same run reuses the object and
  the matching version exactly as before.
- **D4 — `artifactUploaded`.** `upload_artifact` returns an
  `UploadedArtifact(uri, uploaded)`; the summary reports `uploaded`. With
  `--sizes`, the root (baseline) reports the upload and the sizes report
  `false`, since they reuse the object the baseline uploaded.
- **D5 — env only in the helpers.** The CLI reads no environment variable for
  this; `make` maps `RAYITO_E2E_RUN_ID` to the flag, and the e2e reads it,
  so a user's `rayito image publish` never changes behaviour because of a
  test variable.

## Risks

- A version published with a run id and later re-published without it does
  not match (different `codeArtifact.uri`) and builds a new version. This is
  the intended separation; the m7 e2e skips instead of building when the
  run's key is not the artifact of an ACTIVE version.
