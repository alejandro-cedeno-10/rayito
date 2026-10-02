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
is passed or the spec was built with `skip_cache()`/`skipCache()`, which
SHALL behave exactly as `force`.

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

#### Scenario: skip_cache rebuilds even when nothing changed
- **WHEN** `Template.build()` is called with a spec built with
  `skip_cache()` after an identical prior build
- **THEN** `update-microvm-image` is called again

### Requirement: the composed image inherits the base version's whole configuration

The `create`/`update-microvm-image` request of `Template.build()` SHALL
carry every configuration key the base image version declares
(`baseImageArn`, `baseImageVersion`, `buildRoleArn`, `cpuConfigurations`,
`environmentVariables`, `additionalOsCapabilities`, `hooks`,
`egressNetworkConnectors`), replacing only `codeArtifact`, `resources` and
`logging`, through the image-build core shared with `rayito image publish`
(`rayito._images`, `src/images/gateway.ts`).

#### Scenario: a template over the caps variant keeps its OS capabilities
- **WHEN** the base version declares `additionalOsCapabilities: ["ALL"]`
- **THEN** the composed image's request carries
  `additionalOsCapabilities: ["ALL"]`

### Requirement: context files can neither escape the context nor replace base entries

`copy(src, dst)` sources SHALL be read relative to the build context
(`context_dir`/`contextDir`, the current directory by default), and a
`src` that resolves outside it SHALL raise `BuildException`/`BuildError`
with `reason="context_path_outside"` before any upload. Context files
SHALL be stored under `__rayito_context/` in the artifact zip, and every
rendered `COPY` SHALL read from there, so no context file can replace the
composed `Dockerfile` or any entry of the base zip.

#### Scenario: the default context directory works
- **WHEN** `Template.build()` is called without `context_dir` from a
  directory containing `app/main.py` and the spec has `copy("app/", ...)`
- **THEN** the artifact contains `__rayito_context/app/main.py`

#### Scenario: a project Dockerfile never replaces the composed one
- **WHEN** the context contains a file named `Dockerfile` copied with
  `copy(".", "/srv/app/")`
- **THEN** the artifact's root `Dockerfile` is the composed one and the
  project file is at `__rayito_context/Dockerfile`

### Requirement: rendered Dockerfile values and ready commands are escaped identically in both SDKs

`COPY` SHALL be rendered in its JSON-array form, `ENV` values SHALL escape
`\`, `"` and `$`, an `ENV` key SHALL match `[A-Za-z_][A-Za-z0-9_]*`, and a
line break in any value SHALL raise `InvalidArgumentException`/
`InvalidArgumentError`. `wait_for_url`/`wait_for_process`/`wait_for_file`
SHALL quote their argument with POSIX single quotes (`shlex.quote`).
`testdata/templates/dockerfile-cases.json` SHALL be the shared vector
file both SDKs' tests render.

#### Scenario: a newline in RUN cannot inject an instruction
- **WHEN** a step's command contains a newline
- **THEN** rendering raises `InvalidArgumentException` in Python and
  `InvalidArgumentError` in TypeScript

### Requirement: rayd starts the template's start_cmd and gates /ready on its ready_cmd

A `rayd` that finds a valid `/etc/rayito/template.json`
(`rayito.template/1`) at boot SHALL, before answering any hook (so the
build-time `/ready` snapshot already holds it), start `start_cmd` as a
managed process (listed by `commands.list`) and poll `ready_cmd` with
`/bin/sh -c`: `/ready` SHALL answer 503 while `ready_cmd` has not exited 0
within `ready_poll.timeout_seconds`, and 500 once that deadline passes, so
AWS fails the build at once with the "server error" `stateReason` (Q85). A missing, unreadable, malformed or
unknown-version file SHALL leave `/ready` and `/suspend` exactly as in
0.5.x.

#### Scenario: no template.json means no participant
- **WHEN** `rayd` boots in an image without `/etc/rayito/template.json`
- **THEN** no lifecycle participant is registered and `/ready` behaves as
  in 0.5.x

#### Scenario: the gate opens when the probe succeeds
- **WHEN** `ready_cmd` exits non-zero, then zero, before the deadline
- **THEN** the gate answers retry, then ok

#### Scenario: a ready_cmd that never succeeds fails /ready with 500
- **WHEN** the deadline passes with no zero exit
- **THEN** `/ready` answers 500, not 503

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

`Template.build()` SHALL hold its slot until the gate settles, not only
while submitting; `build_in_background` holds it only while composing and
submitting. An AWS `ServiceQuotaExceededException` from
`create`/`update-microvm-image` SHALL raise the same
`reason="build_quota"`.

#### Scenario: the eleventh concurrent build is rejected locally
- **WHEN** 10 builds are already in flight in the same process and an
  eleventh `Template.build()` is started
- **THEN** it raises immediately with `reason="build_quota"`, before any
  AWS call

#### Scenario: the account quota maps to build_quota
- **WHEN** `create-microvm-image` answers `ServiceQuotaExceededException`
- **THEN** `BuildException(reason="build_quota")` is raised, not a raw
  SDK error

### Requirement: template names are validated and errors never echo identifiers

A template name SHALL be 1-64 characters of `[A-Za-z0-9_-]` or a full
image ARN; anything else SHALL raise `TemplateException`/`TemplateError`
before any AWS call. `BuildException`/`NotFoundException` messages SHALL
name the template the caller passed and the image/version states, never an
ARN or the raw `stateReason`.

#### Scenario: an E2B name:tag is rejected
- **WHEN** `Template.build(t, "mi-template:v1", bucket="b")` is called
- **THEN** `TemplateException` is raised before any AWS call

### Requirement: the E2B shim's Template never raises AttributeError for a method E2B defines

Every method E2B's `Template` class defines SHALL exist on
`rayito.e2b.Template`/`rayito/e2b`'s `Template`: the build methods (which
work, delegating to the native class) and the four tag methods
(`alias_exists`/`assign_tags`/`remove_tags`/`get_tags`,
`aliasExists`/`assignTags`/`removeTags`/`getTags`), which raise
`UnimplementedError` naming the limitation (no per-version tagging in
`create`/`update-microvm-image`) rather than being absent.

The shim's build methods SHALL accept E2B's signature
(`build(template, alias, cpu_count, memory_mb, skip_cache, on_build_logs,
**opts)` in Python, `build(template, { alias, cpuCount, memoryMB,
skipCache, onBuildLogs })` or `build(template, alias, {...})` in
TypeScript): `skip_cache` maps to `force`, `memory_mb` rounds up to the
next supported size with `RayitoCompatWarning`, `cpu_count` warns and is
ignored, and `bucket`/`region`/`session` (TS: `bucket`/`region`) come from
the call or from `E2B(...)`; with no bucket in either,
`InvalidArgumentException`/`InvalidArgumentError` names the option.

#### Scenario: calling an unimplemented tag method raises UnimplementedError, not AttributeError
- **WHEN** `rayito.e2b.Template().assign_tags(...)` is called
- **THEN** `UnimplementedError` is raised naming
  `Template.assignTags`/`Template.assign_tags`, not `AttributeError`

#### Scenario: E2B's alias and skip_cache keywords work
- **WHEN** `E2B(bucket="b").Template.build(t, alias="x", skip_cache=True)`
  is called
- **THEN** the native build runs for `x` with `bucket="b"` and `force=True`

### Requirement: the OptionalStack component templates provisions only IAM, never a bucket, image or function

`OptionalStacks.deploy("templates")`/the TS equivalent SHALL create only
the `RayitoTemplateBuilder` managed policy (`infra/templates.yaml`); it
SHALL never create, and `destroy()` SHALL never delete, an S3 bucket, a
MicroVM image or a Lambda function. The policy SHALL scope image actions
to this account's `microvm-image:*`, SHALL deny updating the images named
by `ProtectedImageNamePrefix` (`rayito-base` by default), SHALL grant
`lambda:PassNetworkConnector` only on the AWS managed connectors, and
SHALL grant `Resource: "*"` only for `lambda:CreateMicrovmImage`, which
AWS authorizes on `*` rather than on the new image's ARN (AWS_API_NOTES.md
Q114).

#### Scenario: deploying templates creates no bucket, image or function
- **WHEN** `OptionalStacks.deploy("templates", parameters={...})` is
  called
- **THEN** the stack's only resource is `AWS::IAM::ManagedPolicy`

#### Scenario: the builder cannot overwrite rayito-base
- **WHEN** the policy's statements are read
- **THEN** a `Deny` covers `UpdateMicrovmImage` on
  `microvm-image:${ProtectedImageNamePrefix}*` (a create on an existing
  name fails, so it cannot replace a base image either)
