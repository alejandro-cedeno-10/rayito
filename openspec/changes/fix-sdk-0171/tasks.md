## 1. IAM template fingerprint

- [x] 1.1 `TemplateFingerprint` parameter and output in `infra/iam.yaml`; regenerate stack assets
- [x] 1.2 `template_fingerprint` / `templateFingerprint` and the gateway that sends it
- [x] 1.3 `plan_stack` / `planStack` plan `update` on a missing or different fingerprint, also after `wait`
- [x] 1.4 Shared vectors (`{iamTemplateFingerprint}`), the `stackTemplate` text and a CLI case
- [x] 1.5 Docs (es/en) and changelogs
- [x] 1.6 Keep a stack of a newer SDK (`rayito:sdk-version` tag, `stackSdkVersion`, `stack_newer`) with shared vectors and a CLI case
- [x] 1.7 An update sends the stack's own tags under `--tag`; `DescribeStacks` `Tags` in `AWS_API_NOTES.md` §21
- [x] 1.8 `LegacyCallerPolicy` keeps the 0.6.1 `CallerPolicy` description so an old stack updates without a replacement; measured in `AWS_API_NOTES.md` §21, tests in both SDKs, docs and changelogs

## 2. Token file of a failed deploy

- [x] 2.1 Remove the `--token-file` this run created on failure, keep it with `--keep-on-failure`
- [x] 2.2 Tests in both SDKs, docs and changelogs

## 3. Published image versions in `--json`

- [x] 3.1 `applied_steps` / `appliedSteps` and the JSON document
- [x] 3.2 Tests in both SDKs, docs and changelogs

## 4. botocore DEBUG bodies

- [x] 4.1 `rayito._botocore_logs` installed by `import rayito`
- [x] 4.2 Agent host: quiet `botocore`/`urllib3`, drop body records; regenerate host assets
- [x] 4.3 Tests (real boto3 against a local HTTP server), docs, `SECURITY.md`, changelogs
- [x] 4.4 boto3 resource, collection, `batch_writer` and s3transfer loggers (measured), with tests

## 5. Extra-VM deadline

- [x] 5.1 One turn per VM for hold and limit; limit only an idle VM
- [x] 5.2 Failed invocations restore the scale-in deadline
- [x] 5.3 `restore_seconds` / `restoreMs` with shared vectors; reset `limited`
- [x] 5.4 Race, failure and promotion tests in both SDKs, docs and changelogs
- [x] 5.5 A dedicated fleet remembers the host-assigned session of each VM, so one VM (`max_vms == 1`, no handle) resumes it; tests in both SDKs and changelogs

## 6. Dependencies

- [x] 6.1 `opentelemetry-*` 1.45.1 (`uv lock`), `vitest` 4.1.12 (pnpm), `ctutils` 0.4.4
- [x] 6.2 `otel` extra floor `>=1.45.1` and `check_wheel.py`
- [x] 6.3 `tokio` 1.53.3 (RUSTSEC-2026-0386) and `AsyncFd::register` in the PTY backend

## 7. Flaky tests

- [x] 7.1 `ready_bash` in every `m5_pty` shell, bounded end, `end_of_terminal` paused-time tests
- [x] 7.2 Frozen operation clock in the stalled S3 download tests (sync and async)

## 8. Gates and release

- [x] 8.1 Local gates (Python, TypeScript, Rust in the Linux VM, docs, OpenSpec, root scripts)
- [ ] 8.2 AWS regression and image publish for 0.17.1 (`RELEASING.md` §6.6)
- [ ] 8.3 Archive after acceptance
