## ADDED Requirements

### Requirement: `rayito domain deploy` accepts repeatable `--tag K=V` with the semantics of `rayito stack deploy --tag`
Both CLIs SHALL accept `--tag K=V` (repeatable) on `rayito domain deploy` and pass the parsed pairs as `tags` to `CustomDomain.deploy`, so the stack carries them next to the three fixed `rayito:` tags, which always win. A pair without `=` SHALL exit with code 2 before the confirmation and before any call to AWS. The option does not change the cost.

#### Scenario: tags reach the stack
- **WHEN** `rayito domain deploy ... --tag team=a --tag env=b --yes` runs
- **THEN** the stack is created with `team=a`, `env=b` and the three fixed tags

#### Scenario: a malformed tag
- **WHEN** `rayito domain deploy ... --tag no-equals --yes` runs
- **THEN** the exit code is 2 and no call to the provisioner is made

### Requirement: A stack deploy that ends in a rollback prints the first failed resource's reason on stderr
When `rayito stack deploy` or `rayito domain deploy` fails because `wait()` found the stack in `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` or `UPDATE_ROLLBACK_COMPLETE`, both CLIs SHALL print on stderr the line `  motivo: <reason>` (the redacted result of `failure_reason`) before the usual `rayito: ...` line and exit with the same code as before. Stdout and `--json` SHALL be unchanged. A failure of `CreateStack`/`UpdateStack`, or a wait that ends in another state such as `UPDATE_ROLLBACK_FAILED` or `DELETE_FAILED`, SHALL print no reason and SHALL NOT call `failure_reason`. Reading the reason is best effort: a `StackException` raised while reading it SHALL be ignored and SHALL NOT replace the original error.

#### Scenario: stack deploy rolls back
- **WHEN** `rayito stack deploy <component> --yes` ends in `UPDATE_ROLLBACK_COMPLETE` and the events show a failed `Distribution` resource
- **THEN** stderr contains `  motivo: Distribution (UPDATE_FAILED): <reason>`, the exit code is 1 and stdout has no `motivo`

#### Scenario: domain deploy rolls back
- **WHEN** `rayito domain deploy ... --yes` ends in `UPDATE_ROLLBACK_COMPLETE`
- **THEN** stderr has the same `  motivo:` line and the exit code is 1

#### Scenario: a rejected call on a stack that was rolled back long ago
- **WHEN** `UpdateStack` is rejected on a stack in `UPDATE_ROLLBACK_COMPLETE` whose events still hold an older failure
- **THEN** stderr has no `motivo:` line and `failure_reason` is not called

#### Scenario: a wait that ends in a state that is not explained
- **WHEN** the wait ends in `UPDATE_ROLLBACK_FAILED` or `DELETE_FAILED`
- **THEN** stderr has no `motivo:` line and `failure_reason` is not called

#### Scenario: reading the reason fails
- **WHEN** `failure_reason` raises `StackException` after a rollback
- **THEN** the original `rayito: la pila ... terminó en ROLLBACK_COMPLETE` line is printed and no `motivo:` line appears
