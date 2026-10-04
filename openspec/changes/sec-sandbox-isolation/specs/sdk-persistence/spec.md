## MODIFIED Requirements

### Requirement: S3Prefix names one persisted home
The Python SDK SHALL export `rayito.S3Prefix(bucket: str, prefix: str = "rayito", name: str | None = None, region: str | None = None)`, a frozen dataclass whose construction validates `bucket` (3–63 characters of `[a-z0-9.-]`, starting and ending with `[a-z0-9]`, no `..`, not an IPv4 literal) and the joined key prefix `f"{prefix}/{name}"` when `name` is set (≤ 900 bytes, no leading/trailing `/`, no empty, `.` or `..` component, charset `[A-Za-z0-9!_.*'()/-]`) with `InvalidArgumentException`, and exposes `key_prefix` (raises `InvalidArgumentException` while `name` is `None`), `archive_key`, `manifest_key`, `uri` (`s3://<bucket>/<key_prefix>`) and `with_name(name) -> S3Prefix`. The limits SHALL come from `limits.json` (`PERSIST_KEY_PREFIX_MAX_BYTES`, `PERSIST_EXCLUDE_MAX`, `S3_BUCKET_NAME_MIN`, `S3_BUCKET_NAME_MAX`, `DEFAULT_PERSIST_TIMEOUT_SECONDS`) through the generated `_limits.py` and `limits.ts`. The TypeScript SDK SHALL export an `S3Prefix` class with the same fields, validation, `keyPrefix`, `archiveKey`, `manifestKey`, `uri` and `withName()`. `docs/site/docs/persistence.md` and `SECURITY.md` T15 SHALL state that the prefix is a tenant boundary **only when the sandbox binds it**: `create(persist=)` sends the bucket and the base `prefix` (never `prefix/name`) in the `runHookPayload` and `rayd` answers `PERMISSION_DENIED` to any `Checkpoint`/`Restore` outside it before touching S3, so one `prefix` per tenant isolates tenants that share an execution role (a `name` per tenant does not). Both texts SHALL name the residual: a sandbox created without `persist=`, or by an SDK or a `rayd` that predate the binding, binds nothing, its access token reaches any location its execution role reaches (the `sandbox_id` of the manifest is informative), and such a sandbox must not get a role that reaches the persistence prefix. Neither text SHALL call the binding pending work. The quickstart and the `prefix` bullet of `docs/site/docs/persistence.md` SHALL NOT publish a persistence prefix whose first segment is `rayito`: that namespace holds the image artifacts (`rayito/images/*`) and the `*` of an IAM resource crosses `/`, so the published recipe SHALL use the `PersistencePrefix` default of `infra/iam.yaml` (`rayito-home`) explicitly in both the Python and the TypeScript snippet, and the bullet SHALL say that the value must equal the deployment's `PersistencePrefix` or every `checkpoint_files()` answers `PersistenceException(code="permission_denied")`. The signature line MAY keep documenting the SDK default `prefix="rayito"`, which this change does not alter.

#### Scenario: validation
- **WHEN** the unit test constructs `S3Prefix("My_Bucket")`, `S3Prefix("b", prefix="/x")`, `S3Prefix("b", prefix="a//b", name="n")`, `S3Prefix("b", name="..")` and `S3Prefix("b", prefix="rayito", name="e2e-1")`
- **THEN** the first four raise `InvalidArgumentException` (TypeScript: `InvalidArgumentError`) and the last has `key_prefix == "rayito/e2e-1"`, `archive_key == "rayito/e2e-1/home.tar.gz"` and `uri == "s3://b/rayito/e2e-1"`

#### Scenario: the prefix is documented as not separating tenants
- **WHEN** `scripts/tests/test_security_docs.py::test_prefix_is_a_tenant_boundary_only_when_bound` reads `docs/site/docs/persistence.md` and the T15 row of `SECURITY.md`
- **THEN** both say that without the binding (a sandbox created without `persist=`) `rayd` does not bind the `S3Location` to the sandbox, so the prefix alone does not separate tenants, and both ask for one `prefix` per tenant rather than one `name`

#### Scenario: the prefix is documented as a tenant boundary only when bound
- **WHEN** `scripts/tests/test_security_docs.py::test_prefix_is_a_tenant_boundary_only_when_bound` reads `docs/site/docs/persistence.md`, the T15 row of `SECURITY.md` and the T15 summary of `docs/site/docs/security.md`
- **THEN** all say `persist=` binds the sandbox and `rayd` refuses a location outside it with `permission_denied`, ask for one `prefix` per tenant, name the unbound residual, and none says the binding is pending

#### Scenario: the published recipe stays out of the artifact namespace
- **WHEN** `scripts/tests/test_security_docs.py::test_persistence_quickstart_stays_out_of_the_artifact_namespace` reads the `## Quickstart` and `` ## `S3Prefix` `` sections of `docs/site/docs/persistence.md`
- **THEN** the quickstart passes `rayito-home` explicitly in both snippets and shows no `s3://mi-bucket/rayito/` URI, and the `prefix` bullet names `PersistencePrefix`, its `rayito-home` default, the `rayito/images/*` namespace and the fact that the IAM `*` crosses `/`

### Requirement: create(persist=) requires a role, binds the prefix and auto-restores
`Sandbox.create` and `AsyncSandbox.create` SHALL accept `persist: S3Prefix | None = None` and `persist_timeout: float = 600`. With `persist` and no `execution_role_arn` they SHALL raise `InvalidArgumentException` before any AWS call. With `persist`, the `runHookPayload` SHALL carry `"persist": {"bucket": <bucket>, "key_prefix": <prefix>}` (the base `prefix`, never `prefix/name`; built by `build_run_hook_payload(persist=)` / `buildRunHookPayload({ persist })`), and without it the key SHALL be absent; no AWS call is added. After readiness they SHALL bind `sbx.persist` to `persist` if it has a `name`, else to `persist.with_name(sandbox_id)`. When `persist.name` was given they SHALL call `restore_files(timeout=persist_timeout)` before returning: a missing checkpoint (`NOT_FOUND`) SHALL be swallowed and `sbx.last_restore` SHALL be `None`; any other failure SHALL close the sandbox, terminate the VM unless `keep_on_failure`, and re-raise; a successful restore SHALL be kept in `sbx.last_restore: RestoreResult`. `connect(sandbox_id, ..., persist: S3Prefix | None = None)` SHALL only bind (its `name` must be set, else `InvalidArgumentException`) and never restore. TypeScript: `create({ persist, persistTimeoutMs = 600_000 })`, `connect(id, { persist })`, `sbx.persist`, `sbx.lastRestore`, same rules with `InvalidArgumentError`.

#### Scenario: role required
- **WHEN** the unit test calls `Sandbox.create(template, persist=S3Prefix("b"), control_plane=fake_plane)` without `execution_role_arn`
- **THEN** `InvalidArgumentException` is raised and the fake plane recorded no `run-microvm`

#### Scenario: bind without name and auto-restore with name
- **WHEN** one sandbox is created with `persist=S3Prefix("b")` and another with `persist=S3Prefix("b", name="alice")` against the fake `rayd` whose `Restore` answers `NOT_FOUND` for `rayito/alice`
- **THEN** the first has `persist.name == sandbox_id`, made no `Restore` call and `last_restore is None`; the second made exactly one `Restore` with `key_prefix "rayito/alice"`, swallowed the `NOT_FOUND` and has `last_restore is None`

#### Scenario: auto-restore failure follows the readiness policy
- **WHEN** the fake `Restore` answers `started` then `StreamError{code: "internal"}` for a sandbox created with `persist=S3Prefix("b", name="x")` and `execution_role_arn`
- **THEN** `create()` raises `PersistenceException(code="internal")`, the fake plane recorded one `terminate-microvm`, and with `keep_on_failure=True` it recorded none

#### Scenario: the payload binds the bucket and the base prefix
- **WHEN** the unit test creates a sandbox with `persist=S3Prefix("b", prefix="tenants/acme")` and another without `persist`, capturing each `run-microvm` request
- **THEN** the first `runHookPayload` has `persist == {"bucket": "b", "key_prefix": "tenants/acme"}` and the second has no `persist` key, in Python sync, async and TypeScript
