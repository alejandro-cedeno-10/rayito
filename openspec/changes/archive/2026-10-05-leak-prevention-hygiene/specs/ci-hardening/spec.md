## MODIFIED Requirements

### Requirement: Tracked files carry no environment identifiers
No tracked file SHALL carry an identifier of the environment the project was developed or accepted in. `scripts/check_hygiene.py` (standard library only, no network) SHALL scan every file listed by `git ls-files`, or the files given as arguments, or standard input when an argument is `-` (reported as `<stdin>`), skipping only binary files (a NUL byte or invalid UTF-8), and SHALL report, as `KO <path>:<line>: <rule>` without echoing the line, every occurrence of: a 12-digit account ID in an ARN, in an S3 bucket or ECR registry name, or after an account key (`account`, `account_id`, `accountId`, `sso_account_id`, `"Account":`, `--account-id`) other than the placeholders `123456789012`, `000000000000`, `111122223333`, `444455556666` and any account made of one repeated digit; the default CDK bootstrap qualifier after `cdk-`; an SSO profile or role name carrying account data (`AdministratorAccess-<digit>`, `<PermissionSet>Access-<12 digits>`, `AWSReservedSSO_<set>_<16 hex>`); an IAM Identity Center start URL (`<name>.awsapps.com` other than `my-sso-portal` and `d-xxxxxxxxxx`, or `<name>.portal.<region>.app.aws`), instance ID (`ssoins-<16 hex>`) or identity-store ID (`d-<10 hex>`); a MicroVM ID (`microvm-` followed by eight hex digits) or endpoint (`<uuid>.lambda-microvm.`) whose UUID is not `00000000-0000-0000-0000-` followed by twelve decimal digits (the truncated prefix `00000000` of those fakes also passes); a `vpc-`, `subnet-`, `sg-`, `eni-`, `rtb-`, `igw-`, `acl-`, `pcx-`, `tgw-`, `tgw-attach-`, `vpce-`, `eipalloc-`, `fs-`, `fsap-`, `fsmt-`, `nc-`, `ami-`, `snap-` or `vol-` ID of 8 or 17 hex digits, or a `nat-`, `lt-` or `i-` ID of 17, whose suffix contains neither `0123` nor `abcd` and repeats no character six times in a row; a Windows drive path or Git Bash drive path into `Users`, `tools` or `Projects`; a macOS home path (`/Users/` followed by a name of letters, digits, `.`, `_` or `-`, not preceded by a word character, `.` or `-`) or a macOS temporary path (`tmp/` or `var/` directly under `/private/`); an AWS access key ID (`AKIA`/`ASIA` followed by 16 uppercase letters or digits) that does not end in `EXAMPLE`; a presigned-URL signature (`X-Amz-Signature=` and 64 hex digits), a presigned security token of 40 or more characters, or an STS session token; a compact JWT or JWE (`eyJ…` with dot-separated segments) or a GitHub, npm, PyPI or Slack token; and any term of the private denylist. The private denylist SHALL be read, case-insensitively, from the `RAYITO_HYGIENE_DENYLIST` environment variable (comma- or newline-separated) and from the untracked file `$(git rev-parse --git-path info/hygiene-denylist)` (one term per line, `#` comments), SHALL never be written to a tracked file, and a finding SHALL never name the term. It SHALL exit 1 when it reports anything and 0 otherwise. The CI `check` job and `make lint` SHALL run it next to `scripts/check_pins.py`, the CI step with the `RAYITO_HYGIENE_DENYLIST` repository secret, and its unit tests SHALL live in `scripts/tests/test_check_hygiene.py`, with a positive and a negative case per rule and a run over the real repository.

#### Scenario: a real account in an ARN fails the gate
- **WHEN** a tracked file contains `arn:aws:iam::<a 12-digit account that is not a placeholder>:role/deployer`
- **THEN** the gate prints the file, the line and the rule, and exits 1

#### Scenario: documented placeholders pass
- **WHEN** a tracked file contains `arn:aws:iam::123456789012:role/x`, `arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1`, `amzn-s3-demo-bucket`, `microvm-00000000-0000-0000-0000-000000000142`, `vpc-0123456789abcdef0`, `fs-0123abcd`, `fsap-0456abcd`, `"Account": "111111111111"`, `my-sso-portal.awsapps.com` and `AKIAIOSFODNN7EXAMPLE`
- **THEN** nothing is reported

#### Scenario: an access key is never echoed
- **WHEN** a tracked file contains an access key ID
- **THEN** the gate reports its file and line and the output does not contain the key

#### Scenario: untracked files are out of scope
- **WHEN** an untracked, ignored file such as `.claude/notes.jsonl` contains account IDs and MicroVM IDs
- **THEN** the gate run without arguments does not read it

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_hygiene.py` runs at the root of the repository
- **THEN** it exits 0

#### Scenario: a macOS workstation path fails the gate
- **WHEN** a tracked file contains an absolute path into a macOS home directory (`/Users/<a name>/…`) or into the `tmp` or `var` directory under `/private/`
- **THEN** the gate reports the local-path rule for that line, while `/Users/<tu-usuario>`, a relative `docs/Users/…` and the URL `https://example.com/a/Users/list` pass

#### Scenario: a private term fails the gate without being echoed
- **WHEN** `.git/info/hygiene-denylist` lists a term and a tracked file contains it in another case, inside a URL
- **THEN** the gate reports the file, the line and the private-denylist rule, exits 1, and its output does not contain the term

#### Scenario: an account next to its key fails the gate
- **WHEN** a tracked file contains `sso_account_id = <a 12-digit account that is not a placeholder>`
- **THEN** the gate reports the account-field rule

#### Scenario: PR text is checked through stdin
- **WHEN** `python3 scripts/check_hygiene.py -` reads a PR body whose second line carries a GitHub token
- **THEN** it reports `<stdin>:2` with the token rule, does not print the token, and exits 1

## ADDED Requirements

### Requirement: The history and PR text are scanned for leaks
`.github/workflows/leaks.yml` SHALL run on pushes to `main`, on pull requests (`opened`, `edited`, `reopened`, `synchronize`) and on demand, with `permissions: contents: read`, no Actions cache and `persist-credentials: false`. Its `gitleaks` job SHALL download gitleaks at a fixed version, verify the tarball against a pinned sha256 before extracting it, and run `gitleaks git --log-opts=HEAD --redact --exit-code 1` over a full-depth checkout, so every commit reachable from the checked-out ref is scanned and no finding prints its value. Reviewed false positives SHALL be listed only by fingerprint in `.gitleaksignore`. Its `pr-text` job SHALL pass the pull request's title and body to `scripts/check_hygiene.py -` through environment variables, never by interpolating them into the script, with the `RAYITO_HYGIENE_DENYLIST` secret.

#### Scenario: a secret in any reachable commit fails the job
- **WHEN** a commit reachable from `HEAD` adds a credential that gitleaks' default rules detect and whose fingerprint is not in `.gitleaksignore`
- **THEN** the `gitleaks` job fails and its log shows the finding redacted

#### Scenario: a leaked identifier in a PR body fails the job
- **WHEN** a pull request body is edited to contain a real account in an ARN or a private-denylist term
- **THEN** the `pr-text` job runs on the edit and fails with `<stdin>:<line>` and the rule, without the value
