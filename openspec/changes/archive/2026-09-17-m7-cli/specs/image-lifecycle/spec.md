## MODIFIED Requirements

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
