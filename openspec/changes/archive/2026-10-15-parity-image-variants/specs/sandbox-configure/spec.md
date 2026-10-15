## MODIFIED Requirements

### Requirement: a feature that needs the execution role inside the guest is gated to the caps image variant before launch when the image name allows it
`require_caps_for(feature, image_variant)` / `requireCapsFor(feature, imageVariant)` SHALL raise `UnimplementedError` before `run-microvm` when the template name resolves to a known variant other than `base-caps` and `base-caps-efs`, and SHALL do nothing (deferring the decision to the agent's own `Health.features` after boot) when the variant cannot be determined from the name. A name resolves to a variant only when it matches `rayito-<variant>[-<size>]` with `<variant>` in the closed catalogue `base`, `base-caps`, `base-caps-efs`, `base-poly`, `base-slim` (`limits.json` `officialImageVariants`) and `<size>` in the size catalogue; any other name (including a custom name that starts with `rayito-`) and any ARN SHALL resolve to unknown.

#### Scenario: a known non-caps variant is rejected before any AWS call
- **WHEN** a caps-only feature option is set together with `template="rayito-base"`
- **THEN** `UnimplementedError` is raised before `run-microvm` is called, naming the `base-caps` requirement

#### Scenario: an unresolvable image name defers to the agent
- **WHEN** a caps-only feature option is set together with a custom image name or an ARN
- **THEN** `require_caps_for` does not raise, and the decision is left to the post-boot `Health.features` check

#### Scenario: a custom name that starts with the project prefix is not interpreted
- **WHEN** the template is `rayito-mybuild` or `rayito-accreg-ts-caps-efs`
- **THEN** the variant resolves to unknown and no feature option is rejected before launch

#### Scenario: a sized official name resolves to its variant
- **WHEN** the template is `rayito-base-caps-efs-4gb`
- **THEN** the variant resolves to `base-caps-efs`

## ADDED Requirements

### Requirement: the efs gate is a shared helper
`require_efs_for(feature, image_variant)` / `requireEfsFor(feature, imageVariant)` SHALL raise `UnimplementedError` before `run-microvm` when the variant is known and is not `base-caps-efs`, and SHALL do nothing when the variant is unknown. `plan_volumes`/`planVolumes` and the shim's `volume_mounts=` use it in place of the caps gate; the `volumes=` behaviour itself is specified in `efs-volumes`.

#### Scenario: the helper skips an unknown variant
- **WHEN** `require_efs_for("volumes", None)` is called
- **THEN** nothing is raised
