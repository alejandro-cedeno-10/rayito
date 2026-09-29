## Why

Four Dependabot PRs against `kernel-sidecar`/`clients/typescript` have been
held since before M9 closed:

- #28 (`uv-kernel-sidecar` group, 11 updates): broken, it bumps
  `pydantic-core` to 2.49.0 without bumping `pydantic` (a fresh
  `uv pip compile` of the same inputs resolves `pydantic-core` back to
  2.46.5 for `pydantic==2.13.5`), so the pin file it would produce cannot
  come from a real compile.
- #17 (`ipykernel` 6.31.0 → 7.3.0) and #16 (`pandas` 2.2.3 → 3.0.6): each
  bumps one pin without regenerating the rest of `requirements.txt`, and
  #16 breaks `test_data_formatter_resolves_frames_and_series_lazily`
  (`KeyError: 'e2b/data'`) because pandas 3 changes `DataFrame`/`Series`'s
  `__module__` from `pandas.core.frame`/`pandas.core.series` to the public
  `pandas`, and the sidecar's lazy formatter (`ipython/startup/0002_data.py`)
  matches printers by `(module, class name)`.
- #6 (`typescript` 5.9.3 → 7.0.2, the native/Go compiler): unevaluated.

Separately, `MILESTONES.md`'s M9 deferred list flags that `ci.yml`'s `build`
job and `release.yml`'s `rayd` job compile `rayd` without the
`--remap-path-prefix` rustflags `make build`'s `REMAP_CONFIG` applies, so the
`rayd` binary published from CI embeds the GitHub runner's paths
(`/home/runner/...`) that a local build strips.

## What Changes

- Regenerate `kernel-sidecar/requirements.txt` from a reconstructed
  `requirements.in` (the direct dependencies `uv pip compile
  --only-binary :all: --python-platform aarch64-manylinux_2_28
  --python-version 3.12` was run against, undocumented until now) with the
  updates from #28, `ipykernel` 7.3.0 (#17) and `pandas` 3.0.6 (#16), letting
  `uv` resolve every transitive pin consistently (this also drops `pytz`/
  `tzdata`, no longer required by `pandas` 3 on Linux, and renames
  `nest-asyncio` to `nest-asyncio2`, ipykernel 7's fork of it).
- Fix `ipython/startup/0002_data.py`'s `e2b/data` formatter to match
  `DataFrame`/`Series` under both the pandas < 3 and pandas >= 3 module
  paths, so the same object resolves the same mime type on either pin.
- Bump the `uv-kernel-sidecar` dev-group pin (`uv.lock`) to match #28's
  `ruff` bump.
- Bump `clients/typescript`'s `typescript` devDependency to 7.0.2: verified
  end-to-end (`pnpm lint typecheck build test pack:check`, `pnpm audit
  --prod`), with byte-identical generated `.d.mts`/`.d.cts` files.
- `ci.yml` (`build` job) and `release.yml` (`rayd` job) build `rayd` with the
  same `--remap-path-prefix` rustflags as `make build`'s `REMAP_CONFIG`, and
  a new step in both fails the job if `strings` finds `/home/runner` in the
  built binary.
- `code-execution` spec: update the exact pins named in "Image ships the
  kernel stack and a warm snapshot".
- `release-automation` spec: update "rayd release artefacts carry an
  embedded dependency list, an SBOM and cosign bundles" to require the same
  remap and the new runner-path gate.

## Impact

- Affected specs: `code-execution`, `release-automation`.
- Affected files: `kernel-sidecar/requirements.in` (new),
  `kernel-sidecar/requirements.txt`, `kernel-sidecar/uv.lock`,
  `kernel-sidecar/ipython/startup/0002_data.py`,
  `clients/python/src/rayito/cli/_artifact.py` (exclude `requirements.in`
  from the shipped image zip), `clients/typescript/package.json`,
  `clients/typescript/pnpm-lock.yaml`, `.github/workflows/ci.yml`,
  `.github/workflows/release.yml`, `crates/rayd/CHANGELOG.md`,
  `clients/typescript/CHANGELOG.md`.
- Dependabot PRs #28, #17, #16 and #6 are superseded by this change and can
  be closed once it merges (not closed here).
- No runtime dependency of the published image's contract changes (same
  mime types, same kernel behaviour); the maintainer still needs to accept
  a `rayito-base` republish against real AWS before the next milestone that
  ships it, per `openspec/project.md`'s "a milestone never closes on mocks"
  rule — flagged here, not run (no AWS access from this change).
