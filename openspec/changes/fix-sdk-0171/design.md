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

A `logging.Filter` on `botocore.endpoint`, `botocore.parsers` and
`botocore.auth` lets through only records at INFO and above. Measured with
boto3 against a local HTTP server: the first two are the only loggers that
carry bodies; `botocore.auth` is added because its canonical request lists
the signed headers. `import rayito` installs it once. The agent host,
which cannot import the SDK, installs its own copy and adds `botocore` and
`urllib3` to `QUIET_HTTP_LOGGERS`. The TypeScript SDK sets no logger on its
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
