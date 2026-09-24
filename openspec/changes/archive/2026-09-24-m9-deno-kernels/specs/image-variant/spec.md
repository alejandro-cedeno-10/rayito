## MODIFIED Requirements

### Requirement: The poly image ships the bash kernel through one conditional Dockerfile layer
`image/Dockerfile` SHALL contain exactly one layer guarded by `[ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]`. The layer SHALL do nothing when the marker is absent. When the marker is present it SHALL:
- install the pins of `/opt/rayito/sidecar/requirements-poly.txt` (`bash_kernel` and its resolved dependencies) with `pip`, followed by `pip check`;
- fail the build unless `python3 -c 'import bash_kernel'` succeeds as `user` and the `rayito-bash` kernelspec template exists;
- download `https://github.com/denoland/deno/releases/download/v2.9.7/deno-aarch64-unknown-linux-gnu.zip` with `curl` and verify it with `sha256sum -c` against `c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf`. Version and hash are set in the instruction as `DENO_VERSION` and `DENO_SHA256`;
- extract only the `deno` member with Python `zipfile` (the image has no `unzip`) to `/opt/rayito/deno/deno`, owned by root with mode `0755`, and remove the zip;
- fail the build unless `/opt/rayito/deno/deno --version` run as `user` prints `deno 2.9.7` and the `rayito-javascript` and `rayito-typescript` templates exist.

The snapshot SHALL still be taken after `/ready` with only the Python default kernel warm. The bash and Deno kernels SHALL be started only by the sidecar on first use.

The layer SHALL NOT install Node.js, `ijavascript` or a compiler. The M7 spike showed that `ijavascript@5.2.1` → `jmp@2` → `zeromq@5.3.1` has no linux-arm64 prebuild and fails with `not found: make` (`AWS_API_NOTES.md` Q57). Deno 2.9.7 is one self-contained binary that runs on the image's glibc 2.34 (Q61).

A Deno version change SHALL be a reviewed edit of both values that republishes `rayito-base-poly` and re-runs its acceptance.

#### Scenario: poly build verifies its kernel
- **WHEN** `publish_image.py --artifact image/rayito-image-poly.zip --variant poly --base-image-version 1` runs
- **THEN** the version reaches `SUCCESSFUL`/`ACTIVE`, its layer ran `pip check`, `import bash_kernel` as `user`, the sha256 check, the `deno --version` check as `user` and the three template checks, and the first `run_code("echo hi", language="bash")` and `run_code("1 + 1", language="typescript")` on a sandbox from it return `hi` and `2`

#### Scenario: full build skips the layer
- **WHEN** `rayito-base` is rebuilt from the same Dockerfile with the marker-less zip
- **THEN** the layer installed nothing (no `requirements-poly.txt` install, `bash_kernel` not importable and no `/opt/rayito/deno` in the sandbox), the `snapshotBuild` sizes stay within the M7 bands of the previous `rayito-base` (`codeInstallSizeInBytes` net of the `rayd` binary size change, which other changes of the milestone move), and `run_code("echo hi", language="bash")` and `run_code("1", language="typescript")` on a sandbox from it fail with `UNIMPLEMENTED`

#### Scenario: a tampered download fails the build
- **WHEN** the downloaded zip does not match `DENO_SHA256`
- **THEN** `sha256sum -c` exits non-zero, the poly build fails with that step in its `stateReason`, and no `rayito-base-poly` version reaches `ACTIVE`
