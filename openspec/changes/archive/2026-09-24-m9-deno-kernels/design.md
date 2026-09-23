## Context

### Facts this design rests on (verified, with source)

- **Q61** (`AWS_API_NOTES.md` §16, measured 2026-09-22 on `rayito-base`
  20.0, glibc 2.34, aarch64, as uid 1000):
  - The zip `deno-aarch64-unknown-linux-gnu.zip` is 39 821 318 B, and the
    binary is 84 971 392 B.
  - `deno 2.9.7 (stable, release, aarch64-unknown-linux-gnu)` runs with no
    glibc errors.
  - A real kernel started under `jupyter-client` 8.10.0 in 0.85 s.
    `execute_result` `text/plain` came back ANSI-coloured
    (`\u001b[33m42\u001b[39m`).
  - streams → stdout/stderr; ``Deno.jupyter.html`…` `` → `text/html`;
    `throw new Error('boom')` → `ename` `Error` / `evalue` `boom`; top-level
    `await` works.
  - The image ships no `unzip`, `gcc`, `make`, `node` or `npm`, but it does
    ship `curl` 8.17.0 (Q59).
  - Runtime `dnf` is unusable (uid 1000).
- **Release pin** (`gh api repos/denoland/deno/releases/tags/v2.9.7`, read
  2026-09-22):
  - Asset `deno-aarch64-unknown-linux-gnu.zip`, 39 821 318 B (the same size
    Q61 downloaded).
  - GitHub asset digest `sha256:c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf`.
  - The official `deno-aarch64-unknown-linux-gnu.zip.sha256sum` asset
    carries the same value.
  - The release was published 2026-09-17. Deno is MIT-licensed.
- **Deno kernel internals** (`cli/js/jupyter_kernel.js` and
  `cli/tools/jupyter/mod.rs` at tag `v2.9.7`, read with `gh api`):
  - *Transport: TCP only.* "Jupyter ZMQ kernel implemented entirely in JS
    using `Deno.listen()`". It reads `ip`, `key` and the five ports from the
    connection file and calls `Deno.listen({ hostname: ip, port })` for each
    socket, so it has no `ipc` transport.
  - *HMAC-SHA256 checked.* Incoming messages are verified with
    HMAC-SHA256 (`hmacVerify`, "Incoming messages that fail this check must
    be dropped").
  - *Interrupts by message.* `interrupt_request` on the control channel
    calls `op_jupyter_repl_interrupt()`, which terminates V8 execution.
    Nothing installs a `SIGINT` handler.
  - *`kernel_info_request` answered on control as well as on shell.* The
    sidecar's resume probe uses control.
  - *Full permissions.* The kernel worker runs with
    `PermissionsContainer::allow_all(...)`, the same power a Python kernel
    has as uid 1000.
- **Deno env vars** (`libs/cli_parser/src/env_vars.rs` at `v2.9.7`):
  - `DENO_DIR` "Set the cache directory"
  - `DENO_NO_UPDATE_CHECK` "Set to disable checking if a newer Deno version
    is available"
  - `NO_COLOR` "Set to disable color."
- **jupyter_client 8.10.0** (wheel read in the scratchpad):
  - *Kernelspec `env` merge.* `KernelProvisionerBase.pre_launch()` merges
    the kernelspec `env` on top of the `env` passed to `start_kernel`
    (`env.update(...)`). `__apply_env_substitutions` expands `${VAR}`
    templates with `string.Template.safe_substitute` against that env, so
    `${HOME}` resolves to the sidecar's `HOME`.
  - *Message interrupts.* `interrupt_kernel()` with
    `kernel_spec.interrupt_mode == "message"` sends `interrupt_request` on
    the manager's control socket; with `"signal"` it sends `SIGINT`.
- **Repository state:**
  - `crates/rayd-core/src/code/language.rs` knows `python`, `bash` and
    `javascript`.
  - `kernel-sidecar/src/rayito_kernel_sidecar/languages.py` has the same
    three. The `javascript` entry has no template, so it always reports
    "not installed".
  - `KernelContext._start_kernel` hardcodes `transport="ipc"`,
    `ip=<socket_dir>/k`.
  - `install_kernelspecs()` writes a spec only when its argv resolves.
  - The poly layer of `image/Dockerfile` is the single `RUN if [ "$(cat
    /opt/rayito/sidecar/kernels_variant …)" = "poly" ]` block.
  - `clients/python/src/rayito/_code_base.py` and
    `clients/typescript/src/sandbox/code.ts` have
    `SUPPORTED_LANGUAGES = {python, bash, javascript}` and
    `LANGUAGE_ALIASES = {js: javascript}`.
  - The Python unit test `test_normalize_language_rejects_other_kernels`
    lists `typescript` as rejected.
- **Baselines for the size bands** (`AWS_API_NOTES.md` Q57 row, same
  session):

  | Image | Memory (B) | Code install (B) | Disk (B) |
  |---|---|---|---|
  | `rayito-base-poly` 3.0 | 921 780 224 | 1 323 397 120 | 37 466 112 |
  | `rayito-base` 18.0 | 938 098 688 | 1 320 202 240 | 36 401 152 |

### What is NOT verified and is decided by the real-AWS acceptance (D12)

- Whether `NO_COLOR=1` removes the colour from Deno's `execute_result`
  under the kernel. D5's strip makes the SDK-visible result correct either
  way; the e2e records the raw fact.
- Whether a Deno kernel and its loopback TCP sockets survive
  `pause()`/`resume()`. Loopback TCP is expected to survive: §15 lists
  loopback sockets among what survives, and Q6 inferred it without
  measuring. If the differentiator test fails, the change **does not
  close**: the implementer stops and amends ADR-013, with no silent
  workaround (project rule 8).
- Whether an endless loop is stopped by the message interrupt (context
  state kept) or by rayd's 5 s restart fallback (state lost). Both satisfy
  the acceptance ("`ExecutionTimeout`, then the next cell works"); the e2e
  reports which path ran.
- The real `snapshotBuild` sizes, `kernel_ready_s`, the first- and
  second-cell latency, and the Deno kernel RSS.

## Goals / Non-Goals

**Goals.**
- `javascript` and `typescript` work in `rayito-base-poly` through Deno's
  built-in kernel.
- The kernel starts lazily, on the first `run_code`/`create_code_context`
  of the language, never before `/ready`.
- In every other image the error is `UNIMPLEMENTED` naming
  `rayito-base-poly`.
- E2B `run_code(language='js'|'ts'|'javascript'|'typescript')` works
  through the shim.
- `rayito-base` is unchanged.

**Non-goals.**
- R and Java (SPEC §4).
- Warming Deno.
- Deno outside poly.
- Per-execution `envs` on JS/TS.
- An `npm:` allowlist.
- The TypeScript E2B shim (`m9-e2b-v2-surface`).
- Moving Python or bash off `ipc`.
- Any new AWS API parameter: none is needed. Publishing reuses
  `scripts/publish_image.py --variant poly --base-image-version`, and the
  sizes come from `snapshotBuild{memorySnapshotSizeInBytes,
  codeInstallSizeInBytes, diskSnapshotSizeInBytes}` of
  `get-microvm-image-build` (§4).

## Decisions

### D1. Wire names, aliases and contexts

- **Wire names.** The canonical names are `python`, `bash`, `javascript`
  and `typescript`, in that catalog order. The SDK aliases are `js` →
  `javascript` and `ts` → `typescript`, both in the native Python and
  TypeScript SDKs, and the E2B shim inherits them through
  `normalize_language`. Normalisation is case-insensitive in the SDKs; rayd
  only accepts the canonical lowercase names, as today.
- **Separate kernels.** `javascript` and `typescript` are separate catalog
  entries, each with its own kernelspec, and each context runs its own Deno
  process. Deno transpiles TypeScript for both, so a `javascript` context
  also accepts TS syntax. That is harmless and documented. Two entries
  keep the E2B vocabulary (`RunCodeLanguage` has both), keep
  `ContextInfo.language` truthful, and need no special case in the routing
  table.
- **Language defaults.** `Execute{language: "typescript"}` without
  `context_id` routes to `default-typescript`, created lazily under the
  per-language lock, exactly like `default-bash` and `default-javascript`.
  Both count toward the 8-context cap. Neither is protected against
  `DestroyContext`.
- **Per-execution `envs`.** On a JS/TS context they stay `INVALID_ARGUMENT`
  (the set/restore cells are Python). Context `envs` from
  `create_code_context(envs=)` reach the Deno process environment. Because
  the kernel runs with `allow_all`, `Deno.env.get` reads them without a
  prompt.

### D2. Image: the pinned Deno download inside the existing poly layer

`image/Dockerfile` keeps **exactly one** layer guarded by the marker, so the
`image-variant` rule ("exactly one layer guarded by …") still holds. The
layer becomes (the Spanish header comment is rewritten to explain Deno, the
pin, the use of Python `zipfile` because `unzip` is absent, and that
`rayito-base` never runs it):

```dockerfile
RUN if [ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]; then \
      python3 -m pip install --no-cache-dir --break-system-packages \
           -r /opt/rayito/sidecar/requirements-poly.txt \
      && python3 -m pip check \
      && su user -c "python3 -c 'import bash_kernel'" \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-bash/kernel.json \
      && DENO_VERSION=2.9.7 \
      && DENO_SHA256=c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf \
      && curl -fsSL --retry 3 -o /tmp/deno.zip \
           "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-aarch64-unknown-linux-gnu.zip" \
      && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
      && mkdir -p /opt/rayito/deno \
      && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/opt/rayito/deno')" \
      && rm -f /tmp/deno.zip \
      && chown -R root:root /opt/rayito/deno \
      && chmod 0755 /opt/rayito/deno /opt/rayito/deno/deno \
      && su user -c "DENO_NO_UPDATE_CHECK=1 NO_COLOR=1 /opt/rayito/deno/deno --version" | grep -q "^deno ${DENO_VERSION} " \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-javascript/kernel.json \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-typescript/kernel.json; \
    fi
```

- **Why each step:**
  - The version plus sha256 pin is T10's discipline: the sha256 is
    immutable even if a tag moved.
  - `--retry 3` absorbs transient GitHub errors on the Lambda builder.
  - `zipfile.extract` drops the exec bit, hence the explicit `chmod`.
  - The binary is root-owned, so uid 1000 cannot replace it.
  - `/opt/rayito` is already on `FilesystemService`'s deny list (T11).
  - The `--version` check runs as `user` so a glibc or permission problem
    fails the **build**, not the first cell.
- **The layer runs after the sidecar pip install and before the `su user`
  warm-up**, as today. The warm-up and `/ready` are unchanged: Deno is not
  started before the snapshot.
- **The layer is inert in `rayito-base`:**
  - no marker, so nothing runs
  - no `/opt/rayito/deno`, so `install_kernelspecs()` publishes neither
    Deno spec
  - the sidecar's `ready.languages` stays `["python"]`
  - rayd answers `UNIMPLEMENTED`
- **Version policy.** The pin moves only through a reviewed change that
  republishes poly and re-runs the D12 e2e: a Deno bump is an image
  version, like a base-digest bump. Dependabot does not track it; no
  ecosystem covers a raw GitHub asset. The docs name the pin and how to
  bump it.

### D3. Kernelspec templates (exact files)

`kernel-sidecar/jupyter/kernels/rayito-typescript/kernel.json`:

```json
{
  "argv": [
    "/opt/rayito/deno/deno",
    "jupyter",
    "--kernel",
    "--conn",
    "{connection_file}"
  ],
  "display_name": "rayito-typescript",
  "language": "typescript",
  "interrupt_mode": "message",
  "env": {
    "NO_COLOR": "1",
    "DENO_DIR": "${HOME}/.cache/deno",
    "DENO_NO_UPDATE_CHECK": "1"
  },
  "metadata": {
    "rayito": "installed at start only when /opt/rayito/deno/deno exists (rayito-base-poly); Deno listens on 127.0.0.1 TCP and is interrupted by interrupt_request"
  }
}
```

`kernel-sidecar/jupyter/kernels/rayito-javascript/kernel.json` is the same
file with `display_name` `rayito-javascript` and `language` `javascript`.

- **argv** is what `deno jupyter --install` generates (Q61), with the
  absolute path of D2.
  - `rewritten_argv()` leaves it untouched: it only rewrites
    `/opt/rayito/sidecar` and a leading `python3`.
  - `kernel_available()` requires `argv[0]` to be an existing file.
    `jupyter`, `--kernel` and `--conn` are not absolute paths, and
    `{connection_file}` is accepted.
  - No `-m` module is involved.
- **`interrupt_mode: "message"`.** Deno handles `interrupt_request`. A
  `SIGINT` from `jupyter_client` would hit a process with no handler and
  kill it, so the cell would end as a `KernelDied`-driven restart instead
  of a clean interrupt.
- **`env`:**
  - `NO_COLOR=1` turns off Deno's inspect colours (the Q61 defect).
  - `DENO_DIR=${HOME}/.cache/deno` pins the module/npm cache under the
    sandbox user's `HOME`, whatever `XDG_CACHE_HOME` a context `envs` sets.
    `jupyter_client` expands `${HOME}` from the kernel env (see Context).
  - `DENO_NO_UPDATE_CHECK=1` stops the kernel from calling home for version
    checks from inside the customer's sandbox.
  - Kernelspec env wins over context `envs` for these three keys only,
    because `jupyter_client` applies it last.
- Python's and bash's templates are unchanged.

### D4. Transport: the Deno kernels use loopback TCP (ADR-013)

Deno 2.9.7 cannot bind `ipc` endpoints (see Context). Keeping design D4 /
the `code-execution` requirement "One ipykernel per context over ipc under
/run/rayito/k" for every language is therefore infeasible for Deno. Per
project rule 8, the exception is written down as **ADR-013 — "Kernels Deno
sobre TCP de loopback con HMAC"** in `ARCHITECTURE.md`:

- **Context.** The Deno kernel speaks only TCP.
- **Decision.** Deno contexts use `transport="tcp"`, `ip="127.0.0.1"`, with
  ports chosen by `jupyter_client`. The connection file (and its HMAC key)
  stays in `/run/rayito/k/<context_id>/kernel.json` (0700 directory, owned
  by uid 1000). Python and bash stay on `ipc`.
- **Consequence.** Any local process can open the five ports.
  - shell/control/stdin need the HMAC key; Deno verifies it and drops the
    rest.
  - The iopub PUB socket needs no key to subscribe, so a local process can
    read Deno cell outputs.
  - Every process that can reach 127.0.0.1 already runs inside the same
    sandbox: uid 1000, the same uid that owns the kernel and can read its
    connection file (T12); root `rayd`; or the platform agent (uids
    991–994).
  - The AWS proxy reaches loopback listeners (Q18), but only with a JWE
    whose `allowedPorts` contains that ephemeral port. It speaks HTTP, not
    ZMTP, and the SDK never mints `allPorts` by default (T2/T3).
  - The residual is accepted and documented in SECURITY T12.
- **Reversible.** If a future Deno ships `ipc`, flip
  `KernelLanguage.transport`; no wire or SDK change.

Implementation in `kernel-sidecar/src/rayito_kernel_sidecar`:

- `languages.py`:

  ```python
  Transport = Literal["ipc", "tcp"]

  @dataclass(frozen=True)
  class KernelLanguage:
      name: str
      kernel_name: str
      probe_cell: str
      transport: Transport = "ipc"
      strips_plain_text_ansi: bool = False
  ```

  - The catalog, in this order:
    - `python` → `KernelLanguage("python", "rayito", "pass")`
    - `bash` → `KernelLanguage("bash", "rayito-bash", ":")`
    - `javascript` → `KernelLanguage("javascript", "rayito-javascript", "void 0", transport="tcp", strips_plain_text_ansi=True)`
    - `typescript` → `KernelLanguage("typescript", "rayito-typescript", "void 0", transport="tcp", strips_plain_text_ansi=True)`
  - The probe `void 0` is a no-op expression in Deno, and it runs
    `silent=True`, so it produces no output either way.
  - The module docstring is rewritten: Deno replaces ijavascript, and it
    explains why the transport differs.
- `kernels.py`:

  ```python
  LOOPBACK_IP: Final = "127.0.0.1"

  @dataclass(frozen=True)
  class KernelEndpoint:
      transport: Transport
      ip: str

  def kernel_endpoint(language: KernelLanguage, socket_dir: Path) -> KernelEndpoint:
      """`ipc` under the context's socket directory, or loopback TCP for
      kernels that cannot bind `ipc` endpoints (Deno, ADR-013)."""
  ```

  - `kernel_endpoint` returns `KernelEndpoint("ipc", str(socket_dir / "k"))`
    or `KernelEndpoint("tcp", LOOPBACK_IP)`.
  - `KernelContext._start_kernel` builds `AsyncKernelManager(...,
    transport=endpoint.transport, ip=endpoint.ip, connection_file=...)`
    from `kernel_endpoint(self._kernel, self._socket_dir)`. The socket
    directory is still created (0700) because it holds the connection
    file, and it is still removed on stop.
  - The `KernelContext` docstring changes from "ijavascript" to "Deno
    (loopback TCP, ADR-013)".
- **No `rayd` change is involved.** rayd never touches kernel sockets. It
  kills kernel process groups by `kernel_pid` on sidecar death, which works
  the same for Deno.

### D5. `text/plain` ANSI strip for Deno results (defence in depth)

- **`protocol.py` gains a pure helper:**

  ```python
  PLAIN_TEXT_MIME: Final = "text/plain"

  def strip_plain_text_ansi(mime: Mapping[str, str]) -> dict[str, str]:
      """A copy of a serialised bundle whose `text/plain` has ANSI escape
      sequences removed; every other entry is untouched."""
  ```

  It reuses `strip_ansi`, whose regex `\x1b\[[0-9;]*[A-Za-z]` covers
  `\x1b[33m` / `\x1b[39m`.
- **`executions.py`:**
  - `run_execution(io, request_id, execution_id, code, envs, emit, *,
    strip_plain_text_ansi: bool = False)` threads the flag through
    `_run_user_cell` to `_on_iopub`.
  - For `display_data`/`execute_result`/`update_display_data`,
    `mime = serialise_mime(...)` is passed through `strip_plain_text_ansi`
    when the flag is set.
  - `KernelContext._run_cell` passes `self._kernel.strips_plain_text_ansi`.
- **What is left alone:**
  - Stream text: a user's deliberate colours in `console.log` output are
    data, as with bash.
  - Error tracebacks are already stripped for every language.
  - Python and bash results are unchanged; their flag is `False`.
- **Why both D3 and D5.** `NO_COLOR` is the primary fix. The strip
  guarantees `run_code(...).text == "42"` even if a Deno release ignores
  `NO_COLOR` in the REPL inspector (not verified under the kernel; see
  Context).

### D6. rayd-core `Language`

In `crates/rayd-core/src/code/language.rs`:

- The enum becomes `enum Language { Python, Bash, Javascript, Typescript }`
  and `pub const ALL: [Self; 4]`.
- `parse` gains `"typescript" => Ok(Self::Typescript)`.
- `as_str` gains `Self::Typescript => "typescript"`.
- The module doc names four canonical wire names and the Deno kernel.
- `default_context_id()` needs no change: `default-typescript` is 18
  characters of `[A-Za-z0-9_-]`, well within `ContextId` rules.
- `AvailableLanguages::from_ready` and `require` need no change.
- The rayd per-language lock map (`crates/rayd/src/code/manager.rs`,
  `language_locks()` over `Language::ALL`) gets its fourth lock for free.

In `crates/rayd-core/src/code/error.rs`:

- `#[error("language must be one of python, bash, javascript, typescript")]
  InvalidLanguage`.
- `LanguageUnavailable` keeps its text: "language {0} is not installed in
  this image; use rayito-base-poly". The status stays `UNIMPLEMENTED`
  (`crates/rayd/src/grpc/code.rs`, unchanged mapping).

`ResultBundle::from_mime` is unchanged: `text/plain`, `text/html`,
`image/svg+xml`, `image/png`, `image/jpeg`, `application/json` and
`text/markdown` are already fields, and anything else lands in `extra`.

### D7. SDKs (signatures unchanged; accepted values and errors change)

**Python** (`clients/python/src/rayito/_code_base.py`, the shared pure core
of `Sandbox` and `AsyncSandbox`):

```python
SUPPORTED_LANGUAGES: Final = frozenset({DEFAULT_LANGUAGE, "bash", "javascript", "typescript"})
LANGUAGE_ALIASES: Final[dict[str, str]] = {"js": "javascript", "ts": "typescript"}
```

- The public signatures are unchanged in both trees:
  - `run_code(code, *, language=None, context=None, on_stdout=None,
    on_stderr=None, on_result=None, on_error=None, envs=None, timeout=300,
    request_timeout=None) -> Execution`
  - `create_code_context(*, cwd=None, language=None, envs=None,
    request_timeout=None) -> CodeContext`
- The docstrings of `sandbox_sync/code.py`, `sandbox_sync/main.py` and
  their `sandbox_async` twins say: `javascript`/`typescript` (aliases
  `js`/`ts`) use the Deno kernel of `rayito-base-poly`, with a lazy first
  start.
- `language_default_context_id()` already builds `default-<language>` and
  needs no change.

**TypeScript** (`clients/typescript/src/sandbox/code.ts`):

```ts
export const SUPPORTED_LANGUAGES: ReadonlySet<string> = new Set([
  DEFAULT_LANGUAGE, "bash", "javascript", "typescript",
]);
export const LANGUAGE_ALIASES: ReadonlyMap<string, string> = new Map([
  ["js", "javascript"], ["ts", "typescript"],
]);
```

- The `normalizeLanguage` JSDoc and the `RunCodeOptions.language` JSDoc
  are updated.
- `runCode(code, { language, ... })` and
  `createCodeContext({ cwd, language, envs })` are unchanged.

**Error mapping** (the E2B shim column is Python only):

| Situation | Agent | Python native | TypeScript native | Python E2B shim |
|---|---|---|---|---|
| `language="r"`, `"java"`, anything unknown | not called | `InvalidArgumentException` (client-side) | `InvalidArgumentError` (client-side) | `UnimplementedError(feature="run_code(language='r')")` (unchanged) |
| `javascript`/`typescript` on an M9 image without Deno (`rayito-base`, `-slim`, `-caps`) | `UNIMPLEMENTED` "language typescript is not installed in this image; use rayito-base-poly" | `InvalidArgumentException`, `grpc_code UNIMPLEMENTED`, message names `rayito-base-poly` (unchanged contract) | `InvalidArgumentError` naming `rayito-base-poly` (unchanged contract) | **`UnimplementedError`** (feature `run_code(language='typescript')`, reason naming `rayito-base-poly`), raised `from` the native exception |
| `typescript` against a pre-M9 agent | `INVALID_ARGUMENT` "language must be one of python, bash, javascript" | `InvalidArgumentException` | `InvalidArgumentError` | `InvalidArgumentException` (propagated; documented under "Agentes anteriores a M9" in `kernels.md`) |
| `language` + `context` | not called | `InvalidArgumentException` | `InvalidArgumentError` | `InvalidArgumentException` (unchanged) |
| per-execution `envs` on JS/TS | `INVALID_ARGUMENT` | `InvalidArgumentException` | `InvalidArgumentError` | same |
| Deno cell throws | stream `error` event | `Execution.error` (`name` `Error`, `value` `boom`), never raised | same | same |

### D8. E2B shim (Python, sync and async over `_compat.py`)

- **`_compat.py`:**
  - `AVAILABLE_KERNELS_REASON` becomes "kernels disponibles: python en toda
    imagen; bash, javascript y typescript en la variante rayito-base-poly;
    R y Java no (SPEC.md §4)".
  - `normalized_language_or_unimplemented` gets a new docstring. Its logic
    is unchanged: aliases come from `normalize_language`.
  - New pure helper:

    ```python
    def unimplemented_language(
        error: SandboxException, feature: str, language: str | None
    ) -> UnimplementedError | None:
        """The shim's `UnimplementedError` for an agent that answered
        `UNIMPLEMENTED` to a kernel the image does not ship (`grpc_code`
        `UNIMPLEMENTED`), or `None` for any other error."""
    ```

    It returns `unimplemented(f"{feature}(language={language!r})",
    POLY_KERNELS_REASON)` when `error.grpc_code is
    grpc.StatusCode.UNIMPLEMENTED`. `POLY_KERNELS_REASON` is "este kernel
    sólo existe en la variante rayito-base-poly (publícala con make
    image-publish-poly y úsala como template)".
- **`e2b/_sync.py` and `e2b/_async.py`.** `run_code` and
  `create_code_context` wrap the native call:

  ```python
  try:
      return self._native.run_code(code, **kwargs)
  except InvalidArgumentException as exc:
      mapped = unimplemented_language(exc, "run_code", language)
      if mapped is None:
          raise
      raise mapped from exc
  ```

  The async twin awaits the call. The rule applies to every non-Python
  kernel, so bash on `rayito-base` through the shim now also raises
  `UnimplementedError`. That is deliberate: a feature the image lacks is
  "unimplemented" in the shim contract, never an argument error. The
  docstrings of `run_code` are updated.
- **Scope boundary.** This change maps no other shim methods.
  `list/remove/restart_code_context` exposure belongs to
  `m9-e2b-v2-surface`.
- **Spec coordination.** The shim's `UnimplementedError` requirement is
  also MODIFIED by sibling changes (`m9-server-timeout`,
  `m9-file-transfer`). This delta changes only the language sentences and
  keeps every other clause verbatim. Whichever change archives second must
  re-apply its clause onto the archived text (see Risks).

### D9. Proto delta (comment-only; applied by the Contract agent)

> **Status: applied by the Contract step (2026-09-22), verbatim.** Only the comments above `CreateContextRequest.language = 1` and `ExecuteRequest.language = 5` changed; no field, number, message, enum or RPC changed. The comments reach the TypeScript JSDoc and the Rust docs; the Python `code_pb2.py` descriptor carries no comments, so it is unchanged. Regenerated with `buf generate` (Python and TypeScript, byte-identical to the committed gencode for untouched protos) and `crates/rayito-proto/build.rs` (`PROTO_FILES` now lists `common`, `lifecycle`, `network`, `health`, `process`, `filesystem`, `pty`, `code`). `buf lint` passes with the unchanged `buf.yaml`, and `buf breaking --against` a copy of the pre-M9 `proto/` (FILE) reports nothing. Do not regenerate Python with `python scripts/gen_python.py`: grpcio-tools 1.84.0 emits protobuf 7.35.1 gencode plus a gRPC version-check preamble, which differs from the committed 7.36.1 output of `buf generate`.

In `proto/rayito/v1/code.proto`, replace the comment above
`CreateContextRequest.language = 1` with:

```proto
  // Vacío, "python", "bash", "javascript" o "typescript". bash, javascript y
  // typescript sólo existen en la variante de imagen rayito-base-poly
  // (UNIMPLEMENTED con un mensaje que nombra rayito-base-poly en las demás)
  // y su kernel arranca en la primera celda, nunca antes de /ready.
  // "javascript" y "typescript" los sirve el kernel Jupyter integrado de
  // Deno, un proceso por contexto (los dos aceptan sintaxis TypeScript).
  string language = 1;
```

And replace the comment above `ExecuteRequest.language = 5` with:

```proto
  // Selecciona el contexto por defecto de ese lenguaje ("python", "bash",
  // "javascript", "typescript"), creándolo si no existe
  // ("default-<lenguaje>"; los no Python sólo en rayito-base-poly,
  // UNIMPLEMENTED en las demás imágenes). Incompatible con context_id.
  // Vacío = el contexto indicado o el de Python.
  optional string language = 5;
```

- The sentence `"javascript" es un nombre reservado: hoy ninguna imagen
  trae ese kernel (UNIMPLEMENTED en todas).` disappears.
- No field, number, message, enum or RPC changes. `buf lint` stays clean,
  and `buf breaking` (FILE) reports nothing.
- Regenerate so the comments land in the generated code:
  - `cargo build -p rayito-proto`
  - `python scripts/gen_python.py`
  - `pnpm gen`, which updates the JSDoc in
    `clients/typescript/src/gen/rayito/v1/code_pb.ts`

### D10. `scripts/check_pins.py`: third gate, downloads

- **New constants and function:**

  ```python
  DOCKERFILE_NAME = "Dockerfile"
  PINNED_SHA256 = re.compile(r"\b[A-Z][A-Z0-9_]*_SHA256=[0-9a-f]{64}\b")
  SHA256_CHECK = "sha256sum -c"
  FLOATING_RELEASE = re.compile(r"/releases/latest\b|/latest/download/")
  DOWNLOAD_REASON = "la descarga no está verificada contra un sha256 fijado"

  def unpinned_downloads(text: str) -> list[Finding]: ...
  ```

- **How `unpinned_downloads` works:**
  - It joins backslash-continued lines into logical instructions,
    remembering each one's first line number, and skips comment lines.
  - An instruction containing the word `curl` is a finding unless all three
    hold:
    - it contains a `<NAME>_SHA256=<64 lowercase hex>` assignment
    - it contains `sha256sum -c`
    - it contains no floating-release URL
  - Findings are `(line, first line text, DOWNLOAD_REASON)`.
- **Wiring:**
  - `DEFAULT_PATHS` gains `"image/Dockerfile"`.
  - `findings_for()` applies `unpinned_downloads` when
    `path.name == DOCKERFILE_NAME`. The Dockerfile keeps the `uvx` gate
    too, which finds nothing there.
  - The module docstring describes gate 3.
- **Result.** The real tree passes: D2's layer satisfies all three
  conditions. Without D2 there is no `curl` in the Dockerfile, so the gate
  is vacuous.
- **Refinement after review (2026-09-23).** The three conditions above are
  per instruction, so a second `curl` or a `curl … | sh` inside an already
  verified instruction passed, and `wget`/`ADD <url>` were not gated. The
  gate now also requires, per instruction: every `curl` writes with
  `-o`/`--output` to a path that appears in a `sha256sum -c` command of the
  same instruction; no more `curl` commands than `sha256sum -c` commands; no
  pipe after `curl`. `wget` and an `ADD` whose source is `http(s)://` are
  always findings. The check stays lexical (no shell-variable expansion, no
  subshells), which the module docstring and SECURITY T10 state.

### D11. Tests that fail without the change

**rayd-core** (`language.rs`, `error.rs` unit tests):
- `parse_accepts_the_canonical_names_and_empty` asserts
  `Language::parse("typescript") == Ok(Typescript)`. `"ts"`, `"TypeScript"`
  and `"typescript "` are still `InvalidLanguage` (aliases are the SDKs'
  job).
- `names_and_default_context_ids` loops over the four entries of `ALL` and
  asserts `default-typescript`.
- `execute_target_follows_the_routing_table` gains
  `(None, Some("typescript")) → LanguageDefault(Typescript)`.
- `available_languages_from_ready_keep_python_and_count_unknowns` gains
  `from_ready(["typescript","javascript"])` → both present, `unknown == 0`.
- The `error.rs` test asserts the exact `InvalidLanguage` string with the
  four names, and that
  `LanguageUnavailable(Typescript).to_string()` contains `typescript` and
  `rayito-base-poly`.

**rayd integration** (`crates/rayd/tests/m9_deno.rs`, `#![cfg(unix)]`,
against the stdlib fake sidecar):
- *Lazy default.* With `--languages python,javascript,typescript`, a first
  `Execute{language:"typescript"}` produces exactly one `create_context`
  line with `"language":"typescript"` and `"context_id":"default-typescript"`
  before the `execute` line. `ListContexts` then shows
  `default-typescript`/`typescript` after `default`.
- *Not shipped.* With `--languages python,bash`, `Execute{language:
  "typescript"}` and `CreateContext{language:"javascript"}` fail
  `UNIMPLEMENTED` with `rayito-base-poly` in the message, and no
  `create_context` line was sent.
- *Unknown name.* `Execute{language:"ts"}` fails `INVALID_ARGUMENT` whose
  message names `typescript`.
- *Envs refused.* Per-execution `envs` on `language:"typescript"` fail
  `INVALID_ARGUMENT` with no `execute` line.
- The fake sidecar (`crates/rayd/tests/fixtures/fake_sidecar.py`) sets
  `KNOWN_LANGUAGES = ("python", "bash", "javascript", "typescript")`, with
  its docstring updated. Without that, its `create_context` would answer
  `unknown language` and the lazy-default test would fail.

**Sidecar** (`kernel-sidecar/tests`, run on any host):
- `test_languages.py::test_catalog_names_kernels_and_probe_cells` asserts:
  - the four names in order
  - the `KernelLanguage` values of D4, including `transport` and
    `strips_plain_text_ansi`
  - `language_for("typescript")`
  - `language_for("ts") is None`
- `test_repository_ships_the_deno_templates` replaces
  `test_repository_ships_python_and_bash_templates_only`. For both Deno
  templates it asserts the exact argv of D3, `interrupt_mode == "message"`,
  `env == {"NO_COLOR": "1", "DENO_DIR": "${HOME}/.cache/deno",
  "DENO_NO_UPDATE_CHECK": "1"}` and `language`. For python and bash it
  keeps `interrupt_mode == "signal"`.
- `test_deno_specs_need_the_binary` (monkeypatched):
  - A copy of the templates whose argv[0] points at `tmp_path/"deno"` is
    written for both languages only while that file exists. The ready list
    is then `["javascript", "python", "typescript"]`, plus bash when its
    module is faked importable.
  - With the real templates and no `/opt/rayito/deno` on the host, the
    Deno specs are absent.
  - This replaces `test_javascript_needs_node_and_the_kernel_file`.
- `test_kernel_endpoint` checks `kernel_endpoint(LANGUAGES["python"],
  d) == KernelEndpoint("ipc", str(d / "k"))` and
  `kernel_endpoint(LANGUAGES["typescript"], d) ==
  KernelEndpoint("tcp", "127.0.0.1")`.
- `test_protocol.py::test_strip_plain_text_ansi` checks that
  `{"text/plain": "\x1b[33m42\x1b[39m", "text/html": "<b>\x1b[1m</b>"}`
  becomes `{"text/plain": "42", "text/html": "<b>\x1b[1m</b>"}` (html
  untouched).
- `test_executions.py::test_plain_text_ansi_stripped_only_when_asked`
  feeds a fake `execute_result` with the coloured `text/plain` through
  `run_execution` twice:
  - with `strip_plain_text_ansi=True` the emitted `result.mime["text/plain"]
    == "42"`
  - with the default it keeps the escapes (Python/bash unchanged)
- `test_kernel.py::test_deno_typescript_context` is marked `kernel`, so it
  is excluded by default like the other real-kernel tests. It is skipped
  unless `RAYITO_TEST_DENO` names an executable. It rewrites argv[0] of the
  template copy to that path and starts a `typescript` `KernelContext`,
  then checks:
  - `const x: number = 40 + 2; x` → a `result` whose `text/plain` is `42`
  - `while (true) {}` interrupted by `interrupt(execution_id)` ends
  - `x` still returns `42`, which proves message-interrupt over TCP
  It is a local and optional check; D12 is the acceptance.

**SDK fakes** (test support, updated with the tests below):
- `clients/python/tests/unit/fake_code.py` and
  `clients/typescript/tests/unit/fake/code.ts` gain `typescript` in
  `KNOWN_LANGUAGES`, and their `INVALID_ARGUMENT` text becomes rayd's new
  `InvalidLanguage` message.
- Their docstrings name `default-typescript`.

**Python SDK** (`clients/python/tests/unit`):
- `test_code_base.py::test_normalize_language_table` gains `("typescript",
  "typescript")`, `("TypeScript", "typescript")`, `("ts", "typescript")` and
  `("TS", "typescript")`.
- `typescript` leaves the rejected list of
  `test_normalize_language_rejects_other_kernels`. It keeps `r`, `java`,
  `ruby` and `" bash"` and gains `"tsx"`.
- `test_code_sync.py`/`test_code_async.py` (fake rayd):
  `run_code("1", language="ts")` sends `language == "typescript"`, and
  `create_code_context(language="TypeScript").language == "typescript"`.
- `test_e2b_compat_base.py`:
  - `unimplemented_language()` returns an `UnimplementedError` whose reason
    names `rayito-base-poly` for an `InvalidArgumentException` with
    `grpc_code UNIMPLEMENTED`, and `None` for one with `INVALID_ARGUMENT`.
  - `AVAILABLE_KERNELS_REASON` names `typescript`.
- `test_e2b_compat_sync.py`/`_async.py`:
  - `run_code("1", language="ts")` reaches the fake with `typescript`.
  - When the fake answers `UNIMPLEMENTED` naming `rayito-base-poly`,
    `run_code(language="javascript")` and
    `create_code_context(language="typescript")` raise `UnimplementedError`
    (not `InvalidArgumentException`) with `__cause__` set.
  - `run_code(language="r")` is unchanged.

**TypeScript SDK** (`clients/typescript/tests/unit/code.test.ts`):
- The "language routing against the fake" test gains
  `runCode("1", { language: "ts" })` and
  `createCodeContext({ language: "TypeScript" })` carrying `typescript`,
  and `default-typescript` listed by the fake.
- The "invalid language combinations" test keeps `r` rejected and adds
  `tsx`.
- The "language not shipped" test is parametrised over `javascript` and
  `typescript`.

**Pins** (`scripts/tests/test_check_pins.py`):
- `unpinned_downloads` flags each of:
  - a `curl` instruction with no sha256
  - one with a sha256 but no `sha256sum -c`
  - one with a 63-hex sha256
  - a `releases/latest` URL
- It accepts D2's exact instruction text (as a fixture string with
  placeholders only) and skips commented `curl` lines.
- `main()` over the real tree still returns 0 and prints OK.

### D12. Real-AWS acceptance

Placeholders only in tracked files. Images are referenced by name
(`rayito-base-poly`, `rayito-base`) through `RAYITO_TEMPLATE_POLY` and
`RAYITO_TEMPLATE`.

**Publish.**
1. `make image-publish-poly` is run by hand on Windows, because `make` is
   absent: `copy_sidecar.py`, `image_zip.py --variant poly`, then
   `publish_image.py --variant poly --base-image-version
   $(BASE_IMAGE_VERSION)`.
2. The same for `rayito-base`.
3. Record for both: build seconds and `snapshotBuild` memory / code install
   / disk.

**Shared fixture.** Move `poly_template()` and the `poly_sandbox` fixture
from `test_m7_poly_kernels.py` into `clients/python/tests/e2e/conftest.py`,
unchanged, so both files share them.

**Python** — `clients/python/tests/e2e/test_m9_deno_kernels.py`
(`pytestmark = pytest.mark.e2e`; module docstring in Spanish; `report()`
prints `[m9] label: value`). Each test uses a fresh `poly_sandbox` unless
noted.

1. `test_typescript_cell`:
   - `first = run_code("const x: number = 40 + 2; x", language="typescript")`
     → `first.text == "42"`, `"\x1b" not in first.text`,
     `first.error is None`. First-cell latency (lazy start) is reported.
   - `run_code("x + 1", language="typescript").text == "43"`. Second-cell
     latency is reported.
   - `list_code_contexts()` contains
     `CodeContext(id="default-typescript", language="typescript", …)`.
   - Deno kernel RSS is reported through
     `commands.run("ps -o rss= -C deno")`.
2. `test_javascript_cell`:
   - `run_code("let y = 40 + 2; y", language="javascript").text == "42"`
     with no ESC, first- and second-cell latency reported.
   - `run_code("y", language="js").text == "42"` (alias, same default
     context).
3. `test_deno_outputs`, on `typescript`:
   - `console.log('out')` → `"out" in "".join(logs.stdout)`
   - `console.error('err')` → `"err" in "".join(logs.stderr)`
   - ``Deno.jupyter.html`<b>hi</b>` `` → `results[0].html == "<b>hi</b>"`
   - `throw new Error('boom')` → `error.name == "Error"` and
     `error.value == "boom"`
   - `await new Promise((r) => setTimeout(() => r(7), 200))` →
     `text == "7"`
4. `test_typescript_context_envs_timeout_restart`:
   - `ctx = create_code_context(language="typescript", envs={"K": "1"})` →
     `ctx.language == "typescript"`.
   - `run_code("Deno.env.get('K') === '1'", context=ctx).text == "true"`.
   - `run_code("let z = 5", context=ctx)`.
   - `run_code("while (true) {}", context=ctx, timeout=2)` →
     `error.name == "ExecutionTimeout"`. Elapsed seconds reported.
   - `run_code("1 + 1", context=ctx).text == "2"`.
   - Report whether `z` survived (`run_code("z", context=ctx)`: `text ==
     "5"` means message interrupt, `ReferenceError` means rayd restart).
   - `run_code("let w = 9", context=ctx)`,
     `restart_code_context(ctx)`, then `run_code("w", context=ctx)` →
     `error.name == "ReferenceError"`. `ctx.id` is still listed.
   - `remove_code_context(ctx)`.
5. `test_javascript_survives_pause_resume` (the differentiator):
   - `run_code("globalThis.kept = 42", language="javascript")`,
     `generation = get_health().resume_generation`, `pause()`, `resume()`.
   - `run_code("kept", language="javascript").text == "42"`.
   - `get_health().kernel_state_lost is False`.
   - `get_health().resume_generation > generation`.
6. `test_e2b_shim_js_ts_on_poly`:
   - `shim = rayito.e2b.Sandbox.connect(poly.sandbox_id,
     access_token=poly.access_token, control_plane=control_plane)` →
     `shim.run_code("1 + 1", language="js").text == "2"` and
     `shim.run_code("const n: number = 3; n", language="ts").text == "3"`.
   - The async twin with `rayito.e2b.AsyncSandbox.connect` runs one `ts`
     cell.
7. `test_js_ts_unimplemented_on_base` (native `sandbox` fixture on
   `RAYITO_TEMPLATE`), for `language` in `("javascript", "typescript")`:
   - `run_code("1", language=language)` raises `InvalidArgumentException`
     with `grpc_code UNIMPLEMENTED`, and `"rayito-base-poly"` is in the
     message.
   - `create_code_context(language=language)` behaves the same.
   - `list_code_contexts()` ids stay `["default"]`.
   - `commands.run("test -e /opt/rayito/deno")` raises
     `CommandExitException`: the layer was inert.
   - The shim on the same sandbox:
     `rayito.e2b.Sandbox.connect(...).run_code("1", language="ts")` raises
     `rayito.e2b.UnimplementedError` naming `rayito-base-poly`.
8. `test_deno_snapshot_sizes`, which creates no VM:
   - **Inputs.** It reads:
     - `RAYITO_POLY_SIZES` and `RAYITO_BASE_SIZES`: the two new publishes,
       `memoria,code,disco` in bytes.
     - `RAYITO_BASE_PREVIOUS_SIZES`: the newest `rayito-base` published
       before this change, read with `get-microvm-image-build`
       `snapshotBuild`.
     - `RAYITO_RAYD_BYTES_DELTA`: the new `image/rayd` size minus the rayd
       of that previous version, read with
       `commands.run("stat -c %s /usr/local/bin/rayd")` in a sandbox of it.
     It is skipped with a reason naming the variables when any is missing.
   - **Constants.** `POLY_3_0 = (921_780_224, 1_323_397_120, 37_466_112)`
     and `BASE_18_0 = (938_098_688, 1_320_202_240, 36_401_152)`, the Q57
     same-session pair.
   - **Deno cost**, computed pairwise so the rayd binary cancels out:
     - `deno_code = (poly.code − base.code) − (POLY_3_0.code − BASE_18_0.code)`,
       asserted `110_000_000 ≤ deno_code ≤ 160_000_000`
     - `deno_memory = (poly.mem − base.mem) − (POLY_3_0.mem − BASE_18_0.mem)`,
       asserted `|deno_memory| ≤ 40_000_000`
   - **`rayito-base` inert**:
     - `|base.mem − previous.mem| ≤ 20_000_000` (the M7 band)
     - `|base.code − previous.code − RAYITO_RAYD_BYTES_DELTA| ≤ 50_000_000`
   - **Band revision (2026-09-24, owner-approved, Q77).** The first draft
     asserted `70-100 MB` for `deno_code` (the ≈ 85 MB binary of Q61),
     `±20 MB` for `deno_memory` and the M7 `±10 MB` for the base code
     install. The real publishes measured `deno_code` 136 863 744 B
     (poly 5.0 / base 21.0) and 132 603 904 B (the 22.0 pair), while
     `stat` of the Deno binary in the sandbox gives 84 971 392 B:
     `codeInstallSizeInBytes` is not the sum of the added file bytes, so
     the band is the measured cost with margin (110-160 MB). `deno_memory`
     measured 18 427 904 B and 31 584 256 B; a pairwise delta adds the
     snapshot noise of four builds (±13 MB each, Q50), so the band is
     ±40 MB. The base code install grew 38-41 MB net of `rayd` against the
     pre-M9 20.0 because `m9-e2b-v2-surface` D16 installs git-core
     (Q76, ≈ 31.5 MB of RPMs plus ≈ 6 MB of install growth), not because
     of the Deno layer (still inert: `test -e /opt/rayito/deno` exits 1),
     so that band is 50 MB. For the same reason
     `test_m7_poly_kernels.py::test_snapshot_sizes`, which compares
     against 17.0, moves its code band from 10 MB to 70 MB (measured
     56 285 288 B on 22.0: git-core plus the `rayd` growth since 17.0 that
     `RAYITO_RAYD_BYTES_DELTA`, now measured against the previous version,
     no longer subtracts).
   - All six numbers are reported.

**`kernel_ready_s` unchanged.** Run `scripts/bench_cold_start.py
--phases a --sequential 5` once with `--template rayito-base` and once with
`--template rayito-base-poly`, with `--out` in the scratchpad (raw JSON is
never committed). Accepted when `p50(poly) ≤ p50(base) + 1.0 s`. Both p50s
go in the `AWS_API_NOTES.md` row.

**TypeScript** (`clients/typescript/tests/e2e/poly.e2e.test.ts`).
- **Poly test** ("bash, javascript and typescript cells, exclusive options,
  listing"), which replaces the JS-unimplemented assertion:
  - `runCode("const x: number = 40 + 2; x", { language: "typescript" })` →
    `text === "42"` with no `\u001b`
  - `runCode("let y = 40 + 2; y", { language: "js" })` → `"42"`
  - `console.log('out')` in stdout
  - ``Deno.jupyter.html`<b>hi</b>` `` → `results[0].html === "<b>hi</b>"`
  - `throw new Error('boom')` → `error.name === "Error"`,
    `error.value === "boom"`
  - `ctx = await createCodeContext({ language: "typescript", envs: { K: "1" } })`
    → `runCode("Deno.env.get('K') === '1'", { context: ctx })` gives
    `"true"`
  - `listCodeContexts()` contains `default-typescript` and
    `default-javascript` with their languages
  - the bash and exclusive-option assertions stay
- **New base test** "javascript and typescript are UNIMPLEMENTED on
  rayito-base". It is skipped unless `RAYITO_E2E=1` and `RAYITO_TEMPLATE`
  are set, uses `createTestSandbox(e2e)`, and asserts that both languages
  reject with `InvalidArgumentError` whose message contains
  `rayito-base-poly`.

**Closing.** Closing requires, against real AWS:
- the Python file above
- the rest of `test_m7_poly_kernels.py` (bash unchanged)
- the TypeScript poly test

The implementer deletes
`test_m7_poly_kernels.py::test_javascript_is_known_but_not_shipped`: its
claim becomes false, and D12.1–2 replace it. Zero live VMs must remain
(`list-microvms` for both images).

**`AWS_API_NOTES.md`** gains a §16 row. It takes the next free number at
implementation time (Q63 unless a sibling change took it), titled
"Kernels Deno (`javascript`/`typescript`) en `rayito-base-poly` publicado".
It records, with placeholders only:
- the build seconds of both images
- the six `snapshotBuild` numbers
- `deno_code` and `deno_memory`
- the `kernel_ready_s` p50 of both images
- the first- and second-cell latency for TS and JS
- Deno RSS
- whether `NO_COLOR` alone removed the escapes (the raw `execute_result`,
  seen in a sidecar kernel test or a `commands.run` probe)
- which timeout path ran
- the pause/resume result

### D13. Docs to touch (Spanish prose; identifiers English)

- **`SPEC.md`:**
  - The §3 kernels row: `python`; `bash`, `javascript` and `typescript` in
    `rayito-base-poly` (Deno, lazy); M7/M9.
  - The §4 "Kernels que no sean Python" bullet: JS/TS delivered by M9
    through Deno. R and Java stay out, with the measured reasons (R via
    conda-forge `r-base` + `r-irkernel` 1.4 GB installed; R-core by `dnf`
    127 MB plus cairo/pango/harfbuzz/tk/fonts; Corretto 21 headless 262 MB
    plus IJava without maintenance: one release since 2023, does not build
    on JDK 17/18).
- **`ARCHITECTURE.md`:**
  - ADR-013 (D4).
  - The Capa 1 table row "Kernel Jupyter" says `ipc` for Python/bash and
    loopback TCP for Deno (ADR-013).
  - The Dockerfile paragraph of Capa 1 mentions the Deno pin.
- **`docs/site/docs/kernels.md`:**
  - The languages table: `javascript` (alias `js`) and `typescript` (alias
    `ts`) are Deno in poly and `UNIMPLEMENTED` elsewhere.
  - Examples of TS, JS, `Deno.jupyter.html`, `npm:` imports and
    `Deno.env`.
  - "Arranque perezoso" gains the Deno first-cell latency from D12.
  - "JavaScript: reservado, no incluido" becomes "JavaScript y TypeScript
    con Deno": why Deno, the pin and how to bump it, TCP loopback (ADR-013),
    the message interrupt, `NO_COLOR`/ANSI, and full permissions like the
    Python kernel.
  - "Agentes anteriores a M9": pre-M9 agents answer `INVALID_ARGUMENT` for
    `typescript`.
  - A short "R y Java" paragraph with the numbers.
- **`docs/site/docs/e2b-compat.md`:** the kernels row. `python`, `bash`,
  `javascript` (`js`) and `typescript` (`ts`) are accepted; the missing
  kernel in a non-poly image raises `UnimplementedError`; `r`/`java` stay
  `UnimplementedError`.
- **`README.md`:** line 73, JS/TS through Deno in `rayito-base-poly`.
- **`SECURITY.md`:**
  - T8 appends: `npm:`, `jsr:` and `https:` imports inside a Deno cell
    download and run code over the same egress as `pip install`, with the
    same controls (VPC connector) and no new risk class.
  - T10 appends the Deno download pinned by version + sha256 and checked by
    `check_pins.py` gate 3.
  - T12 appends the ADR-013 residual: Deno kernel ports on 127.0.0.1;
    shell/control need the HMAC key; iopub outputs are readable by local
    processes, which all share the sandbox; Deno `allow_all` equals the
    Python kernel's uid-1000 power.
- **`MILESTONES.md`:** the M9 section (created by the milestone owner if it
  does not exist) lists `m9-deno-kernels` and its acceptance (D12) with a
  status line using placeholders.
- **Other docstrings:**
  - `clients/python/src/rayito/cli/_artifact.py`: the module docstring
    mentions Deno instead of "javascript reserved".
  - The Python/TypeScript docstrings named in D7.

### D14. Security posture and audit rows

- **No new listener or credential.**
  - The Deno kernel runs as uid 1000 under the sidecar's `PreExecPlan`
    (rlimits, process group), like every kernel (T6, T12).
  - The kernel's threads count toward the shared `RLIMIT_NPROC` 512.
    Measured RSS is recorded (D12).
- **Logging.** Unchanged: the sidecar never logs code, output or envs, and
  the Deno template adds no logged field.
- **No `SECURITY_AUDIT.md` §8 regression:**
  - No hook, S3, IMDS or filesystem path changes, so C-01, C-02, C-03,
    C-05, C-07, C-08, C-09, C-10 and C-12 are untouched.
  - `/opt/rayito/deno` is covered by the existing `/opt/rayito` deny entry
    (T11).
- **Supply chain (T10).** The sha256 pin makes the build refuse any other
  binary. The digest is recorded in this design and in the Dockerfile.

## Risks / Trade-offs

- **[Loopback TCP might not survive suspend/resume]** → D12.5 detects it.
  On failure the change stays open and ADR-013 is amended (for example
  with a resume-time context restart that honestly reports
  `kernel_state_lost`). No silent fallback.
- **[`NO_COLOR` ignored by the REPL inspector]** → D5 strips `text/plain`.
  The raw fact is recorded.
- **[The interrupt may not stop a tight loop]** (V8 terminate should, but
  it is unmeasured) → rayd's 5 s restart fallback still delivers
  `ExecutionTimeout` and a working next cell. State loss is reported.
- **[Two Deno processes when both languages are used]** → each context is
  a separate kernel under the same 8-context cap. RSS is reported, and the
  docs recommend one of the two.
- **[Deno kernel marked unstable upstream]** → the exact version is pinned.
  Bumps republish poly and re-run D12.
- **[GitHub availability at build time]** → `--retry 3`. A failure fails
  the poly build loudly; `rayito-base` is unaffected.
- **[iopub readable by local processes]** → accepted residual (ADR-013,
  T12). Every such process is already inside the sandbox trust boundary.
- **[Sibling deltas on the same `e2b-compat` requirement]** → this delta
  changes only the language sentences. The archive agent of the second
  change re-applies its clause onto the archived text before `openspec
  archive`.
- **[Existing e2e `test_javascript_is_known_but_not_shipped` and the TS
  JS-unimplemented assertion become false]** → deleted and replaced in the
  same change (D12).

## Migration Plan

1. Proto comments (Contract agent), then regenerate the three clients.
2. rayd-core `Language`, then sidecar, then SDKs; no ordering constraint
   between the SDKs.
3. Image layer + `check_pins` gate.
4. Publish `rayito-base-poly` and `rayito-base`, then run the e2e, then
   write the `AWS_API_NOTES` row and docs.

Rollback: republish the previous poly version. The SDKs remain compatible
with pre-M9 agents (documented error).

## Open Questions

None blocking. The owner confirms two judgment calls:
- ADR-013, loopback TCP for Deno kernels only.
- The shim mapping every "kernel not in this image" `UNIMPLEMENTED` to
  `UnimplementedError`, which also changes bash on `rayito-base` through
  the shim.
