# size-catalog Specification

## Purpose
TBD - created by archiving change m15-sizes-catalog. Update Purpose after archive.

## Requirements

### Requirement: resolve_size rounds up within the closed catalog and never calls AWS
`resolve_size`/`resolveSize` SHALL round a requested size up to the first value of `SUPPORTED_MEMORY_MIB` (512/1024/2048/4096/8192 MiB) that covers it, SHALL NOT round down, and SHALL raise `InvalidArgumentException`/`InvalidArgumentError` — without making any AWS call — both for a name or MiB value outside the catalog's reachable range and for a request above `MAX_SUPPORTED_MEMORY_MIB`.

#### Scenario: an exact short name resolves with no rounding
- **WHEN** `resolve_size("4gb")` is called
- **THEN** it returns a resolved size with `memory_mib=4096`, `requested_mib=4096`, and `rounded_up` is `False`

#### Scenario: a SizeRequest between two catalog values rounds up
- **WHEN** `resolve_size(SizeRequest(memory_mib=3000))` is called
- **THEN** it returns `memory_mib=4096`, `requested_mib=3000`, and `rounded_up` is `True`

#### Scenario: a request above the maximum published size is rejected before any AWS call
- **WHEN** `resolve_size(SizeRequest(memory_mib=16384))` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and no `ControlPlane` method is called

### Requirement: apply_size_suffix only suffixes non-baseline names and rejects ARNs
`apply_size_suffix`/`applySizeSuffix` SHALL return the image name unchanged for the baseline size (2048 MiB), SHALL append `-<size name>` for every other resolved size, and SHALL raise `InvalidArgumentException`/`InvalidArgumentError` when the given image name is an ARN.

#### Scenario: the baseline size does not add a suffix
- **WHEN** `apply_size_suffix("rayito-base", resolve_size("2gb"))` is called
- **THEN** it returns `"rayito-base"` unchanged

#### Scenario: a non-baseline size appends its name
- **WHEN** `apply_size_suffix("rayito-base", resolve_size("4gb"))` is called
- **THEN** it returns `"rayito-base-4gb"`

#### Scenario: an ARN template is rejected
- **WHEN** `apply_size_suffix("arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base", resolve_size("4gb"))` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised

### Requirement: create() resolves and applies size before resolving the template ARN, with no ConfigureSandbox section
`Sandbox.create(size=...)`/`Sandbox.create({ size })` SHALL resolve and validate `size` purely in the client, SHALL apply its suffix to the template name before calling `ControlPlane.resolve_template_arn`/`resolveTemplateArn`, and SHALL NOT include any size-related section in a `ConfigureSandbox` request.

#### Scenario: the launched image ARN carries the resolved suffix
- **WHEN** `Sandbox.create("rayito-base", size="4gb", ...)` is called against a fake control plane
- **THEN** the control plane's `resolveTemplateArn`/`resolve_template_arn` is called with `"rayito-base-4gb"`, and the resulting `SandboxInfo.template` ends with `:microvm-image:rayito-base-4gb`

#### Scenario: create(pool=, size=) is rejected
- **WHEN** `Sandbox.create(pool=pool, size="4gb")`/`Sandbox.create({ pool, size: "4gb" })` is called
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` naming `size` is raised and no pool method beyond the rejection runs

### Requirement: get_info confirms the baseline memory with exactly one cached call per image version, only when size was used
`get_info()`/`getInfo()` SHALL call `ControlPlane.get_microvm_image_version`/`getMicrovmImageVersion` at most once per distinct `(image_arn, image_version)` per process — cached by `ConventionCatalog`/`DEFAULT_SIZE_CATALOG` — and only when the sandbox was launched with `size=`/`size`; without it, `SandboxInfo.size`/`baseline_memory_mib`/`baselineMemoryMib`/`baseline_cpu`/`baselineCpu` SHALL be `None`/`undefined` and no such call SHALL be made.

#### Scenario: no size means no GetMicrovmImageVersion call ever
- **WHEN** a sandbox is created without `size=`/`size` and `get_info()`/`getInfo()` is called
- **THEN** the control plane records zero `GetMicrovmImageVersion`/`getMicrovmImageVersion` calls and `size`/`baseline_memory_mib`/`baseline_cpu` are all absent

#### Scenario: repeated get_info calls reuse the cached confirmation
- **WHEN** `get_info()`/`getInfo()` is called twice on a sandbox created with `size="4gb"`
- **THEN** both calls report the same `baseline_memory_mib`/`baselineMemoryMib` and `baseline_cpu`/`baselineCpu`, and the control plane recorded exactly one `GetMicrovmImageVersion`/`getMicrovmImageVersion` call across both

### Requirement: the sizes-guard OptionalStack component is real and requires ImageArns
The `sizes-guard` `StackComponent` SHALL be `supported` (no longer a stub), SHALL declare a required `ImageArns` parameter with no default, and SHALL be `CAPABILITY_IAM`-only (no billable resource). `OptionalStacks.deploy("sizes-guard", ...)` SHALL raise `InvalidArgumentException`/`InvalidArgumentError` before any provisioner call when `ImageArns` is missing.

#### Scenario: deploying without ImageArns is rejected before any call
- **WHEN** `OptionalStacks().deploy("sizes-guard")` is called with no `ImageArns` parameter
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` naming `ImageArns` is raised and the `StackProvisioner` records zero calls

#### Scenario: deploying with ImageArns creates the stack
- **WHEN** `OptionalStacks().deploy("sizes-guard", parameters={"ImageArns": "<arn-1>,<arn-2>"})` is called against a provisioner with no existing stack
- **THEN** the provisioner's `create` is called, not `update`, and the resulting `StackStatus.state` reflects a successful creation

### Requirement: publishing without --sizes is byte-for-byte unchanged, and each additional size bakes its own baseline memory variable
`rayito image publish` without `--sizes` SHALL produce the exact same `create`/`update-microvm-image` request it produced before this change existed. `rayito image publish --sizes <names>` SHALL additionally publish one image per listed size from the same artifact, each named `<image-name>-<size>`, each with `resources[0].minimumMemoryInMiB` set to that size's value, and each with `RAYITO_BASELINE_MEMORY_MIB` set to that same value in `environmentVariables`; the baseline name SHALL NOT be accepted in `--sizes` (it is already published, unsuffixed, by the base invocation).

#### Scenario: publish without --sizes is unaffected
- **WHEN** `rayito image publish` is invoked with no `--sizes` and no `--env`
- **THEN** the `desired_configuration` sent to `create`/`update-microvm-image` carries no `environmentVariables` key, identical to before this change

#### Scenario: --sizes 4gb publishes an additional, correctly-sized image
- **WHEN** `rayito image publish --sizes 4gb` is invoked for an image named `rayito-base`
- **THEN** a second image `rayito-base-4gb` is published with `resources: [{"minimumMemoryInMiB": 4096}]` and `environmentVariables: {"RAYITO_BASELINE_MEMORY_MIB": "4096"}`

#### Scenario: the baseline name is rejected in --sizes
- **WHEN** `rayito image publish --sizes 2gb` is invoked (2gb being the baseline)
- **THEN** the CLI rejects it before any AWS call, naming `--sizes`
