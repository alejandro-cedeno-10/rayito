## ADDED Requirements

### Requirement: a local Docker environment runs the SDKs against a real rayd and an emulated AWS
The repository SHALL provide `dev/local/compose.yaml` with three services: `floci` (Floci pinned by image digest), `guest` (built from the unchanged `image/Dockerfile` with `rayd` as PID 1) and `runner` (the Python and Node toolchains). `make local-up` SHALL build and start them and install both SDKs' dependencies from their lockfiles into volumes, `make local-e2e` SHALL run the `local` pytest marker and the `local` vitest project inside the runner, and `make local-down` SHALL remove the containers and volumes. No service SHALL publish a port on the host, run privileged or mount the Docker socket, and no image SHALL contain credentials.

#### Scenario: one command runs both suites
- **WHEN** `make local-up` and then `make local-e2e` run on an arm64 Docker host
- **THEN** the Python tests marked `local` and the TypeScript `local` project run against the guest and Floci, and the command exits non-zero if any of them fails

#### Scenario: nothing is exposed on the host
- **WHEN** the environment is up
- **THEN** `docker compose ps` shows no published ports and the `floci` container runs as uid 1001 with an empty effective capability set, a read-only root filesystem and no route to the Internet

### Requirement: the local control plane is test-only and decorates the real adapter
Each SDK's `tests/local/` SHALL define a `LocalGuestControlPlane` implementing the `ControlPlane` port by delegating `resolve_template_arn`, `run_microvm`, `get_microvm`, `list_microvms`, `list_microvms_page`, `terminate_microvm` and `get_microvm_image_version` to the real adapter pointed at Floci, and SHALL drive the guest's `/ready`, `/run`, `/suspend`, `/resume` and `/terminate` hooks for what Floci does not emulate. It SHALL be selected only by the tests when `RAYITO_LOCAL_GUEST` is set, SHALL NOT be part of the published wheel or npm package, and SHALL NOT require relaxing any SDK check.

#### Scenario: a sandbox runs in the guest
- **WHEN** a local test calls `Sandbox.create` with the local control plane and the loopback transport
- **THEN** Floci records the `run-microvm`, the guest's `/run` answers `installed`, and `commands.run("whoami")` returns `user`

#### Scenario: without the variable the tests skip
- **WHEN** `pytest -m local` or `vitest run --project local` runs without `RAYITO_LOCAL_GUEST`
- **THEN** every local test is skipped and no network call is made

### Requirement: the optional features are exercised against the emulator
The local suites SHALL deploy, read and destroy every `OptionalStack` component that Floci can provision, list sandboxes through the metadata index, inject a secret from `SecretStore` into a sandbox, apply a `gateways=` route in the guest, build a template with `Template.build` (artifact upload and `create-microvm-image`), and run the events pipeline from `LifecycleEvents.deploy` through the forwarder and deliverer handlers to a signed delivery on a loopback receiver.

#### Scenario: a signed webhook reaches the local receiver
- **WHEN** the forwarder handler receives a line signed with the stack key and the deliverer handler processes the events table's stream
- **THEN** the receiver gets exactly one POST whose `e2b-signature` equals base64 of `sha256(secret + body)` and whose body names the sandbox

### Requirement: the local workflow is pinned and least-privilege
`.github/workflows/local-e2e.yml` SHALL run `make local-up` and `make local-e2e` on `ubuntu-24.04-arm` for pull requests that touch the local environment, on a nightly schedule and on `workflow_dispatch`, with `permissions: contents: read`, every action pinned to a commit SHA, no secrets and no `id-token`, and SHALL always run `make local-down`.

#### Scenario: a PR touching dev/local runs the job
- **WHEN** a pull request changes a file under `dev/local/`
- **THEN** the `local-e2e` job runs without access to any repository secret and tears the environment down even if a test failed
