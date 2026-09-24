## MODIFIED Requirements

### Requirement: E2B features without an AWS primitive raise UnimplementedError
`rayito.e2b.UnimplementedError` SHALL subclass `NotImplementedError` (and NOT `SandboxException`), carry `feature` and `reason`, and have a message naming both; it SHALL be the native `rayito.exceptions.UnimplementedError` re-exported (the shim passes its compatibility-doc reference), so an error raised by the native SDK is caught by the E2B name. The shim SHALL raise it, before any AWS or agent call, for: `set_timeout` (instance and class variant), `get_metrics(start=..., end=...)`, the class variant `Sandbox.get_metrics(sandbox_id)`, `connection_config`, `run_code(language=...)` and `create_code_context(language=...)` with a language other than `python`, `bash`, `javascript`, `js`, `typescript` or `ts` (case-insensitive) or `None` (the reason SHALL name the available kernels and the `rayito-base-poly` variant), `Sandbox.list(next_token=...)`, `Sandbox.list(query=SandboxQuery(metadata=...))` combined with a `state` filter other than `[SandboxState.RUNNING]`, and `Sandbox.beta_create(...)` with a non-`None` `auto_pause`, `network` or `mcp`. `upload_url` and `download_url` are mapped (requirement "The shim maps upload_url, download_url and the E2B 2.x file kwargs") and SHALL NOT be in this list. `run_code(language=)` and `create_code_context(language=)` with an accepted name SHALL forward the normalised canonical name to the core SDK (`js` → `javascript`, `ts` → `typescript`), which decides at the agent whether the image ships it; when the agent answers `UNIMPLEMENTED` because the image does not ship that kernel, the shim SHALL raise `UnimplementedError` (feature `run_code(language=<given>)` or `create_code_context(language=<given>)`, reason naming `rayito-base-poly`) chained from the core's `InvalidArgumentException`, and SHALL re-raise every other core error unchanged. No E2B feature SHALL be approximated silently: anything not mapped and not raising SHALL fail with `TypeError` at the call site.

#### Scenario: set_timeout
- **WHEN** the unit test calls `sbx.set_timeout(60)` and `Sandbox.set_timeout(sbx.sandbox_id, 60)`
- **THEN** both raise `UnimplementedError`, `isinstance(err, NotImplementedError)` is `True`, `isinstance(err, SandboxException)` is `False`, `err.feature == "set_timeout"` and the message mentions `UpdateMicrovm`

#### Scenario: every listed feature
- **WHEN** the parametrised unit test exercises each feature in the list above
- **THEN** each raises `UnimplementedError` and no request reaches the stubbed control plane or the fake `rayd`

#### Scenario: bash and javascript are forwarded, other kernels are not
- **WHEN** the unit test calls `sbx.run_code("echo 1", language="Bash")`, `sbx.run_code("1", language="js")`, `sbx.run_code("1", language="Python")` and `sbx.run_code("1", language="r")`
- **THEN** the first two reach the fake `rayd` with `language` `"bash"` and `"javascript"`, the third executes on the default context with no `language` on the wire, and the fourth raises `UnimplementedError` with `feature == "run_code(language='r')"` and a reason naming `rayito-base-poly`

#### Scenario: async shim parity for languages
- **WHEN** `AsyncSandbox.run_code("echo 1", language="bash")` and `AsyncSandbox.create_code_context(language="javascript")` run against the fake
- **THEN** both requests carry the canonical language and `create_code_context` returns a `CodeContext` whose `language` is `javascript`

#### Scenario: typescript is forwarded and a missing kernel is unimplemented
- **WHEN** the unit test calls `sbx.run_code("1", language="ts")` against a fake `rayd` that ships the Deno kernels, and then `sbx.run_code("1", language="javascript")` and `sbx.create_code_context(language="typescript")` against a fake that answers `UNIMPLEMENTED` naming `rayito-base-poly`
- **THEN** the first reaches the fake with `language == "typescript"`, and the other two raise `UnimplementedError` (not `InvalidArgumentException`) whose reason names `rayito-base-poly` and whose `__cause__` is the core exception, in the sync and the async shim

#### Scenario: the shim runs JavaScript and TypeScript on the poly image
- **WHEN** the e2e connects `rayito.e2b.Sandbox.connect(id, access_token=...)` to a `rayito-base-poly` sandbox and runs `run_code("1 + 1", language="js")` and `run_code("const n: number = 3; n", language="ts")`, and the async shim runs one `ts` cell
- **THEN** the texts are `2` and `3`, and on a `rayito-base` sandbox `run_code("1", language="ts")` raises `UnimplementedError` naming `rayito-base-poly`

#### Scenario: native error caught by the E2B name
- **WHEN** the native SDK raises `rayito.exceptions.UnimplementedError("upload_url", "configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET")` inside a shim call
- **THEN** `except rayito.e2b.UnimplementedError` catches it

## ADDED Requirements

### Requirement: The shim maps upload_url, download_url and the E2B 2.x file kwargs
`rayito.e2b.Sandbox.upload_url(path=None, user=None, use_signature=False, use_signature_expiration=None)` and `download_url(path, user=None, use_signature=False, use_signature_expiration=None)` SHALL delegate to the native `files.upload_url` / `files.download_url` with `expires_in = use_signature_expiration or 3600` and return the native `UploadTicket` / `DownloadLink` (both `str`, so `requests.put(url, data=f)` and `urlopen(url)` work unchanged). `path=None` SHALL raise `InvalidArgumentException` (an S3 URL carries no file name), `use_signature_expiration <= 0` SHALL raise `InvalidArgumentException`, and `use_signature` SHALL be accepted and ignored because Rayito URLs are always signed. `rayito.e2b.AsyncSandbox` SHALL offer both as coroutines. The shim's `files.write(...)` (both overloads) SHALL accept `gzip`, `metadata` and `use_octet_stream`, and `files.read(...)` SHALL accept `gzip` and `stream_idle_timeout`, passing them to the native calls. `docs/site/docs/e2b-compat.md` SHALL list both methods under "Se mapea, con una nota" with the divergences of design D20 (raw-body PUT, asynchronous landing covered by the barrier and `wait()`, single-use, snapshot download, expiry always set, transfer bucket required, missing file raises at call time, async coroutines).

#### Scenario: E2B upload pattern with only the import changed
- **WHEN** the unit test runs `url = sbx.upload_url("/home/user/in.bin")`, PUTs 1 KiB to `url` on the fake S3 and then calls `sbx.files.read("/home/user/in.bin", format="bytes")`
- **THEN** the read returns the 1 KiB (the fake `rayd` applied the barrier) and `url` is the native `UploadTicket`

#### Scenario: invalid expiration
- **WHEN** the unit test calls `sbx.upload_url("/home/user/x", use_signature_expiration=-1)` and `sbx.download_url("/home/user/x", use_signature_expiration=0)`
- **THEN** both raise `InvalidArgumentException` and no RPC reached the fake

#### Scenario: E2B 2.x write kwargs
- **WHEN** the unit test calls `sbx.files.write("/home/user/a.txt", "x", gzip=True, metadata={"k": "v"}, use_octet_stream=True)`
- **THEN** the fake `rayd` received a gzip-compressed `Write` whose first message carries `metadata == {"k": "v"}`
