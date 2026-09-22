## ADDED Requirements

### Requirement: image_prune keeps the newest launchable versions and never deletes a running one
`scripts/image_prune.py --image-name <name> [--keep N] [--dry-run]` SHALL list every version of the image (paginated), keep the `N` (default 5) newest versions whose state is `SUCCESSFUL` and status `ACTIVE`, keep every version referenced by a MicroVM whose state is not `TERMINATED`/`TERMINATING` (read from `list-microvms` right before deleting), keep versions in `PENDING`, `IN_PROGRESS`, `DELETING` or `DELETED`, and delete the rest oldest-first. `--dry-run` SHALL print the plan and call no mutating API. The script SHALL print a table (version, state, status, `createdAt`, action) and a JSON summary, and SHALL exit 1 if any candidate is still present after its attempts. Only versions are deleted, never the image.

#### Scenario: keep set with a live MicroVM
- **WHEN** the Stubber test lists versions `1.0`–`8.0` (all `SUCCESSFUL`/`ACTIVE`), a `RUNNING` MicroVM on `2.0` and `--keep 3`
- **THEN** the plan keeps `8.0`, `7.0`, `6.0` and `2.0`, deletes `1.0`, `3.0`, `4.0`, `5.0` in that order, and with `--dry-run` no `delete_microvm_image_version` call is made

#### Scenario: in-flight build untouched
- **WHEN** a version is `IN_PROGRESS`
- **THEN** it is neither counted in the keep quota nor deleted

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
