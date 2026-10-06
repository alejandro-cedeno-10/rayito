## Why

The project is developed and accepted on an AWS account that belongs to
neither the project nor its users, and nothing about that environment may
reach the public repository: not the organization's name or domains, not
its account IDs, not its SSO portal, not the IDs of the resources an
acceptance run touched. A leak audit (2026-10) re-read the whole pushed
history, every PR head, every PR and issue body, comment, review and edit
history, the release notes, the Actions logs and the published packages.
It found no identifier of that environment, but the gate that keeps it
that way (`scripts/check_hygiene.py`) only knew about four resource
prefixes, only knew account IDs inside ARNs and bucket names, could not
know the organization's name without publishing it, never looked at PR
text, and no secret scanner ran over the history.

## What Changes

- **More fixed-shape rules** in `scripts/check_hygiene.py`: every common
  AWS resource-ID prefix (route tables, gateways, endpoints, EFS file
  systems, access points and mount targets, network connectors, AMIs,
  snapshots, volumes, instances, launch templates), with a placeholder
  test that accepts low-entropy suffixes (containing `0123` or `abcd`, a character
  repeated six times in a row); account IDs next to an account key
  (`accountId`, `sso_account_id`, `--account-id`, …); IAM Identity Center
  start URLs, instance IDs and identity-store IDs; presigned-URL
  signatures and STS session tokens; JWT/JWE and GitHub, npm, PyPI and
  Slack tokens. Single-digit-repeated accounts (`111111111111`) count as
  placeholders.
- **Private denylist**: terms that must never appear but cannot be written
  in the gate (the organization, its domains, its accounts) come from the
  `RAYITO_HYGIENE_DENYLIST` environment variable (a repository secret in
  CI) and from the untracked `.git/info/hygiene-denylist`. A match reports
  the file, the line and a generic reason, never the term.
- **stdin**: `check_hygiene.py -` scans standard input, so CI can check a
  PR's title and body with the same rules.
- **`leaks.yml`**: a read-only workflow with `gitleaks` 8.30.1 (pinned by
  version and sha256) over the whole history reachable from `HEAD`,
  redacted, with the reviewed false positives fingerprinted in
  `.gitleaksignore`, and a `pr-text` job that runs the gate over the PR
  title and body on open, edit, reopen and push.
- The CI `check` job passes the denylist secret to its hygiene step;
  `CONTRIBUTING.md` and the project skill state the rule.

## Capabilities

### Modified Capabilities

- `ci-hardening`: the hygiene requirement gains the new rules, the private
  denylist and stdin; a new requirement adds the history and PR-text
  scans.

## Impact

`scripts/check_hygiene.py` and its tests, `.github/workflows/ci.yml`, a new
`.github/workflows/leaks.yml`, `.gitleaksignore`, `CONTRIBUTING.md`,
`.claude/skills/rayito-engineering/`. No runtime, SDK or image change; no
package version changes.
