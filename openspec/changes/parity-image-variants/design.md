## Decisions

- **D1. Closed catalogue, not name parsing.** The name only moves a rejection
  earlier; it is not a fact AWS verifies. Recognising only the official
  pattern keeps that shortcut honest. The catalogue lives in `limits.json`
  (`officialImageVariants`), the single source both SDKs are generated from.
- **D2. Unknown means defer.** A custom name, a name outside the catalogue and
  an ARN return `None`/`undefined`. The caller launches and `Health.features`
  decides after `/run` (terminating the sandbox unless `keep_on_failure`).
- **D3. `volumes=` needs `base-caps-efs` when known.** A new
  `require_efs_for`/`requireEfsFor` is used by `plan_volumes`,
  `require_volume_mounts` and the shim. `mounts=` and `telemetry=` keep
  `require_caps_for` (both caps variants pass).
- **D4. Sizes come from `SIZE_NAMES`.** The suffix is stripped only when it is
  one of the catalogued sizes; `rayito-base-caps-efs-3gb` is unknown.
- **D5. One fixture.** `testdata/image-variants/vectors.json` holds the
  resolve and gate vectors both SDK suites read.
- **D6. No reading the capability from the image.** Reading
  `additionalOsCapabilities` or a tag would cost a call and a permission and
  is not measured; it stays an open question in `AWS_API_NOTES.md` §24.
- **D7. Parity script.** Names are compared normalised (no underscores, lower
  case, `Exception` read as `Error`). Python-only names need an allowlist
  entry (except `Async*` with a sync twin); TypeScript is compared on runtime
  values only, since option types have no `__all__` counterpart. A stale
  allowlist entry is itself a finding.
