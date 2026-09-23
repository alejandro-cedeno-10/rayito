# sandbox-git Specification

## Purpose
TBD - created by archiving change m9-e2b-v2-surface. Update Purpose after archive.

## Requirements

### Requirement: Git module over commands.run
The SDK SHALL ship a native git module that follows E2B 2.51's git method set and runs every operation as a foreground `commands.run` in the sandbox. There SHALL be no new RPC and no agent change. The module SHALL be available as:

- `rayito.Git` (sync, over `Commands`)
- `rayito.AsyncGit` (async, over the async `Commands`)
- the TypeScript `Git`

Each SHALL be exposed as the lazily built property `sandbox.git` of the native `Sandbox`/`AsyncSandbox`, of `rayito.e2b.Sandbox`/`AsyncSandbox`, and of the TS native and `rayito/e2b` sandboxes.

The Python methods SHALL take E2B's Python parameters in E2B's order:

- `clone(url, path, branch, depth, username, password, envs, user, cwd, timeout, request_timeout, dangerously_store_credentials)`
- `init(path, bare, initial_branch, ...)`
- `remote_add(path, name, url, fetch, overwrite, ...)`
- `remote_get(path, name, ...) -> str | None`
- `status(path, ...) -> GitStatus`
- `branches(path, ...) -> GitBranches`
- `create_branch(path, branch, ...)`, `checkout_branch(path, branch, ...)`, `delete_branch(path, branch, force, ...)`
- `add(path, files, all, ...)`
- `commit(path, message, author_name, author_email, allow_empty, ...)`
- `reset(path, mode, target, paths, ...)`
- `restore(path, paths, staged, worktree, source, ...)`
- `push(path, remote, branch, set_upstream, username, password, ...)`
- `pull(path, remote, branch, username, password, ...)`
- `set_config(key, value, scope, path, ...)`, `get_config(key, scope, path, ...) -> str | None`
- `dangerously_authenticate(username, password, host, protocol, ...)`
- `configure_user(name, email, scope, path, ...)`

Here `...` is `envs, user, cwd, timeout, request_timeout`. The TypeScript methods SHALL be the camelCase mirror, with an options object: `clone(url, opts)`, `remoteAdd(path, name, url, opts)`, `status(path, opts)`, `setConfig(key, value, opts)`, `dangerouslyAuthenticate(opts)`, and so on.

Each command SHALL be a shell-quoted `git [-C <path>] <args...>` whose arguments equal E2B's for the same call. For example, `status` SHALL run `git status --porcelain=1 -b`, and `branches` SHALL run `git branch --format=%(refname:short)\t%(HEAD)`. `envs` SHALL be `{"GIT_TERMINAL_PROMPT": "0", **envs}`. `timeout=None` SHALL mean no server deadline. Invalid arguments SHALL raise `InvalidArgumentException` (TS `InvalidArgumentError`) before any RPC: an unknown reset mode, an empty `restore` path list, a password without a username, or credentials on a non-http(s) URL.

#### Scenario: exact commands on the wire
- **WHEN** the unit test calls `sbx.git.status("/repo")`, `sbx.git.add("/repo")`, `sbx.git.commit("/repo", "msg", allow_empty=True)` and `sbx.git.reset("/repo", mode="soft", target="HEAD~1")` against the fake `ProcessService`
- **THEN** the recorded `Start` commands are `'git' '-C' '/repo' 'status' '--porcelain=1' '-b'`, `'git' '-C' '/repo' 'add' '-A'`, `'git' '-C' '/repo' 'commit' '-m' 'msg' '--allow-empty'` and `'git' '-C' '/repo' 'reset' '--soft' 'HEAD~1'`, and each carries `GIT_TERMINAL_PROMPT=0`

#### Scenario: invalid arguments fail before any RPC
- **WHEN** the unit test calls `sbx.git.reset("/repo", mode="bogus")` and `sbx.git.restore("/repo", [])`
- **THEN** both raise `InvalidArgumentException`, and the fake records no `Start`

### Requirement: Git models and parsing
The SDK SHALL define the following models, frozen in Python:

- `GitFileStatus(name, status, index_status, working_tree_status, staged, renamed_from=None)`.
- `GitStatus(current_branch, upstream, ahead, behind, detached, file_status)`, with the derived `is_clean`, `has_changes`, `has_staged`, `has_untracked`, `has_conflicts`, `total_count`, `staged_count`, `unstaged_count`, `untracked_count` and `conflict_count`.
- `GitBranches(branches, current_branch)`.
- `GitResetMode = Literal["soft", "mixed", "hard", "merge", "keep"]`.

TypeScript SHALL provide the camelCase mirror of all four.

`status` SHALL parse the porcelain v1 output with `-b`, including:

- the branch line with upstream and `ahead N, behind M`
- `No commits yet on`
- a detached `HEAD (no branch)`
- renames (`R  old -> new`)
- untracked `??`
- the conflict codes `DD`, `AU`, `UD`, `UA`, `DU`, `AA`, `UU`

`branches` SHALL parse the `%(refname:short)\t%(HEAD)` listing.

#### Scenario: porcelain fixtures
- **WHEN** the unit test parses the fixtures for a clean repo, a repo ahead 2 and behind 1 of `origin/main`, a detached HEAD, a repo with no commits, a rename, a conflict and an untracked file
- **THEN** the results report `is_clean`, `ahead == 2` and `behind == 1`, `detached`, the unborn branch name, `renamed_from`, `has_conflicts` and `untracked_count == 1` respectively

### Requirement: Git credentials
The module SHALL handle credentials with E2B's semantics and SHALL never log them or place them in an exception.

- **`clone` with `username` and `password`:** it SHALL clone from the URL carrying the percent-encoded credentials. Unless `dangerously_store_credentials=True`, it SHALL then reset `origin` to the URL without them. When no destination path is given and none can be derived from the URL, it SHALL raise `InvalidArgumentException` before any RPC.
- **`push` and `pull` with credentials:** they SHALL read the remote URL, set the remote to the URL with credentials, run the command, and always restore the original URL, even when the command fails.
- **`dangerously_authenticate`:** it SHALL set `credential.helper` to `store` globally and approve the credentials with `git credential approve`. They are then stored in `~/.git-credentials` of the sandbox user, readable by code running in the sandbox, and the docstring and `docs/site/docs/git.md` SHALL state that in bold.

The git module SHALL emit no log records.

#### Scenario: credentials are restored after a failing push
- **WHEN** the unit test calls `sbx.git.push("/repo", username="u", password="s3cr3t")` and the fake answers the push with exit 128
- **THEN** the recorded commands are, in order, `remote get-url`, `remote set-url` with the credentialed URL, `push`, and `remote set-url` with the original URL

#### Scenario: failures never leak the password
- **WHEN** a credentialed `clone` fails with the password present in the fake's stderr
- **THEN** the raised `CommandExitException` has neither the password nor its percent-encoded form in its message, `stderr`, `stdout` or cause chain, and no `rayito.*` log record contains it

### Requirement: Git errors
A `CommandExitException` from a git command SHALL be mapped as follows:

- Output matching E2B's authentication snippets (for example `authentication failed`, `terminal prompts disabled`, `could not read username`, `permission denied`) SHALL become `GitAuthException`, a subclass of `AuthenticationException`. TypeScript SHALL use `GitAuthError`, a subclass of `AuthenticationError`. The message SHALL name the git action and SHALL NOT contain the URL.
- For `push` and `pull`, output matching E2B's missing-upstream snippets SHALL become `GitUpstreamException`, a subclass of `SandboxException`. TypeScript SHALL use `GitUpstreamError`. The message SHALL be guidance written in Spanish.
- Any other failure SHALL re-raise the `CommandExitException`, redacted when credentials were involved.

#### Scenario: classification
- **WHEN** the fake answers a push with exit 128 and stderr `fatal: could not read Username for 'https://github.com': terminal prompts disabled`, and a pull with exit 1 and stderr `There is no tracking information for the current branch.`
- **THEN** the first raises `GitAuthException` and the second raises `GitUpstreamException`

### Requirement: The default image ships git-core
`image/Dockerfile` SHALL install `git-core-2.50.1-1.amzn2023.0.1`, with version and release pinned, in its first `dnf install` layer, so all four image variants have `git`. The build-time tool check SHALL include `git`.

`scripts/check_pins.py` SHALL gate every package listed in `PINNED_DNF_PACKAGES`, which contains `git-core`. A `dnf install` token in `image/Dockerfile` naming such a package without `-<version>-<release>` SHALL be a finding.

The installed size of `git-core` and its dependencies SHALL be recorded in `AWS_API_NOTES.md` §16 after the M9 image is published, using placeholders only. The recorded values SHALL be:

- the RPM installed size, measured in an emulated arm64 build at 31,452,146 B across seven packages
- the published version's `codeInstallSizeInBytes` and `memorySnapshotSizeInBytes`, compared with the previous published version

#### Scenario: the pin gate
- **WHEN** `scripts/tests/test_check_pins.py` runs the gate over a Dockerfile fragment with a bare `git-core`, one with `git-core-2.50.1-1.amzn2023.0.1`, and one with an unrelated unpinned package
- **THEN** only the first yields a finding

### Requirement: Git accepted against real AWS
The e2e `tests/e2e/test_m9_git.py` SHALL run against a real MicroVM of the M9 `rayito-base` image and SHALL perform, through `sbx.git`:

1. Clone a public HTTPS repository with `depth=1`. The default is the public `https://github.com/octocat/Hello-World.git`, overridable with `RAYITO_E2E_GIT_REPO`.
2. `configure_user`, `status`, `branches`, `create_branch`, `checkout_branch`.
3. `add`, `commit`, `reset(mode="soft")`, `restore(staged=True)`.
4. `set_config` and `get_config` with `scope="local"`.
5. `init` a bare repository and a working repository inside the sandbox, `remote_add`, `push`, clone the bare repository, push a second commit and `pull` it.
6. A `pull` on a branch without upstream.
7. A `push` to the public HTTPS remote without credentials.
8. `dangerously_authenticate` against a placeholder host.

The e2e SHALL print the git version and per-operation seconds. The TypeScript e2e corpus SHALL run `git.clone` and `status`.

#### Scenario: git on AWS
- **WHEN** the e2e runs with `RAYITO_E2E=1` and `RAYITO_TEMPLATE` pointing at the M9 image
- **THEN** every step succeeds and the status fields match the performed changes; the `pull` of the bare clone contains the second commit; the upstream-less `pull` raises `GitUpstreamException`; the credential-less `push` raises `GitAuthException`; `cat ~/.git-credentials` inside the sandbox shows the placeholder host line; and the SDK's captured logs do not contain the password
