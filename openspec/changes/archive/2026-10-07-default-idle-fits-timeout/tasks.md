## 1. Implementation

- [x] 1.1 Python: `fit_default_idle`, `fit_default_pause_idle` and `PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS` in `_lifecycle_base`
- [x] 1.2 TypeScript: `fitDefaultIdle`, `fitDefaultPauseIdle` and `PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS` in `sandbox/lifecycle.ts`
- [x] 1.3 Unit tests: plan level, sync and async `create()`, TypeScript `create()`, both shims
- [x] 1.4 Docs (pausar-reanudar, concepts, lifecycle, limits, ciclo-de-vida) and CHANGELOGs
- [x] 1.5 E2E precondition for `s3-mounts` and `rayd-otlp` in the test headers, `CONTRIBUTING.md` and the gates reference

## 2. Acceptance

- [x] 2.1 Python and TypeScript `create(timeout=120)` without `idle` against real AWS end at their timeout
