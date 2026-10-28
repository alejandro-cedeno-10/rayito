## Why

Two gaps remain in the optional-stack CLI, and the real-AWS e2e harness has
several defects:

- `rayito domain deploy` has no `--tag`, although `CustomDomain.deploy` takes
  `tags` and `rayito stack deploy` and `rayito events deploy` offer the flag.
  An organization that enforces tags on created resources cannot use the
  documented command.
- A `deploy` that ends in `ROLLBACK_COMPLETE` only says
  `la pila 'X' terminó en ROLLBACK_COMPLETE`; the cause lives in the stack
  events and the user has to open the console.
- E2E harness: the Python session sweeper terminates every live MicroVM of the
  image, also those it did not create; the custom-domain e2e ignore
  `RAYITO_E2E_TAGS`, quote the WebSocket echo server through the shell (it
  breaks), start background servers with the 60 s default timeout, and accept
  a single 404 as a TTL expiry; the interrupted-setup e2e leaves the artifact
  zip; the Python setup helper sends another region's bucket to the default
  region; the 60-minute credential rotation has no TypeScript test.

## What Changes

- `rayito domain deploy` accepts repeatable `--tag K=V` with the semantics of
  `rayito stack deploy --tag`, parsed before any confirmation or AWS call.
- `OptionalStacks.failure_reason()` / `failureReason()` (public, read-only):
  the earliest failed resource of the latest operation from
  `DescribeStackEvents` (first page, `StackName` only), redacted, falling back
  to `StackStatusReason`. `StackException` / `StackError` gain the additive
  attribute `state`, set only by `wait()` with the terminal state of the stack.
- `rayito stack deploy` and `rayito domain deploy` print
  `  motivo: <resource> (<status>): <reason>` on stderr when the error comes from
  `wait()` and its `state` is `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` or
  `UPDATE_ROLLBACK_COMPLETE`. A rejected `CreateStack`/`UpdateStack`, or a wait
  that ends in another state (`UPDATE_ROLLBACK_FAILED`, `DELETE_FAILED`), prints
  no reason and never reads it, because the reason could come from an older
  operation. Reading it is best effort: a failure is ignored.
- The Python e2e session terminates only the MicroVMs its own control plane
  created; the others are reported by number. `RAYITO_E2E_TAGS` reaches the
  custom-domain e2e; the WebSocket echo server is one shared file uploaded with
  the files API; background servers run without a timeout; the TTL 404 needs
  `STABLE_OBSERVATIONS` consecutive answers and at least the TTL (minus a named
  clock-skew tolerance) since the route was registered; the interrupted-setup
  zip is deleted only when the run is shown to have uploaded it; the default
  region test no longer receives the test region's setup arguments; the
  TypeScript persistence e2e gains the slow credential-rotation test.

No new environment variable or kill switch in production code. The new IAM
permission `cloudformation:DescribeStackEvents` is optional.

## Impact

- Affected specs: `cli`, `optional-stacks`, `sdk-persistence`, `e2e-workflow`.
- Affected code: `cli/{domain,stack}.py`, `commands/{domain,stack}.ts`, the
  `_stacks`/`stacks` model, adapter, port documentation and services,
  `StackException`/`StackError`, and the e2e harness of both SDKs.
- Docs (es/en): CLI, custom domain, optional stacks, IAM; `AWS_API_NOTES.md`
  §21; the `rayito-engineering` skill and `CONTRIBUTING.md`.
