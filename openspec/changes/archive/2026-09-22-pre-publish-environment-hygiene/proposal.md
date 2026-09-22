## Why

The repository is about to be published under Apache-2.0 and must be
environment-agnostic: a fork has to build, test and read the same way in any
AWS account and on any machine. Today the tracked tree still carries the
environment it was developed in:

- the maintainer's AWS account ID (39 files), in prose, ARNs, raw benchmark
  data and the M0 spike output;
- the account's CDK bootstrap bucket as a **code default** (`Makefile`
  `BUCKET ?=`, `spike/m0/run_m0.py` `DEFAULT_BUCKET`) and in docs and a Rust
  unit test;
- an SSO profile name built from the account ID and an SSO role suffix;
- real MicroVM IDs and endpoints (unit-test fixtures, raw benchmark JSON,
  task logs);
- the ID and CIDR of a VPC that belongs to another workload;
- local machine paths and names (toolchain directories, the repo's absolute
  path, the user profile) in `CONTRIBUTING.md`, the docs and task logs;
- the raw M0 spike output (`spike/m0/out/`), versioned on purpose until now,
  which concentrates all of the above.

None of it is needed to build, test or understand Rayito, and every fork
would copy it.

## What Changes

- Replace the account ID with the AWS documentation placeholder
  `123456789012` in ARNs, and describe resources by name in prose
  ("una cuenta de pruebas", "la imagen `rayito-base`").
- Bucket: `amzn-s3-demo-bucket` in code, tests and fixtures, `<tu-bucket>` /
  `<bucket>` in shell examples. `Makefile` loses its default
  (`BUCKET ?= $(RAYITO_BUCKET)`, and every `image-publish*` target stops with
  a `make` error naming `BUCKET` and `RAYITO_BUCKET` before building
  anything); `spike/m0/run_m0.py` requires `RAYITO_BUCKET`.
- SSO profile → `<tu-perfil>`; SSO role suffix → `<suffix>`.
- MicroVM IDs: `microvm-<id>` in prose; stable fakes
  `microvm-00000000-0000-0000-0000-<12 decimal digits>` in unit tests and in
  the versioned raw benchmark JSON (one fake per distinct real ID, so joins
  inside a file survive; every number untouched).
- VPC: the ID and CIDR of the foreign VPC are dropped from prose.
- Local paths: `CONTRIBUTING.md` describes the toolchain generically for
  Linux, macOS and Windows; docs and task logs use repo-relative paths and
  bare tool names.
- `spike/m0/out/` stops being versioned (`git rm --cached`, `.gitignore`);
  the quota fixture of `test_doctor.py` moves to a sanitized copy under
  `clients/python/tests/unit/cli/fixtures/quotas.json`.
- New gate `scripts/check_hygiene.py` (stdlib, over `git ls-files`) that
  fails on real account IDs in ARNs and bucket or registry names, the default
  CDK bootstrap qualifier, SSO profile and role names with account data,
  real-looking MicroVM IDs and endpoints, real VPC/subnet/security-group/ENI
  IDs, Windows and Git Bash user or drive paths, and AWS access key IDs; unit
  tests in `scripts/tests/test_check_hygiene.py`; wired into `make lint` and
  the CI `check` job next to `check_pins.py`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-hardening`: a new requirement for the environment-hygiene gate.
- `cli`: the `Makefile` requirement of the shim change no longer names a
  default bucket.

## Impact

- Code: `Makefile`, `spike/m0/run_m0.py`,
  `crates/rayd-core/src/persistence/keys.rs` (test only), Python and
  TypeScript unit-test fixtures, `scripts/check_hygiene.py`,
  `scripts/tests/test_check_hygiene.py`, `.github/workflows/ci.yml`,
  `.gitignore`.
- Docs: `AWS_API_NOTES.md`, `MILESTONES.md`, `CONTRIBUTING.md`,
  `docs/SECURITY_AUDIT.md`, `docs/benchmarks/`, `docs/site/docs/kernels.md`,
  `infra/README.md`, `spike/m0/README.md`, `spike/m0/M0_RESULTS.md`,
  `openspec/specs/cli/spec.md` and the archived task logs.
- No runtime surface, no proto change, no AWS call, no image publish.
- Adopters who relied on `make image-publish` without `BUCKET` get a clear
  error instead of a publish attempt against a bucket they do not own.
