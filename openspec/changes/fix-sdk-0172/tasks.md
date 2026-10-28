## 1. `rayito domain deploy --tag`

- [x] 1.1 Python and TypeScript option, parsed before the confirmation; module docs
- [x] 1.2 Regenerate `testdata/cli/surface.json`
- [x] 1.3 Tests in both SDKs, docs (es/en) and changelogs

## 2. Reason of a rollback

- [x] 2.1 `StackEvent`, `first_failed_resource`, `describe_failed_resource` and the shared vectors
- [x] 2.2 `StackException.state` / `StackError.state`, set only by `wait()`
- [x] 2.3 `CloudFormationProvisioner.failure_reason` / `failureReason` with `DescribeStackEvents`
- [x] 2.4 `OptionalStacks.failure_reason` / `failureReason` (sync and async)
- [x] 2.5 `deploy_explaining_failure` in `rayito stack deploy` and `rayito domain deploy`
- [x] 2.6 Shared CLI cases: rollback, rejected call, `UPDATE_ROLLBACK_FAILED`, `DELETE_FAILED`
- [x] 2.7 `AWS_API_NOTES.md` §21, docs (es/en) including the optional IAM permission, changelogs

## 3. E2E harness

- [x] 3.1 Python sweeper terminates only the session's MicroVMs (`session_sweep.py`)
- [x] 3.2 `RAYITO_E2E_TAGS` in the custom-domain e2e (`e2e_tags` / `tagsFromEnvironment`)
- [x] 3.3 Shared WebSocket echo server file, background servers without a timeout
- [x] 3.4 TTL 404 with `STABLE_OBSERVATIONS` and the elapsed-time check
- [x] 3.5 Interrupted-setup zip ownership in both SDKs
- [x] 3.6 Python setup helper: `cli_arguments` / `setup_region_arguments`
- [x] 3.7 TypeScript slow credential-rotation test and `HEALTH_POLL_*`
- [x] 3.8 Skill, `CONTRIBUTING.md` and changelogs

## 4. Validation

- [x] 4.1 Local gates (Python, TypeScript, docs, OpenSpec, root checks)
- [ ] 4.2 Targeted AWS checks by the main session
