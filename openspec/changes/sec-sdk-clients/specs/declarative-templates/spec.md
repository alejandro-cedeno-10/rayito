## ADDED Requirements

### Requirement: Build contexts never follow symlinks inside copied directories
`collect_context_files` (Python) and `collectContextFiles` (TypeScript) SHALL skip every symbolic link found while walking a copied directory, whether it points to a file or a directory and whether its target is inside or outside the context, so a link's target is never read or packed. A top-level `CopyStep.src` SHALL still be resolved and rejected with `context_path_outside` when it resolves outside the context. Right before reading, each file SHALL be re-checked to resolve inside the context and opened with `O_NOFOLLOW` where the platform has it; a file that no longer passes SHALL raise `context_path_outside`. (The image zip of `rayito image zip`/`publish` refuses symlinks instead; that rule comes from `sec-supply-chain-followups`.)

#### Scenario: a file symlink to a secret outside the context
- **WHEN** `Template().copy("app", "/app")` builds a context where `app/config` links to a file outside the context
- **THEN** the collected entries contain the regular files of `app/` and not `app/config`, in both SDKs

#### Scenario: a directory symlink inside a copied directory
- **WHEN** the copied directory holds a symlink to an outside directory
- **THEN** none of that directory's files are collected, in both SDKs

### Requirement: .dockerignore follows Docker's pattern semantics in both SDKs
`DockerIgnore` (Python `rayito._templates._dockerignore`, TypeScript `templates/dockerignore.ts`) SHALL implement the semantics of Docker's `.dockerignore` (moby `patternmatcher`): lines are trimmed, blank lines and lines starting with `#` are ignored, a leading UTF-8 BOM is dropped, `!` negates; each pattern is cleaned like `filepath.Clean` and anchored at the context root; `*` and `?` never match `/`; `[...]` is a character class (`!` or `^` negates, `a-z` ranges, `\` escapes); a `**` segment matches zero or more whole segments (one or more when it is the last segment), so a leading `**/` also matches at the root; a pattern that matches a parent directory excludes its descendants; and the last matching pattern wins. Matching SHALL NOT use backtracking regular expressions, so a hostile pattern costs at most polynomial time. Both SDKs SHALL pass the shared vectors in `testdata/templates/dockerignore-vectors.json`.

#### Scenario: docker init defaults exclude root secrets
- **WHEN** the context's `.dockerignore` holds `**/.env` and `**/.git` and the context root has `.env` and `.git/config`
- **THEN** neither file is collected, in both SDKs

#### Scenario: a single star does not cross directories
- **WHEN** the `.dockerignore` holds `*.pyc`
- **THEN** `x.pyc` is excluded and `sub/x.pyc` is collected

### Requirement: Packaging likely secrets warns
After collecting the context, both SDKs SHALL warn once (Python `UserWarning`, TypeScript `process.emitWarning` with type `RayitoContextWarning`) when any collected path matches `SENSITIVE_PATTERNS` (`**/.env`, `**/.env.*`, `**/.git`, `**/.aws`, `**/.ssh`, `**/*.pem`, `**/*.key`), naming the count and up to `SENSITIVE_SAMPLE_SIZE` paths and never any file content. The warning SHALL NOT exclude anything.

#### Scenario: a root .env without a .dockerignore
- **WHEN** a context with a root `.env` is collected without a `.dockerignore`
- **THEN** a warning names `.env` and `.dockerignore`, does not contain the file's content, and `.env` is still collected

### Requirement: A rejected template build is a sanitized BuildException
`submit_build` (Python) and `submitBuild` (TypeScript) SHALL translate any AWS error other than the build quota into `BuildException`/`BuildError` with `reason="aws_error"`, whose message and cause carry only the sanitized summary (`sanitize_aws_error`/`sanitizeAwsError`). The TypeScript build adapter SHALL route every AWS SDK call through the sanitizer, keeping the error `name` so code-based handling still works.

#### Scenario: a signature error on create-microvm-image
- **WHEN** `create-microvm-image` fails with `InvalidSignatureException` carrying the canonical string
- **THEN** the build raises `reason="aws_error"` and neither its message, its cause nor `util.inspect`/a traceback contains the session token, the access key id or `$response`
