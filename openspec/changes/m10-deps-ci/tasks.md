## 1. Kernel sidecar runtime pins

- [x] 1.1 Reconstruct `kernel-sidecar/requirements.in` (the direct
      dependencies behind `requirements.txt`'s header comment) and confirm
      it reproduces the current file via `uv pip compile --only-binary :all:
      --python-platform aarch64-manylinux_2_28 --python-version 3.12`
- [x] 1.2 Regenerate `requirements.txt` with `ipykernel==7.3.0` (#17),
      `pandas==3.0.6` (#16) and the rest of #28's updates, letting `uv`
      resolve every transitive pin
- [x] 1.3 Root-cause and fix `test_data_formatter_resolves_frames_and_series_lazily`'s
      `KeyError: 'e2b/data'` under pandas 3 in
      `ipython/startup/0002_data.py`, keeping pandas 2 behaviour identical
- [x] 1.4 Bump the `ruff` dev pin in `kernel-sidecar/uv.lock` to match #28
      (`uv lock --upgrade-package ruff`)
- [x] 1.5 Exclude `requirements.in` from the shipped image zip
      (`clients/python/src/rayito/cli/_artifact.py`'s `EXCLUDED_FILES`)
- [x] 1.6 `cd kernel-sidecar && uv run --with-requirements requirements.txt pytest` green
- [x] 1.7 `RAYITO_REQUIRE_BASH_KERNEL=1 uv run --with-requirements
      requirements.txt --with-requirements requirements-poly.txt pytest -m
      kernel` green (Linux aarch64, Lima VM `rayito`)
- [x] 1.8 `uvx ruff==0.16.7 check .`, `uvx ruff==0.16.7 format --check .`,
      `uv run mypy src` green (matches `ci.yml`'s kernel-sidecar job)
- [x] 1.9 `uvx pip-audit==2.10.1 -r requirements.txt --no-deps --strict` and
      the same for `requirements-poly.txt` green
- [x] 1.10 Build `image/rayito-image.zip` locally (`cp`, `copy_sidecar.py`,
      `image_zip.py`, no publish) and sanity-import the new pins the way
      `image/Dockerfile` does (`matplotlib.pyplot, pandas, numpy, scipy,
      sklearn, ipykernel, jupyter_client, zmq`, `python -m ipykernel
      --version`)

## 2. TypeScript 7

- [x] 2.1 Bump `typescript` to 7.0.2 in `clients/typescript/package.json`,
      `pnpm install`
- [x] 2.2 `pnpm lint typecheck build test pack:check` green; diff the
      generated `.d.mts`/`.d.cts` against a 5.9.3 build
- [x] 2.3 `pnpm audit --prod --audit-level moderate` green
- [x] 2.4 Record the verdict and evidence in `clients/typescript/CHANGELOG.md`
      (kept at 7.0.2; no Dependabot ignore needed since it works end-to-end)

## 3. `--remap-path-prefix` in CI and release

- [x] 3.1 `ci.yml` (`build` job): build `rayd` with the same
      `--remap-path-prefix` rustflags as `make build`'s `REMAP_CONFIG`
- [x] 3.2 `release.yml` (`rayd` job): same fix
- [x] 3.3 New step in both jobs: fail if `strings <binary> | grep -c
      /home/runner` is non-zero
- [x] 3.4 Verify locally (Lima VM `rayito`, real `cargo auditable zigbuild`
      with the same `--config` string): binary has zero `/home/runner`
      matches and zero matches of the local build machine's home either
- [x] 3.5 `actionlint`, `python3 scripts/check_pins.py` clean on both
      workflows
- [x] 3.6 Record the fix in `crates/rayd/CHANGELOG.md`

## 4. Specs

- [x] 4.1 `code-execution` spec: update the exact pins in "Image ships the
      kernel stack and a warm snapshot"
- [x] 4.2 `release-automation` spec: update "rayd release artefacts carry an
      embedded dependency list, an SBOM and cosign bundles" with the remap
      and the runner-path gate
- [x] 4.3 `openspec validate m10-deps-ci --strict` passes

## 5. Close the loop

- [x] 5.1 One PR to `main`, Spanish body, CI green
- [ ] 5.2 Tell the maintainer to close #28, #17, #16 and #6 once the PR
      merges (not done from this change)
