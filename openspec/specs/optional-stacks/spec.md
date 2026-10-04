# optional-stacks Specification

## Purpose
TBD - created by archiving change v06-foundations. Update Purpose after archive.

## Requirements

### Requirement: OptionalStacks is never invoked implicitly and components() makes no AWS call
No `Sandbox.create()`, listing, getter or constructor in the SDK SHALL call any method of `OptionalStacks`/`AsyncOptionalStacks`. Constructing `OptionalStacks()` SHALL build no AWS client. `OptionalStacks().components()` SHALL return the full static catalog without making any AWS call.

#### Scenario: constructing OptionalStacks builds no client
- **WHEN** `OptionalStacks()` (Python, TypeScript or via `rayito stack`) is constructed
- **THEN** no `boto3.session.Session.client`/`@aws-sdk` client constructor call is made

#### Scenario: components() is pure metadata
- **WHEN** `OptionalStacks().components()` is called
- **THEN** it returns nine `StackComponent` entries and makes no AWS call

### Requirement: deploying or destroying an unsupported component fails before touching the provisioner
`OptionalStacks.deploy`/`destroy` (and the `rayito stack deploy`/`destroy` CLI commands) SHALL raise `UnimplementedError` naming the component and the OpenSpec change that will implement it, without calling any `StackProvisioner` method, when the resolved `StackComponent.supported` is `false`.

#### Scenario: deploying a stub component raises before any provisioner call
- **WHEN** `OptionalStacks().deploy("s3-mounts")` is called before `m15-s3-mounts` lands
- **THEN** `UnimplementedError` is raised and the `StackProvisioner` given to `OptionalStacks` records zero calls

### Requirement: deploy follows plan_deploy and never updates a ROLLBACK_COMPLETE stack
`OptionalStacks.deploy` SHALL call `StackProvisioner.describe` first; when the result is absent it SHALL call `create`, when the result's state is anything other than `ROLLBACK_COMPLETE` it SHALL call `update`, and when the state is `ROLLBACK_COMPLETE` it SHALL raise `StackException`/`StackError` with code `blocked` and SHALL NOT call `create` or `update`.

#### Scenario: a missing stack is created
- **WHEN** `deploy` is called and `describe` returns no stack
- **THEN** `create` is called, not `update`

#### Scenario: an existing stack is updated
- **WHEN** `deploy` is called and `describe` returns a stack in `CREATE_COMPLETE`
- **THEN** `update` is called, not `create`

#### Scenario: a ROLLBACK_COMPLETE stack blocks the deploy
- **WHEN** `deploy` is called and `describe` returns a stack in `ROLLBACK_COMPLETE`
- **THEN** `StackException`/`StackError` with code `blocked` is raised and neither `create` nor `update` is called

### Requirement: deploy validates parameters against the component's declared set before any AWS call
`OptionalStacks.deploy` SHALL reject (with `InvalidArgumentException`/`InvalidArgumentError`, before calling `StackProvisioner.describe`) any parameter name not declared on the resolved `StackComponent`, and any missing parameter declared `required` with no default.

#### Scenario: an unknown parameter is rejected before any call
- **WHEN** `deploy("metadata-index", parameters={"TotallyMadeUp": "x"})` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and the `StackProvisioner` records zero calls

### Requirement: every stack carries the three fixed tags, which the caller cannot override
`stack_tags`/`stackTags` SHALL merge the caller's tags with `rayito:component`, `rayito:managed-by` and `rayito:sdk-version`, and the three fixed values SHALL always win over a caller-supplied tag of the same key.

#### Scenario: a caller tag with a fixed key is overridden
- **WHEN** `deploy(component, tags={"rayito:component": "spoofed"})` is called
- **THEN** the tags sent to `StackProvisioner.create`/`update` have `rayito:component` equal to the component's real name, not `"spoofed"`

### Requirement: the CLI's deploy command always prints the component's cost statement and requires confirmation unless --yes
`rayito stack deploy <component>` SHALL print the component's `CostStatement` (what it creates, idle and per-use cost, removal, source) before acting, and SHALL prompt for confirmation and abort on a negative answer unless `--yes` is given or `--json` output is requested.

#### Scenario: deploy without --yes prompts and aborts on "no"
- **WHEN** `rayito stack deploy metadata-index` is run without `--yes` and the user answers "n"
- **THEN** the command exits non-zero, the cost statement was printed, and no `StackProvisioner` call was made
