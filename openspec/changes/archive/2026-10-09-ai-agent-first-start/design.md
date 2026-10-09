## Context

Measured on real AWS on 2026-10-09 (`AWS_API_NOTES.md` Q155, Q156), with
disposable images built from `main` on a disposable `rayito-base-caps`,
2 GB, `allow_internet_access=False`, `bedrock_gateway` to Claude Haiku 4.5,
a one-word prompt, two discarded launches per new version.

## Decisions

- **D1 — kernel warm-up off in the agent image, by default.** The agent
  sandbox does not need the scientific stack preloaded; the kernel warm-up
  costs 4.4 s of `create()` (p50) and 248 MB of memory snapshot. The
  first token did not change by itself (16.4 s vs 16.3 s p50, n=6): the
  first `opencode` exec simply paid the slow restore window instead. The
  option stays (`kernel_warmup=True`) for agent sandboxes that also run
  data code. It reuses the existing `warmup_variant` marker; no sidecar or
  `rayd` change.
- **D2 — warm the binary before the snapshot, not only after a restore.**
  With the kernel slim, the template's `ready_cmd` holds the build
  snapshot until the daemon has read `prefetch_paths` once, so those pages
  travel in the memory snapshot. With deepagents' imports included (first
  try, image D) the first token p50 fell to 12.4 s (max 13.6 s) against
  15.6 s for its same-batch control. The shipped version reads only
  `prefetch_paths` (image E) so the memory snapshot (858 MB) stays below
  today's (920 MB): deepagents is not the default runtime and its imports
  still happen after each restore. E against its same-batch control:
  `create()` 6.4 s vs 19.1 s, first token 20.9 s vs 23.7 s p50, with a
  wide spread (14.7–34.9 s). The restore window varies a lot between
  batches, so the first-token gain is reported as a median shift within a
  batch, not as a guarantee.
- **D3 — rejected: prefetch without waiting for the guest to settle.**
  Re-measured with the slim kernel (image C): `create()` 15–26 s, the same
  contention Q146 saw. The settle wait stays.
- **D4 — not changed: client-side steps.** The gateway secret (≈ 0.35 s)
  and the config write (≈ 0.2 s) are under 5 % of the total; the STS call
  to resolve a bare image name (≈ 1 s) happens once per process and is
  avoided by passing the image ARN. Documented, not reworked.

## Risks

- A `run_code` cell that imports pandas in an agent sandbox pays the import
  the first time (documented; `kernel_warmup=True` restores it).
- The build waits for the daemon's first read (up to 300 s, under the
  600 s ready hook); a template without OpenCode has nothing to read and is
  ready at once.
