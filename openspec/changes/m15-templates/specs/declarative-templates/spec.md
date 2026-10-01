## ADDED Requirements

### Requirement: Constructing a Template builder makes no AWS call and no network call

`Template()`/`new Template()`, every fluent DSL method
(`fromBaseImage`/`copy`/`runCmd`/`pipInstall`/`setEnvs`/`workdir`/
`setUser`/`setStartCmd`/`skipCache`) and `toDockerfile()`/`toJSON()` SHALL
be pure: no `boto3`/AWS-SDK-v3 client is constructed and no file is opened
or AWS call made until `Template.build()`, `build_in_background`/
`buildInBackground`, `get_build_status`/`getBuildStatus` or `exists`/
`templateExists` is called.

#### Scenario: building a full spec with the fluent DSL opens no client
- **WHEN** a `Template` is built through `fromBaseImage`, `pipInstall`,
  `copy`, `setEnvs` and `setStartCmd`, then `toDockerfile()`/`toJSON()` is
  called
- **THEN** no `boto3.client`/AWS-SDK-v3 client constructor runs, and no
  exception is raised

### Requirement: only fromBaseImage() is a supported base source in 0.6

`from_image`/`fromImage`, `from_template`/`fromTemplate`,
`from_dockerfile`/`fromDockerfile` and `from_gcp_registry`/
`fromGcpRegistry` SHALL raise `UnimplementedError` immediately, before any
I/O, naming the method and the reason (injecting `rayd` into an external
base has no supported path in 0.6). `apt_install`/`aptInstall` SHALL raise
`UnimplementedError` naming `dnf` as the Amazon Linux 2023 equivalent.

#### Scenario: fromImage raises before any I/O
- **WHEN** `Template().fromImage(...)` is called
- **THEN** `UnimplementedError` is raised naming `Template.fromImage`,
  and no file is read and no AWS call is made

### Requirement: Template.build() composes on the base image's own Dockerfile and keeps rayd as PID 1

`Template.build()`/`AsyncTemplate.build()`/`build()` (TS) SHALL: resolve
the base image version (the one named by `fromBaseImage(version=)`, or the
newest version whose state is `SUCCESSFUL` and status is `ACTIVE`); fetch
that version's `codeArtifact` zip; compile the DSL's steps into a
Dockerfile fragment and insert it immediately before the base Dockerfile's
last `CMD`/`ENTRYPOINT` instruction; and close the composed Dockerfile
with `USER root` followed by that same terminal instruction, so the final
image's entrypoint is unchanged. A base Dockerfile with no `CMD`/
`ENTRYPOINT` SHALL raise `BuildException`/`BuildError` with
`reason="base_image_missing_entrypoint"` before any upload.

#### Scenario: a RUN step is inserted before the base's CMD, which is preserved
- **WHEN** a `Template` with a `pipInstall` step builds on a base
  Dockerfile ending in `CMD ["/usr/local/bin/rayd"]`
- **THEN** the composed Dockerfile contains the `RUN pip install …`
  instruction before a final `USER root` and `CMD
  ["/usr/local/bin/rayd"]`, in that order

#### Scenario: composing twice does not stack layers
- **WHEN** `Template.build()` composes on top of a Dockerfile that already
  contains a previous template layer (its begin/end markers)
- **THEN** the previous layer's instructions are replaced, not
  accumulated, so rebuilding a template repeatedly never grows its
  Dockerfile

### Requirement: the composed artifact includes the base image's own files, not only the new layer's

`assemble_artifact`/`assembleArtifact` SHALL include every entry already
present in the base image's zip (so its own `COPY`/`RUN` instructions keep
working), replacing only the `Dockerfile` entry with the composed one, and
adding the caller's own context files plus `template.json` (only if
`setStartCmd` was called) on top.

#### Scenario: a base-image file referenced by the base's own Dockerfile survives composition
- **WHEN** the base zip contains `rayd` at `/usr/local/bin/rayd` via its
  own `COPY rayd /usr/local/bin/rayd` instruction
- **THEN** the composed zip still contains that `rayd` entry unchanged

### Requirement: Template.build() is deterministic and reuses an identical, already-built version instead of rebuilding

Two builds of the same `Template` spec against the same base version and
`memory_mb`/`memoryMb` SHALL produce byte-identical artifact zips, and
`Template.build()` SHALL reuse an existing `SUCCESSFUL`/`ACTIVE` version
whose configuration matches instead of calling
`create`/`update-microvm-image` again, unless `force=True`/`force: true`
is passed.

#### Scenario: an identical rebuild makes no create/update call
- **WHEN** `Template.build()` is called twice with the same spec, name,
  bucket and memory
- **THEN** the second call uploads no new artifact and calls neither
  `create-microvm-image` nor `update-microvm-image`

#### Scenario: force rebuilds even when nothing changed
- **WHEN** `Template.build(..., force=True)` is called after an identical
  prior build
- **THEN** `update-microvm-image` (or `create-microvm-image` for a new
  name) is called again

### Requirement: a build with an unsupported memory size is rejected before any AWS call

`Template.build()`/`build()` SHALL validate `memory_mb`/`memoryMb` against
`limits.json`'s `supportedMemoryMiB` and raise
`InvalidArgumentException`/`InvalidArgumentError` before constructing any
AWS client when the value is not one of the five supported sizes (RES-1).

#### Scenario: an unsupported memory size never reaches AWS
- **WHEN** `Template.build(t, "name", bucket="b", memory_mb=3000)` is
  called
- **THEN** `InvalidArgumentException` is raised and no AWS call is made

### Requirement: a failed build is explained from the image log group, never by re-running anything

When a build settles in any state other than version `SUCCESSFUL` and
image status/state launchable, `Template.build()`/`build()` SHALL read the
image's CloudWatch log group and raise `BuildException`/`BuildError`
carrying the last BuildKit step number and command
(`#N [k/n] RUN …`) and exit code (`exit code: N`) parsed from the log
lines, or, when `stateReason` names a 4xx/5xx `ready_cmd` failure (Q85),
`reason="ready_client_error"`/`"ready_server_error"` instead.

#### Scenario: a failing RUN step surfaces its step, command and exit code
- **WHEN** a build's image log group contains
  `#4 [2/5] RUN pip install not-a-real-package` followed by a line
  containing `exit code: 1`
- **THEN** the raised `BuildException`/`BuildError` has `step=2`,
  `command` containing the `RUN` line and `exitCode=1`

#### Scenario: a 5xx ready_cmd failure is classified, not parsed from logs
- **WHEN** a build's version `stateReason` contains
  "the application returned a server error (HTTP 5xx) response"
- **THEN** the raised exception has `reason="ready_server_error"` and no
  log-parsing fallback is attempted

### Requirement: an in-process concurrency guard rejects an eleventh simultaneous build

`Template.build()`/`build_in_background`/`buildInBackground` SHALL reject
an attempt to start more than `MAX_CONCURRENT_BUILDS` (10, Q83)
simultaneous builds in the same process with
`BuildException(reason="build_quota")`/`BuildError({reason:
"build_quota"})`, raised immediately, without calling AWS, and the guard
SHALL always release its slot, including when the build raises.

#### Scenario: the eleventh concurrent build is rejected locally
- **WHEN** 10 builds are already in flight in the same process and an
  eleventh `Template.build()` is started
- **THEN** it raises immediately with `reason="build_quota"`, before any
  AWS call

### Requirement: the E2B shim's Template never raises AttributeError for a method E2B defines

Every method E2B's `Template` class defines SHALL exist on
`rayito.e2b.Template`/`rayito/e2b`'s `Template`: the build methods (which
work, delegating to the native class) and the four tag methods
(`alias_exists`/`assign_tags`/`remove_tags`/`get_tags`,
`aliasExists`/`assignTags`/`removeTags`/`getTags`), which raise
`UnimplementedError` naming the limitation (no per-version tagging in
`create`/`update-microvm-image`) rather than being absent.

#### Scenario: calling an unimplemented tag method raises UnimplementedError, not AttributeError
- **WHEN** `rayito.e2b.Template().assign_tags(...)` is called
- **THEN** `UnimplementedError` is raised naming
  `Template.assignTags`/`Template.assign_tags`, not `AttributeError`

### Requirement: the OptionalStack component templates provisions only IAM, never a bucket, image or function

`OptionalStacks.deploy("templates")`/the TS equivalent SHALL create only
the `RayitoTemplateBuilder` managed policy (`infra/templates.yaml`); it
SHALL never create, and `destroy()` SHALL never delete, an S3 bucket, a
MicroVM image or a Lambda function.

#### Scenario: deploying templates creates no bucket, image or function
- **WHEN** `OptionalStacks.deploy("templates", parameters={...})` is
  called
- **THEN** the stack's only resource is `AWS::IAM::ManagedPolicy`
