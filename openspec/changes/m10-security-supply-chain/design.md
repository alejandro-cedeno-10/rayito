# Design — m10-security-supply-chain

Both rows are verifiable with local gates and a `workflow_dispatch` dry run;
neither needs an AWS call, an image build or an e2e. Source of truth:
`docs/SECURITY_AUDIT.md` §8, rows C-10 and C-12.

---

## D1 — C-10: why a `*-build`/`*-publish` split and not a narrower fix

**Today**: `release.yml`'s `python` and `typescript` jobs each hold
`environment: pypi`/`npm` and `permissions: id-token: write` from their
first step, and the same job runs `uv build` / `pnpm install && pnpm build
&& pnpm pack`. A compromised dependency's build-time code (a
`postinstall`, or the `uv_build` PEP 517 backend `uv build` invokes) runs
inside a process that can already read `ACTIONS_ID_TOKEN_REQUEST_TOKEN` and
mint an OIDC assertion, before the workflow's own `if:
needs.resolve.outputs.publish == 'true'` gate is even evaluated.

**Why a job split and not, say, `--ignore-scripts` alone**: `--ignore-scripts`
(added anyway, D3) stops an npm lifecycle script but does nothing for `uv
build`'s own build backend, which is not a lifecycle script and cannot be
disabled — building a wheel means running the backend. The only fix that
survives "the build backend itself is malicious" is separating build and
publish into different jobs, so `id-token: write` is only ever granted to a
job that has no repository checkout and executes nothing but the publish
action/CLI and a `sha256sum -c`.

**Why the build job still needs to be trusted to produce the right bytes**:
splitting jobs does not remove the build job's ability to produce a bad
artefact — it removes its ability to *also* hold a publishing credential
while doing so. The threat this closes is "compromised build code steals or
forges a publish", not "compromised build code produces a bad release";
the latter is unpinned dependencies in general (Dependabot's lane) and the
existing `check_wheel.py`/`twine check`/`pnpm pack:check` gates, unaffected
by this change.

**Why the digest hand-off instead of trusting `actions/upload-artifact` /
`actions/download-artifact` alone**: those actions already checksum their
own transport, but the *publish* job has no way to know that the artefact it
downloaded is bit-for-bit what the *build* job produced without a build-job
step (the sha256) and its digest surviving as a job `output` — a GitHub
Actions job output is a small string set once, not a target dependency-
injection-prone shell expansion. The publish job's own `sha256sum -c` step
reads the digest through `env:`, not by interpolating
`${{ needs.<job>.outputs.* }}` inside the script body, so a value containing
shell metacharacters cannot break out of the `sha256sum -c -` line
(actionlint's guidance on `${{ }}` inside `run:`).

**Why `packages-dir` for PyPI must not contain `SHA256SUMS`**:
`pypa/gh-action-pypi-publish` walks `packages-dir` and rejects any file that
is not a wheel or sdist. The build job writes `SHA256SUMS` as a *sibling* of
`dist/` (inside `clients/python/`, not inside `clients/python/dist/`) and
uploads both paths as one artefact; `actions/upload-artifact` preserves
their common ancestor, so the downloaded artefact reproduces the same
layout (`<download>/SHA256SUMS` next to `<download>/dist/*.whl` and
`*.tar.gz`) and `packages-dir: <download>/dist` never sees `SHA256SUMS`.

**Why `actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c #
v8.0.1`**: the repository's `actions/upload-artifact` pin is already v7.0.1;
`download-artifact` needs v4+ to read a v7 artifact (GitHub's own
compatibility note), so v8.0.1 — the newest full release tag as of
2026-09-29 — is the smallest change that satisfies that floor. The commit
was looked up with `gh api repos/actions/download-artifact/git/ref/tags/v8.0.1`
(a lightweight tag; the object is the commit itself,
`3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`), matching what
`scripts/check_pins.py`'s first gate requires (a 40-hex SHA with an optional
version comment).

**Why Trusted Publishing needs no reconfiguration**: both PyPI's and npm's
Trusted Publisher bind the OIDC claim to the repository, the workflow
*file* (`release.yml`) and the `environment` claim, never to a job name or
job id inside that file. Renaming `python`/`typescript` to
`python-build`/`python-publish` and `typescript-build`/`typescript-publish`
does not change any of those three claims, so the existing Trusted
Publisher entries on pypi.org and npmjs.com keep working unmodified; this is
now stated in `docs/RELEASING.md` so a future reader does not go looking for
a registry setting to update.

**Blast radius**: `resolve` and `rayd` are byte-identical (diffed against
`git show origin/main:.github/workflows/release.yml` before committing).
`workflow_dispatch` dry runs (`dry_run: true`) now show four jobs instead of
two for a `python-v*`/`typescript-v*` tag — the `*-build` job green and
uploading its artefact, the `*-publish` job present but skipped by its own
`if:` — which is the evidence this change records instead of a real
publish.

## D2 — C-10 follow-up not fixed here: `rayd`'s own build/publish residue

**Named so it is not lost, not fixed in this change** (task assignment
scoped this row to `python`/`typescript` only): the `rayd` job holds
`permissions: id-token: write` (for cosign's keyless signing) in the same
job that runs `cargo install`, `cargo auditable zigbuild`, `cargo
cyclonedx` and `scripts/image_zip.py` — the same residue class as C-10, just
for a Sigstore identity token instead of a registry publish token. Splitting
it needs its own `*-build`/`*-sign` design (what artefact hand-off proves
the binary `cosign sign-blob` receives is the one `cargo auditable zigbuild`
produced, and whether cosign's identity check still names the right
workflow ref when the signing job is not the one that built) and is left for
a future milestone.

## D3 — C-10: `clients/typescript/.npmrc`

**Verification before committing** (the plan's explicit gate): on a clean
tree (`rm -rf node_modules dist`), `pnpm install --frozen-lockfile` printed
no "ignored build scripts" warning and installed cleanly (only
`esbuild@0.28.2` declares a `postinstall` today, and esbuild's own script
only fetches the platform binary it already ships as an optional
dependency — see below); `pnpm lint`, `pnpm typecheck`, `pnpm build`
(tsdown, both CJS and ESM bundles), `pnpm test` (877 tests) and `pnpm
pack:check` all passed unmodified. The file is kept.

**Why the flag is also repeated in `release.yml`'s `pnpm install`** (not
just relying on the `.npmrc`): `--ignore-scripts` on the CI invocation is
what the new `test_release_workflow.py::test_every_pnpm_install_carries_ignore_scripts`
pins directly against the workflow text, independent of whether a future
edit to `clients/typescript/.npmrc` weakens or removes it — belt-and-
suspenders, as the plan asked for.

## D4 — C-12: hash-pinning without changing any version

**Regeneration command** (uv 0.12.18, 2026-09-29):

```bash
cd kernel-sidecar
cp requirements.txt /tmp/pins.txt
uv pip compile --only-binary :all: --python-platform aarch64-manylinux_2_28 \
  --python-version 3.12 --generate-hashes --no-header --no-annotate \
  -c /tmp/pins.txt requirements.in -o requirements.txt
```

`--no-header --no-annotate` drop uv's autogenerated command comment and
`# via` provenance lines (which otherwise echo the local `/tmp/pins.txt`
path) so the file keeps its own hand-written header, now describing the
hash-pinning; the `-c /tmp/pins.txt` constraint is the current file, so the
regeneration only adds hashes and cannot silently move a version.

**Version identity, asserted before committing and pinned by
`test_the_sidecar_requirements_are_hash_pinned_and_version_stable`**: a
`name==version` diff between the old and new files, ignoring hash lines, is
empty. `requirements-poly.txt` is regenerated the same way but with
`--no-deps` and itself as the only input (no `requirements-poly.in` exists;
the four pins already carry exact versions, so compiling the file against
itself with `--no-deps` re-resolves nothing beyond what it already says).
uv normalizes `bash_kernel` to `bash-kernel` (PEP 503); the pin is renamed
back to `bash_kernel` before committing to match the header comment,
`image/Dockerfile`'s `import bash_kernel` check and every other reference in
the repository — pip treats `-`/`_` as equivalent, so this is spelling only,
not a behaviour change.

**Hash coverage is not aarch64-only, checked instead of assumed**: uv's
`--generate-hashes` hashes every file PyPI lists for the pinned *version*,
not only the ones matching `--python-platform`; `numpy==2.5.3` alone carries
607 `--hash=` entries (aarch64 and x86_64 manylinux and musllinux, macOS,
Windows). This matters because CI's x86_64 `sidecar` job and local macOS
development both run `uv run --with-requirements requirements.txt pytest`
against this same file. Confirmed locally: `uv run --with-requirements
requirements.txt python -c "import numpy, pandas, matplotlib, scipy,
sklearn, pydantic, ipykernel"` and the full sidecar suite (86 passed) both
succeed on macOS aarch64 with the hash-pinned file, and a `pip install
--dry-run --require-hashes --no-deps --only-binary=:all:` against a bare
Python 3.12 venv resolves and validates macOS arm64 wheels for every pin
with no missing-hash error, for both `requirements.txt` and
`requirements-poly.txt`.

**`uv run --with-requirements requirements-poly.txt pytest -m kernel`
still fails four tests on macOS, before and after this change**: the
`kernel` marker is documented in `kernel-sidecar/pyproject.toml` as "sólo
Linux" (it starts a real ipykernel with only the poly variant's four pins
installed — no scientific stack — and expects a `slim_harness` context to
still be able to import it, which needs the platform's process/socket
behaviour this repo's kernel launcher assumes). Verified with `git stash`:
the same four tests
(`test_startup_scripts_produce_chart_data_and_png`,
`test_warmup_leaves_the_namespace_clean`, `test_second_context_is_isolated`,
`test_slim_variant_starts_without_the_stack`) fail identically against the
pre-change `requirements-poly.txt` (no hashes). Not a regression; the
authoritative check for this suite is CI's `sidecar` (x86_64) and `arm`
(aarch64) jobs, both Linux, run in the PR.

**`uvx pip-audit==2.10.1 -r kernel-sidecar/requirements.txt --no-deps
--strict` fails locally on macOS with an unrelated resolver artefact, not a
finding**: pip-audit's `--no-deps` flag does not reach the underlying `pip
install --dry-run --report` subprocess it shells out to (observed: the
failing invocation in the error message carries no `--no-deps`), so pip's
own resolver still evaluates `ipykernel`'s environment markers for the
*local* platform and finds `appnope>=0.1.2; platform_system == "Darwin"` —
a macOS-only dependency that is correctly absent from a file compiled for
`aarch64-manylinux_2_28` (Linux) and is never installed by the image or by
CI's Linux `audit` job. Confirmed clean with the platform-independent path:
`uvx pip-audit==2.10.1 -r kernel-sidecar/requirements.txt --require-hashes
--disable-pip --strict` → `No known vulnerabilities found` (and the same
for `requirements-poly.txt`). CI's `audit` job runs on `ubuntu-24.04`, where
`platform_system == "Darwin"` is always false, so the exact command the job
runs (`--no-deps --strict`, no `--disable-pip`) is unaffected by this local
artefact.

## D5 — C-12: `scripts/check_pins.py` gate 5

Two related checks, wired into `findings_for` next to gates 3 (Dockerfile
downloads) and 4 (dnf packages), both reusing `dockerfile_instructions`'s
backslash-continuation joiner (the same joiner works unmodified on a
`requirements.txt`'s `name==version \` + `--hash=... \` continuation
lines — it only cares about line continuation and comment-skipping, not
about `RUN`):

- `unhashed_pip_installs`: every `pip install`/`python3[.x] -m pip install`
  naming `-r`/`--requirement` in a Dockerfile instruction must carry
  `--require-hashes`, `--no-deps` and some spelling of `--only-binary
  :all:`/`--only-binary=:all:`/`--only-binary all`.
- `unhashed_requirement_pins`: every `name==version` line of a file
  `scripts/check_pins.py` is given whose name starts with `requirements` and
  ends in `.txt` must carry at least one `--hash=sha256:<64 hex>`.
  `kernel-sidecar/requirements*.txt` is added to `DEFAULT_PATHS` so a bare
  `python3 scripts/check_pins.py` covers both files without arguments.
