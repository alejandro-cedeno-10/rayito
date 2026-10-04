## Why

Every end-to-end check of the SDKs today needs a real AWS account
(`RAYITO_E2E=1`): it costs money, needs credentials, is serialized per image
and cannot run on a contributor's laptop or in CI for a PR from a fork. The
unit tests use a fake `rayd`, so nothing exercises the real agent, the real
product image and the real boto3/AWS SDK v3 adapters together before the
AWS acceptance run. A local loop closes that gap without weakening any SDK
check (TLS off loopback, the deliverer's SSRF guard) and without shipping
anything new in the packages.

## What Changes

- A Docker Compose environment in `dev/local/`: Floci 2.1.0 (MIT AWS
  emulator, pinned by digest, unprivileged, no Docker socket, no Internet,
  no host ports); a `guest` built from the unchanged product
  `image/Dockerfile` with `rayd` as PID 1; and a `runner` with the Python
  (uv) and Node (pnpm) toolchains that owns the network namespace the guest
  joins.
- `make local-guest-context`, `make local-up`, `make local-e2e` and
  `make local-down`; `rayd` is compiled inside Docker
  (`dev/local/rayd/Dockerfile`, on the product base) unless
  `LOCAL_RAYD_BIN` points at a prebuilt one.
- A test-only `LocalGuestControlPlane` in each SDK's `tests/local/` (never
  in the wheel or the npm package): a decorator of the existing
  `ControlPlane` port over the real adapter pointed at Floci, which drives
  the guest's lifecycle hooks where AWS would run a MicroVM.
- A `local` pytest marker (excluded by default) and a `local` vitest project
  with suites for commands, files, PTY, `run_code`, `connect`, metrics,
  pause/resume and kill, every deployable `OptionalStack`, the metadata
  index, `SecretStore`/`secrets=`/`gateways=`, `Template.build`, and the
  events pipeline (the three Lambda handlers run in process, delivering a
  signed webhook to a local receiver).
- `.github/workflows/local-e2e.yml` on `ubuntu-24.04-arm` for PRs touching
  the environment, nightly and on demand; Dependabot for the new images;
  `scripts/check_pins.py` also covers `dev/local/*/Dockerfile`.
- `docs/research/2026-10-local-testing.md` (vetting, trust decision,
  vulnerability sweep, Floci differences) and the guide
  `docs/site/docs/guias/probar-en-local.md`, linked from `CONTRIBUTING.md`.

## Impact

- No change to `rayd`, the proto or any public SDK API; no new AWS resource
  or call. `clients/python/pyproject.toml` gains the `local` marker and
  `clients/typescript/vitest.config.ts` the `local` project.
- AWS acceptance still closes a milestone or a release (`CLAUDE.md`, rule 4).
