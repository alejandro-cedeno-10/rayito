## ADDED Requirements

### Requirement: Build contexts never follow symlinks inside copied directories
`collect_context_files` (Python) and `collectContextFiles` (TypeScript) SHALL skip every symbolic link found while walking a copied directory, whether it points to a file or a directory and whether its target is inside or outside the context, so a link's target is never read or packed. A top-level `CopyStep.src` SHALL still be resolved and rejected with `context_path_outside` when it resolves outside the context. `rayito.cli._artifact.shipped_files` (used by `rayito image publish` and `scripts/image_zip.py`) SHALL apply the same rule and SHALL prune excluded directories before touching anything inside them.

#### Scenario: a file symlink to a secret outside the context
- **WHEN** `Template().copy("app", "/app")` builds a context where `app/config` links to a file outside the context
- **THEN** the collected entries contain the regular files of `app/` and not `app/config`, in both SDKs

#### Scenario: a directory symlink inside the image directory
- **WHEN** the image directory holds a symlink to an outside directory
- **THEN** the image zip contains none of that directory's files

### Requirement: A rejected template build is a sanitized BuildException
`submit_build` (Python) and `submitBuild` (TypeScript) SHALL translate any AWS error other than the build quota into `BuildException`/`BuildError` with `reason="aws_error"`, whose message and cause carry only the sanitized summary (`sanitize_aws_error`/`sanitizeAwsError`). The TypeScript build adapter SHALL route every AWS SDK call through the sanitizer, keeping the error `name` so code-based handling still works.

#### Scenario: a signature error on create-microvm-image
- **WHEN** `create-microvm-image` fails with `InvalidSignatureException` carrying the canonical string
- **THEN** the build raises `reason="aws_error"` and neither its message, its cause nor `util.inspect`/a traceback contains the session token, the access key id or `$response`
