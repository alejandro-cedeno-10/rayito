## 1. Python

- [x] 1.1 Tests first: sync and async `reincarnate()` of a sandbox with
  `persist=`, `mounts=`, `telemetry=` and `events=` against the fake agent
  sends both `Configure` requests with every section, `k_sbx` derived from
  the successor's id, and polls `ConfigureStatus` until `mounted`; no 0.6
  options → no `Configure`; `relaunch_features` drops `size` and copies
  mappings; `launch_kwargs` forwards every feature field.
- [x] 1.2 `relaunch_features`/`feature_kwargs` in `_feature_options`,
  `LaunchOptions.features`, `create()` stores it, `launch_kwargs` spreads it.

## 2. TypeScript

- [x] 2.1 Same tests (`m15-reincarnate-configure-replay.test.ts`); fake
  `ConfigureService.s3MountsStatuses`.
- [x] 2.2 `relaunchFeatures`, `LaunchOptions.features`,
  `relaunchCreateOptions` used by `reincarnate()` (never re-sends `size`).

## 3. Docs

- [x] 3.1 `persistence.md`, ADR-020 note, `m15-events-webhooks` design note,
  docstrings/TSDoc, CHANGELOGs.

## 4. Acceptance

- [ ] 4.1 Real AWS: a sandbox created with `mounts=`/`telemetry=`/`events=`
  and reincarnated gets all three sections re-applied (events key
  re-derived for the new id).
