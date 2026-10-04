## 1. Reproduce (tests first, red on the old tree)

- [x] 1.1 `scripts/tests/test_release_workflow.py`: credential split,
  no caches, fresh tool root, hash-first Python build, hashed twine, pinned
  uv, `rayd-upload` gated without `--clobber`, sign after `sha256sum -c`,
  tag on `main`, exact identity, concurrency (15 red).
- [x] 1.2 `scripts/tests/test_check_pins.py`: workflow and composite-action
  download gate (6 red).
- [x] 1.3 `scripts/tests/test_dependabot_cooldown.py`,
  `scripts/tests/test_e2e_workflow.py`,
  `scripts/tests/test_security_docs.py::test_cosign_recipes_bind_the_installed_version`
  (3 red).
- [x] 1.4 `clients/typescript/tests/unit/linear-time-parsing.test.ts`:
  six call sites, 1.5-2.2 s each on 50 000 characters (red).

## 2. Fix

- [x] 2.1 `release.yml`: `rayd-build`/`rayd-sign`/`rayd-upload`, no caches,
  fresh tool root, tag on `main`, exact identity, concurrency, uv pinned,
  hash-first `python-build`, hashed twine, `package-manager-cache: false`.
- [x] 2.2 `.github/actions/zig`, `.github/actions/cargo-deny`;
  `ci.yml` (`deny`, `build`, `check`) and `audit.yml` use them.
- [x] 2.3 `scripts/check_pins.py` sixth gate and new default paths.
- [x] 2.4 `.github/release/requirements-twine.txt` (hash-pinned).
- [x] 2.5 `.github/dependabot.yml` cooldown; `e2e.yml` role gate.
- [x] 2.6 TypeScript: `src/strings.ts` and the five call sites.

## 3. Docs

- [x] 3.1 `SECURITY.md` T10 and the supply-chain table,
  `docs/site/docs/{verify,security}.md`, `README.md`, `docs/RELEASING.md`,
  `infra/README.md`.
- [x] 3.2 `[Unreleased]` → `Security` in the three package changelogs.

## 4. Repository settings and triage (outside the tree)

- [x] 4.1 Environment `release` (reviewer, tag policy `rayd-v*`); `e2e`
  limited to `main` with a reviewer; tag ruleset (no update, no deletion);
  fork-PR approval for all external contributors.
- [x] 4.2 Dismiss CodeQL #9 and #16-#20 after checking each location;
  #10-#14 close with 2.6. Done 2026-10-04: #16-#20 `used in tests`, #9
  `won't fix` (dev-only simulator, not in the wheel).

## 5. Verification

- [x] 5.1 Gates: Python, TypeScript, docs, OpenSpec, root scripts,
  actionlint (macOS; no Rust change).
- [x] 5.2 Dry run of `release.yml` for `rayd-v<current>` from this branch:
  `rayd-build` and `rayd-sign` green, `rayd-upload` skipped (run of
  2026-10-04: zig sha256 OK, build output `sha256sum -c` OK in the sign job,
  cosign self-check `Verified OK` with the exact identity, no cache restored).
- [x] 5.3 Green CI on the pull request (the `deny` job runs the verified
  cargo-deny, `build` the verified zig).
