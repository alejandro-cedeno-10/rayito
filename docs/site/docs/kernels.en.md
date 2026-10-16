# Kernels

`run_code` runs each cell in a stateful Jupyter kernel inside the MicroVM.
The kernel is chosen per cell with `language`; the contract is the same for
all of them (`ExecuteRequest.language`, `CreateContextRequest.language`)
and the image decides which kernels exist.

## Languages and images { #lenguajes-e-imagenes }

| `language` | Kernel | `rayito-base` | `rayito-base-poly` |
|---|---|---|---|
| `python` (default) | `ipykernel` with the scientific stack, warmed before `/ready` | yes | yes |
| `bash` | [`bash_kernel`](https://pypi.org/project/bash_kernel/) 0.10.0 (`pexpect` over `bash`), lazy start | `UNIMPLEMENTED` | yes |
| `javascript` (alias `js`) | [Deno](https://deno.com) 2.9.7 Jupyter kernel, lazy start | `UNIMPLEMENTED` | yes |
| `typescript` (alias `ts`) | the same Deno binary with its TypeScript kernelspec, lazy start | `UNIMPLEMENTED` | yes |

Names are normalized in the SDK (`Bash`, `JS`, `TypeScript`, `ts` all
work); only `python`, `bash`, `javascript` and `typescript` travel on the
wire. Any other (`r`, `java`, `tsx`) is `InvalidArgumentException` /
`InvalidArgumentError` before calling the agent; in the `rayito.e2b` shim
it is `UnimplementedError`, as in E2B for what does not exist. On an image
that does not ship the kernel the agent answers `UNIMPLEMENTED` naming
`rayito-base-poly` (see below).

## Lazy start and first-cell latency { #arranque-perezoso-y-latencia-de-la-primera-celda }

`rayito-base-poly` warms neither bash nor Deno before `/ready`: the
snapshot still contains only the warm Python kernel, so the variant's
`kernel_ready` and memory size are those of `rayito-base`. The first cell
of each language creates its default context (`default-bash`,
`default-javascript`, `default-typescript`; the agent starts the kernel
under a per-language lock, so two concurrent cells start a single kernel)
and pays for that startup within its own `timeout`; the stream deadline
(`timeout + 15 s`) already covers it. The bash figures measured against
real AWS are in `AWS_API_NOTES.md` Q57 (the variant's `kernel_ready_s` and
the latency of the first and second bash cell). The Deno ones are in Q77,
measured on `rayito-base-poly` 5.0: the first `typescript` or `javascript`
cell of a new sandbox takes **0.5-0.75 s** (including starting Deno) and
the following ones **≈ 0.1 s**. Right after publishing the image, the first
VMs took as long as 10-16 s on that first cell (cold read of the Deno
binary); if that wait matters, run a trivial cell
(`run_code("0", language="typescript")`) when you create the sandbox. Each
Deno kernel takes ≈ 200 MiB of RSS. `javascript` and `typescript` are two
kernels (two Deno processes) if you use both; each one counts toward the
cap of 8 contexts, so choose one.

## Examples { #ejemplos }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="rayito-base-poly") as sbx:
        out = sbx.run_code("echo hi", language="bash")
        print("".join(out.logs.stdout))  # "hi\n"

        ctx = sbx.create_code_context(language="bash", envs={"MODE": "dev"})
        print("".join(sbx.run_code("echo $MODE", context=ctx).logs.stdout))  # "dev\n"

        print([c.id for c in sbx.list_code_contexts()])  # ["default", "default-bash", "ctx-…"]
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create({ template: "rayito-base-poly" });
    const out = await sbx.runCode("echo hi", { language: "bash" });
    console.log(out.logs.stdout.join("")); // "hi\n"
    const ctx = await sbx.createCodeContext({ language: "bash" });
    console.log((await sbx.runCode("echo $HOME", { context: ctx })).logs.stdout.join(""));
    await sbx.kill();
    ```

Rules:

- `language` and `context` are mutually exclusive (`INVALID_ARGUMENT`): a
  context already has its language; `language` only selects that kernel's
  default context.
- Per-run `envs` only on Python contexts (`INVALID_ARGUMENT` on the rest:
  the silent cells that set and restore variables are Python code). The
  `envs` of `create_code_context` do work for any language: they are the
  environment of the kernel process.
- `default-bash` is a normal context: it counts toward the cap of 8, it can
  be restarted and destroyed (`remove_code_context("default-bash")`); the
  next `bash` cell creates it again. Only `default` (Python) is protected.
- `list_code_contexts()` reports each context's actual language.

## JavaScript and TypeScript with Deno { #javascript-y-typescript-con-deno }

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="rayito-base-poly") as sbx:
        print(sbx.run_code("const x: number = 40 + 2; x", language="typescript").text)  # "42"
        print(sbx.run_code("x + 1", language="ts").text)                                # "43"
        print(sbx.run_code("let y = [1, 2, 3].map((n) => n * 2); y", language="js").text)

        out = sbx.run_code('Deno.jupyter.html`<b>hello</b>`', language="typescript")
        print(out.results[0].html)                                                     # "<b>hello</b>"

        ctx = sbx.create_code_context(language="typescript", envs={"MODE": "dev"})
        print(sbx.run_code('Deno.env.get("MODE")', context=ctx).text)                   # the value of MODE

        sbx.run_code('import { camelCase } from "npm:lodash-es@4"; camelCase("hello world")',
                     language="typescript")
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(template="rayito-base-poly") as sbx:
            out = await sbx.run_code("const x: number = 40 + 2; x", language="typescript")
            print(out.text)                                                        # "42"
            ctx = await sbx.create_code_context(language="javascript")
            print((await sbx.run_code("[1, 2, 3].length", context=ctx)).text)      # "3"


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ template: "rayito-base-poly" });
    console.log((await sbx.runCode("const x: number = 40 + 2; x", { language: "typescript" })).text);
    console.log((await sbx.runCode("x + 1", { language: "ts" })).text);
    const ctx = await sbx.createCodeContext({ language: "javascript" });
    console.log((await sbx.runCode("[1, 2, 3].map((n) => n * 2)", { context: ctx })).text);
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import Sandbox

    with Sandbox.create("rayito-base-poly") as sbx:
        print(sbx.run_code("console.log('hello'); 1 + 1", language="javascript").text)  # "2"
        print(sbx.run_code("const n: number = 2; n * 21", language="typescript").text)  # "42"
    ```

- **Why Deno.** `ijavascript` does not install on `al2023-minimal` ARM64
  without a compiler (Q57, below); Deno is a single binary linked against
  glibc that ships its own ZeroMQ and a Jupyter kernel (`deno jupyter`), and
  serves both languages (Q61).
- **The pin.** `image/Dockerfile` downloads
  `deno-aarch64-unknown-linux-gnu.zip` from the `v2.9.7` release with a
  pinned sha256 checked with `sha256sum -c` (`scripts/check_pins.py`
  requires it, `SECURITY.md` T10) and leaves it at `/opt/rayito/deno/deno`,
  owned by root. Bumping Deno means changing `DENO_VERSION` and
  `DENO_SHA256` (the release's official `.sha256sum`), republishing
  `rayito-base-poly` and repeating its e2e; Dependabot does not track it.
- **Loopback TCP.** Deno cannot open `ipc` endpoints, so its kernels listen
  on `127.0.0.1` with the connection file's HMAC key (ADR-013 in
  `ARCHITECTURE.md`); Python and bash stay on `ipc`. Accepted consequence:
  a process in the sandbox itself can read the outputs of Deno cells
  (`SECURITY.md` T12).
- **Interruption.** The `timeout` interrupts with a protocol
  `interrupt_request` message (Deno does not forward `SIGINT`); if the
  kernel is not idle within 5 s, the agent restarts the context. An
  interrupted Deno cell does not emit an `error` of its own: the agent
  synthesizes `ExecutionTimeout` just as with Python.
- **`NO_COLOR` and ANSI.** The kernelspecs start Deno with `NO_COLOR=1`,
  `DENO_NO_UPDATE_CHECK=1` and a `DENO_DIR` under `HOME`; in addition the
  sidecar strips the ANSI sequences from the `text/plain` of Deno results
  (defense in depth), so `.text` is `42` and not `\x1b[33m42\x1b[39m`.
- **Permissions.** Deno runs with all permissions (`allow_all`), just like
  the Python kernel as uid 1000: `Deno.env`, `Deno.readTextFile`, network.
- **`npm:`, `jsr:` and `https:` imports** download code through the same
  egress as a `pip install` (with the same [outbound network](network.md)
  policy).
- Per-run `envs` only work in Python: in Deno use the `envs` of
  `create_code_context`.

## What remains Python-only { #lo-que-sigue-siendo-solo-python }

- The `e2b/chart` and `e2b/data` formatters, the IPython configuration and
  the startup scripts: a bash kernel emits `stream` and `error` and the
  agent maps them to `logs`/`error` with the same rules.
- The warm-up before `/ready` and the kernel rotation on `/run`.
- The reseed of `random`/`numpy.random` after `/resume`: non-Python
  contexts are listed as `skipped` (their random state is that of the
  process itself).

## Timeouts and errors in bash { #timeouts-y-errores-en-bash }

The `timeout` follows the generic rule: when it expires the agent
interrupts the kernel (`SIGINT`, which `bash_kernel` forwards to its
`bash`) and, if it is not idle within 5 s, restarts the context; in both
cases the `Execution` carries `error.name == "ExecutionTimeout"` and the
next cell works (measured in Q57: `sleep 30` with `timeout=2` comes back at
5.8 s through the restart branch, because `SIGINT` does not leave
`bash_kernel` idle within 5 s). A command that exits with a non-zero code
ends with the `error` that `bash_kernel` emits: an empty name and the exit
code as `value` (`false` → `ExecutionError(name='', value='1')`); the
output keeps arriving through `logs.stdout`/`logs.stderr`.

## `UNIMPLEMENTED` on `rayito-base` { #unimplemented-en-rayito-base }

On an image without the kernel, `run_code("echo hi", language="bash")`
fails before creating anything with `UNIMPLEMENTED` and a message that
names `rayito-base-poly`; an absent kernel is `UnimplementedError` in both
Python and TypeScript (0.4.0: no longer `InvalidArgumentException`/
`InvalidArgumentError`, because an absent feature is never an argument
error). The agent knows which kernels exist because the sidecar announces
them in `ready` (`languages`, according to the kernelspecs it could install
at startup).

## Why not `ijavascript` { #por-que-no-ijavascript }

The original design planned `ijavascript` on Node 20 (`dnf install
nodejs20`). The 2026-09-16 test (Q57) ruled it out: `ijavascript@5.2.1`
depends on `jmp@2`, which only accepts `zeromq@5`, and `zeromq@5.3.1` does
not publish prebuilt binaries for `linux-arm64` or for Node 20 (ABI 115:
its `prebuilds` go up to `node.abi108`, only `darwin-x64`, `linux-x64` and
`win32`); npm falls back to `node-gyp rebuild`, which dies with `not found:
make` because `al2023-minimal` ships no compilers. Since 0.3.0,
`javascript` and `typescript` are served by Deno, which needs none of that.

## R and Java { #r-y-java }

They remain out (`SPEC.md` §4), with measured numbers: R through
conda-forge (`r-base` + `r-irkernel`) takes 1.4 GB installed, and R-core
through `dnf` 127 MB plus cairo, pango, harfbuzz, tk and fonts; Java would
be 262 MB of Corretto 21 headless plus IJava, unmaintained (one release
since 2023, does not compile on JDK 17/18).

## Publish the poly variant { #publicar-la-variante-poly }

The same `Dockerfile`, a marker inside the zip
(`kernel-sidecar/kernels_variant` with `poly`) that activates a
conditional layer: it installs the pins from
`kernel-sidecar/requirements-poly.txt` (`bash_kernel` and its dependencies,
`pip check`), checks `import bash_kernel` as `user`, downloads and verifies
Deno 2.9.7 and checks `deno --version` as `user`. `rayito-base` carries no
marker and runs nothing from that layer.

```bash
# Linux / macOS
make image-publish-poly BUCKET=<bucket>          # publishes rayito-base-poly

# Windows (no make; toolchain from CONTRIBUTING.md §1.3)
cargo zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd
cp target/aarch64-unknown-linux-musl/release/rayd image/rayd
python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar
python scripts/image_zip.py image image/rayito-image-poly.zip --variant poly
uv run --project clients/python python scripts/publish_image.py \
    --artifact image/rayito-image-poly.zip --variant poly \
    --base-image-version 1 --bucket <bucket>
```

`publish_image.py` refuses to publish a zip whose marker does not match
`--variant`. The measured `snapshotBuild` sizes of `rayito-base-poly` and
of `rayito-base` rebuilt with the same `Dockerfile` (within the ±20 MB
memory / ±10 MB code band relative to 17.0) are in Q57.

<a id="agentes-anteriores-a-m7"></a><a id="agentes-anteriores-a-m9"></a>

## Old agents { #agentes-antiguos }

??? note "Agents before M7 (`rayd` < 0.2.0)"
    `ExecuteRequest.language` is an optional proto3 field: a `rayd` earlier
    than 0.2.0 ignores it and runs the cell in the default context's Python
    kernel without warning. Use an image 0.2.0 or later before relying on
    `language`; the SDK/agent compatibility table is published with each
    release (`docs/RELEASING.md`).


??? note "Agents before M9 (`rayd` < 0.3.0)"

    A `rayd` earlier than 0.3.0 does not know `typescript`: it answers
    `INVALID_ARGUMENT` ("language must be one of python, bash, javascript")
    and the SDKs deliver it as `InvalidArgumentException` /
    `InvalidArgumentError` (also through the shim). `javascript` against
    that agent is still `UNIMPLEMENTED` on every image. Publish
    `rayito-base-poly` from a 0.3.0 or later release before using JS/TS.
