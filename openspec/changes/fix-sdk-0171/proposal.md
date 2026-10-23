## Why

After 0.17.0 shipped, seven defects remained open. One of them came in with
0.17 (the deadline of extra fleet VMs); the others are older and showed up
with it:

- `rayito setup` reuses `rayito-m0-iam` whenever its `ArtifactBucket`
  matches, even if the stack was deployed with an older `infra/iam.yaml`.
  A 0.16 stack therefore misses the 0.17 permissions on the fleet recipe.
- A `--token-file` written by a failed `rayito agent deploy` makes the retry
  fail with «el fichero de token ya existe».
- `rayito setup --json` reports `imageVersion: null` on the image steps it
  has just published.
- botocore's DEBUG log writes request and response bodies (secret values,
  the sandbox access token) when the root logger is at DEBUG, in the SDK
  process and inside the agent host.
- The three known limitations of the extra-VM deadline (PR #286): a release
  can cut the hold of an invocation that started meanwhile on the same VM;
  a failed invocation leaves the hold on the VM; a shortened VM promoted to
  base with an unknown cap keeps its short deadline.
- Dependency alerts: `opentelemetry-api` 1.45.0, `@vitest/mocker` 4.1.11
  and `ctutils` 0.4.2 (RUSTSEC-2026-0385).
- Two flaky tests: `m5_pty` types into the terminal before interactive bash
  owns it, and the stalled S3 download test lets the export spend the whole
  `request_timeout`.

## What Changes

- `infra/iam.yaml` gains the parameter and output `TemplateFingerprint`.
  `rayito setup` sends the sha256 of the packaged template and plans
  `update` when the stack lacks it or has another value, unless the
  stack's `rayito:sdk-version` tag is newer than the SDK: then it is kept
  (`stackSdkVersion`) or, with another bucket, the plan fails with
  `stack_newer`. An update sends the stack's own tags again under `--tag`.
- `rayito agent deploy` deletes the `--token-file` it created when the
  deploy fails before the fleet exists, except with `--keep-on-failure`.
- After `apply`, the `--json` steps of a published image carry its version.
- `import rayito` drops DEBUG records of the botocore, boto3 and
  s3transfer loggers that carry parameters, bodies or signed headers; the
  agent host also quiets `botocore`, `boto3`, `s3transfer` and `urllib3`
  and drops the same records.
- The `otel` extra requires `opentelemetry-api>=1.45.1`.
- Fleet deadline changes are serialised per VM, failed invocations restore
  the scale-in deadline, and restoring a promoted VM with an unknown cap
  asks for the platform maximum and retries with the cap `rayd` reports.
- Lock files: `opentelemetry-*` 1.45.1, `vitest` 4.1.12, `ctutils` 0.4.4.
- Tests: `ready_bash` in `m5_pty`, a bounded end for the background-child
  test, paused-time tests of the PTY drain grace, and a frozen operation
  clock in the stalled S3 download tests.

Everything is backward compatible with 0.16 and 0.17 stacks, fleets and
token files. Nothing new costs money.

## Impact

- Python and TypeScript SDKs: account setup, agent deploy CLI, setup JSON,
  agent fleet, agent host assets.
- `infra/iam.yaml` (one parameter, one output), regenerated stack assets.
- `rayd` tests only, plus `Cargo.lock`.
- Docs in Spanish and English; both SDK changelogs and the `rayd` one.
