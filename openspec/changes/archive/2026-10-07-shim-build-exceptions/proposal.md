## Why

In Python, `rayito.e2b.BuildException` and `rayito.e2b.TemplateException` were
shim-only classes that nothing raised, while `Template.build()` (native and
shim) raises `rayito.BuildException`/`rayito.TemplateException`. An E2B
program with `except BuildException` imported from `rayito.e2b` never caught a
failed build. TypeScript already re-exports the native classes.

## What Changes

- `rayito.e2b.BuildException`/`TemplateException` become the native classes
  re-exported (same objects), so `except rayito.BuildException` and
  `except rayito.e2b.BuildException` catch the same failure.
- **BREAKING (Python):** `rayito.e2b.BuildException` is now a
  `SandboxException` (in E2B it subclasses `Exception` directly).
- Tests in both SDKs assert identity; docs updated.

## Capabilities

### Modified Capabilities

- `e2b-compat`: E2B exception names.

## Impact

`clients/python/src/rayito/e2b/exceptions.py`, unit tests in both SDKs,
`docs/site/docs/e2b-compat.md`, `docs/site/docs/e2b-parity.md`, changelogs.
