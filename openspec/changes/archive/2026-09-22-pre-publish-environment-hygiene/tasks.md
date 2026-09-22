## 1. Raw data out of the tree

- [x] 1.1 `git rm -r --cached spike/m0/out` (files stay on disk); `.gitignore` ignores `/spike/m0/out/` and its comment says the raw output is regenerated locally and never committed
- [x] 1.2 Sanitized fixture `clients/python/tests/unit/cli/fixtures/quotas.json` (account → `123456789012` in `QuotaArn`, every other field identical) and `test_doctor.py` reads it
- [x] 1.3 `docs/benchmarks/raw/*.json`: each distinct real MicroVM ID → `microvm-00000000-0000-0000-0000-<n:012d>`, account → `123456789012`, text substitution only; every numeric value compared before and after

## 2. Code defaults and fixtures

- [x] 2.1 `Makefile`: `BUCKET ?= $(RAYITO_BUCKET)`, phony `require-bucket` first prerequisite of every `image-publish*` target with a `$(error …)` naming `BUCKET` and `RAYITO_BUCKET`
- [x] 2.2 `spike/m0/run_m0.py`: no `DEFAULT_BUCKET`; `RAYITO_BUCKET` required at startup with an error message
- [x] 2.3 `crates/rayd-core/src/persistence/keys.rs` `bucket_rules`: a long valid name with dashes and digits built on `amzn-s3-demo-bucket`
- [x] 2.4 Python and TypeScript unit-test MicroVM IDs → fakes of design D1

## 3. Docs and task logs

- [x] 3.1 Account, bucket, SSO profile and role, MicroVM IDs and endpoints, VPC ID and CIDR replaced per design D1 in `AWS_API_NOTES.md`, `MILESTONES.md`, `docs/`, `infra/README.md`, `spike/m0/`, `openspec/specs/cli/spec.md` and `openspec/changes/archive/`
- [x] 3.2 `CONTRIBUTING.md` toolchain for Linux/macOS/Windows without personal paths; `docs/site/docs/kernels.md` and the archived task logs use repo-relative paths and bare tool names
- [x] 3.3 `spike/m0/README.md`, `spike/m0/M0_RESULTS.md` and `AWS_API_NOTES.md` say the raw output is regenerated locally and never committed

## 4. Gate

- [x] 4.1 `scripts/check_hygiene.py` with the rules of design D5
- [x] 4.2 `scripts/tests/test_check_hygiene.py`: a positive and a negative case per rule, `main` output and exit codes, binary skip, untracked files ignored, the real repository clean
- [x] 4.3 Wired into `make lint` and the CI `check` job next to `check_pins.py`

## 5. Verification

- [x] 5.1 `python scripts/check_hygiene.py`, `python scripts/check_pins.py`, `python scripts/check_license.py` and `scripts/tests` green
- [x] 5.2 Python client: `pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy src tests`
- [x] 5.3 `cargo test -p rayd-core --locked` and `cargo clippy -p rayd-core --all-targets -- -D warnings`
- [x] 5.4 `openspec validate --all --strict --no-interactive`
- [x] 5.5 Scans over `git ls-files` for every pattern of design D5 show zero hits outside the kept owner decisions
