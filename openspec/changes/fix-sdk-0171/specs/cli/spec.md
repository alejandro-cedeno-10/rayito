## ADDED Requirements

### Requirement: A failed agent deploy removes the token file it created
When `rayito agent deploy --token-file F` fails after writing `F` and before the fleet exists (a build, host or transport failure, an invalid argument, a declined confirmation or any other error), both CLIs SHALL delete `F`, so the same command can be retried. With `--keep-on-failure` they SHALL keep `F`, the only access to the kept sandbox. A file that existed before the run SHALL never be touched (the command already refuses it), and the default `.rayito/agent-token` SHALL never be deleted.

#### Scenario: a failed deploy can be retried
- **WHEN** `rayito agent deploy --token-file F` fails with a `SandboxException`, an `InvalidArgumentException` or any other error and is run again
- **THEN** `F` does not exist after the failure and the retry succeeds and writes a new token to `F`

#### Scenario: keep-on-failure keeps the token
- **WHEN** `rayito agent deploy --token-file F --keep-on-failure` fails
- **THEN** `F` still holds the token

#### Scenario: the default token file of an earlier run survives
- **WHEN** a deploy without `--token-file` fails and `.rayito/agent-token` existed before
- **THEN** the file keeps its content
