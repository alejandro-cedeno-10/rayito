## Context

State on 2026-09-16: M0–M6 accepted on real AWS and archived;
`m7-oss-hygiene` and `m7-supply-chain` implemented; `rayito-base` 17.0 is
the current product image (`memorySnapshotSizeInBytes` 928 100 352 B,
`AWS_API_NOTES.md` Q52) built from a single `image/Dockerfile` pinned by
digest; the `slim` and `caps` variants reuse that Dockerfile. No git
repository exists on the box; files are written, not committed.

Facts verified in the repository for this design:

- `proto/rayito/v1/code.proto`: `CreateContextRequest` has `string
  language = 1` ("Vacío o \"python\" en el alcance actual"); `ExecuteRequest`
  has fields 1–4 (`context_id`, `code`, `timeout_ms`, `envs`) and **no**
  `language`; `ContextInfo.language` exists and `ListContexts` fills it.
- `crates/rayd-core/src/code/context.rs`: `CONTEXT_LANGUAGE = "python"`,
  `plan_context` rejects anything but `""`/`"python"` with
  `CodeError::InvalidLanguage` (`INVALID_ARGUMENT`), `ContextEntry::new`
  hard-codes the language, `ContextId::parse` accepts `[A-Za-z0-9_-]{1,64}`
  so `default-bash` is a valid id. `crates/rayd/src/code/manager.rs`:
  `execute_unchecked` resolves `ContextId::parse_or_default(context_id)` and
  sends `SidecarOp::Execute{context_id, execution_id, code, envs}`;
  `create_context` sends `SidecarOp::CreateContext{context_id, cwd, envs}`.
  `status_for` already maps `CodeError::Unsupported` to `UNIMPLEMENTED`.
- `crates/rayd-core/src/code/protocol.rs`: `SidecarEvent::Ready{v,
  default_context_id, kernel_pid, warmup_ms}`; `ReseedPayload{reseeded,
  deferred (default), failed}`; unknown fields are ignored by serde (no
  `deny_unknown_fields`). The golden file
  `kernel-sidecar/tests/fixtures/protocol_v1.jsonl` line 2 is the
  `create_context` request and is compared byte for byte by both codecs.
- `kernel-sidecar/src/rayito_kernel_sidecar/kernels.py`: one kernelspec
  `rayito` (`KERNEL_NAME`), written at start by `install_kernelspec` from
  `jupyter/kernels/rayito/kernel.json` (rewriting `/opt/rayito/sidecar` and
  `python3`); `KernelContext._start_kernel` uses
  `AsyncKernelManager(kernel_name=KERNEL_NAME, transport="ipc", ...)`, a
  `KernelSpecManager(kernel_dirs=[<socket_root>/kernelspec/kernels],
  ensure_native_kernel=False)`, then `run_silent(self, "pass")`;
  `ContextBase.language` is a property returning `"python"`;
  `RESEED_CELL`, `set_envs_cell` and `RESTORE_ENVS_CELL`
  (`executions.py`) are Python source; `_probe_kernel` sends a
  `kernel_info_request` on the control channel (language-agnostic).
  `server.py`: `ContextFactory = Callable[[str, str, Mapping[str, str]],
  ContextBase]`; `_op_reseed` replies `{reseeded, deferred, failed}`.
- `scripts/image_zip.py`: `VARIANTS`, `WARMUP_MARKER_ENTRY =
  "kernel-sidecar/ipython/startup/warmup_variant"`, `marker_variant()`;
  `scripts/publish_image.py`: `DEFAULT_IMAGE_NAMES = {"full", "slim"}`,
  `require_matching_variant` before any AWS call. `image/Dockerfile`: one
  file, `COPY kernel-sidecar/ /opt/rayito/sidecar/` precedes the pip
  install, the tool-check loop fails the build when a tool is missing.
  `Makefile`: `image-zip[-slim]`, `image-publish[-slim|-caps]`; CI asserts
  the slim marker is present in the slim zip and absent in the full one.
- The Lambda MicroVMs image API has no build-argument parameter: the zip's
  root `Dockerfile` is what AWS builds (`AWS_API_NOTES.md` §4,
  `codeArtifact`, `ARCHIVE_DOCKERFILE_NOT_FOUND`). `snapshotBuild
  {memorySnapshotSizeInBytes, codeInstallSizeInBytes, diskSnapshotSizeInBytes}`
  is what `publish_image.py` prints (Q21, Q34, Q50, Q52); identical builds of
  `rayito-base` oscillate ±13 MB in memory size (Q50).
- Amazon Linux 2023 (docs.aws.amazon.com/linux/al2023/ug/nodejs.html, read
  2026-09-16): Node.js is packaged as namespaced `nodejs20` / `nodejs20-npm`
  (also 22, 24); the namespaced executables `node-20` and `npm-20` always
  exist, while `node`/`npm` are `alternatives` symlinks that may point
  elsewhere. `dnf install nodejs20` is the documented command.
- `clients/python`: `_code_base.py` has `SUPPORTED_LANGUAGES = {"",
  "python"}`, `validate_language`, `build_execute_request(code, *,
  context_id, envs, timeout)`, `build_create_context_request(language, cwd,
  envs)`; `Sandbox.run_code` / `AsyncSandbox.run_code` have no `language`;
  `rayito/e2b/_compat.py::language_or_unimplemented` raises for anything
  but `python`/`None`. `clients/typescript/src/sandbox/code.ts`:
  `SUPPORTED_LANGUAGES`, `validateLanguage`, `RunCodeOptions` without
  `language`, `CreateContextOptions.language`.
- `openspec/specs/e2b-compat/spec.md` requires `run_code("1",
  language="js")` to raise `UnimplementedError`; `openspec/specs/
  code-execution/spec.md` requires `CreateContext` to reject anything but
  `""`/`"python"`, one `ipykernel` per context and `ipython_kernel_config.py`
  for every kernel. All three are modified by this change's deltas.
- Not verified on the box (decided by a gated spike, D5): that
  `ijavascript`'s native dependency (`zeromq` via `jmp`) installs from
  prebuilt binaries on al2023 ARM64 without a compiler, and the exact
  current PyPI/npm versions of `bash_kernel` and `ijavascript`.

## Goals / Non-Goals

Goals:

- `run_code(code, language="bash")` and `run_code(code, language=
  "javascript")` return `Execution`s with the same shape as Python cells
  (stdout, results, structured errors, `ExecutionTimeout`), on an image
  that ships those kernels, from both SDKs and the E2B shim.
- The non-Python kernels start lazily on first use, so `/ready`, the
  snapshot and `kernel_ready` timings of the Python kernel do not move.
- `rayito-base` is unaffected in content: same packages, same warm-up,
  `memorySnapshotSizeInBytes` within the measured noise band.
- Honest declaration everywhere: a language the image does not ship is
  `UNIMPLEMENTED` with a clear message, never a Python `SyntaxError`.
- One Dockerfile, one sidecar, one `rayd` binary for every variant.

Non-goals (report §5 and the brief):

- R, Java, TypeScript-as-a-kernel, or any kernel beyond `bash` and
  `javascript`; language aliases beyond `js`.
- Warming non-Python kernels before `/ready`, or a "poly" snapshot with
  three live kernels.
- Per-execution `envs` for non-Python contexts (context-level `envs` do
  work: they are process environment).
- `Health` advertising languages, an SDK compatibility gate on
  `agent_version`, version bumps (`docs/RELEASING.md`).
- Composite variants (`slim`+`poly`, `caps`+`poly`).
- Rich outputs for non-Python kernels beyond what the kernels already emit
  (no chart/data formatters for JS or bash).

## Decisions

### D1. Wire: `ExecuteRequest.language` and the canonical names

`proto/rayito/v1/code.proto`, `ExecuteRequest` gains:

```proto
  // Selecciona el contexto por defecto de ese lenguaje ("python", "bash",
  // "javascript"), creándolo si no existe. Incompatible con context_id.
  // Vacío = el contexto indicado o el de Python.
  optional string language = 5;
```

Field number 5 is the next free one; `optional` so presence is explicit
(`has_language`). `CreateContextRequest.language` keeps its number and
comment updated to "Vacío, \"python\", \"bash\" o \"javascript\"".
`ContextInfo.language` is unchanged. `buf lint` and `buf breaking
--against` the archived proto pass (additive). Regeneration: `cargo build`
(Rust, `tonic-prost-build` + `protox`), `python scripts/gen_python.py`,
`cd clients/typescript && pnpm gen` (the repo's `make proto` steps, `buf lint`
+ `buf generate`, run by hand).

Canonical wire values: `python`, `bash`, `javascript`. Nothing else is ever
sent by the SDKs; `rayd` treats anything else as `INVALID_ARGUMENT` with
the message `language must be one of python, bash, javascript`. Aliases
(`js`, capitalisation) are resolved in the SDKs (D7), never on the wire.

### D2. rayd: `Language` value type, per-language default contexts, lazy creation

`rayd-core/src/code/language.rs` (new, pure):

```rust
pub enum Language { Python, Bash, Javascript }
impl Language {
    pub fn parse(raw: &str) -> Result<Self, CodeError>;   // "" -> Python; else the three names
    pub fn as_str(self) -> &'static str;                    // "python" | "bash" | "javascript"
    pub fn default_context_id(self) -> ContextId;           // "default" | "default-bash" | "default-javascript"
}
```

`CONTEXT_LANGUAGE` is removed; `ContextInfo.language` becomes `Language`
(converted with `as_str` at the gRPC boundary); `ContextEntry::new(context_id,
language, cwd, envs)`; `CreateContextInput.language` stays a `String` and
`plan_context` returns the parsed `Language` alongside `cwd` and `envs`.
`CodeError::InvalidLanguage` message becomes `language must be one of
python, bash, javascript` (still `INVALID_ARGUMENT`).

Availability. The sidecar's `ready` event (D3) carries `languages`; the
supervisor stores them in `SidecarState` (a `BTreeSet<Language>`;
`python` always present; absent field = `{python}` for an older sidecar).
`plan_context` and the `Execute` path check the set:
`CodeError::LanguageUnavailable(Language)` with message `language bash is
not installed in this image; use rayito-base-poly` maps to
`Status::unimplemented` in `status_for`.

`Execute` routing (`CodeManager::execute_unchecked`, `ExecuteInput` gains
`language: Option<String>`):

| `context_id` | `language` | Behaviour |
|---|---|---|
| absent | absent / `""` | `default` (Python), as today |
| absent | `python` | `default` |
| absent | `bash` / `javascript` | the per-language default context; created lazily if not live |
| present | absent / `""` | that context, as today |
| present | anything non-empty | `INVALID_ARGUMENT` `language cannot be combined with context_id` (E2B raises the same way) |

Lazy creation: `ensure_language_default(language)` takes a per-language
`tokio::sync::Mutex` (a `BTreeMap<Language, Arc<Mutex<()>>>` on
`CodeManager`), re-checks the registry under the lock, and if the context
is absent runs the same path as `create_context` with the fixed id
`Language::default_context_id`, `cwd` = the `/run` payload `workdir` or the
home (same default as `CreateContext`), `envs` = the `/run` payload envs
(what the rotated Python default gets), then falls through to the normal
execute. Two concurrent first `Execute`s for the same language therefore
produce one kernel; the second waits on the mutex. The creation counts
toward the 8-context cap (`RESOURCE_EXHAUSTED` if full) and is logged as
`context created` with `language` and `lazy: true`. The first `Execute`
with a new language pays the kernel start (bash ≈ 0.5 s, node ≈ 1 s,
bounded by the sidecar's 120 s kernel-ready timeout); the SDK's stream
deadline (`timeout + 15 s`) already covers it.

`DestroyContext` on `default-bash` / `default-javascript` is allowed (they
are ordinary contexts; only `default` is `FAILED_PRECONDITION`); the next
`Execute{language}` re-creates them. `RestartContext` on them behaves like
any other context. An explicit `Execute{context_id: "default-bash"}` before
the lazy creation is `NOT_FOUND` (only the `language` path creates). `/run`
rotation stays limited to `default`: lazy contexts cannot exist before
`/run` because nothing but `/validate` (a Python cell on `default`) runs
before it. `/ready`, `Health.kernel_ready` and `/validate` are unchanged.

Per-execution `envs` on a non-Python context: `CodeError::EnvsPythonOnly`
→ `INVALID_ARGUMENT` `envs per execution are only supported on python
contexts; pass envs to create_code_context instead`, checked in `rayd`
from the registry's language before the sidecar is touched. Context-level
`envs` keep working for every language (they are kernel process
environment, D4).

`ListContexts` reports `language` from the registry (`bash`,
`javascript`, `python`), default first, then creation order (lazy defaults
take their creation slot like any other).

Host tests (`crates/rayd/tests/m4_code.rs` + a new `m7_poly.rs` against
the fake sidecar): the routing table above, the lazy creation happening
once under concurrency, `INVALID_ARGUMENT` for `context_id` + `language`,
`UNIMPLEMENTED` when the fake's `ready` lists only `python`,
`INVALID_ARGUMENT` for `envs` on a bash context, `ListContexts` showing
`default-bash` with `language == "bash"` after a lazy creation, and the
request line sent to the sidecar carrying `"language":"bash"`.

### D3. Sidecar protocol: additive v1 fields

Protocol version stays `1` (both codecs ignore unknown fields, so a mixed
pair keeps working; a new `rayd` with an old sidecar simply sees
`languages` absent and treats it as `{python}`):

| Message | Addition | Field order (both codecs) |
|---|---|---|
| request `create_context` | `language` (string; absent = `python`) | `context_id, language, cwd, envs` |
| event `ready` | `languages` (list of strings, canonical names, sorted, always includes `python`) | `v, default_context_id, kernel_pid, warmup_ms, languages` |
| reply of `reseed` | `skipped` (list of context ids: non-Python contexts, never reseeded) | keys sorted inside `payload` as today |

`REQUEST_FIELDS`/`EVENT_FIELDS` in `protocol.py`, `SidecarOp::CreateContext`
(`language: String`, serialised always) and `SidecarEvent::Ready`
(`#[serde(default)] languages: Vec<String>`) and `ReseedPayload`
(`#[serde(default)] skipped: Vec<String>`) in `protocol.rs` change together;
`protocol_v1.jsonl` line 2 becomes
`{"id":2,"op":"create_context","context_id":"ctx-0123456789ab","language":"python","cwd":"/tmp","envs":{"A":"1","B":"dos"}}`
and the `ready` line gains `"languages":["python"]`; one new golden line per
new shape (`create_context` with `bash`, `ready` with three languages, a
`reseed` reply with `skipped`). `rayd` logs `skipped` as a fourth count.
`Request` TypedDict gains `language: str`; `decode_request` validates it as
a string like `cwd`.

### D4. Sidecar: kernel catalog, availability, no warm-up for non-Python kernels

New module `rayito_kernel_sidecar/languages.py`:

```python
@dataclass(frozen=True)
class KernelLanguage:
    name: str            # "python" | "bash" | "javascript"
    kernel_name: str     # "rayito" | "rayito-bash" | "rayito-javascript"
    probe_cell: str      # "pass" | ":" | "void 0"

LANGUAGES: Final = {...}   # keyed by name, insertion order python, bash, javascript
```

Templates (`kernel-sidecar/jupyter/kernels/<kernel_name>/kernel.json`):

| Kernel | `argv` | `language` | `interrupt_mode` |
|---|---|---|---|
| `rayito` (exists) | `python3 -m ipykernel_launcher -f {connection_file} --config=/opt/rayito/sidecar/ipython/ipython_kernel_config.py` | `python` | `signal` |
| `rayito-bash` | `python3 -m bash_kernel -f {connection_file}` | `bash` | `signal` |
| `rayito-javascript` | `/usr/bin/node-20 /opt/rayito/node/lib/node_modules/ijavascript/lib/kernel.js --protocol=5.1 {connection_file}` | `javascript` | `signal` |

`install_kernelspec(paths)` becomes `install_kernelspecs(paths) ->
dict[str, Path]` (language → written `kernel.json`), applying the existing
rewrites (`/opt/rayito/sidecar` → `--sidecar-root`; `python3` →
`sys.executable`) and writing only the specs that are **available**:
`argv[0]` resolves (`shutil.which` or an existing absolute path), every
absolute path in `argv` exists, and for `python3 -m <module>` specs
`importlib.util.find_spec(module)` is not `None`. `python` is always
available (it is this interpreter). The available names are what `ready`
announces (D3). `KernelPaths.kernelspec_dir` becomes
`kernelspec_dir_for(kernel_name)`; the `KernelSpecManager` keeps
`kernel_dirs=[<socket_root>/kernelspec/kernels]`.

`ContextBase.__init__` gains `language: str` (stored, returned by the
`language` property; `context_summary` unchanged); `ContextFactory` gains
the language as its second positional argument; `SidecarServer._op_create_context`
reads `request.get("language") or "python"`, rejects an unknown name with
`invalid_argument` `unknown language` and an unavailable one with
`invalid_argument` `language not installed` (rayd never sends those thanks
to D2; the sidecar still refuses). `KernelContext._start_kernel` uses the
language's `kernel_name` and `probe_cell` instead of `KERNEL_NAME` and
`"pass"`. The environment from `kernel_environment` is passed unchanged to
every kernel (the Python-specific variables are harmless to bash and node;
the context `envs` are what users care about).

Non-Python kernels: no `ipython_kernel_config.py`, no startup scripts, no
warm-up, no `e2b/chart`/`e2b/data` formatters; whatever the kernel emits
through `display_data`/`execute_result`/`stream`/`error` is mapped by
`executions.py` exactly as today (the mapping is message-based, not
language-based). `reseed` skips contexts whose language is not `python`
(D3 `skipped`; their RNG state after a resume is whatever the kernel keeps,
documented in `kernels.md`). `resume` probes every context with
`kernel_info_request` as today. Timeouts follow the generic rule
(`interrupt_kernel` = SIGINT via `interrupt_mode: signal`; not idle in 5 s
→ restart + `ExecutionTimeout`); `bash_kernel` forwards SIGINT to its bash
child, `ijavascript` may end in the restart path (state lost) — accepted
and documented.

Sidecar unit tests (host, no kernels): the availability rule with fake
templates in `tmp_path` (`node-20` missing → `javascript` absent; module
missing → `bash` absent; python always present); `ready.languages`
content; `create_context` with unknown/unavailable language; `reseed`
`skipped` for a fake bash context; golden lines. `test_kernel.py` (real
kernel, Linux/WSL2 only) gains a bash case that runs when `bash_kernel` is
importable in the test venv and skips otherwise.

### D5. Image: `poly` variant through a marker, Node 20 + ijavascript + bash_kernel

Same mechanism as `slim`: no second Dockerfile, no build args (the API has
none). `scripts/image_zip.py --variant poly` adds the synthetic entry
`kernel-sidecar/kernels_variant` with content `poly\n` (mode 0644, fixed
1980-01-01 date); `full` adds nothing; `slim` keeps its own marker;
`marker_variant()` returns `poly` when that entry holds `poly`, `slim` for
the warm-up marker, `full` otherwise, and raises `SystemExit` naming the
archive if both markers are present (never produced by the script).
`VARIANTS = ("full", "slim", "poly")`.

`image/Dockerfile`, after the sidecar `COPY` + pip install + `pip check`
and before the `su user` warm-up, one conditional layer:

```dockerfile
# Variante poly (marcador kernels_variant sólo dentro del zip): Node.js 20
# (paquetes con espacio de nombres de AL2023: node-20/npm-20 existen siempre,
# node/npm son alternatives), ijavascript bajo /opt/rayito/node y bash_kernel
# con sus pines. rayito-base no tiene el marcador y no ejecuta nada de esto.
RUN if [ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]; then \
      dnf install -y --setopt=install_weak_deps=0 nodejs20 nodejs20-npm \
      && dnf clean all \
      && npm-20 install -g --prefix /opt/rayito/node ijavascript@<pin> \
      && chmod -R a+rX /opt/rayito/node \
      && python3 -m pip install --no-cache-dir --break-system-packages \
           -r /opt/rayito/sidecar/requirements-poly.txt \
      && python3 -m pip check \
      && /usr/bin/node-20 --version \
      && test -f /opt/rayito/node/lib/node_modules/ijavascript/lib/kernel.js \
      && su user -c "python3 -c 'import bash_kernel'" \
      && su user -c "/usr/bin/node-20 -e 'require(\"/opt/rayito/node/lib/node_modules/ijavascript/node_modules/jmp\")'"; \
    fi
```

`kernel-sidecar/requirements-poly.txt` pins `bash_kernel==<newest on PyPI
at implementation time>` and its resolved `pexpect`/`ptyprocess`
(recorded in the file, installed only in `poly`; `copy_sidecar.py` copies
it like `requirements.txt`; `pip-audit` in CI covers it as a fourth
requirements file). `ijavascript@<pin>` is the newest release on npm at
implementation time, recorded in the Dockerfile. The snapshot is still
taken after `/ready` with only the Python kernel warm; the node and bash
kernels are files on disk until first use.

**Spike gate (task 0.3).** Before any SDK work, publish `rayito-base-poly`
once from the poly zip. Outcomes:

- Build `SUCCESSFUL` and the `require("jmp")` check passed → ship `bash` +
  `javascript` as designed.
- Build fails on the `npm-20 install` step or the `jmp` require (no prebuilt
  `zeromq` binary for linux-arm64 on al2023, or a compiler needed) → **ship
  `bash` only**: remove the `npm-20`/`node` lines from the conditional layer
  and the `rayito-javascript` template, keep `javascript` in the `Language`
  enum, the SDK validation and the docs (it is then reported as
  `UNIMPLEMENTED` by every image), delete the JavaScript scenarios from this
  change's `code-execution` and `typescript-sdk` deltas (re-run `openspec
  validate`), and record the exact `stateReason`/log lines and the reason
  in `AWS_API_NOTES.md` Q57 and `docs/site/docs/kernels.md`. No attempt to
  add compilers or a source build to the image (it would move the
  `codeInstallSizeInBytes` and the build time for a kernel nobody asked
  for yet).

`scripts/publish_image.py`: `DEFAULT_IMAGE_NAMES["poly"] = "rayito-base-poly"`,
log group `/rayito/rayito-base-poly`, `require_matching_variant` covers
`poly`, everything else (hooks, `--memory-mib`, `cpuConfigurations`,
`baseImageArn`, `--base-image-version`, three-state gate, `/validate`)
identical. `Makefile`: `image-zip-poly` (`image/rayito-image-poly.zip`),
`image-publish-poly`, `clean` removes the poly zip; `.github/workflows/
ci.yml` builds the poly zip after the slim one and asserts
`marker_variant` for the three archives. `scripts/tests/test_image_zip.py`
and `test_publish_image.py` gain the poly cases.

Size gate. `rayito-base` is rebuilt once from the new Dockerfile text
(same layers: the conditional is a no-op without the marker). Its new
`memorySnapshotSizeInBytes` must lie within the Q50 band of the previous
version (`|delta| ≤ 20 MB` against 17.0's 928 100 352 B, i.e. inside the
observed ±13 MB oscillation plus margin) and `codeInstallSizeInBytes`
within ±10 MB net of the `rayd` binary size change (the binary is rebuilt
from the whole workspace, so the S3 persistence crates of the same
milestone move it: 4 700 984 B in 17.0 → 12 524 384 B, +7 823 400 B, at
implementation time); the poly image's three sizes are reported alongside.
The decisive proof that the conditional stayed inert is functional: a
sandbox from the rebuilt `rayito-base` has no `bash_kernel` importable and
answers `UNIMPLEMENTED` (the builder publishes no Dockerfile log, Q57). A
`rayito-base` delta above the band with `bash_kernel` present is a defect
(the conditional executed): stop and fix the marker logic before
acceptance. Measured 2026-09-16: memory +9 998 336 B, code install
+14 376 960 B raw / +6 553 560 B net of the binary, `import bash_kernel`
exits 1 on 18.0.

### D6. Behaviour matrix that stays fixed

| Concern | Python | bash / javascript |
|---|---|---|
| Kernel start | before `/ready`, warm, rotated on `/run` | lazily on first `Execute{language}` or `CreateContext{language}` after `/run` |
| `Health.kernel_ready` | default Python kernel | not reflected |
| Startup scripts / formatters | yes | none |
| Context `envs` | kernel environment | kernel environment |
| Per-execution `envs` | silent set/restore cells | `INVALID_ARGUMENT` |
| Timeout | interrupt, 5 s, restart | same rule |
| `/resume` reseed | `random.seed()` (+numpy) | listed as `skipped` |
| `/resume` probe | `kernel_info_request` | same |
| 8-context cap | counted | counted |
| `DestroyContext` on the default | `FAILED_PRECONDITION` | allowed, re-created lazily |
| Ring / `Reattach` | yes | yes (execution-based, not language-based) |
| Logging | allowlist | allowlist + `language`, `languages`, `lazy`, `skipped` |

### D7. SDK surfaces

Python (`_code_base.py`, pure): `SUPPORTED_LANGUAGES = frozenset({"python",
"bash", "javascript"})`, `LANGUAGE_ALIASES = {"js": "javascript"}`;
`normalize_language(language: str | None) -> str | None` lower-cases,
resolves aliases, returns `None` for `None`/`""`, raises
`InvalidArgumentException` (`language debe ser uno de python, bash,
javascript (o None), recibido ...`) otherwise; `validate_language` (used
by `create_code_context`) returns the canonical name or `"python"`.
`build_execute_request(code, *, context_id, language, envs, timeout)` sets
`request.language` only when `language` is not `None`, and raises
`InvalidArgumentException` (`language y context son excluyentes`) when
both `context_id` and `language` are given. `Sandbox.run_code(code, *,
language=None, context=None, ...)` and `AsyncSandbox.run_code` gain the
keyword in the same position E2B uses (after `code`, before `context`);
`CodeClient.run_code` threads it through; docstrings updated (Spanish).
`CodeContext.language` keeps reporting whatever `ListContexts` returned.
Unit tests (`test_code_base.py`, `test_code_sync.py`, `test_code_async.py`)
cover normalisation, the exclusivity rule, the request field presence and
parity; `tests/unit/fake_code.py` honours `language` (routes to a
per-language fake context and echoes it in `ListContexts`).

`rayito.e2b` (`_compat.py`): `language_or_unimplemented` becomes
`normalized_language_or_unimplemented(language, feature)`: `None`/`python`
/`bash`/`javascript`/`js` (case-insensitive) are forwarded normalised;
anything else (`r`, `java`, `ts`, …) raises `UnimplementedError(feature,
"kernels disponibles: python, bash, javascript (variante rayito-base-poly)")`
before any call. `_sync.py`/`_async.py` pass `language` to the core.

TypeScript (`sandbox/code.ts`): `SUPPORTED_LANGUAGES`, `LANGUAGE_ALIASES`,
`normalizeLanguage`, `RunCodeOptions.language?: string`,
`buildExecuteRequest` sets `language` only when defined and throws
`InvalidArgumentError` for `context` + `language`; `Sandbox.runCode` passes
it; `createCodeContext({ language })` accepts the three names + alias.
Unit tests in `tests/unit/code.test.ts` against the fake `rayd`
(`tests/unit/fake` gains per-language default contexts and the
`INVALID_ARGUMENT`/`UNIMPLEMENTED` answers).

No `agent_version` gate: an older `rayd` ignores the field (proto3), so
`kernels.md` states the minimum image (`rayito-base-poly` built from this
change) and that older agents run the cell in Python. The compatibility
table lives in `docs/site/docs/limits.md` when a release is cut
(`docs/RELEASING.md`), not here.

### D8. Acceptance on real AWS

`clients/python/tests/e2e/test_m7_poly_kernels.py` (marker `e2e`) uses a
`poly_sandbox` fixture modelled on `caps_sandbox`: skipped with a clear
reason unless `RAYITO_TEMPLATE_POLY` is set (ARN or name, resolved through
`control_plane.resolve_template_arn`); `timeout=900`, `idle=None`,
terminated in teardown; boot `kernel_ready_s` reported. Tests:

1. `test_bash_cell`: `sbx.run_code("echo hi", language="bash")` → `"hi"`
   in `"".join(execution.logs.stdout)`, `error is None`; then
   `list_code_contexts()` contains a context with `id == "default-bash"`
   and `language == "bash"`; `run_code("echo $M7_CTX", language="bash")`
   after `create_code_context(language="bash", envs={"M7_CTX": "1"})` on
   that context prints `1`; `run_code("x", language="bash", envs={"A":
   "1"})` raises `InvalidArgumentException`.
2. `test_javascript_cell` (deleted if D5 falls back): `run_code("1 + 1",
   language="javascript").text == "2"`; `run_code("console.log('hi')",
   language="js")` puts `hi` in stdout; `run_code("throw new
   Error('boom')", language="javascript").error` is not `None`.
3. `test_bash_timeout`: `run_code("sleep 30", language="bash",
   timeout=2).error.name == "ExecutionTimeout"` and a following `run_code(
   "echo back", language="bash")` prints `back` (context alive or restarted,
   either is fine).
4. `test_python_unchanged_on_poly`: `run_code("x = 42"); run_code("x").text
   == "42"` and `get_health().kernel_ready` is true right after create.
5. `test_language_unimplemented_on_base` (uses the ordinary `sandbox`
   fixture on `RAYITO_TEMPLATE`): `run_code("echo hi", language="bash")`
   raises `InvalidArgumentException` with `grpc_code ==
   grpc.StatusCode.UNIMPLEMENTED` (the SDK's existing mapping of
   `UNIMPLEMENTED`, `_transport.py`) and `rayito-base-poly` in the message;
   TypeScript maps it to `InvalidArgumentError` the same way.
6. `test_snapshot_sizes` (marker `e2e`, no VM): reads the sizes printed by
   the two publishes from `RAYITO_BASE_SIZES` / `RAYITO_POLY_SIZES`
   (`memory,code,disk` bytes, exported by the implementer from the
   `publish_image.py` output) and asserts the D5 band for `rayito-base`
   against the recorded 17.0 baseline; skipped when unset.

`clients/typescript/tests/e2e/poly.e2e.test.ts`: skipped unless
`RAYITO_E2E=1` and `RAYITO_TEMPLATE_POLY`; one sandbox; `runCode("echo
hi", { language: "bash" })` → `hi` in `logs.stdout.join("")`; the JS cell
`1 + 1` → `text === "2"` (deleted on fallback); `runCode("x", { context:
"default", language: "bash" })` rejects with `InvalidArgumentError`;
`afterAll` kills the sandbox. `pnpm test:e2e` picks it up through the
existing vitest e2e config.

Cost: ≤ 4 MicroVMs per Python run, 1 per TS run, all under the
`maximumDurationInSeconds=900` guard and the session sweeper.

### D9. Documentation

- `docs/site/docs/kernels.md` (new, Spanish; nav entry "Kernels" after
  "Referencia de API"): the three languages, which image ships them,
  lazy start and its first-cell latency, `run_code(language=)` examples in
  Python and TypeScript, what is Python-only (per-execution `envs`,
  charts/data formatters, warm-up, reseed after resume), timeouts on bash
  and JS, `default-bash`/`default-javascript` ids and `create_code_context(
  language=)`, `UNIMPLEMENTED` on `rayito-base`, how to publish the poly
  image (`make image-publish-poly` / the Windows commands), and the
  measured sizes from Q57. If D5 fell back, a "JavaScript" section states
  it is not shipped and why.
- `docs/site/docs/e2b-compat.md`: the `run_code(language=...)` row becomes
  "con algo distinto de `python`, `bash`, `javascript`" → "kernels R y
  Java no incluidos (§4)". `clients/python/README.md` and
  `clients/typescript/README.md`: the "kernels no Python" phrase becomes
  "kernels R/Java". Root `README.md` "Cómo funciona": one sentence on
  `bash`/`javascript` in the poly variant.
- `SPEC.md` §4: the bullet "Kernels que no sean Python." becomes "Kernels
  que no sean Python en `rayito-base`. `bash` y `javascript` llegan en M7
  (`m7-poly-kernels`) como variante `rayito-base-poly` con arranque
  perezoso; R y Java siguen fuera."
- `MILESTONES.md` M7 row 7: status and evidence when accepted.
- `AWS_API_NOTES.md` measured table: Q57 "Tamaños de snapshot de
  `rayito-base-poly` y de `rayito-base` reconstruida con el Dockerfile
  condicional; ¿instala `ijavascript` en al2023 ARM64 sin compilador?" with
  the three sizes of each image, build times, `kernel_ready_s`, the
  first-bash-cell and first-JS-cell latencies, and the spike outcome.
- `clients/python/CHANGELOG.md`, `clients/typescript/CHANGELOG.md`,
  `crates/rayd/CHANGELOG.md` `[Unreleased]` → `### Added` entries.

### D10. Gates (all on the box unless noted)

Rust: `cargo fmt --check`, `cargo clippy --workspace --all-targets --
-D warnings`, `cargo test --workspace`, `cargo zigbuild --release --target
aarch64-unknown-linux-musl -p rayd`. Proto: `buf lint`, `buf breaking
--against` the archived copy. Python client: `uv run pytest tests/unit`,
`ruff check .`, `ruff format --check .`, `mypy src tests`. Sidecar: `uvx
ruff check .`, `uvx ruff format --check .`, `uv run mypy src`, `uv run
pytest tests` (kernel tests on WSL2). Scripts: `uv run pytest
../../scripts/tests`, `uvx ruff check scripts`. TypeScript: `pnpm lint`,
`pnpm typecheck`, `pnpm test`, `pnpm build`. Docs: `mkdocs build --strict`.
AWS: `rayito-base-poly` published and launchable, `rayito-base` rebuilt
within the size band, the Python and TypeScript poly e2e green, zero live
MicroVMs afterwards.

## Risks / Trade-offs

- **`ijavascript` native build on al2023 ARM64.** Mitigated by the D5 spike
  gate with an explicit bash-only fallback and a recorded reason; the
  variant is still useful with bash alone. Outcome 2026-09-16: the fallback
  applied (`zeromq@5.3.1` ships no linux-arm64 / Node 20 prebuild and
  `node-gyp rebuild` needs `make`, Q57); the `javascript` name stays
  reserved and every image answers `UNIMPLEMENTED` for it.
- **Lazy first-cell latency.** The first `run_code(language="javascript")`
  pays ≈ 1 s (node) inside the user's `timeout`; documented, measured in
  Q57. The alternative (warm three kernels before `/ready`) would move the
  snapshot size and `kernel_ready` for every user of the variant.
- **Silent Python execution against an old agent.** A pre-change `rayd`
  ignores `language`; documented in `kernels.md`; the honest fix (a
  version gate) is deferred with the release compatibility table.
- **Per-execution `envs` refused on non-Python contexts.** Honest rather
  than approximate; context `envs` cover the common case.
- **Dockerfile text change forces a `rayito-base` rebuild.** One build
  (≈ 200 s, $0.037 minimum storage); the size gate turns it into evidence
  that the conditional is inert.
- **`bash_kernel` error shape.** A non-zero exit becomes an `error` event
  with whatever `bash_kernel` sends (empty name is possible); the spec only
  requires an `error`/`ExecutionTimeout` where the kernel semantics are
  ours (timeouts) and documents the rest.

## Migration Plan

Additive proto field and additive sidecar fields: old clients keep working
unchanged; a new SDK against an old image gets Python behaviour for
`language` (documented). No data migration. Rollback = do not publish the
poly image; `rayito-base` rebuilt from the new Dockerfile is
content-identical to 17.0.

## Open Questions

None. The only runtime unknown (whether `ijavascript` installs on al2023
ARM64) is closed procedurally by the D5 spike gate with both outcomes
specified.
