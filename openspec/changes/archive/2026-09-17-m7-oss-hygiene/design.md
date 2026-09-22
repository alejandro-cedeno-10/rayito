## Context

State on 2026-09-16: M0–M6 accepted on real AWS and archived; `rayito`
0.1.0 (Python), `rayito` 0.0.5 (npm, unpublished), `rayd` 0.1.0
(`publish = false`), image `rayito-base` 10.0. No git repository exists
yet on the development box; files are written, not committed. The M7
research report (`docs/research/2026-09-m7-oss-readiness.md`) is the input
of this change; its §0 and §1 are the brief, §2 feeds the languages table,
§5 fixes the split between this change and `m7-supply-chain`.

Facts verified for this design:

- Five licence declarations, all MIT: `Cargo.toml` `[workspace.package]
  license = "MIT"`; `clients/python/pyproject.toml` `license = "MIT"` plus
  classifier `License :: OSI Approved :: MIT License`;
  `kernel-sidecar/pyproject.toml` `license = "MIT"`;
  `clients/typescript/package.json` `"license": "MIT"`;
  `clients/typescript/LICENSE` is the MIT text ("Copyright (c) 2026 Rayito
  contributors"). No root `LICENSE`, `NOTICE`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, `CHANGELOG.md`; `.github/` holds
  only `workflows/{ci,release}.yml`.
- `openspec/specs/python-release/spec.md` currently **requires** the MIT
  classifier ("Package metadata for the first public release") and
  `openspec/specs/typescript-sdk/spec.md` requires the tarball to list only
  `dist/**`, `README.md`, `package.json` and `LICENSE`; both requirements
  are modified by this change's deltas.
- `uv_build` (pinned `>=0.7.19,<0.9`; `uv` 0.7.21 on the box) with
  `license = "Apache-2.0"` and `license-files = ["LICENSE", "NOTICE"]`
  builds a wheel whose `METADATA` reads `Metadata-Version: 2.4`,
  `License-Expression: Apache-2.0`, `License-File: LICENSE`,
  `License-File: NOTICE`, and whose archive contains
  `<name>-<version>.dist-info/licenses/LICENSE` and `.../licenses/NOTICE`
  (probe project built in the session scratchpad on 2026-09-16).
- `pnpm pack` always includes `package.json`, `README.md` and `LICENSE`
  regardless of `files`; `NOTICE` is **not** auto-included, so it must be
  listed in `files`. `pack-check.mjs` already asserts `package/LICENSE`.
- Apache-2.0 text: `https://www.apache.org/licenses/LICENSE-2.0.txt`,
  202 lines, sha256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
- Contributor Covenant 3.0 markdown:
  `https://www.contributor-covenant.org/version/3/0/code_of_conduct/code_of_conduct.md`,
  93 lines, sha256
  `ed3f8089262ea04de59d089a267bfa0822b93c96ac5a65d0647fd767a25c8422`,
  CC BY-SA 4.0, with two bracketed `[NOTE: …]` placeholders (reporting
  means; remedies) that the adopter fills in.
- npm trusted publishing (docs.npmjs.com/trusted-publishers): npm CLI ≥
  11.5.1 and Node ≥ 22.14.0, `id-token: write`, provenance automatic; the
  trusted publisher is configured from the package's settings page
  (organization/user, repository, workflow filename, optional environment),
  so the package must already exist on the registry. pnpm 9.15.4 (pinned)
  is not an OIDC publisher.
- Dependabot `package-ecosystem` values: `cargo`, `uv`, `pip`, `npm`
  (pnpm lockfiles supported), `github-actions`, `docker`; `groups` with
  `patterns` / `dependency-type` / `update-types`.
- The vendored `e2b_charts` 1.0.0 (MIT, Copyright (c) 2025 FOUNDRYLABS,
  INC.) keeps its `LICENSE` beside the code and is described in
  `kernel-sidecar/src/rayito_kernel_sidecar/_vendor/VENDORED.md`. Nothing
  else is vendored.
- Repository coordinates already used by the manifests:
  `https://github.com/alejandro-cedeno-10/rayito`. Authenticated GitHub account on the
  box: `alejandro-cedeno-10`.

## Goals / Non-Goals

Goals:

- One licence, Apache-2.0, declared identically in the five manifests and
  shipped inside every artifact (wheel, sdist, npm tarball, crate tree).
- Correct third-party attribution (`NOTICE`) for the only vendored code.
- A contributor can clone the repo, read one file, and run every gate on
  Windows or WSL2; a reporter can file a bug, a feature or a vulnerability
  through a template; a reviewer has a checklist.
- The architecture is understandable in five minutes from the README, in
  detail from `ARCHITECTURE.md`, and comparable with E2B / Daytona / Modal
  from the docs site.
- Everything asserted by a gate that already exists or is added here
  (`check_license.py`, `check_wheel.py`, `pack-check.mjs`, mkdocs strict).

Non-goals (report §5 and the brief):

- Registering names or publishing to PyPI, npm or crates.io. The change
  stops at documenting the exact manual steps (`docs/RELEASING.md`).
- The npm release workflow, release-please, `linked-versions`, SHA-pinned
  actions, `permissions:` hardening, `cargo-deny`, SBOMs, cosign, the OIDC
  e2e workflow: `m7-supply-chain`.
- GitHub repository settings (Private Vulnerability Reporting, DCO app,
  branch protection, Discussions): manual, documented.
- Version bumps or a 0.2.0 lockstep; SPDX headers; REUSE; trademark
  checks; translations of the Code of Conduct.
- Any runtime change to `rayd`, the sidecar, the image or the SDKs.

## Decisions

### D1. Licence: Apache-2.0, the five declarations and the copies

Final decision from the owner; the report §0 gives the rationale (patent
grant, `NOTICE` mechanism, what AWS, E2B infra and comparable projects
use). Concretely:

| Place | After this change |
|---|---|
| `Cargo.toml` `[workspace.package]` | `license = "Apache-2.0"`, `repository = "https://github.com/alejandro-cedeno-10/rayito"`, `homepage = "https://github.com/alejandro-cedeno-10/rayito"`; `publish = false` unchanged (the three crates inherit via `license.workspace = true`, already the case for `rayd`) |
| `clients/python/pyproject.toml` | `license = "Apache-2.0"`, `license-files = ["LICENSE", "NOTICE"]`; the line `"License :: OSI Approved :: MIT License",` removed from `classifiers`; nothing else in the classifier list changes |
| `kernel-sidecar/pyproject.toml` | `license = "Apache-2.0"`; no `license-files` (the package is never built or published; `Private :: Do Not Upload` stays) |
| `clients/typescript/package.json` | `"license": "Apache-2.0"`, `"repository": { "type": "git", "url": "git+https://github.com/alejandro-cedeno-10/rayito.git", "directory": "clients/typescript" }`, `"files": ["dist", "README.md", "LICENSE", "NOTICE"]`; no `publishConfig.provenance` (trusted publishing makes it redundant, report §1) |
| `clients/typescript/LICENSE` | replaced by the Apache-2.0 text |

Copies: `LICENSE` at `clients/python/`, `clients/typescript/`,
`crates/rayd/`; `NOTICE` at `clients/python/`, `clients/typescript/`. The
root files are authoritative and the copies must be byte-identical (D3
enforces it). `crates/rayd` gets no `NOTICE`: it is `publish = false` and
the `rayd` binary embeds no third-party non-Rust code; Rust crate licences
are covered by `Cargo.lock` and, in `m7-supply-chain`, by `cargo-deny`.
`crates/rayd-core` and `crates/rayito-proto` get no copy: they are
workspace-internal; when `rayito-proto` is published (deferred, report §2)
its own `LICENSE` is added then.

Header lines in `LICENSE` copies: the verbatim apache.org text, unmodified,
including its appendix ("How to apply the Apache License to your work").
No per-file SPDX headers (report §1: covered by package metadata; REUSE
later if ever).

### D2. `NOTICE` content

Root `NOTICE`, English (it is legal text that travels inside the
artifacts), exactly this shape:

```
Rayito
Copyright 2026 Rayito contributors

This product includes software developed by third parties:

- e2b_charts 1.0.0 (MIT License), Copyright (c) 2025 FOUNDRYLABS, INC.
  Vendored at kernel-sidecar/src/rayito_kernel_sidecar/_vendor/e2b_charts
  with its LICENSE file alongside; see VENDORED.md there for the upstream
  source, the sdist checksum and the single import-path change.
```

The copyright holder line reuses "Rayito contributors", the only
copyright line the owner has written so far (`clients/typescript/LICENSE`).
Substituting a legal entity is a one-line edit of the root file plus
`check_license.py`'s copy check; it is not blocking and is flagged in the
"Open Questions" section. `VENDORED.md` and the vendored MIT `LICENSE`
stay as they are (MIT only requires the notice to be kept; Apache-2.0 §4(d)
is satisfied by listing it in `NOTICE`). The `NOTICE` copies inside the
Python and TypeScript packages are identical to the root one even though
neither package contains `e2b_charts`: one file, one truth, no per-package
variants to drift.

### D3. `scripts/check_license.py`: the "five places" gate

A standard-library Python script (same style as `gen_limits.py` and
`check_wheel.py`, `ruff check scripts` clean, unit-tested in
`scripts/tests/test_check_license.py` with `tmp_path` fixtures) that exits
non-zero naming every problem when:

- `Cargo.toml` `[workspace.package].license` is not `"Apache-2.0"`
  (`tomllib`);
- `clients/python/pyproject.toml` `[project].license` is not
  `"Apache-2.0"`, `license-files` is not `["LICENSE", "NOTICE"]`, or any
  classifier starts with `License ::`;
- `kernel-sidecar/pyproject.toml` `[project].license` is not
  `"Apache-2.0"`;
- `clients/typescript/package.json` `license` is not `"Apache-2.0"` or
  `files` lacks `LICENSE` or `NOTICE`;
- the root `LICENSE` sha256 differs from the apache.org checksum recorded
  in the script, or any copy (`clients/python/LICENSE`,
  `clients/typescript/LICENSE`, `crates/rayd/LICENSE`) is not byte-identical
  to it;
- any `NOTICE` copy (`clients/python/NOTICE`, `clients/typescript/NOTICE`)
  is not byte-identical to the root `NOTICE`, or the root `NOTICE` does not
  mention `e2b_charts`;
- the string `MIT` appears as a licence value anywhere in the five
  manifests (guards against a half-done revert).

Wired into `make lint` and the CI `check` job right after
`python3 scripts/gen_limits.py --check`. It reads only the repo tree; no
network (the checksum is a constant with the fetch date in its docstring).

The gate hashes working-tree bytes, and so do `uv build` and `pnpm pack`,
so the tree must reach every contributor with LF endings. Git for Windows
defaults to `core.autocrlf=true`, which would rewrite `LICENSE` to CRLF on
checkout and turn the gate red (sha256 `3ddf9be5…` instead of
`cfc7749b…`). A root `.gitattributes` with `* text=auto eol=lf` plus
explicit `LICENSE text eol=lf` / `NOTICE text eol=lf` (and `binary` for
`png`, `jpg`, `ico`, `zip`, `gz`, `whl`) overrides that setting; the
byte-exact comparison stays as is (normalising before hashing would hide
the very drift the packages would ship). When the root `LICENSE` fails
the checksum and contains `\r\n`, the message adds "tiene finales CRLF"
and points to `CONTRIBUTING.md` §3, which tells a pre-`.gitattributes`
clone to run `git add --renormalize .` and check out again.

### D4. Wheel and tarball assertions

`scripts/check_wheel.py` gains, next to the existing checks: `METADATA`
contains `License-Expression: Apache-2.0`, `License-File: LICENSE` and
`License-File: NOTICE`; no line starts with `Classifier: License ::`; the
archive contains one entry ending in `.dist-info/licenses/LICENSE` and one
ending in `.dist-info/licenses/NOTICE` (matched by suffix because the
dist-info directory carries the version). `clients/python/tests/unit/
test_packaging.py` gains a test that `pyproject.toml` declares
`license == "Apache-2.0"` and no `License ::` classifier.
`clients/typescript/scripts/pack-check.mjs` adds `package/NOTICE` to
`REQUIRED_ENTRIES` (nothing else: `LICENSE` is already required and its
content is checked by D3, not by the pack). `uvx twine check` stays as is
(verified to accept Metadata 2.4 on the probe).

### D5. `CONTRIBUTING.md` (Spanish, DCO not CLA)

Spanish like every other doc of the repo; the DCO text itself is quoted in
English verbatim from developercertificate.org (it is a legal text).
Sections, in order:

1. **Antes de empezar**: read order `CLAUDE.md` → `SPEC.md` →
   `ARCHITECTURE.md` → `AWS_API_NOTES.md` → `MILESTONES.md`; the four hard
   rules (never invent an AWS parameter, the `.proto` is the source of
   truth, one milestone at a time, no milestone closes on mocks) with a
   one-line reason each; ARM64 only.
2. **Flujo OpenSpec**: every non-trivial change starts as
   `openspec/changes/<name>/` (`proposal.md`, `design.md`, `tasks.md`,
   delta specs), `openspec validate <name> --strict --no-interactive` must
   pass before implementation, implementers tick `tasks.md`, acceptance
   archives with `openspec archive <name> --yes` after the real-AWS e2e.
3. **Toolchain y gates**: Linux/WSL2 as the reference (`make lint`,
   `make test`, `make build`, `make test-e2e`), and the Windows section
   from the brief: `make` absent, run the Makefile steps by hand, the
   `RUSTUP_HOME`/`CARGO_HOME`/`PATH` exports and the zig linker variables
   (generalised to "any directory", no personal paths), `pnpm` only (never npm/yarn), `uv` for
   Python; the exact gate commands for Rust, Python client, kernel
   sidecar, TypeScript and docs, copied from `ci.yml` so they cannot
   drift in wording; note that adapter tests marked `cfg(unix)` and the
   sidecar `-m kernel` tests need WSL2 or Linux.
4. **Convenciones**: identifiers English, Spanish only in user-facing
   strings and docs; comment WHY not WHAT, no inline comments in function
   bodies; no ticket tags; clippy pedantic clean, no `unwrap`/`expect`/
   `panic` outside tests; Python fully typed with sync/async parity;
   TypeScript strict; never log file contents, executed code, PTY bytes,
   tokens or hook bodies.
5. **Commits y PRs**: Conventional Commits (`feat:`, `fix:`, `refactor:`,
   `docs:`, `chore:`, `test:`, `ci:`), branch names
   `feature/`/`bugfix/`/`chore/` + slug, **DCO**: every commit carries
   `Signed-off-by:` (`git commit -s`), what the sign-off means (the quoted
   DCO 1.1), that the DCO GitHub App will be a required check once the
   repo is public (manual step in `docs/RELEASING.md`), no force-push to
   `main`, the PR template checklist, and that e2e runs against AWS cost
   money (≈ $0.03 per run) so the PR says whether one ran.
6. **Cómo reportar**: bugs and features through the templates, security
   through `SECURITY.md`, conduct through `CODE_OF_CONDUCT.md`.

### D6. `CODE_OF_CONDUCT.md`: Contributor Covenant 3.0, verbatim, English

The 3.0 markdown is copied verbatim (CC BY-SA 4.0 attribution block kept)
and only its two bracketed placeholders are replaced: the reporting means
becomes "by email to the address published on the maintainer's GitHub
profile (`https://github.com/alejandro-cedeno-10`), or, when the
report concerns the maintainer, to GitHub Support"; the remedies note is
replaced by one sentence stating the suggested remedies apply as written.
English rather than a Spanish translation because the Organization for
Ethical Source publishes 3.0 in English and a self-made translation would
need its own marking under CC BY-SA; a one-line Spanish preface above the
title points readers to `CONTRIBUTING.md` for project conventions.

### D7. `SECURITY.md`: reporting section at the top, threat model untouched

A new first section, "Reportar una vulnerabilidad", inserted between the
title/blockquote and "## Modelo", containing: (a) report privately through
GitHub Private Vulnerability Reporting
(`https://github.com/alejandro-cedeno-10/rayito/security/advisories/new`); never open a
public issue and never paste a JWE, an access token or a `runHookPayload`;
(b) what a useful report contains (SDK and version, `agent_version` from
`get_health()`, image name and version, region, minimal reproduction);
(c) **Versiones soportadas**: a table stating that only the latest
published release of each component (`rayito` PyPI, `rayito` npm,
`rayito-base` image / `rayd`) receives fixes, with the current values
(0.1.0, 0.0.5 unpublished, image 10.0 / `rayd` 0.1.0); (d) **Plazos**:
acknowledgement within 7 days, fix or mitigation targeted within 90 days
of the report, coordinated disclosure at 90 days or at fix release,
whichever is earlier, credit in the advisory if wanted; (e) **Sin
recompensas**: no bug bounty. Everything from "## Modelo" downward is
unchanged. The enabling of Private Vulnerability Reporting in the
repository settings is a manual owner step listed in `docs/RELEASING.md`.

### D8. `GOVERNANCE.md`

One page, Spanish: the project has a single maintainer
(`@alejandro-cedeno-10`), who merges, cuts releases and owns
`CODEOWNERS`; architecture decisions are made **in writing as ADRs** in
`ARCHITECTURE.md` (an ADR has Contexto / Decisión / Razón / Consecuencia,
numbered, never rewritten, superseded by a new one) and scoped work as
OpenSpec changes; anyone becomes a maintainer by sustained contributions
(three accepted non-trivial changes across at least two of `crates/`,
`clients/python`, `clients/typescript`, `kernel-sidecar`) and the
existing maintainers' agreement, recorded in this file; the file states it
is deliberately minimal and will be revisited at three maintainers (report
§1). No `MAINTAINERS.md`: the list lives here.

### D9. `.github/`: CODEOWNERS, templates, Dependabot

- `CODEOWNERS`: `* @alejandro-cedeno-10` plus the four explicit
  patterns from the report (`/proto/`, `/crates/`, `/clients/python/`,
  `/clients/typescript/`, `/AWS_API_NOTES.md`) with the same owner, so
  the file already has the shape it will have with more maintainers.
- `ISSUE_TEMPLATE/bug_report.yml` (Spanish, `type: form`): dropdown SDK
  (`python`, `typescript`, `rayd`/imagen, `scripts`), input SDK version,
  input `agent_version` (from `get_health()` / `getHealth()`), input image
  name and version, dropdown region, textarea redacted `stateReason`
  (`get_info().state_reason`), textarea reproduction, textarea expected vs
  actual, textarea logs with a reminder not to paste tokens/JWE/payloads,
  checkbox "he leído SECURITY.md y esto no es una vulnerabilidad".
- `ISSUE_TEMPLATE/feature_request.yml`: problem, proposal, E2B equivalent
  if any (and whether it is in `docs/site/docs/e2b-compat.md` as
  `UnimplementedError`), which AWS primitive it needs (with the reminder
  that only `AWS_API_NOTES.md` facts count), willingness to write the
  OpenSpec change.
- `ISSUE_TEMPLATE/config.yml`: `blank_issues_enabled: false`; contact
  links to the security advisory form and to the docs site.
- `PULL_REQUEST_TEMPLATE.md`: title in Conventional Commits; summary;
  checklist: OpenSpec change linked and `tasks.md` ticked; `buf breaking`
  clean or the breaking change justified; gates run locally (which OS);
  e2e against AWS run (cost noted) or "sin cambio de runtime"; no secrets,
  tokens, JWE, payloads or file contents in logs, tests or fixtures; docs
  updated (`ARCHITECTURE.md` / `AWS_API_NOTES.md` when a platform fact is
  new); `CHANGELOG.md` of the touched package updated; every commit
  signed off (DCO).
- `dependabot.yml` (`version: 2`), weekly, one `updates` entry per
  ecosystem: `cargo` at `/`; `uv` at `/clients/python` and at
  `/kernel-sidecar`; `pip` at `/kernel-sidecar` (the `requirements.txt`
  the image installs); `npm` at `/clients/typescript`; `github-actions` at
  `/`; `docker` at `/image`. Each entry has one group named after its
  ecosystem with `patterns: ["*"]` and `update-types: ["minor", "patch"]`,
  so majors open their own PR; `open-pull-requests-limit: 5`;
  `commit-message.prefix: "chore(deps)"`. Cargo dependencies are pinned
  with `=` in `Cargo.toml`, which Dependabot updates in place.

### D10. Changelogs and badges

- `clients/typescript/CHANGELOG.md` and `crates/rayd/CHANGELOG.md`: Keep a
  Changelog 1.1.0, Spanish, same preamble as the Python one; `[Unreleased]`
  first; for TypeScript a `[0.0.5] - 2026-09-16` entry summarising the M6
  surface (the section headings of the Python 0.1.0 entry apply) and noting
  it is unpublished; for `rayd` a `[0.1.0] - 2026-09-16` entry listing the
  services and hooks by milestone and the image compatibility line
  (`agent_version`, `rayito-base` ≥ 10.0). Both `[Unreleased]` sections
  record the licence change to Apache-2.0.
- Root `CHANGELOG.md`: a pointer file listing the three component
  changelogs, the tag prefixes (`python-v`, `typescript-v`, `rayd-v`), and
  that image versions are opaque AWS build numbers recorded in
  `MILESTONES.md`.
- `clients/python/CHANGELOG.md` `[Unreleased]` gains "Changed: licencia
  MIT → Apache-2.0 (PEP 639 `License-Expression`)" and its "Publicación"
  subsection points to `docs/RELEASING.md` as the canonical steps.
- README badges, first line under the title, shields.io, left to right:
  CI (`https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml/badge.svg`
  linking to the workflow), PyPI (`https://img.shields.io/pypi/v/rayito`
  linking to `https://pypi.org/project/rayito/`), npm
  (`https://img.shields.io/npm/v/rayito` linking to
  `https://www.npmjs.com/package/rayito`), licence
  (`https://img.shields.io/badge/license-Apache--2.0-blue` linking to
  `LICENSE`). They render "not found" until the names are published; that
  is the intended placeholder behaviour and is stated in `docs/RELEASING.md`.

### D11. `docs/RELEASING.md`: manual steps, nothing automated here

Spanish. Sections:

1. **Qué se publica y con qué tag**: `clients/python` → PyPI `rayito`,
   tag `python-v<version>`; `clients/typescript` → npm `rayito`, tag
   `typescript-v<version>`; `crates/rayd` → GitHub Release asset (`rayd`
   binary + `rayito-image.zip` from the CI `build` job), tag
   `rayd-v<version>`; the image itself is published to AWS by
   `make image-publish` (not a tag). Version parity rule: each tag must
   equal the manifest version (the `release.yml` check for Python; the
   same rule stated for the others until `m7-supply-chain` automates it).
2. **PyPI**: the three existing manual steps (Trusted Publisher for
   project `rayito`, repo `alejandro-cedeno-10/rayito`, workflow `release.yml`,
   environment `pypi`; create the `pypi` environment; push the tag), plus
   the note that PyPI's "pending publisher" form lets the trusted
   publisher be registered **before** the project exists, so no token
   publish is needed; attestations are automatic with
   `pypa/gh-action-pypi-publish` ≥ v1.11.
3. **npm**: prerequisites as verified (npm CLI ≥ 11.5.1, Node ≥ 22.14.0,
   `id-token: write`, provenance automatic); because the trusted publisher
   is configured on an existing package's settings, the **first** publish
   is manual by the owner (`pnpm build && pnpm pack:check && pnpm pack`,
   then `npm publish rayito-<version>.tgz --access public` with a granular
   token and 2FA); afterwards configure the trusted publisher (repo
   `alejandro-cedeno-10/rayito`, workflow filename `release-npm.yml`, environment
   `npm`) and the workflow itself arrives with `m7-supply-chain`. pnpm
   9.15.4 stays the package manager for everything except the publish
   command.
4. **crates.io**: not now; `rayito-proto` may be published later (report
   §2) and would need its own `LICENSE`, `description`, `repository` and
   `publish = true`; `cargo publish --dry-run -p rayito-proto` is the
   rehearsal.
5. **GitHub, una sola vez**: enable Private Vulnerability Reporting;
   install the DCO app and make it a required check; branch protection on
   `main`; create environments `pypi` and `npm`; add the repository
   secrets none (trusted publishing needs none).
6. **Checklist de release** per component: `CHANGELOG.md` entry moved from
   `[Unreleased]`, version in the manifest, gates green, e2e green with the
   cost noted, tag pushed, badge turns green.

The task instruction is explicit: names are **not** reserved by this
change; the file says who does it and how.

### D12. `ARCHITECTURE.md`: "Qué corre dónde"

New `## Qué corre dónde` section placed right after "Vista general" and
before "Capa 1 — Imagen base". Content:

1. An ASCII diagram of the processes inside one MicroVM, complementary to
   the existing layered diagram (that one shows the RPC surface; this one
   shows processes, users and transports):

```
AWS Lambda MicroVM (Firecracker, ARM64)
│
├─ rayd (Rust, estático musl, root)
│    :8080  gRPC h2c  ── lo llama el SDK a través del proxy de AWS
│    :9000  HTTP/1.1  ── lo llama sólo Lambda (hooks /ready /run /suspend /resume /terminate)
│    │
│    └─ hijo: kernel-sidecar (Python 3.12, uid 1000)
│         stdin/stdout JSON lines  ◄──►  rayd
│         │
│         └─ hijos: ipykernel × contexto (Python, uid 1000)
│              ZMQ ipc:// bajo /run/rayito/k/<contexto>/
│              ►► aquí corre el código del usuario (run_code)
│
└─ procesos y PTYs de commands.run / pty.create (uid 1000, hijos directos de rayd)
```

2. The table, exactly these rows:

| Pieza | Lenguaje | Dónde corre | Quién la usa |
|---|---|---|---|
| `rayd` | Rust | dentro del MicroVM, como root, un proceso por VM | el SDK (gRPC :8080) y Lambda (hooks :9000) |
| kernel-sidecar | Python 3.12 | dentro del MicroVM, hijo de `rayd`, uid 1000 | sólo `rayd` (JSON lines por stdio) |
| ipykernel (uno por contexto) | Python | dentro del MicroVM, hijo del sidecar, uid 1000; **aquí corre el código del usuario** | el sidecar (`jupyter_client`, ZMQ `ipc://`) |
| procesos y PTYs (`commands`, `pty`) | lo que el usuario lance | dentro del MicroVM, hijos de `rayd`, uid 1000 | el SDK vía `ProcessService` / `PtyService` |
| SDK Python (`rayito`) | Python ≥ 3.11 | **fuera** del MicroVM, en el proceso del cliente (tu app, tu agente, tu CI) | tu código |
| SDK TypeScript (`rayito`) | TypeScript / Node ≥ 20 | **fuera**, en el proceso del cliente | tu código |
| llamadas al plano de control | boto3 / AWS SDK JS v3 dentro del SDK | **fuera**, desde el proceso del cliente hacia la API `lambda-microvms` | el SDK (`create`, `list`, `pause`, `resume`, `kill`, tokens) |

3. One paragraph "Por qué el sidecar es Python" restating ADR-002: the
   kernel is Python anyway, `jupyter_client` is the battle-tested client of
   the Jupyter wire protocol (ZMQ, five sockets, HMAC), the parts that cost
   effort (IPython formatters, `e2b_charts`, warm-up, kernel supervision
   and restart, reseed) are Python, the stdio hop is negligible against
   execution latency, the `.proto` is independent of the backend so the
   decision is reversible, and consolidation into Rust with
   `jupyter-zmq-client` remains an evaluated option (ADR-002, MILESTONES
   "opciones a evaluar"), not a goal. The paragraph links to ADR-002 and
   does not restate its consequences (two processes to supervise) beyond
   one clause.

### D13. `README.md`: "Cómo funciona"

Inserted after the E2B shim snippet and before the "Estado" paragraph, so
a newcomer sees install → example → how it works → state. Content: a
compact client ↔ VM diagram

```
tu proceso (Python / TypeScript)                AWS, tu cuenta
┌───────────────────────────────┐   HTTPS/2    ┌──────────────────────────────┐
│ SDK rayito                    │ ───────────► │ proxy de Lambda MicroVMs     │
│  · boto3 / SDK JS: run-microvm│              │   ▼ h2c                      │
│  · gRPC: commands, files,     │              │ MicroVM: rayd → sidecar →    │
│    run_code, pty              │ ◄─────────── │   ipykernel (tu código aquí) │
└───────────────────────────────┘   streams    └──────────────────────────────┘
```

followed by three sentences: the SDK **never runs inside the sandbox**
(it lives in your process and only sends RPCs; nothing of your agent's
runtime is copied into the VM); the code you send with `run_code` runs in
a Jupyter kernel inside the MicroVM as uid 1000, and `commands` / `pty`
spawn ordinary processes there; the languages you can use Rayito **from**
are Python and TypeScript today, and any language with a gRPC client can
talk to `rayd` from the `.proto` in `proto/rayito/v1/` (the contract is
the source of truth; the SDKs add lifecycle, tokens and reconnection). A
pointer to `ARCHITECTURE.md` "Qué corre dónde" and to the docs site
`concepts.md` closes the section. The existing "Estructura prevista" and
"Documentos" sections stay.

### D14. `docs/site/docs/concepts.md`: expanded "Cómo funciona" and languages

Two new sections at the top of `concepts.md` (before "Vida del sandbox vs.
política de idle"), keeping every existing section:

1. `## Qué corre dónde` — the same diagram and table as D12 (copied, not
   linked, because the site is built from `docs/site/docs` only; the
   `mkdocs --strict` gate forbids links outside the docs dir) plus the
   ADR-002 paragraph shortened to three sentences.
2. `## Desde qué lenguajes se usa Rayito` — the comparison table from
   report §2 with the sources cited inline:

| Producto | Agente dentro de la VM | SDKs oficiales de cliente |
|---|---|---|
| E2B | `envd` (Go) | Python, JS/TS (Go: forks comunitarios) |
| Daytona | daemon en Go | Python, TypeScript, Ruby, Go, Java + CLI |
| Modal | propietario | Python; JS y Go vía libmodal / modal-client |
| Rayito | `rayd` (Rust) | Python, TypeScript; cualquier otro lenguaje generando un cliente gRPC desde `proto/rayito/v1/` |

followed by the report's position: a Rust client is trivial to build
(`aws-sdk-lambdamicrovms` on crates.io, `tonic` client already in the
workspace) and will ship when a Rig user asks; a Go client is not planned
(bring your own from the proto, `buf.gen.yaml` Go example to be written on
request); publishing `rayito-proto` to crates.io is deferred. The
requirements for "any language" are spelled out: (a) call the
`lambda-microvms` control plane, (b) speak gRPC over HTTPS/2 with the four
lowercase metadata headers (`x-aws-proxy-auth`, `x-aws-proxy-port`,
`x-aws-proxy-force-h2`, `x-access-token`), (c) implement readiness on
`Health` and the reconnection contract if pause/resume is used. No nav
change in `mkdocs.yml` (the page already exists).

### D15. Language, style and what stays out of the docs

All new prose files are Spanish except `LICENSE`, `NOTICE`,
`CODE_OF_CONDUCT.md` and the quoted DCO (legal texts kept in their
canonical language). Identifiers, YAML keys and script names are English.
No file reproduces tokens, JWEs, payload examples with secrets, or file
contents from a sandbox. The `MILESTONES.md` edit is limited to adding an
"M7 — Preparación open source" section with the report §5 table (change
name, scope, acceptance test, size) and marking `m7-oss-hygiene` as the
active change; if the orchestrator has already added that section through
another change, only this change's row is edited.

### D16. Acceptance without AWS

This change has no runtime surface, so the constitution's real-AWS e2e
rule is satisfied vacuously and the acceptance evidence is the gate set
(spec `oss-licensing` and `community-health` scenarios): `python
scripts/check_license.py` exit 0; `cd clients/python && uv build &&
python scripts/check_wheel.py dist/*.whl && uvx twine check dist/*`;
`cd clients/typescript && pnpm pack:check` listing `package/LICENSE` and
`package/NOTICE`; `mkdocs build --strict` green; every pre-existing gate
green (Rust fmt/clippy/test, Python pytest/ruff/mypy, TypeScript
lint/typecheck/test/build, sidecar ruff/mypy). A reviewer additionally
reads `README.md` → `ARCHITECTURE.md` → `concepts.md` and checks the three
docs statements of the `architecture-docs` spec. The DCO check "green on a
PR" from the report is unverifiable until the repo is on GitHub with the
app installed; it is recorded as a manual step, not as an acceptance
criterion of this change.

## Risks / Trade-offs

- **Copyright holder line.** "Rayito contributors" is the safe default
  and matches the existing file; if the owner wants a legal entity, it is
  one line in `NOTICE` (root + two copies via `check_license.py`). Not
  blocking.
- **PyPI and Metadata 2.4.** PyPI accepts `License-Expression` since
  2024 and `twine check` passes on the probe; a very old `pip` on a
  user's machine ignores the field (it is informational). No install
  impact.
- **Removing the classifier.** Older tooling that reads
  `License ::` classifiers shows no licence for the wheel; PEP 639 says
  the expression supersedes it. Accepted.
- **English Code of Conduct in a Spanish repo.** Trade-off taken for
  licence fidelity (D6); a Spanish preface mitigates it.
- **Badges render "not found" until publication.** Intended; stated in
  `docs/RELEASING.md`.
- **Dependabot noise.** Grouped minor/patch updates and a limit of five
  open PRs per ecosystem keep it bounded; majors are separate on purpose
  because the `=`-pinned Cargo tree and `tonic` majors need review.
- **Docs duplication (README ↔ ARCHITECTURE ↔ concepts).** Three levels of
  detail by design (five minutes / full / comparative); the table is
  copied once into `concepts.md` because the site cannot link outside its
  docs dir. If it drifts, `ARCHITECTURE.md` wins (stated in
  `concepts.md`).

## Migration Plan

No runtime migration. Order of implementation is `tasks.md` §1 (licence
manifests + files + gate) → §2 (packaging assertions) → §3 (community
files) → §4 (architecture docs) → §5 (changelogs, README, MILESTONES) →
§6 (full gate run). Rollback is deleting the new files and restoring the
five manifests; nothing else depends on them.

## Open Questions

None blocking. Two owner decisions are recorded for after this change,
not inside it: (1) whether `NOTICE` should name a legal entity instead of
"Rayito contributors"; (2) when to do the first manual npm publish that
unlocks trusted publishing (`docs/RELEASING.md` §3).
