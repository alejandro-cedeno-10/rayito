## 1. Dependabot

- [x] 1.1 Remove the `pip` entry for `/kernel-sidecar` from `.github/dependabot.yml` and explain why in the header comment
- [x] 1.2 Ignore semver-major updates of `@types/node` in the `npm` entry
- [x] 1.3 `uv run --with pyyaml python -c "import sys, yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in sys.argv[1:]]" .github/ISSUE_TEMPLATE/*.yml .github/dependabot.yml` exits 0 with six `updates` entries
- [x] 1.4 Close the duplicate `pip` pull requests and the `@types/node` 26 one
