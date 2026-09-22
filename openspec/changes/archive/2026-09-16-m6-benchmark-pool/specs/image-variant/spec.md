## ADDED Requirements

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
`scripts/publish_image.py --variant full|slim` (default `full`) SHALL default the image name to `rayito-base` for `full` and `rayito-base-slim` for `slim` (`--image-name` still overrides), keep the log group `/rayito/<image-name>`, and before any upload SHALL open the artifact zip and require that the marker entry is present with content `slim` if and only if `--variant slim` was given; a mismatch SHALL exit with a message and no AWS call. `IMAGE_HOOKS`, `--memory-mib`, `cpuConfigurations`, `baseImageArn` and the three-state gate SHALL be identical for both variants, and `/validate` SHALL remain enabled so the slim image gets the same disk prefetch sampling as the full one.

#### Scenario: slim publish
- **WHEN** `publish_image.py --artifact image/rayito-image-slim.zip --variant slim` runs
- **THEN** it targets `arn:…:microvm-image:rayito-base-slim`, logs to `/rayito/rayito-base-slim`, sends the same `hooks` and `resources` as the full publish, and prints `RAYITO_TEMPLATE=` with the slim ARN

#### Scenario: mismatched artifact refused
- **WHEN** `--variant slim` is given with `image/rayito-image.zip` (no marker), or `--variant full` with a slim zip
- **THEN** the script exits non-zero naming the mismatch before `head_object`, `put_object` or any `lambda-microvms` call

### Requirement: Make and CI expose the slim variant
`Makefile` SHALL provide `image-zip-slim` (`image/rayito-image-slim.zip`, reusing `build` and the sidecar copy), `image-publish-slim` (`publish_image.py --artifact image/rayito-image-slim.zip --variant slim $(PUBLISH_ARGS)`) and remove the slim zip in `clean`; `.github/workflows/ci.yml` SHALL build the slim zip after the existing full zip step and assert that the marker entry is present in the slim archive and absent in the full one.

#### Scenario: CI smoke
- **WHEN** the CI image job runs
- **THEN** both zips are produced and the marker assertion passes without any AWS call
