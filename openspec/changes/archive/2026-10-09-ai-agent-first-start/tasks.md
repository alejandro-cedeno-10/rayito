## 1. Measure

- [x] 1.1 Phase breakdown of `create()` → first token on real AWS (Q155).
- [x] 1.2 OpenCode start with a warm page cache in the local harness.
- [x] 1.3 Kernel warm-up on/off, prefetch without settle, build-time warm
      (images A–E, Q155–Q156).

## 2. Template

- [x] 2.1 `limits.json`: `agentKernelWarmupMarkerPath`,
      `agentKernelWarmupSlimValue`, `agentPrefetchBuildMarkerPath`,
      `agentPrefetchBuildTimeoutSeconds`.
- [x] 2.2 `AgentTemplate(kernel_warmup=False)` in Python and TypeScript;
      CLI `--kernel-warmup/--no-kernel-warmup`.
- [x] 2.3 Prefetch daemon: build-time read of `prefetch_paths` and marker;
      `ready_cmd` waits for it.
- [x] 2.4 Shared vectors and unit tests in both SDKs.

## 3. Docs

- [x] 3.1 Agent guide (fast-start table), agent template page,
      `AWS_API_NOTES.md`, CHANGELOGs.

## 4. Acceptance

- [x] 4.1 Real AWS: the shipped template (image E) built and measured.
