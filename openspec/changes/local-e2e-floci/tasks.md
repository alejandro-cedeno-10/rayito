## 1. Vetting

- [x] 1.1 Floci README, licence, releases, advisories, image provenance and
  SBOM; pick 2.1.0 and pin the index digest.
- [x] 1.2 Probe every `OptionalStack` template and the MicroVMs control plane
  against Floci with the real SDK.
- [x] 1.3 Trivy sweep of the Floci image, its dependency graph and the
  runner image; `docs/research/2026-10-local-testing.md`.

## 2. Environment

- [x] 2.1 `dev/local/compose.yaml` (floci, runner, guest), `runner/Dockerfile`,
  `rayd/Dockerfile`, `bootstrap.sh`, `run-e2e.sh`, root `.dockerignore`.
- [x] 2.2 `make local-guest-context|local-up|local-e2e|local-down`.

## 3. Tests

- [x] 3.1 `LocalGuestControlPlane` in `clients/python/tests/local/guest.py`
  and `clients/typescript/tests/local/guest.ts`.
- [x] 3.2 Python suites: sandbox, optional features, events pipeline;
  `local` marker excluded by default.
- [x] 3.3 TypeScript suites: sandbox and optional features; `local` vitest
  project.

## 4. CI, docs, gates

- [x] 4.1 `.github/workflows/local-e2e.yml`, Dependabot, `check_pins.py`.
- [x] 4.2 `guias/probar-en-local.md`, nav, `CONTRIBUTING.md`.
- [x] 4.3 Run `make local-e2e` end to end; gates; green CI.
