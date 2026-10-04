## ADDED Requirements

### Requirement: ConfigureSandbox is off by default and the only per-sandbox configuration channel after /run
No SDK call SHALL build a `ConfigureService` gRPC stub, send a `Configure` request, or populate any section of `ConfigureRequest` unless the caller sets at least one of the seven 0.6 options (`mounts`, `volumes`, `size`, `events`, `telemetry`, `gateways`, `domain`) to something other than `None`/`undefined`. With all seven absent, `Sandbox.create()` SHALL make exactly the same boto3/AWS-SDK-v3 calls and gRPC calls as 0.5.x, byte for byte.

#### Scenario: a sandbox with no 0.6 option matches the 0.5.x golden trace
- **WHEN** a scripted session (`create → commands.run → files.write → pause → resume → commands.run → kill → list`) runs with none of the seven options set
- **THEN** the recorded sequence of boto3/AWS-SDK-v3 operations, the `runHookPayload` JSON and the sequence of gRPC methods equal the checked-in `zero_cost_0_5_trace.json` fixture exactly

#### Scenario: setting any one option raises before any AWS call
- **WHEN** `Sandbox.create(template, mounts={...})` (or any of the other six options) is called while the owning feature is still a stub
- **THEN** `UnimplementedError`/`UnimplementedError` is raised naming the option and the OpenSpec change that will implement it, and no `run-microvm`/`RunMicrovm` call is made

### Requirement: a present ConfigureRequest section replaces the feature's state; an absent one is untouched
`ConfigureService.Configure` SHALL apply sections in the fixed order `events → telemetry → gateway → s3_mounts → efs_volumes` (`rayd_core::configure::APPLY_ORDER`). A section absent from the request SHALL leave that feature's current configuration unchanged. A section present in the request, including one with no fields set, SHALL completely replace that feature's previous configuration.

#### Scenario: an absent section changes nothing
- **WHEN** a `ConfigureRequest` with only `lifecycle_events` set is sent
- **THEN** `s3_mounts`, `efs_volumes`, `telemetry_export` and `secret_gateway` keep whatever configuration (or lack of one) they had before the call

### Requirement: ConfigureSandbox is only accepted while the sandbox is RUNNING or RESUMED
`ConfigureService.Configure` and `ConfigureStatus` SHALL answer `FAILED_PRECONDITION` with detail `not_running` when the sandbox's hook phase is not `Running` or `Resumed` (before the first accepted `/run`, during `/suspend`, or during `/terminate`).

#### Scenario: Configure before /run is rejected
- **WHEN** `Configure` is called on a session that has not accepted `/run` yet
- **THEN** the RPC fails with `FAILED_PRECONDITION` and detail `not_running`, and no feature slot's `apply()` is called

### Requirement: a pre-0.6 agent is distinguished from a 0.6 agent whose feature is still a stub
`Health.features` (`AgentFeatures`, proto field 16) SHALL be absent (proto3 message non-presence) on any agent built before this change, and present with `configure=true` on any agent built from this change onward, regardless of whether a given feature's slot is a real adapter or `Unsupported`. The SDK SHALL treat an absent `features` field as "agent needs an image published from a 0.6.0 rayd tag" and a present field with a `false` flag as "this build has no adapter for that feature yet", and these SHALL be distinguishable in the error raised to the caller.

#### Scenario: a 0.5.x agent is reported as needing a 0.6.0 image
- **WHEN** the SDK calls `Health` against an agent built before this change and then needs to use a 0.6 feature
- **THEN** it raises `UnimplementedError` naming "necesita una imagen 0.6.0 o posterior"

#### Scenario: a 0.6.0 agent with every feature still a stub reports only configure=true
- **WHEN** `Health` is called against an agent built from this change with no feature implemented yet
- **THEN** `features.configure` is `true` and every other flag (`s3_mounts`, `efs_volumes`, `lifecycle_events`, `telemetry_export`, `secret_gateway`, `template_start`) is `false`

### Requirement: a feature that needs the execution role inside the guest is gated to the caps image variant before launch when the image name allows it
`require_caps_for(feature, image_variant)` / `requireCapsFor(feature, imageVariant)` SHALL raise `UnimplementedError` before `run-microvm` when the template name resolves (by Rayito's own `rayito-<variant>[-<size>]` naming convention) to a variant other than `base-caps`, and SHALL do nothing (deferring the decision to the agent's own `Health.features` after boot) when the variant cannot be determined from the name (a custom image name or an ARN).

#### Scenario: a known non-caps variant is rejected before any AWS call
- **WHEN** a caps-only feature option is set together with `template="rayito-base"`
- **THEN** `UnimplementedError` is raised before `run-microvm` is called, naming the `base-caps` requirement

#### Scenario: an unresolvable image name defers to the agent
- **WHEN** a caps-only feature option is set together with a custom image name or an ARN
- **THEN** `require_caps_for` does not raise, and the decision is left to the post-boot `Health.features` check

### Requirement: one FeatureSet per agent process backs ConfigureService, Health.features and the lifecycle participants
`rayd` SHALL build exactly one `FeatureSet` per process and share it between `ConfigureService`, `Health.features` and the hooks' lifecycle participants. `Health.features` SHALL be derived from each slot's `supported()`; with every slot `Unsupported` it SHALL equal `AgentFeatures::foundations_only()` and the participant list SHALL be empty.

#### Scenario: an all-Unsupported build reports foundations_only
- **WHEN** `Health` is called on an agent whose six slots are all `Unsupported`
- **THEN** `features` has `configure=true` and every other flag `false`, and `/suspend`, `/resume` and `/terminate` run no participant

### Requirement: lifecycle participants run once per accepted transition, each under its own cap
`/suspend`, `/resume` and `/terminate` SHALL call their participants' `on_suspend`, `on_resume` and `on_terminate` only when the hook's transition was accepted and changed the phase. Each call SHALL run concurrently with the others under its own cap: its `SuspendShares` allocation on `/suspend`, `PARTICIPANT_RESUME_TIMEOUT` on `/resume` and `PARTICIPANT_TERMINATE_TIMEOUT` on `/terminate`. A participant that exceeds its cap SHALL be abandoned and logged by name, and the hook SHALL still answer 200.

#### Scenario: a repeated /terminate runs the participants once
- **WHEN** `/terminate` is called twice
- **THEN** each participant's `on_terminate` ran exactly once

#### Scenario: a hung on_resume does not hold /resume
- **WHEN** one participant's `on_resume` never returns
- **THEN** `/resume` answers 200 after about `PARTICIPANT_RESUME_TIMEOUT`, and every other participant's `on_resume` still ran

