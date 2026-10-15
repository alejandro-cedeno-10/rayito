## Why

`resolve_image_variant` / `resolveImageVariant` parse the template name with
an open regular expression: any `rayito-<letters and hyphens>` name is taken
as a known variant. A user image that merely starts with `rayito-` (for
example one published with `--image-name` on top of the caps and EFS layers)
resolves to a variant that is not in `CAPS_VARIANTS`, so `require_caps_for`
rejects `mounts=`, `volumes=` and `telemetry=` before launch even though the
image supports them. Names with digits happen to return `None` and work, so
the behaviour depends on the spelling of the name. Both SDKs and the E2B shim
share the flaw.

## What Changes

- The variant is recognised only for the closed pattern
  `rayito-<variant>[-<size>]`, with `<variant>` in a closed catalogue
  (`base`, `base-caps`, `base-caps-efs`, `base-poly`, `base-slim`, kept in
  `limits.json` as `officialImageVariants` and generated into both SDKs) and
  `<size>` in the existing size catalogue (`SIZE_NAMES`). Any other name and
  any ARN resolves to "unknown" and the decision is deferred to
  `Health.features`, which stays the authority.
- `volumes=` (and the shim's `volume_mounts=`) require `base-caps-efs` when
  the variant is known; `base-caps` without `amazon-efs-utils` is now
  rejected before launch instead of after paying for it. Unknown variants
  are still deferred.
- One shared JSON fixture, `testdata/image-variants/vectors.json`, drives
  both SDKs' tests.
- Parity tooling: `scripts/check_sdk_parity.py` compares Python `__all__` with
  the TypeScript exports (`rayito`, `rayito.e2b`) against a commented
  allowlist, wired into `make lint` and CI. TypeScript gains `sbx.metadata`,
  the chart types, `ProcessInfo`, `WriteEntry` and `WatchHandle` in
  `rayito/e2b`, an exhaustive exports test and the golden zero-cost trace
  test. The engineering skill records what is tested once (guest and `rayd`),
  what is Python-only (CLI, MCP, proxy, sync/async twins) and what needs
  both SDKs.

## Impact

Backward compatible for every correct use: a name that used to resolve to an
official variant resolves to the same one. Names that were wrongly rejected
now launch. The only newly rejected case is `volumes=` on the known
`base-caps` image, which could never mount EFS (`UnimplementedError` after
launch, now before it). No public API, CLI flag or default changes.
