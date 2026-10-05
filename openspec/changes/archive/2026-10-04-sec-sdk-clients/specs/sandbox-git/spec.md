## ADDED Requirements

### Requirement: Credentialed git operations always try to remove the token
For `clone`, `push` and `pull` with `username`/`password`, both SDKs SHALL attempt to restore the remote's clean URL whenever the credentialed `set-url` or the operation fails for any reason (including a timeout or a stream error), SHALL suppress any error of that restore while an operation error is propagating, and SHALL propagate a restore error after a successful operation. Every failed restore SHALL log a warning (Python logger `rayito.git`, TypeScript the injected `logger.warn`) that names the git action only, never the URL, remote or path. A credentialed `clone` that fails with anything other than a git exit error SHALL still attempt to strip `origin`; one that exits non-zero SHALL NOT touch the destination.

#### Scenario: the restore times out after a failed push
- **WHEN** a credentialed push exits 128 and the restoring `set-url` times out
- **THEN** the caller receives the push's `CommandExitException` and the `rayito.git` logger has a warning without the password or the URL

#### Scenario: the clone times out
- **WHEN** a credentialed clone times out
- **THEN** a `remote set-url origin <clean-url>` is attempted in the destination and the timeout is raised

### Requirement: Credentialed git invocations run isolated and refuse URL rewrites
A credentialed `git clone/push/pull` command SHALL carry `-c core.hooksPath=/dev/null -c credential.helper=` before the subcommand, and before the token is sent the SDKs SHALL run `git config --get-regexp '^url\..*\.(push)?insteadof$'` (in the repository for push/pull): exit 1 means none and the operation proceeds; output means a rewrite exists and the call SHALL fail with `GitAuthException`/`GitAuthError` without sending the credentials; any other failure SHALL propagate. Anonymous calls SHALL be unchanged.

#### Scenario: a planted insteadOf
- **WHEN** the sandbox's git config contains `url."http://127.0.0.1:9999/".insteadOf https://`
- **THEN** a credentialed clone or push raises `GitAuthException` and no command containing the password is sent
