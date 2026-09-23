## Why

E2B's `run_code(code, language=...)` is used by agent frameworks to run
JavaScript and TypeScript snippets in the same stateful sandbox as Python;
`RunCodeLanguage` lists `javascript` and `typescript`. Rayito reserves the
name `javascript` and answers `UNIMPLEMENTED` in every image, and does not
know `typescript` at all. The M7 attempt with `ijavascript` failed because
its `zeromq@5` binding has no linux-arm64 prebuild and al2023-minimal ships
no compiler (`AWS_API_NOTES.md` Q57).

Q61 (measured 2026-09-22 on `rayito-base` 20.0, glibc 2.34, uid 1000)
removes that blocker. Deno 2.9.7 `aarch64-unknown-linux-gnu` is one
self-contained binary with no npm, node-gyp or zeromq dependency, and it
runs on the image's glibc. Driven by the sidecar's own `jupyter-client`
8.10.0, its built-in Jupyter kernel started in 0.85 s and ran TypeScript,
streams, HTML output, errors and top-level `await`.

Two facts found while designing this change shape it:

- Deno 2.9.7's kernel implements ZeroMQ itself on `Deno.listen()` and only
  speaks TCP. It ignores the `ipc` transport that ADR-002 / design D4
  mandate for every kernel. ADR-013 records that exception in writing.
- It handles interrupts through `interrupt_request` on the control channel,
  not through `SIGINT`.

Both facts come from `cli/js/jupyter_kernel.js` at tag `v2.9.7`.

## What Changes

- **Image.** The existing poly-only conditional layer of `image/Dockerfile`
  (gated by the `kernels_variant` marker) also downloads Deno 2.9.7:
  - the source is the GitHub release zip, pinned by exact version and
    sha256 `c832298b…2f65bbf`, and verified with `sha256sum -c`
  - Python `zipfile` extracts it, because the image has no `unzip`
  - the binary is installed at `/opt/rayito/deno/deno` (root-owned, 0755)

  `rayito-base` never runs the layer. `scripts/check_pins.py` gains a third
  gate: every `curl` download in a Dockerfile must be verified against a
  pinned 64-hex sha256.
- **Sidecar.**
  - Two new kernelspec templates, `rayito-javascript` and
    `rayito-typescript`, both with argv `/opt/rayito/deno/deno jupyter
    --kernel --conn {connection_file}`. Their env is `NO_COLOR=1`,
    `DENO_DIR=${HOME}/.cache/deno` and `DENO_NO_UPDATE_CHECK=1`, and they use
    `interrupt_mode: message`.
  - The `KernelLanguage` catalog gains `typescript`. Each entry gains two
    fields: `transport` (`ipc`, or `tcp` for the Deno kernels bound to
    `127.0.0.1`) and `strips_plain_text_ansi`, which enables a
    defence-in-depth ANSI strip of `text/plain` results for Deno.
  - `install_kernelspecs()` already publishes a spec only when its argv
    resolves, so the templates stay inert in `rayito-base`.
- **rayd.**
  - `Language` gains `Typescript`, and `default-typescript` becomes a lazily
    created language default.
  - The `InvalidLanguage` message names the four languages.
  - `ResultBundle` is unchanged: Deno's MIME types are already mapped.
- **Proto.** Comment-only edits to `CreateContextRequest.language` and
  `ExecuteRequest.language` in `proto/rayito/v1/code.proto`: they document
  `javascript`/`typescript` (Deno, poly only, lazy start, `UNIMPLEMENTED`
  naming `rayito-base-poly` elsewhere) and drop the "reserved name"
  sentence. No field, message or RPC change. The single Contract agent
  applies it (design D9).
- **SDKs.**
  - Python (sync and async) and TypeScript accept `typescript`. The `ts`
    alias joins `js` in both native SDKs, so the E2B shim inherits both.
  - An image without the kernel keeps the native error: Python
    `InvalidArgumentException` with `grpc_code UNIMPLEMENTED`, TypeScript
    `InvalidArgumentError`. The message names `rayito-base-poly`.
  - The Python E2B shim turns that agent `UNIMPLEMENTED` into
    `rayito.e2b.UnimplementedError`, so a missing kernel is never reported
    as an "unknown language".
- **Docs.** The following are updated:
  - SPEC §3/§4: JavaScript/TypeScript delivered. R and Java stay out, with
    the measured reasons: R through conda-forge is 1.4 GB, and R-core is
    127 MB plus graphics deps. Corretto 21 is 262 MB, and IJava is
    unmaintained.
  - ARCHITECTURE: the Capa 1 table and the new ADR-013.
  - `kernels.md`, `e2b-compat.md` and the README.
  - SECURITY: T8 (`npm:`/`jsr:` imports fetch code over egress like
    `pip`), T10 (Deno pinned by sha256) and T12 (loopback TCP kernel
    sockets).
  - A new `AWS_API_NOTES.md` §16 row with the real publish numbers.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `code-execution`:
  - the language catalog, per-language defaults and SDK language
    normalisation gain `typescript`
  - `javascript`/`typescript` are served by Deno over loopback TCP, with
    message interrupts and the `text/plain` ANSI strip
  - the image requirement and a real-AWS acceptance requirement for the
    Deno kernels
- `image-variant`: the poly conditional layer also installs the pinned
  Deno binary.
- `typescript-sdk`: the `runCode`/`createCodeContext` language set, and the
  poly acceptance test running JS/TS cells.
- `e2b-compat`: the accepted language names, and the agent's
  `UNIMPLEMENTED` mapped to `UnimplementedError`.
- `ci-hardening`: a new requirement that Dockerfile downloads are pinned by
  sha256 and checked by `scripts/check_pins.py`.

## Non-goals

- R and Java kernels (SPEC §4, with the measured sizes above).
- Warming Deno before `/ready`: it starts only on the first cell or context
  of its language.
- Deno in `rayito-base`, `-slim` or `-caps`.
- Kernel support for per-execution `envs` on JS/TS contexts: it stays
  `INVALID_ARGUMENT`, as for bash.
- An `npm:` package allowlist or mirror.
- A TypeScript E2B shim (`m9-e2b-v2-surface`).
- Changing the `ipc` transport of the Python and bash kernels.

## Impact

- **Code.** Implementation touches:
  - rayd: `crates/rayd-core/src/code/{language,error}.rs` and the tests in
    `crates/rayd`.
  - Sidecar: `kernel-sidecar/src/rayito_kernel_sidecar/{languages,kernels,executions,protocol}.py`,
    two new templates, and the sidecar tests.
  - Image and pins: `image/Dockerfile` and `scripts/check_pins.py` (+ test).
  - Python SDK: `clients/python/src/rayito/{_code_base,e2b/_compat,e2b/_sync,e2b/_async}.py`,
    plus docstrings.
  - TypeScript SDK: `clients/typescript/src/sandbox/code.ts`.
  - Tests: the unit tests, `clients/python/tests/e2e/test_m9_deno_kernels.py`
    (new) and `clients/typescript/tests/e2e/poly.e2e.test.ts`.
- **Proto.** Comments only.
- **AWS.** No new API parameter: publishing reuses `publish_image.py
  --variant poly`, and the sizes come from the documented `snapshotBuild`.
- **Size.** About +85 MB of code install on `rayito-base-poly` only, with
  memory unchanged (not warmed). `rayito-base` is unchanged.
- **Closing rule.** The change closes only after a real publish of
  `rayito-base-poly` and the Python and TypeScript e2e run green against
  real AWS. Q61 verified correctness only under emulation plus an ad-hoc
  in-VM run.
