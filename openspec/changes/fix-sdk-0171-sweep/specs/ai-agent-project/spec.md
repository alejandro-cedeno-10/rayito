## ADDED Requirements

### Requirement: The base image is required only when the deploy builds
`deploy_agent` / `deployAgent` SHALL resolve the artifact bucket before the base image and SHALL check the base image only when the deploy will build. With `build_image=False` / `buildImage: false`, or without a bucket and with the project image already built, the base image SHALL NOT be read. When the build needs a missing base image and the project image exists, the error SHALL say how to reuse it (`--no-build`).

#### Scenario: reuse without a bucket
- **WHEN** the project image exists, `rayito-base-caps` does not and no bucket resolves
- **THEN** the deploy starts the fleet without building and without reading `rayito-base-caps`

#### Scenario: the build needs the missing base
- **WHEN** a bucket resolves, the project image exists and the base image does not
- **THEN** the deploy fails before creating anything with a message that names `--no-build`
