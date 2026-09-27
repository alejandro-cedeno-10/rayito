## Why

Dependabot started running on 2026-09-25 (GitHub events had not fired in this
repository before) and opened every `kernel-sidecar` update twice: the `uv`
and the `pip` entries both watch `/kernel-sidecar` and both edit
`requirements.txt` (#9/#14, #11/#15, #12/#17, #13/#16). It also proposed
`@types/node` 26 while `engines` says Node >= 20, so the types would describe
APIs the minimum runtime does not have.

## What Changes

- `.github/dependabot.yml`: drop the `pip` entry for `/kernel-sidecar` (the
  `uv` entry already covers it) and ignore semver-major updates of
  `@types/node`.
- `community-health` spec: six `updates` entries instead of seven.

## Impact

- Affected specs: `community-health`.
- Affected files: `.github/dependabot.yml`. No runtime or published artefact
  changes.
