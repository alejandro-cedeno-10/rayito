## ADDED Requirements

### Requirement: The base image is pinned by digest and by explicit version
`image/Dockerfile` SHALL start with `FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3` (the manifest-list digest of the tag recorded 2026-09-16; its single `linux/arm64` manifest is `sha256:63831a97f9e498f7693c6f42951fe5d935947e6ae25a11fcab1fe0f5ae60b2a7`), and the comment above it SHALL state the date, both digests and how to refresh them with Docker (`docker buildx imagetools inspect`) and without it (the `public.ecr.aws` token endpoint and a `HEAD /v2/lambda/microvms/manifests/al2023-minimal`, reading `Docker-Content-Digest`). `scripts/publish_image.py` SHALL require `--base-image-version` (exit 2 with the usage line before any AWS call when absent) and SHALL always send `baseImageVersion` in `create-microvm-image`/`update-microvm-image`. The Makefile SHALL pass `--base-image-version $(BASE_IMAGE_VERSION)` on `image-publish`, `image-publish-slim` and `image-publish-caps`, with `BASE_IMAGE_VERSION` defaulting to the `imageVersion` that `list-managed-microvm-image-versions` returns for the newest managed version (`1` today); if the API rejects that spelling the default SHALL become the spelling the API echoes (`1.0`) and both facts SHALL be recorded in `AWS_API_NOTES.md` §16 Q52 together with the result of the first digest-pinned build. A digest bump SHALL be treated as a new image version, published and accepted with the e2e like any other.

#### Scenario: publish without the version is refused
- **WHEN** `python scripts/publish_image.py --artifact image/rayito-image.zip` runs without `--base-image-version`
- **THEN** it exits 2 with the usage line and makes no AWS call (asserted in `scripts/tests/test_publish_image.py`)

#### Scenario: digest-pinned build succeeds
- **WHEN** `make image-publish` runs with the pinned `Dockerfile` and `BASE_IMAGE_VERSION` set
- **THEN** the new `rayito-base` version reaches `SUCCESSFUL`/`ACTIVE`, `get-microvm-image-version` echoes a `baseImageVersion`, Q52 records the accepted spelling, the build time and the `snapshotBuild` sizes next to 16.0's, and the e2e passes on that version
