## 1. SDKs

- [x] 1.1 Python: `dnf_install`, `npm_install(packages=None, g=False)`, root
      wrap, `NODE_PACKAGES`, `NPM_MISSING_EXIT_CODE`.
- [x] 1.2 TypeScript: `dnfInstall`, `npmInstall(packages?, { g })`, same
      constants.
- [x] 1.3 `apt_install`/`aptInstall` message names `dnf_install`/`dnfInstall`.

## 2. Tests and docs

- [x] 2.1 Unit tests in both SDKs and the e2b shim: quoting, validation,
      optional packages, positional `g`, root wrap, async builder.
- [x] 2.2 e2e: `dnf_install` after a non-root `set_user`, `npm ls --prefix`,
      missing npm exit code (AWS real).
- [x] 2.3 `templates.md` section and E2B compatibility table, CHANGELOGs.

## 3. Acceptance

- [x] 3.1 Gates and e2e on AWS real (Python and TypeScript, `dnf_install`/`npm_install` and the missing-npm case).
