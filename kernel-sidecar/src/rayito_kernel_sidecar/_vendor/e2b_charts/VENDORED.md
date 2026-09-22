# Vendored `e2b_charts`

- Package: `e2b-charts` 1.0.0 (PyPI), MIT, copyright FOUNDRYLABS, INC.
- Upstream: https://github.com/e2b-dev/code-interpreter (`python/e2b_charts`)
- sdist: `e2b_charts-1.0.0.tar.gz`,
  sha256 `95aecbd4b62cf0998c3460a5b2e40412696f03cb7b17176ddd8d4e08c50a9572`
- Copied verbatim (the `e2b_charts/` tree and `LICENSE`); the only change is
  the import path `rayito_kernel_sidecar._vendor.e2b_charts`.
- Runtime dependencies it brings: `matplotlib`, `numpy`, `pydantic` (pinned in
  `kernel-sidecar/requirements.txt`).
- Excluded from `ruff` and `mypy` (`pyproject.toml`).
