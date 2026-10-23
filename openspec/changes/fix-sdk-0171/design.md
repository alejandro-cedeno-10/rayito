## Decisions

### D1. The IAM template carries its own fingerprint as a parameter

The fingerprint is the sha256 (hex) of the packaged `infra/iam.yaml`, read
as UTF-8 text. Both SDKs package the same bytes (`gen_stack_assets.py`), so
the value is the same in Python and TypeScript; a unit test in each SDK
compares it with the sha256 of the repository file. It travels as the
parameter `TemplateFingerprint` (default empty) and comes back as an output
of the same name, so a change of its value is always a stack change. The
plan reads it from the stack parameters: missing or different means
`update`, with `previousBucket` empty when the bucket does not change. A
hand-deployed stack is updated once; every other parameter keeps its value
through `UsePreviousValue`. Older SDKs ignore the parameter, so a 0.17.0
`setup` against an updated stack still reuses it.

Rejected: comparing `GetTemplate` bodies (a new AWS call that is not in
`AWS_API_NOTES.md`) and a version string in the template (it would drift
from the content).

A hash has no direction, so a different fingerprint alone could also mean
the stack is *newer*: a CI job on 0.17.1 would then roll back the template
a later SDK deployed. The plan therefore also reads the stack's
`rayito:sdk-version` tag (`DescribeStacks` `Tags`, verified against the
botocore model and added to `AWS_API_NOTES.md` §21), which every deploy
already sets to the deploying SDK's version. When its three release
numbers are greater than the running SDK's, the stack is never updated:
with the same bucket the step is `reuse` with `stackSdkVersion`, and with a
different bucket the plan fails with `stack_newer` (exit 2). A missing or
unparsable tag (a hand-deployed stack) is not newer.

`UpdateStack` with `Tags` replaces the whole list, and every template
update now goes through it. So on an update `setup` sends the stack's
current tags (without the fixed `rayito:` ones and the reserved `aws:` ones)
with the `--tag` ones on top; the fixed tags still win.

Updating a stack created with the template of 0.6.1 or earlier rolled back
in the AWS regression: 0.7.0 changed the `Description` of `CallerPolicy`,
IAM cannot change a policy's description, so CloudFormation replaces it,
and the replacement collides with its own fixed `ManagedPolicyName`
(`AWS_API_NOTES.md` §21, measured with a change set). The template keeps
both descriptions byte for byte behind a new `LegacyCallerPolicy`
parameter (`"false"` by default). `setup` sends the value the stack already
has; a stack without it gets `"true"` when it also lacks the
`SandboxLauncherPolicyArn` output (added together with the new description
in 0.7.0, and already in `DescribeStacks`), `"false"` otherwise, and a new
stack `"false"`. Rejected: renaming the policy (a replacement detaches it
from every user, group and role that has it) and reading the description
with `iam:GetPolicy` (a new call and a new permission for a value the
outputs already tell).

### D2. Only the token file this run created is removed

`deploy_token` already refuses an existing `--token-file`, so a file that
reaches the deploy was created by this run. It is removed on any failure
before the fleet exists, including a declined confirmation and `fail()`.
With `--keep-on-failure` it stays, because the kept sandbox is reachable
only with that token. The default `.rayito/agent-token` is never removed:
it is reused by design.

### D3. Published versions are filled in from the result

`applied_steps(plan, result)` / `appliedSteps` returns the plan's steps
with the `imageVersion` of each `publish` image step taken from
`SetupResult`. Without a result (dry run, failure) the steps are the plan's.

### D4. botocore bodies are dropped at the logger, not silenced globally

A `logging.Filter` on `botocore.endpoint`, `botocore.parsers`, `botocore.auth`, `botocore.regions`, `boto3.resources.action`, `boto3.resources.collection`, `boto3.dynamodb.table`, `s3transfer.tasks` and `s3transfer.futures` lets through only records at INFO
and above. Measured with boto3 1.43.102, botocore 1.43.103 and s3transfer
0.19.2 against a local HTTP server, with client, resource, collection,
`batch_writer` and `upload_fileobj` calls and a canary in bodies, items,
metadata, keys and prefixes: those are the loggers whose DEBUG carries
parameters or bodies, plus `botocore.auth`, whose canonical request lists
the signed headers. `urllib3.connectionpool` is left alone: it is not part
of the AWS SDK and its DEBUG only has the request line. `import rayito`
installs it once. The agent host, which cannot import the SDK, installs its
own copy and adds `botocore`, `boto3`, `s3transfer` and `urllib3` to
`QUIET_HTTP_LOGGERS`. The TypeScript SDK sets no logger on its
AWS SDK v3 clients, so nothing changes there besides the host assets.

### D5. One turn per VM orders its deadline changes

Each fleet keeps one lock per VM (`threading.Lock`, `asyncio.Lock`, a
promise chain in TypeScript). Holding a VM for an invocation and shortening
it after a release both run inside that turn. The release checks
`inflight == 0` inside the turn, so a release that loses the race to a new
lease sends nothing, and a hold that waits for the turn always lands after
the release's `set_timeout`. Every failure path of an invocation except a
lost VM and a cancellation goes through the same release.

### D6. Restoring with an unknown cap asks for the platform maximum

`restore_seconds(cap, now)` / `restoreMs` returns everything up to the
known cap, or `MAX_DURATION_SECONDS` when the cap is unknown. `rayd` rejects
a deadline beyond its cap with `timeout beyond cap; cap_unix_ms=`, and the
existing retry adopts that cap. When nothing can be sent (the cap is too
close) or `rayd` reports no cap, the VM stops counting as limited, so the
fleet does not retry on every invocation. Shared vectors:
`restoreSeconds` in `testdata/agent-fleet/lifetime.json`.

### D7. Flaky tests are made deterministic, not retried

`m5_pty` types a numbered marker until bash runs it (`ready_bash`) before
any test input, and returns what it read so the ring replay test still
compares from the first fragment. The background-child test bounds the end
by `PTY_DRAIN_GRACE + 5 s` against a `sleep 60`. `end_of_terminal` is the
select of `supervise`, unit-tested with paused time. The stalled S3
download tests freeze the monotonic clock of the transfer module, so the
whole `request_timeout` goes to the S3 leg.
