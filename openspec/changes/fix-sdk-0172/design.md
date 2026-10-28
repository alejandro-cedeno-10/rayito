## Decisions

### D1. The reason is read only when the error comes from the wait

`CloudFormationProvisioner.wait()` raises `StackException(code="failed",
state=<terminal state>)`. The same `code` is used for every botocore error of
the adapter, so a rejected `UpdateStack` (for example `AccessDenied`) on a stack
that is already in `UPDATE_ROLLBACK_COMPLETE` from an old operation also has
`code == "failed"`. Reading the events then would print the reason of that old
operation. The attribute `state` (additive, `None`/`undefined` by default) is
set only by `wait()`, and the CLI helper reads the reason only for
`ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` and `UPDATE_ROLLBACK_COMPLETE`.
`UPDATE_ROLLBACK_FAILED` and `DELETE_FAILED` are not explained either: the
latest operation may not be the one that failed.

### D2. The reason is printed by the CLI, never carried by the exception

`StackException` keeps its rule that the message never repeats CloudFormation's.
The CLI prints the redacted text on stderr (`redact_aws_text` /
`redactAwsText`, control characters visible on a terminal), before the usual
`rayito: ...` line, and re-raises the original error: stdout, `--json` and the
exit code do not change.

### D3. Pure domain, adapter that translates

`first_failed_resource(events, stack_name)` walks the events from newest to
oldest (the API order) and stops at the stack's own `CREATE_IN_PROGRESS` /
`UPDATE_IN_PROGRESS` event, so it never mixes an earlier deployment; of the
resource events with `CREATE_FAILED` / `UPDATE_FAILED` it returns the oldest
one seen (the later ones are usually cancellations). Shared vectors:
`testdata/stacks/failure-events.json`. The adapter calls `DescribeStackEvents`
with `StackName` only, reads the first page and maps three fields. The port
signature does not change.

Rejected: putting the reason in the exception message (breaks D2); a new port
method (breaks implementers of the exported TypeScript `StackProvisioner`);
paginating with `NextToken` (not needed for the small stacks of the catalog).

### D4. The Python sweeper terminates what its control plane created

The session `control_plane` is a subclass that records the id returned by every
`run_microvm`. At the end only those ids that are still live are terminated.
MicroVMs that were live before the session and those born during it outside
that control plane (another person, another session) are reported by number,
without ids. What is born through paths that do not use that control plane
(the E2B corpus, the CLI in subprocesses) is bounded by the
`maximumDurationInSeconds=900` guardrail of every test sandbox. TypeScript
already terminates only `created`.

### D5. The interrupted-setup zip

The zip belongs to the run if its key is known and either the resumed run said
`artifactUploaded: true` or a `HeadObject` before the killed subprocess showed
the key did not exist (it needs `RAYITO_E2E_SETUP_ARTIFACT` and
`RAYITO_E2E_SETUP_BUCKET` to know the key beforehand). Otherwise nothing is
deleted and the teardown leaves a note. The zip is uploaded by the interrupted
run, and the resumed run finds it and answers `false`.

### D6. The TTL 404

`STABLE_OBSERVATIONS` consecutive 404s, and the time from `register` to them
must be at least `SHORT_ROUTE_TTL_SECONDS` minus
`EDGE_CLOCK_SKEW_TOLERANCE_SECONDS` (5 s: the `int()` truncation of
`expires_at` plus a clock offset between the test machine and the edge; a
criterion fixed before measuring, not an AWS measurement). An earlier 404 comes
from an edge server that does not have the route yet and fails the test.

## Not in this change

`rayito events deploy` does not use the reason helper. `gates.md` still says
`npx`; this change validates with `pnpm dlx`.
