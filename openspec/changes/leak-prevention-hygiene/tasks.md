## 1. Tests first

- [x] 1.1 `test_check_hygiene.py`: a positive and a negative case for every
  new rule (resource prefixes, account keys, SSO portal, presigned URL and
  session token, tokens), the private denylist from the environment and
  from `.git/info/hygiene-denylist`, a finding that never echoes the term,
  and stdin.

## 2. Gate

- [x] 2.1 `check_hygiene.py`: new rules, low-entropy placeholder suffixes,
  repeated-digit accounts, the private denylist and `-` for stdin; the
  module docstring lists every rule.
- [x] 2.2 The repository passes the extended gate with no change to the
  tree.

## 3. CI

- [x] 3.1 `ci.yml`: the hygiene step receives `RAYITO_HYGIENE_DENYLIST`.
- [x] 3.2 `leaks.yml`: `gitleaks` 8.30.1 pinned by sha256 over the `HEAD`
  history, redacted; `pr-text` over the PR title and body;
  `contents: read`.
- [x] 3.3 `.gitleaksignore` with the fingerprints of the reviewed false
  positives.
- [x] 3.4 Repository secret `RAYITO_HYGIENE_DENYLIST` set by the
  maintainer.

## 4. Docs

- [x] 4.1 `CONTRIBUTING.md` and the project skill (`SKILL.md`,
  `reference/gates.md`): the private denylist, PR text, gitleaks.

## 5. Gates

- [x] 5.1 `pytest scripts/tests`, `ruff`, `mypy`, `actionlint`,
  `check_pins.py`, `check_hygiene.py`, `gitleaks`, `mkdocs --strict` and
  OpenSpec `validate --all --strict`.
