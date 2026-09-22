## MODIFIED Requirements

### Requirement: The four scripts are thin shims over rayito.cli with unchanged argv
`scripts/publish_image.py` and `scripts/image_prune.py` SHALL delegate to the typer application (`rayito.cli.app.run_shim(["image", "publish", *argv])` and `["image", "prune", *argv]`), so their options are exactly those of `rayito image publish` and `rayito image prune`; when `typer` or `rayito` cannot be imported they SHALL print the `uv run --project clients/python python scripts/<name>.py …` hint and exit 2. `scripts/image_zip.py` and `scripts/copy_sidecar.py` SHALL load `clients/python/src/rayito/cli/_artifact.py` by file path (`importlib.util.spec_from_file_location`, path derived from `__file__`) and run `zip_main(argv)` / `copy_main(argv)`, keeping today's argv (`IMAGE_DIR DESTINATION [--variant full|slim]` and `SOURCE DESTINATION`) and working on a bare `python3` with no third-party packages; `image_zip.py` SHALL re-export `marker_variant`, `VARIANTS`, `write_zip` and `is_excluded`. The `Makefile` SHALL run the two AWS shims through `uv run --project clients/python` (variable `PY`), pass `--bucket $(BUCKET)` on every `image-publish*` target with `BUCKET ?= $(RAYITO_BUCKET)` and no other default (an `image-publish*` target with neither set SHALL stop with a `make` error naming `BUCKET` and `RAYITO_BUCKET` before building anything), remove `BOTO3_SPEC`, and leave `image-zip`/`image-zip-slim` on plain `python`. `.github/workflows/ci.yml` `build` job and `release.yml` SHALL be unchanged.

#### Scenario: stdlib shims run isolated
- **WHEN** `scripts/tests/test_shims.py` runs `python -I scripts/copy_sidecar.py <src> <dst>` and `python -I scripts/image_zip.py <dir> <zip> --variant slim` as subprocesses on a temporary image tree with a `Dockerfile` and a `requirements.txt`
- **THEN** both exit 0, the zip carries `kernel-sidecar/ipython/startup/warmup_variant` with `slim`, excludes `__pycache__`, `tests`, `.venv` and `uv.lock`, and `python -I -c "import sys; sys.path.insert(0, 'scripts'); from image_zip import marker_variant; print(marker_variant('<zip>'))"` prints `slim`

#### Scenario: AWS shims share the CLI parser
- **WHEN** `python scripts/publish_image.py` runs with no arguments inside the client environment
- **THEN** it exits 2 with typer's usage error naming `--artifact` (the missing required option) and makes no AWS call; the same for `--artifact x.zip` without `--base-image-version`

#### Scenario: make targets still work
- **WHEN** `make image-zip` and `make image-publish PUBLISH_ARGS=--force` are run on a box with the client environment and AWS credentials
- **THEN** the zip is produced by the stdlib shim and the publish runs through `uv run --project clients/python python scripts/publish_image.py --artifact image/rayito-image.zip --bucket <BUCKET> --base-image-version 1 --force`

#### Scenario: publishing without a bucket stops before the build
- **WHEN** `make image-publish` runs with neither `BUCKET` nor `RAYITO_BUCKET` set
- **THEN** make stops with an error naming `BUCKET=<tu-bucket>` and `RAYITO_BUCKET` before `cargo zigbuild` runs, and `make image-publish BUCKET=<tu-bucket>` (or with `RAYITO_BUCKET` exported) passes `--bucket <tu-bucket>` to the shim
