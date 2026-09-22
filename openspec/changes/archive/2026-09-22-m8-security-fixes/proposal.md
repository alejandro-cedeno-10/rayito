## Why

`docs/SECURITY_AUDIT.md` (internal audit of 2026-09-22: six surfaces, 38
proposed findings, 76 refutations) closes with a triage table in §8 whose
"arreglar ahora" rows mean **before the repository is published**, because
they are either what other people will copy or a sentence the published
documentation already claims and the code does not honour.

This change is the **code, IAM and CI half** of that list. Its sibling
`m8-security-docs` carries the documentation-truth half (C-01, C-02, C-03,
C-04, C-07, the C-08/C-09 prose and H-06). Nothing marked **M8** or
**Aceptar** in §8 is implemented here: those rows are deliberately out of
scope and the audit says why (`openspec/project.md` hard rule 3).

The nine rows in this change:

| Id | What the audit asks for now |
|---|---|
| H-01 | Persistence and artifact prefixes disjoint by construction, an explicit `Deny` on the execution role over the artifact prefix, and the `infra/README.md` recipe that today points both stack parameters at one bucket |
| H-02 | Every `uvx` invocation pinned with `==`, plus a CI gate that fails on an unpinned one (the release job holds a PyPI OIDC token while it runs an unpinned `twine`) |
| C-05 | `UserPolicy::authorize_identity` turned from a uid-0 blacklist into a positive check, and the duplicated gate in `persistence/mod.rs` deleted |
| C-13 | `*` (and the quote/paren/bang characters) dropped from the `AllowedPattern` of `PersistencePrefix` — template only |
| H-03 | The Python pool file backend writes its temporary through an exclusive, no-follow descriptor whose mode is set on the descriptor |
| H-04 | The TypeScript twin of H-03 — the same defect, the same fix, landed together |
| H-05 | `rayito-mcp --http` always passes an explicit `TransportSecuritySettings` instead of relying on a host-string heuristic inside a third-party SDK |
| C-11 | The action-pinning gate inverted, so it fails unless every non-local `uses:` is a 40-hex commit SHA — what `SECURITY.md:157` already claims |
| C-08 | One-time `logger.warning` when the Python SDK falls back to `RAYITO_ACCESS_TOKEN` for a `create()` (the prose is the sibling's) |

No AWS call is needed to verify any of it: every row is checkable with the
local gates. No image is published and no e2e runs against the account.

## What Changes

Every decision is closed in `design.md`; the summary:

- **IAM template (`spike/m0/iam.yaml`).** The `PersistencePrefix` default
  becomes `rayito-home`, so a default deployment can no longer overlap the
  `rayito/` artifact namespace; the `persistence` inline policy of
  `ExecutionRole` gains a `Deny` over `arn:aws:s3:::${ArtifactBucket}/rayito/*`
  that survives any prefix the operator chooses; the `AllowedPattern` of
  `PersistencePrefix` loses `*`, `'`, `(`, `)` and `!`. `infra/README.md`
  deploys with two distinct values and says why overlapping namespaces turn
  sandbox code into image code.
- **Positive identity check (`crates/rayd-core`).** `authorize_identity`
  accepts an identity only when `uid >= 1000`, `gid >= 1000` and no
  supplementary group is 0, refusing everything else with a new domain error
  mapped to `PERMISSION_DENIED` in process, PTY, filesystem and persistence.
  The `RAYITO_ALLOW_ROOT=1` image opt-in still bypasses the gate for the
  process paths, but persistence drops it (`resolve_home_identity` resolves
  with `UserPolicy::without_root()`), so it refuses root and every privileged
  account whatever the image says — T15's `/root` is never archived. The
  duplicated `uid == 0` gate of `persistence/mod.rs` is deleted: with the
  opt-in off the single gate is strictly stronger than that check. Domain-only change; no adapter type enters
  `rayd-core`.
- **Pool file backends (both SDKs).** The state file is written through a
  descriptor created exclusively (`O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` /
  `open(..., "wx")`) under an unpredictable temporary name, its mode set on
  the descriptor before the first byte, then renamed over the target; reads
  refuse to follow a symlink where the platform offers `O_NOFOLLOW`. That is
  what makes the "0600, written atomically" sentence of `SECURITY.md` T14
  and `docs/site/docs/pool.md` true.
- **MCP HTTP transport.** `run()` always builds
  `TransportSecuritySettings(enable_dns_rebinding_protection=True, ...)` from
  the host and port actually bound, so DNS-rebinding protection no longer
  depends on the host string matching a three-element tuple inside `mcp`;
  `docs/site/docs/mcp.md` stops claiming that listening on loopback keeps
  browsers out.
- **Pin gates (`scripts/check_pins.py`).** One script with two checks —
  every non-local `uses:` is `@[0-9a-f]{40}` with an optional comment, and
  every `uvx` names a version with `==` — wired into the `check` job of
  `ci.yml` and into `make lint`. The unpinned invocations themselves are
  pinned: `twine==7.0.0`, `ruff==0.16.7` (the version both `uv.lock` files
  already resolve) and `cfn-lint==1.56.3` (the version the M6/M7 acceptance
  recorded).
- **Shared-token warning.** `resolve_access_token` logs one warning per
  process when `create()` falls back to `RAYITO_ACCESS_TOKEN`, so turning a
  per-sandbox secret into a fleet secret is never silent.

## Capabilities

### Modified Capabilities

- `ci-hardening`: the pinning gate becomes an allowlist of 40-hex SHAs
  instead of a denylist of three spellings, and gains a companion
  requirement for `uvx` version pins.
- `filesystem-persistence`: the `spike/m0/iam.yaml` requirement gains the
  disjoint default, the explicit `Deny` and the narrowed `AllowedPattern`.
- `process-lifecycle`: the identity requirement becomes a positive check
  with a uid/gid floor and a group rule; the cross-process token requirement
  gains the one-time warning of the environment fallback.
- `sandbox-pool`: the file-backend requirement states how the temporary is
  created and that the mode is set on the descriptor, for both SDKs.
- `typescript-sdk`: the one clause describing `<path>.tmp` plus `mode` is
  replaced by the exclusive-create rule.
- `mcp-server`: `--http` always passes explicit transport-security settings,
  and the documentation page says what loopback does and does not buy.

### New Capabilities

None. Every row lands inside a capability that already owns its surface.

## Impact

- Edited (code): `crates/rayd-core/src/process/identity.rs`,
  `crates/rayd-core/src/process/error.rs`,
  `crates/rayd-core/src/filesystem/error.rs`,
  `crates/rayd-core/src/filesystem/identity.rs`,
  `crates/rayd-core/src/persistence/mod.rs`,
  `crates/rayd-core/src/persistence/error.rs`,
  `crates/rayd/src/grpc/process.rs`, `crates/rayd/src/grpc/pty.rs`,
  `crates/rayd/src/grpc/filesystem.rs`,
  `clients/python/src/rayito/_pool_backends.py`,
  `clients/python/src/rayito/_sandbox_base.py`,
  `clients/python/src/rayito/mcp/_cli.py`,
  `clients/typescript/src/pool/backend.ts`.
- Edited (infra, CI and the two doc sentences these rows own):
  `spike/m0/iam.yaml`, `infra/README.md` (the persistence recipe only),
  `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `Makefile`,
  `docs/RELEASING.md`, `CONTRIBUTING.md`, `docs/site/docs/mcp.md`.
- New: `scripts/check_pins.py` and `scripts/tests/test_check_pins.py`.
- Tests: new Rust unit tests for the identity floor and the persistence
  gate, new Python unit tests for the pool descriptor, the MCP settings and
  the token warning, new TypeScript unit tests for the pool descriptor, and
  the `scripts/` suite for both pin gates. The existing counts (382 Rust,
  1066 Python, 386 TypeScript) only grow.
- Behaviour: a request that asked for a system account (`user="operator"`,
  `user="bin"`) now gets `PERMISSION_DENIED` instead of a privileged
  process; `rayito-mcp --http` answers only requests whose `Host` matches
  the bound authority; everything else is unchanged at runtime.
- Cost: none. No AWS call, no image build and no e2e in this change.

## Out of scope (named so nobody wonders)

- **The M8 rows** of §8: C-07's payload binding, C-01's peer-uid
  authentication, C-02's phase guard, C-03's two `audit()` calls, C-08's MCP
  opt-out, C-09's runtime/publisher split, C-10's build/publish split,
  C-12's `--require-hashes`, H-01's `_publish.py` digest check and bucket
  versioning, C-05's two `uidrange` rules and its e2e.
- **C-06** is accepted with a written reason; only the sibling touches its
  sentence.
- **The C-09 removal of `lambda:DeleteMicrovmImage`** (`spike/m0/iam.yaml:148`)
  is an IAM edit that the task assignment placed with neither change. It is
  not made here; `design.md` D12 records it so it is not lost.
- **`SECURITY.md` and `ARCHITECTURE.md`** are not touched by this change at
  all, so the sibling owns them without a conflict.
