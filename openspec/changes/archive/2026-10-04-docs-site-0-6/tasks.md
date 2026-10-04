## 1. Docs site

- [x] 1.1 `novedades/index.md`, `novedades/0.6.0.md`, `novedades/0.6.1.md`.
- [x] 1.2 Home page, nav ("Novedades" tab, "En desarrollo" group),
  optional-features, E2B parity, images, cost, concepts, errors.
- [x] 1.3 Guides: webhook signature verification (Python, Node), template
  background builds/logs and E2B mapping, stack parameters and
  `parameter_changes`, stale notes removed.
- [x] 1.4 Python reference (`OptionalStacks`, `S3Mount`, `LifecycleEvents`
  and their models) and TypeScript reference (0.6 options, members, peers).

## 2. Tooling

- [x] 2.1 `check_docs_examples.py --cli` with unit tests and a test over
  the real site.
- [x] 2.2 CI docs job, `make docs-examples` and the gates reference run it.
- [x] 2.3 Fix the documented commands it flags (`rayito [--json] doctor`).
