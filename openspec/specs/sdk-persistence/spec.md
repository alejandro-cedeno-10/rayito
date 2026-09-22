# sdk-persistence Specification

## Purpose
TBD - created by archiving change m7-s3-persistence. Update Purpose after archive.
## Requirements
### Requirement: S3Prefix names one persisted home
The Python SDK SHALL export `rayito.S3Prefix(bucket: str, prefix: str = "rayito", name: str | None = None, region: str | None = None)`, a frozen dataclass whose construction validates `bucket` (3–63 characters of `[a-z0-9.-]`, starting and ending with `[a-z0-9]`, no `..`, not an IPv4 literal) and the joined key prefix `f"{prefix}/{name}"` when `name` is set (≤ 900 bytes, no leading/trailing `/`, no empty, `.` or `..` component, charset `[A-Za-z0-9!_.*'()/-]`) with `InvalidArgumentException`, and exposes `key_prefix` (raises `InvalidArgumentException` while `name` is `None`), `archive_key`, `manifest_key`, `uri` (`s3://<bucket>/<key_prefix>`) and `with_name(name) -> S3Prefix`. The limits SHALL come from `limits.json` (`PERSIST_KEY_PREFIX_MAX_BYTES`, `PERSIST_EXCLUDE_MAX`, `S3_BUCKET_NAME_MIN`, `S3_BUCKET_NAME_MAX`, `DEFAULT_PERSIST_TIMEOUT_SECONDS`) through the generated `_limits.py` and `limits.ts`. The TypeScript SDK SHALL export an `S3Prefix` class with the same fields, validation, `keyPrefix`, `archiveKey`, `manifestKey`, `uri` and `withName()`. `docs/site/docs/persistence.md` and `SECURITY.md` T15 SHALL state that the prefix is **not** a tenant boundary: `rayd` never binds the `S3Location` of `Checkpoint`/`Restore` to the sandbox that asks (`resolve_location()` validates syntax only, and the `sandbox_id` of the manifest is informative), so whoever holds one sandbox's access token can read or overwrite any `name` the execution role reaches under `<bucket>/<prefix>/*`, including another sandbox's home. Both texts SHALL say that isolating tenants that do not trust each other takes one execution role and one prefix per tenant (a `name` per tenant does not isolate), and SHALL name binding the location to the sandbox as pending work, never as an existing control. The quickstart and the `prefix` bullet of `docs/site/docs/persistence.md` SHALL NOT publish a persistence prefix whose first segment is `rayito`: that namespace holds the image artifacts (`rayito/images/*`) and the `*` of an IAM resource crosses `/`, so the published recipe SHALL use the `PersistencePrefix` default of `spike/m0/iam.yaml` (`rayito-home`) explicitly in both the Python and the TypeScript snippet, and the bullet SHALL say that the value must equal the deployment's `PersistencePrefix` or every `checkpoint_files()` answers `PersistenceException(code="permission_denied")`. The signature line MAY keep documenting the SDK default `prefix="rayito"`, which this change does not alter.

#### Scenario: validation
- **WHEN** the unit test constructs `S3Prefix("My_Bucket")`, `S3Prefix("b", prefix="/x")`, `S3Prefix("b", prefix="a//b", name="n")`, `S3Prefix("b", name="..")` and `S3Prefix("b", prefix="rayito", name="e2e-1")`
- **THEN** the first four raise `InvalidArgumentException` (TypeScript: `InvalidArgumentError`) and the last has `key_prefix == "rayito/e2e-1"`, `archive_key == "rayito/e2e-1/home.tar.gz"` and `uri == "s3://b/rayito/e2e-1"`

#### Scenario: the prefix is documented as not separating tenants
- **WHEN** `scripts/tests/test_security_docs.py::test_prefix_is_not_a_tenant_boundary` reads `docs/site/docs/persistence.md` and the T15 row of `SECURITY.md`
- **THEN** both say the prefix does not separate tenants and both ask for one execution role and one prefix per tenant

#### Scenario: the published recipe stays out of the artifact namespace
- **WHEN** `scripts/tests/test_security_docs.py::test_persistence_quickstart_stays_out_of_the_artifact_namespace` reads the `## Quickstart` and `` ## `S3Prefix` `` sections of `docs/site/docs/persistence.md`
- **THEN** the quickstart passes `rayito-home` explicitly in both snippets and shows no `s3://mi-bucket/rayito/` URI, and the `prefix` bullet names `PersistencePrefix`, its `rayito-home` default, the `rayito/images/*` namespace and the fact that the IAM `*` crosses `/`

### Requirement: create(persist=) requires a role, binds the prefix and auto-restores
`Sandbox.create` and `AsyncSandbox.create` SHALL accept `persist: S3Prefix | None = None` and `persist_timeout: float = 600`. With `persist` and no `execution_role_arn` they SHALL raise `InvalidArgumentException` before any AWS call. After readiness they SHALL bind `sbx.persist` to `persist` if it has a `name`, else to `persist.with_name(sandbox_id)`. When `persist.name` was given they SHALL call `restore_files(timeout=persist_timeout)` before returning: a missing checkpoint (`NOT_FOUND`) SHALL be swallowed and `sbx.last_restore` SHALL be `None`; any other failure SHALL close the sandbox, terminate the VM unless `keep_on_failure`, and re-raise; a successful restore SHALL be kept in `sbx.last_restore: RestoreResult`. `connect(sandbox_id, ..., persist: S3Prefix | None = None)` SHALL only bind (its `name` must be set, else `InvalidArgumentException`) and never restore. TypeScript: `create({ persist, persistTimeoutMs = 600_000 })`, `connect(id, { persist })`, `sbx.persist`, `sbx.lastRestore`, same rules with `InvalidArgumentError`.

#### Scenario: role required
- **WHEN** the unit test calls `Sandbox.create(template, persist=S3Prefix("b"), control_plane=fake_plane)` without `execution_role_arn`
- **THEN** `InvalidArgumentException` is raised and the fake plane recorded no `run-microvm`

#### Scenario: bind without name and auto-restore with name
- **WHEN** one sandbox is created with `persist=S3Prefix("b")` and another with `persist=S3Prefix("b", name="alice")` against the fake `rayd` whose `Restore` answers `NOT_FOUND` for `rayito/alice`
- **THEN** the first has `persist.name == sandbox_id`, made no `Restore` call and `last_restore is None`; the second made exactly one `Restore` with `key_prefix "rayito/alice"`, swallowed the `NOT_FOUND` and has `last_restore is None`

#### Scenario: auto-restore failure follows the readiness policy
- **WHEN** the fake `Restore` answers `started` then `StreamError{code: "internal"}` for a sandbox created with `persist=S3Prefix("b", name="x")` and `execution_role_arn`
- **THEN** `create()` raises `PersistenceException(code="internal")`, the fake plane recorded one `terminate-microvm`, and with `keep_on_failure=True` it recorded none

### Requirement: checkpoint_files and restore_files
`Sandbox` and `AsyncSandbox` SHALL expose `checkpoint_files(*, target: S3Prefix | None = None, exclude: Sequence[str] = (), timeout: float = 600, on_progress: Callable[[CheckpointProgress], None] | None = None) -> CheckpointResult` and `restore_files(*, source: S3Prefix | None = None, timeout: float = 600, on_progress: Callable[[RestoreProgress], None] | None = None) -> RestoreResult`. `target`/`source` SHALL default to `sbx.persist` (`InvalidArgumentException` when neither is available). `exclude` SHALL be validated client-side (≤ 64 entries, each non-empty, no NUL, no `..`, not absolute) before any RPC. Both SHALL open their stream on the stream channel with deadline `timeout`, retry once on a proxy 403 by re-minting before the first message, never reconnect mid-stream, invoke `on_progress` for every `progress` event, and return `CheckpointResult(bucket, key_prefix, files, bytes_read, archive_bytes, sha256, skipped, duration)` / `RestoreResult(files, bytes_written, archive_bytes, sha256, skipped, duration)` on `done`. Mapping: local validation and `INVALID_ARGUMENT`/`invalid_argument` → `InvalidArgumentException`; `NOT_FOUND`/`not_found` → `NotFoundException` (explicit `restore_files` never swallows it); `PERMISSION_DENIED`/`permission_denied`, `FAILED_PRECONDITION`, `INTERNAL`/`internal`, `suspending`, `UNAVAILABLE` → `PersistenceException(SandboxException)` with `code` set to the code or the lower-case status name and, for `suspending`/`UNAVAILABLE`, a message saying the operation was interrupted and must be re-run; the deadline → `TimeoutException`; gRPC `UNIMPLEMENTED` → `PersistenceException(code="unimplemented")` whose message says the image must be republished with an agent implementing `Checkpoint`. The SDK SHALL log at `info` only the `uri` at start and end. TypeScript: `checkpointFiles({ target?, exclude = [], timeoutMs = 600_000, onProgress? })`, `restoreFiles({ source?, timeoutMs, onProgress? })`, results with `durationMs`, `PersistenceError extends SandboxError` with `code`, `NotFoundError` for the missing checkpoint.

#### Scenario: checkpoint result and progress
- **WHEN** the fake `Checkpoint` emits `started{files 3, bytes 100}`, two `progress`, `done{files 3, bytes_read 100, archive_bytes 80, sha256 "ab…", skipped 0, duration_ms 42}` and the test passes `on_progress`
- **THEN** the callback ran twice, the result has `files == 3`, `archive_bytes == 80`, `sha256 == "ab…"`, `duration == 0.042`, `key_prefix == sbx.persist.key_prefix`, and the fake saw `exclude` exactly as passed

#### Scenario: error mapping
- **WHEN** the fake answers, in separate tests, gRPC `PERMISSION_DENIED`, `FAILED_PRECONDITION`, `UNIMPLEMENTED`, `NOT_FOUND` (restore), and `started` followed by `StreamError{code: "suspending"}`
- **THEN** the calls raise `PersistenceException` with `code` `permission_denied`, `failed_precondition`, `unimplemented`, `NotFoundException`, and `PersistenceException(code="suspending")` whose message says to re-run, respectively, and none of them reconnected

#### Scenario: local exclude validation
- **WHEN** `checkpoint_files(exclude=["../x"])` and `checkpoint_files(exclude=["a"] * 65)` are called
- **THEN** both raise `InvalidArgumentException` and the fake received no `Checkpoint`

### Requirement: reincarnate replaces the VM and keeps the files
`Sandbox.reincarnate(*, exclude: Sequence[str] = (), persist_timeout: float = 600) -> Sandbox` (async: `-> AsyncSandbox`) SHALL require `sbx.persist` and the launch options recorded by `create()` (a `connect()` handle SHALL raise `InvalidArgumentException` naming `create(persist=)`), SHALL run `checkpoint_files(exclude=exclude, timeout=persist_timeout)`, then `Sandbox.create(**recorded options, persist=sbx.persist, persist_timeout=persist_timeout)` (same template ARN and version, `timeout`, `idle`, `envs`, `metadata`, `cpu_time_limit`, `execution_role_arn`, `allowed_ports`, `ingress`, `egress`, `logging`, control plane, transport, `ready_timeout`, `request_timeout`, `reconnect_timeout`, `keep_on_failure`, and the explicit `access_token` if one was given), then `kill()` the old VM (`SandboxNotFoundException` suppressed) and `close()` the old handle, returning the new sandbox. If `create` fails the old sandbox SHALL be left running and the exception SHALL propagate with a note that the checkpoint under `sbx.persist.uri` is complete. Docstrings and `persistence.md` SHALL state that kernel variables, processes and PTYs do not survive (ADR-007). TypeScript: `reincarnate({ exclude, persistTimeoutMs }) → Sandbox` with the same ordering. `rayito.e2b` SHALL NOT gain `persist`.

#### Scenario: ordering against the fakes
- **WHEN** `new = sbx.reincarnate()` runs for a sandbox created with `persist=S3Prefix("b", name="n")` and `metadata={"k": "v"}`
- **THEN** the recorded sequence is `Checkpoint` on the old endpoint, `run-microvm` with the same image ARN and metadata, `Restore` on the new endpoint with `key_prefix "rayito/n"`, `terminate-microvm` of the old id; `new.sandbox_id != sbx.sandbox_id`, `new.persist == sbx.persist`, `new.metadata == {"k": "v"}`, and `sbx` is closed

#### Scenario: create failure keeps the old sandbox
- **WHEN** the fake plane makes `run-microvm` raise during `reincarnate()`
- **THEN** the exception propagates, the fake recorded the `Checkpoint` and no `terminate-microvm`, and `sbx.is_running()` is still `True`

#### Scenario: connect handles cannot reincarnate
- **WHEN** `Sandbox.connect(id, access_token=..., persist=S3Prefix("b", name="n")).reincarnate()` is called
- **THEN** `InvalidArgumentException` is raised before any RPC

### Requirement: Sync/async parity and the e2b shim message
The async tree SHALL expose exactly the same persistence names and signatures as the sync tree (`persist`, `last_restore`, `checkpoint_files`, `restore_files`, `reincarnate`, `create(persist=, persist_timeout=)`, `connect(persist=)`), asserted by the existing parity test; `_persistence_base.py` SHALL hold the shared pure helpers (request builders, exclude validation, event-to-result translation, status-to-exception mapping). `rayito.e2b.Sandbox.set_timeout` (instance and class) SHALL keep raising `UnimplementedError(feature="set_timeout")` before any AWS or agent call, with a `reason` naming `UpdateMicrovm` and `rayito.Sandbox.reincarnate()`.

#### Scenario: parity
- **WHEN** the parity unit test compares the public method names of `Sandbox` and `AsyncSandbox`
- **THEN** the five persistence names appear in both and every signature matches

### Requirement: Real-AWS acceptance of persistence
`clients/python/tests/e2e/test_m7_persistence.py` SHALL be skipped unless `RAYITO_E2E=1`, `RAYITO_TEMPLATE_CAPS`, `RAYITO_EXECUTION_ROLE_ARN` and `RAYITO_PERSIST_BUCKET` are set (`RAYITO_PERSIST_PREFIX` default `rayito-e2e`), SHALL delete every object it created in teardown with the developer's boto3 client, and SHALL: (1) create a caps sandbox with the role and `persist=S3Prefix(bucket, prefix, name=f"e2e-{uuid}")`, assert `last_restore is None`, generate inside the VM a 50 MB `/dev/urandom` file plus a text file, a nested file, a symlink, a `0755` script, a file under `.cache/` and one under `skipme/`, record their sha256 and `stat -c '%a %U:%G'`, assert `imds_blocked` becomes `true` and the uid 1000 IMDS probe fails on that sandbox, run `checkpoint_files(exclude=["skipme"])` twice (first and warm timings), `kill()`, create again with the same `persist`, assert `last_restore` is set, identical sha256 for the regular files, the symlink target, `755 user:user` on the script, `.cache/` and `skipme/` absent, `imds_blocked` still `true`, and report bytes, seconds and MB/s per direction; (2) `reincarnate()` yields a new id with the marker file present, the old id `TERMINATING|TERMINATED`, `persist` equal; (3) explicit `restore_files()` on a fresh name raises `NotFoundException` in < 5 s and `create(persist=)` with that name has `last_restore is None`; (4) on the default image without a role `checkpoint_files(target=...)` raises `PersistenceException(code="permission_denied")` in < 5 s and `imds_blocked is False`; (5) with `RAYITO_E2E_SLOW=1` only, a checkpoint after 3 600 s of uptime whose outcome (success or `permission_denied`) is printed and recorded in `AWS_API_NOTES.md` Q1. Checkpoint and restore of the 50 MB home SHALL each complete within 300 s. `clients/typescript/tests/e2e/m7.e2e.test.ts` SHALL run the round trip of (1) with a 20 MB blob and `reincarnate()` under the same variables. The measured numbers SHALL be written to `AWS_API_NOTES.md` §16 Q53/Q54, `MILESTONES.md` M7 row 4 and ADR-009, and the run SHALL leave zero MicroVMs and zero objects under the prefix.

#### Scenario: round trip
- **WHEN** test (1) runs on the newly published caps image version
- **THEN** it passes with the sha256 of `blob.bin` equal before and after, `imds_blocked == True` on both sandboxes, both operations under 300 s, and the report lines present in the output

#### Scenario: nothing left behind
- **WHEN** the whole `-m e2e` run finishes, green or not
- **THEN** `list-microvms` on both templates shows no non-terminated VM and `aws s3api list-objects-v2 --prefix rayito-e2e/` returns no keys

