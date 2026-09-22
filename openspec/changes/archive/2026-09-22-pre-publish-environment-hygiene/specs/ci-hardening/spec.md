## ADDED Requirements

### Requirement: Tracked files carry no environment identifiers
No tracked file SHALL carry an identifier of the environment the project was developed or accepted in. `scripts/check_hygiene.py` (standard library only, no network) SHALL scan every file listed by `git ls-files`, or the files given as arguments, skipping only binary files (a NUL byte or invalid UTF-8), and SHALL report, as `KO <path>:<line>: <rule>` without echoing the line, every occurrence of: a 12-digit account ID in an ARN or in an S3 bucket or ECR registry name other than the placeholders `123456789012`, `000000000000`, `111122223333` and `444455556666`; the default CDK bootstrap qualifier after `cdk-`; an SSO profile or role name carrying account data (`AdministratorAccess-<digit>`, `<PermissionSet>Access-<12 digits>`, `AWSReservedSSO_<set>_<16 hex>`); a MicroVM ID (`microvm-` followed by eight hex digits) or endpoint (`<uuid>.lambda-microvm.`) whose UUID is not `00000000-0000-0000-0000-` followed by twelve decimal digits (the truncated prefix `00000000` of those fakes also passes); a `vpc-`, `subnet-`, `sg-` or `eni-` ID of 8 or 17 hex digits other than `0123456789abcdef0`; a Windows drive path or Git Bash drive path into `Users`, `tools` or `Projects`; and an AWS access key ID (`AKIA`/`ASIA` followed by 16 uppercase letters or digits) that does not end in `EXAMPLE`. It SHALL exit 1 when it reports anything and 0 otherwise. The CI `check` job and `make lint` SHALL run it next to `scripts/check_pins.py`, and its unit tests SHALL live in `scripts/tests/test_check_hygiene.py`, with a positive and a negative case per rule and a run over the real repository.

#### Scenario: a real account in an ARN fails the gate
- **WHEN** a tracked file contains `arn:aws:iam::<a 12-digit account that is not a placeholder>:role/deployer`
- **THEN** the gate prints the file, the line and the rule, and exits 1

#### Scenario: documented placeholders pass
- **WHEN** a tracked file contains `arn:aws:iam::123456789012:role/x`, `arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1`, `amzn-s3-demo-bucket`, `microvm-00000000-0000-0000-0000-000000000142`, `vpc-0123456789abcdef0` and `AKIAIOSFODNN7EXAMPLE`
- **THEN** nothing is reported

#### Scenario: an access key is never echoed
- **WHEN** a tracked file contains an access key ID
- **THEN** the gate reports its file and line and the output does not contain the key

#### Scenario: untracked files are out of scope
- **WHEN** an untracked, ignored file such as `spike/m0/out/results.jsonl` contains account IDs and MicroVM IDs
- **THEN** the gate run without arguments does not read it

#### Scenario: the repository is clean
- **WHEN** `python3 scripts/check_hygiene.py` runs at the root of the repository
- **THEN** it exits 0
