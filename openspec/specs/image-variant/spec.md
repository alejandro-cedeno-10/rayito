# image-variant Specification

## Purpose
TBD - created by archiving change m6-benchmark-pool. Update Purpose after archive.
## Requirements
### Requirement: The kernel warm-up is gated by a marker file next to the startup scripts
`kernel-sidecar/ipython/startup/0004_warmup.py` SHALL read the sibling file `warmup_variant` (`Path(__file__).with_name("warmup_variant")`); when it exists and its stripped content is `slim`, the warm-up SHALL return before importing any module; when it is absent or holds any other content, the full warm-up (numpy, pandas, matplotlib.pyplot, scipy.stats, sklearn.linear_model, one PNG, one chart extraction, `describe()`, `linalg.inv`, namespace left clean) SHALL run as today. The repository SHALL NOT contain the marker; it exists only inside a slim artifact. An image environment variable SHALL NOT be used for this flag (the sidecar's and the kernel's environments are built from scratch by `rayd` and never carry image variables).

#### Scenario: full warm-up without marker
- **WHEN** a kernel starts from the default sidecar root
- **THEN** after `ready` `numpy` and `pandas` are in `sys.modules` and the user namespace contains none of `np`, `pd`, `plt`, `numpy`, `pandas`, `matplotlib`, `scipy`, `sklearn`

#### Scenario: slim kernel imports nothing scientific
- **WHEN** a kernel starts from a sidecar root whose `ipython/startup/warmup_variant` holds `slim`
- **THEN** after `ready` none of `numpy`, `pandas`, `matplotlib` is in `sys.modules` (the `e2b/data` and `e2b/chart` formatters import nothing at start), and `ready.warmup_ms` is reported as usual

### Requirement: The chart and data formatters resolve their types lazily so a slim kernel starts without the stack
`0001_charts.py` SHALL install the `e2b/chart` formatter without importing any matplotlib module: the formatter's `lookup_by_type` SHALL match a class whose MRO contains a class with `(__module__, __name__) == ("matplotlib.figure", "Figure")` and return the chart printer, falling back to `BaseFormatter.lookup_by_type` otherwise. `0002_data.py` SHALL install the `e2b/data` formatter the same way for `("pandas.core.frame", "DataFrame")` and `("pandas.core.series", "Series")` without importing pandas. Neither formatter SHALL rely on `for_type_by_name` or a class attribute set at start: IPython's inline backend setup pops `Figure` from every formatter's registry on the first `pyplot` import (`select_figure_formats`), which erases a deferred registration (measured 2026-09-16 on `rayito-base-slim` 1.0). The first displayed `Figure`, `DataFrame` or `Series` SHALL yield `e2b/chart` / `e2b/data` in both variants exactly as before.

#### Scenario: chart on a slim kernel
- **WHEN** a slim kernel runs `import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()`
- **THEN** the result carries `image/png` and `e2b/chart` with `type == "line"` and three points, exactly as on a full kernel, and no scientific module was in `sys.modules` before that cell

#### Scenario: registration survives the inline backend setup
- **WHEN** `IPython.core.pylabtools.select_figure_formats` runs after the startup scripts (as the first `pyplot` import in an ipykernel does) and a figure is then displayed
- **THEN** the result still carries `e2b/chart`

### Requirement: `image_zip.py --variant slim` writes the marker into the artifact only
`scripts/image_zip.py <image_dir> <destination> [--variant full|slim]` SHALL, for `slim`, add a synthetic entry `kernel-sidecar/ipython/startup/warmup_variant` with content `slim\n` (mode 0644, the fixed 1980-01-01 date of every entry) to the zip without creating any file under `image/`; for `full` (default) it SHALL add nothing. The exclusion rules and the deterministic ordering SHALL be unchanged, so the slim and full zips of the same tree differ only by that entry and therefore by their content hash and S3 key.

#### Scenario: slim zip
- **WHEN** `image_zip.py image image/rayito-image-slim.zip --variant slim` runs
- **THEN** the archive lists `kernel-sidecar/ipython/startup/warmup_variant` with content `slim\n`, `image/kernel-sidecar/ipython/startup/` on disk has no such file, and `image/rayito-image.zip` built with the default variant has no such entry

### Requirement: `publish_image.py --variant` names the image and checks the artifact
`scripts/publish_image.py --variant full|slim|poly` (default `full`) SHALL default the image name to `rayito-base` for `full`, `rayito-base-slim` for `slim` and `rayito-base-poly` for `poly` (`--image-name` still overrides), keep the log group `/rayito/<image-name>`, and before any upload SHALL open the artifact zip and require that `marker_variant` equals the flag (`poly` marker present with `poly` iff `--variant poly`; `slim` marker iff `--variant slim`); a mismatch SHALL exit with a message and no AWS call. `IMAGE_HOOKS`, `--memory-mib`, `cpuConfigurations`, `baseImageArn`, `--base-image-version` and the three-state gate SHALL be identical for the three variants, and `/validate` SHALL remain enabled so every variant gets the same disk prefetch sampling.

#### Scenario: slim publish
- **WHEN** `publish_image.py --artifact image/rayito-image-slim.zip --variant slim` runs
- **THEN** it targets `arn:…:microvm-image:rayito-base-slim`, logs to `/rayito/rayito-base-slim`, sends the same `hooks` and `resources` as the full publish, and prints `RAYITO_TEMPLATE=` with the slim ARN

#### Scenario: poly publish
- **WHEN** `publish_image.py --artifact image/rayito-image-poly.zip --variant poly --base-image-version 1` runs
- **THEN** it targets `arn:…:microvm-image:rayito-base-poly`, logs to `/rayito/rayito-base-poly`, sends the same `hooks`, `resources` and `baseImageVersion` as the full publish, and prints `RAYITO_TEMPLATE=` with the poly ARN

#### Scenario: mismatched artifact refused
- **WHEN** `--variant slim` is given with `image/rayito-image.zip` (no marker), `--variant full` with a slim or poly zip, or `--variant poly` with the full or slim zip
- **THEN** the script exits non-zero naming the mismatch before `head_object`, `put_object` or any `lambda-microvms` call

### Requirement: Make and CI expose the slim variant
`Makefile` SHALL provide `image-zip-slim` (`image/rayito-image-slim.zip`, reusing `build` and the sidecar copy), `image-publish-slim` (`publish_image.py --artifact image/rayito-image-slim.zip --variant slim --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)`), `image-zip-poly` (`image/rayito-image-poly.zip`, same recipe with `--variant poly`), `image-publish-poly` (`publish_image.py --artifact image/rayito-image-poly.zip --variant poly --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)`) and remove the slim and poly zips in `clean`; `.github/workflows/ci.yml` SHALL build the slim and poly zips after the existing full zip step and assert that `marker_variant` returns `slim`, `poly` and `full` for the three archives. `scripts/copy_sidecar.py` SHALL copy `requirements-poly.txt` alongside `requirements.txt` and fail when either is missing.

#### Scenario: CI smoke
- **WHEN** the CI image job runs
- **THEN** the three zips are produced and the marker assertions pass without any AWS call

#### Scenario: sidecar copy carries the poly pins
- **WHEN** `python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar` runs
- **THEN** `image/kernel-sidecar/requirements-poly.txt` exists next to `requirements.txt` and `tests`/caches are still excluded

### Requirement: `image_zip.py --variant poly` writes the kernels marker into the artifact only
`scripts/image_zip.py <image_dir> <destination> [--variant full|slim|poly]` SHALL, for `poly`, add a synthetic entry `kernel-sidecar/kernels_variant` with content `poly\n` (mode 0644, the fixed 1980-01-01 date of every entry) to the zip without creating any file under `image/`; `slim` SHALL keep writing only its warm-up marker and `full` SHALL add nothing. `marker_variant(artifact)` SHALL return `poly` when the kernels marker holds `poly`, `slim` when the warm-up marker holds `slim`, `full` otherwise, and SHALL raise `SystemExit` naming the archive when both markers are present. The exclusion rules and the deterministic ordering SHALL be unchanged, so the three zips of one tree differ only by their marker entries.

#### Scenario: poly zip
- **WHEN** `image_zip.py image image/rayito-image-poly.zip --variant poly` runs
- **THEN** the archive lists `kernel-sidecar/kernels_variant` with content `poly\n` and no `warmup_variant` entry, `image/kernel-sidecar/` on disk has no such file, and `marker_variant` returns `poly` for it, `slim` for the slim zip and `full` for the default zip

#### Scenario: two markers refused
- **WHEN** `marker_variant` reads a hand-made zip carrying both `kernels_variant` and `warmup_variant`
- **THEN** it exits non-zero naming the archive

### Requirement: The poly image ships the bash kernel through one conditional Dockerfile layer
`image/Dockerfile` SHALL contain exactly one layer guarded by `[ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]` that installs the pins of `/opt/rayito/sidecar/requirements-poly.txt` (`bash_kernel` and its resolved dependencies) with `pip` followed by `pip check`, and fails the build unless `python3 -c 'import bash_kernel'` succeeds as `user` and the `rayito-bash` kernelspec template exists. The layer SHALL do nothing when the marker is absent. The snapshot SHALL still be taken after `/ready` with only the Python default kernel warm; the bash kernel SHALL be started only by the sidecar on first use. The layer SHALL NOT install Node.js, `ijavascript` or a compiler: the design's spike showed that `ijavascript@5.2.1` → `jmp@2` → `zeromq@5.3.1` publishes no prebuilt binary for linux-arm64 nor for Node 20 (ABI 115) and its `node-gyp rebuild` fails with `not found: make` on al2023-minimal (`AWS_API_NOTES.md` Q57, `docs/site/docs/kernels.md`), so `javascript` is a known language no image ships (`UNIMPLEMENTED`).

#### Scenario: poly build verifies its kernel
- **WHEN** `publish_image.py --artifact image/rayito-image-poly.zip --variant poly --base-image-version 1` runs
- **THEN** the version reaches `SUCCESSFUL`/`ACTIVE`, its layer ran `pip check`, `import bash_kernel` as `user` and the template check, and the first `run_code("echo hi", language="bash")` on a sandbox from it prints `hi`

#### Scenario: full build skips the layer
- **WHEN** `rayito-base` is rebuilt from the same Dockerfile with the marker-less zip
- **THEN** the layer installed nothing (no `requirements-poly.txt` install, `bash_kernel` not importable in the sandbox), the `snapshotBuild` sizes stay within the D5 band of 17.0 (`codeInstallSizeInBytes` net of the `rayd` binary size change, which other changes of the milestone move), and `run_code("echo hi", language="bash")` on a sandbox from it fails with `UNIMPLEMENTED`

