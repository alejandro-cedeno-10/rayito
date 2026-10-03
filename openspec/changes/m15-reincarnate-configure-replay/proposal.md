## Why

`reincarnate()` relaunches a sandbox with the options its original `create()`
recorded in `LaunchOptions`, but of the 0.6 options that become
`ConfigureSandbox` sections only `gateways=` was recorded. A reincarnated
sandbox silently lost its S3 mounts, its lifecycle events (`k_sbx` was never
sent, so `rayd` emitted nothing for the successor) and its telemetry export.
The gap was documented in ADR-020 and the `m15-events-webhooks` design ("does
not replay it, as with `mounts=`/`telemetry=`"). In TypeScript the spread of
`LaunchOptions` also re-sent the resolved `size` next to the template ARN,
a combination `create()` rejects, so reincarnating a sized sandbox failed.

## What Changes

- Python `_feature_options`: `relaunch_features(options)` (the seven options
  minus `size`, mappings copied) and `feature_kwargs(options)` (field by
  field, so a future 0.6 option is replayed without touching
  `launch_kwargs`). `LaunchOptions.gateways` becomes
  `LaunchOptions.features`; `create()` (sync and async) builds its
  `FeatureOptions` once, plans it and stores `relaunch_features` of it;
  `_persistence_base.launch_kwargs` spreads `feature_kwargs`.
- TypeScript `feature-options.ts`: `relaunchFeatures` (drops `size`);
  `persistence.ts`: `LaunchOptions.features` (`RelaunchedFeatures`) and
  `relaunchCreateOptions(launch)`, used by `reincarnate()` instead of a raw
  spread (never re-sends `size`).
- The successor goes through the unchanged `create()` path, so each section
  is re-planned with the successor's facts: `events=` derives `k_sbx` from
  the new `sandbox_id`, `mounts=` waits for `mounted`, `telemetry=` uses the
  successor's image and memory, `gateways=` re-reads every header.
- Unit tests in both SDKs against the fake agent (real gRPC/Connect,
  scripted `Configure`/`ConfigureStatus`); the TypeScript fake
  `ConfigureService` gains scripted `s3MountsStatuses`.
- Docs: `persistence.md` (table of what each option does on reincarnation),
  ADR-020 in `ARCHITECTURE.md`, the `m15-events-webhooks` design note,
  docstrings/TSDoc of `reincarnate()`, component CHANGELOGs.

## Impact

- Affected specs: `sdk-persistence` (reincarnate requirement).
- No proto, `rayd` or AWS API change; no new AWS call beyond what the
  successor's own `create()` already makes for the same options (one
  `GetSecretValue` of the events stack key per `LifecycleEvents` instance,
  already cached; header reads for `gateways=` as before).
