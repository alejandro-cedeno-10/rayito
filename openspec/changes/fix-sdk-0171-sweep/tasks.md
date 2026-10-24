## 1. Fleet listing with a terminating member

- [x] 1.1 `select_members` / `selectMembers` with `unreadable`; shared vector
- [x] 1.2 Per-VM probe in the sync, async and TypeScript directories, with the warning
- [x] 1.3 Tests in both SDKs (sync and async in Python), docs and changelogs

## 2. Template environment (ADR-035)

- [x] 2.1 `template-env.json` in the artifact and its `COPY`, both SDKs; vectors
- [x] 2.2 `rayd` boot read and `spawn_defaults` layering, with tests
- [x] 2.3 ADR-035, docs (es/en) and changelogs

## 3. Base image only when building

- [x] 3.1 Preflight order and the reuse hint, both SDKs, with tests
- [x] 3.2 The agent-project CLI e2e writes `[image] base`

## 4. Entrypoint checked at host start

- [x] 4.1 deepagents and Strands `warm()`; Node bridge `--check-entry` and the Node driver
- [x] 4.2 Regenerated host assets and vectors; tests in both SDKs

## 5. CLI

- [x] 5.1 Quiet exit on a closed output, both CLIs, with tests
- [x] 5.2 TypeScript CLI warnings logger and the reconnected fleet; shared line

## 6. e2e

- [x] 6.1 Renewal e2e with the injectable clock
- [x] 6.2 Account-setup expects the `capsImage` step
- [x] 6.3 Agent-project CLI stops by fleet name and checks the recipe is gone
- [x] 6.4 Template e2e skips without its bucket
- [x] 6.5 Custom-domain and dashboard read-only determinism
