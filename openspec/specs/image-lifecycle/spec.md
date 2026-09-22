# image-lifecycle Specification

## Purpose
TBD - created by archiving change m6-hardening. Update Purpose after archive.
## Requirements
### Requirement: image_prune keeps the newest launchable versions and never deletes a running one
The prune logic SHALL live in `rayito.cli._prune` and be reachable through two equivalent entry points, `rayito image prune --image-name <name> [--keep N] [--dry-run] [--wait-timeout S]` and the shim `scripts/image_prune.py` with the same argv. It SHALL list every version of the image (paginated), keep the `N` (default 5) newest versions whose state is `SUCCESSFUL` and status `ACTIVE`, keep every version referenced by a MicroVM whose state is not `TERMINATED`/`TERMINATING` (read from `list-microvms` right before deleting), keep versions in `PENDING`, `IN_PROGRESS`, `DELETING` or `DELETED`, and delete the rest oldest-first. `--dry-run` SHALL print the plan and call no mutating API. Both entry points SHALL print a table (version, state, status, `createdAt`, action) and a JSON summary (only the summary with `--json`), and SHALL exit 1 if any candidate is still present after its attempts. Only versions are deleted, never the image. The unit tests of the keep set, the conflict/retry path, the dry run and the summary SHALL live in `clients/python/tests/unit/cli/test_prune.py` against `rayito.cli._prune` with `botocore.stub.Stubber`.

#### Scenario: keep set with a live MicroVM
- **WHEN** the Stubber test lists versions `1.0`–`8.0` (all `SUCCESSFUL`/`ACTIVE`), a `RUNNING` MicroVM on `2.0` and `--keep 3`
- **THEN** the plan keeps `8.0`, `7.0`, `6.0` and `2.0`, deletes `1.0`, `3.0`, `4.0`, `5.0` in that order, and with `--dry-run` no `delete_microvm_image_version` call is made

#### Scenario: in-flight build untouched
- **WHEN** a version is `IN_PROGRESS`
- **THEN** it is neither counted in the keep quota nor deleted

#### Scenario: both entry points agree
- **WHEN** `rayito image prune --image-name rayito-base --keep 3 --dry-run` and `python scripts/image_prune.py --image-name rayito-base --keep 3 --dry-run` run against the same Stubber fixture
- **THEN** they print the same plan table and the same JSON summary

### Requirement: Deletes are serialized and wait out ConflictException
`image_prune.py` SHALL delete one version at a time: after each `delete-microvm-image-version` it SHALL wait until `get-microvm-image-version` reports `DELETED` or raises `ResourceNotFoundException` **and** `get-microvm-image` reports a state other than `UPDATING`/`DELETING`, before the next delete. On `ConflictException` it SHALL poll `get-microvm-image` every 5 s until the image leaves `UPDATING`/`DELETING` (bounded by `--wait-timeout`, default 600 s) and retry the delete with backoff 5/10/20/40/80 s, at most 5 attempts. The acceptance SHALL record in Q45 whether an `ACTIVE` version deletes directly and every conflict/wait observed; if `delete` refuses an `ACTIVE` version, the script SHALL deactivate it first (`update-microvm-image-version --status INACTIVE`), wait for the image to leave `UPDATING`, then delete.

#### Scenario: conflict then success
- **WHEN** the Stubber test makes the first `delete_microvm_image_version` raise `ConflictException`, `get_microvm_image` answer `UPDATING` twice then `UPDATED`, and the retry succeed
- **THEN** the script waited for the image state, retried once, waited for `DELETED`, and the summary lists the version as `deleted` with `attempts: 2`

#### Scenario: real prune
- **WHEN** the acceptance runs `make image-prune PRUNE_ARGS="--keep 5"` on `rayito-base`
- **THEN** afterwards `list-microvm-image-versions` shows at most 5 launchable versions plus those pinned by live MicroVMs, and the wait/retry timings are in the task notes and Q49 (the design's Q45)

### Requirement: The warm-up list is decided by a measured rule
The acceptance SHALL measure, on one MicroVM of the current image, the RSS each warm-up import adds (`numpy`, `pandas`, `matplotlib.pyplot` with one `savefig`, `scipy.stats`, `sklearn.linear_model`) and record the deltas as Q50 (the design's Q46). `scipy.stats` and `sklearn.linear_model` SHALL leave `0004_warmup.py` if and only if their combined delta exceeds 100 MB; the packages SHALL stay pinned and installed either way. The next publish SHALL report `memorySnapshotSizeInBytes` next to 10.0's 919 146 496 B, together with `run-microvm → kernel_ready` p50 over the e2e session before and after, in `MILESTONES.md`.

#### Scenario: rule applied
- **WHEN** the measured combined delta of `scipy.stats` and `sklearn.linear_model` exceeds 100 MB
- **THEN** `0004_warmup.py` no longer imports them, the warm-up docstring states the measured rule, and the 11.0 memory snapshot reported by `image-publish` is smaller than 10.0's

#### Scenario: rule not triggered
- **WHEN** the combined delta is 100 MB or less
- **THEN** the warm-up list is unchanged and the deltas plus the 11.0 snapshot size are still reported

### Requirement: The base image is pinned by digest and by explicit version
`image/Dockerfile` SHALL start with `FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3` (the manifest-list digest of the tag recorded 2026-09-16; its single `linux/arm64` manifest is `sha256:63831a97f9e498f7693c6f42951fe5d935947e6ae25a11fcab1fe0f5ae60b2a7`), and the comment above it SHALL state the date, both digests and how to refresh them with Docker (`docker buildx imagetools inspect`) and without it (the `public.ecr.aws` token endpoint and a `HEAD /v2/lambda/microvms/manifests/al2023-minimal`, reading `Docker-Content-Digest`). `scripts/publish_image.py` SHALL require `--base-image-version` (exit 2 with the usage line before any AWS call when absent) and SHALL always send `baseImageVersion` in `create-microvm-image`/`update-microvm-image`. The Makefile SHALL pass `--base-image-version $(BASE_IMAGE_VERSION)` on `image-publish`, `image-publish-slim` and `image-publish-caps`, with `BASE_IMAGE_VERSION` defaulting to the `imageVersion` that `list-managed-microvm-image-versions` returns for the newest managed version (`1` today); if the API rejects that spelling the default SHALL become the spelling the API echoes (`1.0`) and both facts SHALL be recorded in `AWS_API_NOTES.md` §16 Q52 together with the result of the first digest-pinned build. A digest bump SHALL be treated as a new image version, published and accepted with the e2e like any other.

#### Scenario: publish without the version is refused
- **WHEN** `python scripts/publish_image.py --artifact image/rayito-image.zip` runs without `--base-image-version`
- **THEN** it exits 2 with the usage line and makes no AWS call (asserted in `scripts/tests/test_publish_image.py`)

#### Scenario: digest-pinned build succeeds
- **WHEN** `make image-publish` runs with the pinned `Dockerfile` and `BASE_IMAGE_VERSION` set
- **THEN** the new `rayito-base` version reaches `SUCCESSFUL`/`ACTIVE`, `get-microvm-image-version` echoes a `baseImageVersion`, Q52 records the accepted spelling, the build time and the `snapshotBuild` sizes next to 16.0's, and the e2e passes on that version

