## Why

Rayito's code execution is Python-only by an explicit v0.1 non-goal
(`SPEC.md` §4 "Kernels que no sean Python"; `CreateContextRequest.language`
accepts only `""`/`"python"`, `rayito.e2b` raises `UnimplementedError` for
anything else). E2B's template ships `python3`, `bash`, `javascript`, `r`
and `java` kernels and its `run_code(code, language=...)` is used by agent
frameworks to run shell one-liners and JS snippets in the same stateful
sandbox. The M7 research report (`docs/research/2026-09-m7-oss-readiness.md`
§3b) measured that the sidecar already abstracts the kernelspec through
`AsyncKernelManager(kernel_name=...)`, that `bash_kernel` costs ≈ 0 MB and
`ijavascript` needs Node 20 (+≈ 100 MB of disk), that restore time is
dominated by kernel warm-up rather than snapshot size, and recommended for
v0.2: `bash` and `javascript` only, **lazy start on first use**, shipped as
a `rayito-base-poly` image variant so `rayito-base` stays lean. Report §5
lists this as item 7 of M7 (`m7-poly-kernels`, size M), with the acceptance
test "`run_code("echo hi", language="bash")` and a JS cell return results;
`rayito-base` snapshot size unchanged".

## What Changes

Track 7 of M7, decided in full by `design.md`:

- **Proto.** `ExecuteRequest` gains `optional string language = 5`
  (`CreateContextRequest.language` already exists and is reused). Canonical
  values on the wire: `python`, `bash`, `javascript` (empty = python or the
  context's own language). `buf lint`, `buf breaking` clean, all three
  clients regenerated.
- **rayd.** A `Language` value type in `rayd-core` replaces the
  `CONTEXT_LANGUAGE` constant; `CreateContext` accepts the three names;
  `Execute{language, no context_id}` routes to a per-language default
  context (`default-bash`, `default-javascript`) that `rayd` creates lazily
  on first use, never before `/ready`; `context_id` + `language` together
  is `INVALID_ARGUMENT`; a language the running image does not ship is
  `UNIMPLEMENTED`; per-execution `envs` on a non-Python context is
  `INVALID_ARGUMENT` (the set/restore cells are Python). `ListContexts`
  reports the real language. `/run` rotation, `/ready`, `/validate`,
  `Health.kernel_ready` and the 8-context cap are unchanged.
- **Sidecar.** Kernelspec templates `rayito`, `rayito-bash`,
  `rayito-javascript` under `kernel-sidecar/jupyter/kernels/`, installed at
  start only when their interpreter/module is present; `create_context`
  carries `language`; `ready` announces `languages`; `reseed` lists
  non-Python contexts as `skipped`; non-Python kernels get no IPython config,
  startup scripts or warm-up; the same Jupyter → event mapping applies.
- **Image.** A `poly` variant of the single `Dockerfile`, selected by a
  marker entry that `image_zip.py --variant poly` writes into the zip (same
  mechanism as `slim`): `dnf install nodejs20 nodejs20-npm`, `ijavascript`
  under `/opt/rayito/node`, `bash_kernel` from
  `kernel-sidecar/requirements-poly.txt`. `publish_image.py --variant poly`
  publishes `rayito-base-poly`. The `full` zip carries no marker, so
  `rayito-base` installs nothing new; its `memorySnapshotSizeInBytes` is
  reported before and after. If `ijavascript` does not build on al2023
  ARM64 the variant ships `bash` only and the reason is recorded (design D5).
- **SDKs.** Python `Sandbox.run_code(code, *, language=None, ...)` and
  `AsyncSandbox` (sync/async parity), `create_code_context(language=)`
  accepting the three names (case-insensitive, `js` alias); the `rayito.e2b`
  shim forwards them and keeps `UnimplementedError` for `r`, `java` and
  anything else; TypeScript `runCode(code, { language })` and
  `createCodeContext({ language })` with the same rules.
- **Acceptance.** `clients/python/tests/e2e/test_m7_poly_kernels.py` and
  `clients/typescript/tests/e2e/poly.e2e.test.ts`, both skipped unless
  `RAYITO_TEMPLATE_POLY` is set, run a bash cell, a JavaScript cell, a bash
  timeout and the `rayito-base` size comparison against real AWS.
- **Docs.** `docs/site/docs/kernels.md` (nav entry), the E2B compatibility
  table, README, `SPEC.md` §4 amendment, `MILESTONES.md` row, the three
  changelogs and a Q57 row in `AWS_API_NOTES.md` with the measured sizes.

**Not in this change:** R, Java or any further kernel; `Health` reporting
the available languages; SDK feature gating on `agent_version`; warming
non-Python kernels before `/ready`; per-execution `envs` for non-Python
kernels; a version bump of `rayd` or the SDKs (release decisions live in
`docs/RELEASING.md`); `rayito-base-slim`/`-caps` combined with `poly`.

## Capabilities

### Modified Capabilities

- `code-execution`: `ExecuteRequest.language`; the `Language` value type and
  the per-language lazy default context; sidecar protocol v1 additions
  (`language` in `create_context`, `languages` in `ready`, `skipped` in the
  `reseed` reply); kernelspec catalog and availability; Python-only
  per-execution `envs`; SDK `run_code(language=)`; logging allowlist; image
  requirement extended with the `poly` conditional.
- `image-variant`: the `poly` marker in `image_zip.py`, `publish_image.py
  --variant poly` → `rayito-base-poly`, Make/CI targets, the size gate on
  `rayito-base`.
- `typescript-sdk`: `runCode({ language })`, `createCodeContext({ language
  })`, the guarded poly e2e.
- `e2b-compat`: `run_code(language=)` / `create_code_context(language=)`
  accept `bash` and `javascript`; the rest keep raising `UnimplementedError`.

## Impact

- `proto/rayito/v1/code.proto` (one optional field), regenerated
  `crates/rayito-proto`, `clients/python/src/rayito/v1`,
  `clients/typescript/src/gen`.
- `crates/rayd-core/src/code/{context,error,protocol,hooks}.rs`,
  `crates/rayd/src/code/{manager,executions,fake_sidecar}.rs`,
  `crates/rayd/src/grpc/code.rs`, `crates/rayd/tests/{common,m4_code}.rs`
  and new host tests.
- `kernel-sidecar/src/rayito_kernel_sidecar/{kernels,server,protocol,
  logging,__main__}.py`, a new `languages.py`,
  `kernel-sidecar/jupyter/kernels/{rayito-bash,rayito-javascript}/kernel.json`,
  `kernel-sidecar/requirements-poly.txt`,
  `kernel-sidecar/tests/fixtures/protocol_v1.jsonl` and the sidecar tests.
- `image/Dockerfile`, `scripts/image_zip.py`, `scripts/publish_image.py`,
  `scripts/copy_sidecar.py`, `scripts/tests/*`, `Makefile`,
  `.github/workflows/ci.yml`.
- `clients/python/src/rayito/{_code_base,sandbox_sync/*,sandbox_async/*,
  e2b/*}.py`, unit tests, `tests/e2e/test_m7_poly_kernels.py`;
  `clients/typescript/src/sandbox/{code,sandbox}.ts`, unit tests,
  `tests/e2e/poly.e2e.test.ts`, the unit fakes of both SDKs.
- `docs/site/docs/kernels.md`, `docs/site/mkdocs.yml`,
  `docs/site/docs/e2b-compat.md`, `README.md`, `clients/python/README.md`,
  `clients/typescript/README.md`, `SPEC.md`, `MILESTONES.md`,
  `AWS_API_NOTES.md`, the three `CHANGELOG.md`.
- AWS: one new image (`rayito-base-poly`) and one new version of
  `rayito-base` (Dockerfile text changes even though the layers do not);
  each e2e run creates at most three MicroVMs from the poly image and one
  from `rayito-base`.
