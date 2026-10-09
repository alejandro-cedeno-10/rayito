## Why

A new agent sandbox took ≈ 13–28 s from `create()` to the first token
(`AWS_API_NOTES.md` Q146, Q153), and the prefetch daemon of option A barely
moved the end-to-end number. Nobody had split that time into its parts, so
it was not clear what to change. A phase-by-phase measurement on real AWS
(Q155) shows:

- The client side is small: resolving the image ARN (one STS call, ≈ 1 s,
  first `create()` of a process only), `run-microvm` ≈ 0.5 s, the gateway
  secret ≈ 0.35 s, `Configure` ≈ 0.6–0.8 s, writing the OpenCode config
  ≈ 0.2 s.
- OpenCode itself starts in ≈ 1.3 s with a warm page cache (local harness)
  and a second run on the same VM reaches the first token in ≈ 3 s.
- What dominates is the restored VM: for ≈ 10 s after a launch every first
  read of code pages is slow. Two things compete for it: the `/run`
  rotation of the `run_code` kernel, which with the default warm-up imports
  numpy, pandas, matplotlib, scipy and scikit-learn (7.5 s between
  `agent_ready` and `kernel_ready`), and the first `opencode` exec, which
  pays 4–5 s to read its binary.

## What Changes

- `AgentTemplate(kernel_warmup=False)` (new option, off by default) writes
  the `slim` warm-up marker the sidecar already honours (the one
  `image_zip.py --variant slim` writes), so the agent image's kernel no
  longer imports the scientific stack at every start. `create()` returns
  4.4 s earlier (p50) and the memory snapshot shrinks by 248 MB (−27 %).
  `kernel_warmup=True` keeps today's kernel. CLI:
  `--kernel-warmup/--no-kernel-warmup`.
- The prefetch daemon gets a fourth argument: before the build snapshot it
  reads `prefetch_paths` once (OpenCode and ripgrep) and creates
  `AGENT_PREFETCH_BUILD_MARKER_PATH`; the template's `ready_cmd` waits for
  that marker (at most `AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS`), so the
  memory snapshot already holds the binary's pages. After a restore it
  behaves as before.
- New shared constants in `limits.json`; shared vectors in
  `testdata/agent/agent-template/cases.json` gain `readyCmd` and a
  `kernel-warmup` case.
- Docs: the agent guide's fast-start section and table, the agent template
  page, `AWS_API_NOTES.md` Q155–Q156, CHANGELOGs.

## Impact

- Python and TypeScript `AgentTemplate` (default image changes on the next
  build; existing images are untouched). No `rayd` or proto change.
- Cost: lower memory snapshot than before for the default template
  (858 MB vs 920 MB: the binary pages added are fewer than the kernel pages
  removed); no new AWS call or resource.
