## Context

`release.yml` already split Python and TypeScript into a credential-free
`*-build` job and a `*-publish` job that holds the OIDC token, never checks
out and verifies the build output with `sha256sum -c` (M10, C-10). `rayd`
was left as one job. The review of 2026-10 (SC-01..SC-12) confirmed, from
the workflow text, the logs of the `rayd-v0.6.1` run and the repository
API, that this job restored main-scope caches, ran third-party build code
next to both credentials, and uploaded with `--clobber`; that the cosign
identity in the docs was not bound to a version; and that a few CI
downloads and the Dependabot policy widened the window for a foothold on
`main`. This change closes what can be closed in the tree and records the
repository settings it depends on.

## Goals / Non-Goals

**Goals:** no job holding a credential runs third-party build code; no
release job restores a cache; every downloaded tool is checked against a
pinned sha256; the published hash of every artefact is taken before any
third-party check runs; a user's cosign verification is bound to the
version installed; the TypeScript SDK parses caller strings in linear time.

**Non-Goals:** draft releases plus GitHub immutable releases (needs a
release-please flow change, see D9); replacing `RELEASE_PLEASE_TOKEN` by a
GitHub App; `egress-policy: block` in harden-runner; opt-in bundle
verification inside `rayito image publish`; fuzzing (Scorecard).

## Decisions

### D1. `rayd` in three jobs, mirroring C-10

`rayd-build` (`contents: read`) does everything that executes repository or
third-party code and uploads `release/` with an unsigned `SHA256SUMS`
whose own digest is a job output. `rayd-sign` (`id-token: write`,
`contents: read`) has no checkout: it downloads the artefact, checks the
digest and then `sha256sum -c SHA256SUMS`, runs `cosign sign-blob` and the
`verify-blob` self-check, rewrites `SHA256SUMS` over the five files and
exposes its digest. `rayd-upload` (`contents: write`,
`environment: release`) has no checkout either: it checks the digest and
the sums again, refuses a release that already carries any asset name, and
runs `gh release upload` without `--clobber`. The asset list lives once,
in the workflow `env` (`RAYD_ASSETS`). Alternative rejected: one job with
the credentials moved to the last steps; a background process started by a
build script outlives its step and reads later steps' environment.

### D2. No cache in release jobs, enforced on the job graph

`scripts/tests/test_release_workflow.py` fails if any job of `release.yml`
uses `actions/cache*`, `Swatinem/rust-cache` or `mlugg/setup-zig`, if a
`setup-uv` step lacks `enable-cache: false`, if a `setup-node` step sets
`cache` or lacks `package-manager-cache: false`, or if `pnpm/action-setup`
sets `cache: true`. A YAML-aware test (the suite already parses the job
graph) was preferred over a lexical rule in `check_pins.py`, which stays
standard-library only. `ci.yml` keeps `rust-cache` (it publishes nothing);
it gets the verified zig action too, so both builds link the same zig.

### D3. Tools in a fresh root

`CARGO_HOME="$RUNNER_TEMP/cargo-tools-home" cargo install --locked --force
--root "$RUNNER_TEMP/cargo-tools" …` and that `bin` prepended to
`GITHUB_PATH`. `--force` is redundant on an empty root and kept as
belt-and-braces. The build itself keeps the default `CARGO_HOME`, so the
`--remap-path-prefix` rustflags do not change.

### D4. zig and cargo-deny as local composite actions

`.github/actions/zig` downloads `zig-x86_64-linux-0.16.0.tar.xz` from
ziglang.org and checks the sha256 of `index.json`
(`70e49664…`), also verified on 2026-10-04 against the minisign signature
with ziglang.org's public key. `.github/actions/cargo-deny` downloads
cargo-deny 0.20.2 (`9f12ed4c…`, identical in the release's `.sha256`, the
asset `digest` of the API and a local computation) and replicates the
action's defaults: `cargo deny --log-level warn --manifest-path
./Cargo.toml --all-features check [advisories]`. One definition each, used
by `ci.yml`, `audit.yml` and `release.yml`.

### D5. Sixth gate of `check_pins.py`

A workflow step is a YAML list item; its `run:` is the block or line after
the key (continuations joined, comments skipped, lines joined with `;`).
A step whose script names `curl` or `wget` must pin a `<NAME>_SHA256` of 64
hex in its `env:` (or assign it in the script) and satisfy the existing
Dockerfile rule: each `curl` writes with `-o` a path that a `sha256sum -c`
of the same script checks, no pipe, no floating release, no `wget`. The
default paths gain `.github/actions/*/action.y{a,}ml` (so action pinning
covers them too) and `.github/release/requirements*.txt` (hash gate). The
gate is lexical and fail-safe: the word `curl` anywhere in a script counts
as a download.

### D6. Hash first in `python-build`

`SHA256SUMS` and its digest move right after `uv build`; `check_wheel.py`
and twine run afterwards and the job ends with `sha256sum -c SHA256SUMS`.
twine 7.0.0 and its 26 dependencies come from a committed file compiled
with `uv pip compile --generate-hashes --exclude-newer <7 days ago>` and
installed with `uv pip install --require-hashes --no-deps --only-binary
:all:` into a venv under `RUNNER_TEMP`; `ci.yml`'s `check` job installs the
same file, so a stale pin fails on a pull request, not on a release. uv is
pinned to 0.12.18 with the sha256 of its x86_64 tarball (12 days old on
2026-10-04).

### D7. Tagged commit on `main`

`resolve` checks out with `fetch-depth: 0` and `persist-credentials:
false` and, only when it will publish, runs `git merge-base --is-ancestor
"$GITHUB_SHA" origin/main`. Dry runs from a release branch keep working.

### D8. Exact cosign identity

The self-check uses the run's own ref
(`https://github.com/${GITHUB_REPOSITORY}/.github/workflows/release.yml@${GITHUB_REF}`),
which also covers dry runs from a branch without a special case. User docs
bind `refs/tags/rayd-v${RAYD_VERSION}`. `--certificate-github-workflow-trigger
push` is deliberately not documented: the documented fallback dispatches
the workflow from the tag, whose certificate carries `workflow_dispatch`.

### D9. What stays a repository setting or a follow-up

Applied with the API on 2026-10-04 (outside the tree): environment
`release` (required reviewer, deployment tag policy `rayd-v*`);
`e2e` limited to `main` with a required reviewer; a tag ruleset on
`refs/tags/python-v*`, `typescript-v*` and `rayd-v*` forbidding update and
deletion; fork-PR approval for all external contributors. Left as
follow-ups, because each changes the release flow or needs a decision:
release-please drafts plus immutable releases (uploads to a published
immutable release fail, so it needs `draft: true`, `force-tag-creation`
and a publish step), a GitHub App token instead of the PAT,
`sha_pinning_required` and an allow-list of actions (nested composite
actions must be audited first), and `egress-policy: block` for the
credential jobs.

### D10. Linear-time string helpers in the TypeScript SDK

`src/strings.ts` exports `stripTrailing(text, char)` and
`stripLeading(text, char)`, used by `decodeAccessToken`,
`downloadFilename`, `deriveRepoDirFromUrl` and `DockerIgnore.fromText`.
`HTTP_URL` becomes `/^(https?:\/\/)([^/?#]*)([\s\S]*)$/i` and
`splitHttpUrl` returns `undefined` when the tail contains a line
terminator, which is exactly when the old `(.*)$` failed to match, so
`withCredentials` and `stripCredentials` keep their behaviour. The Python
SDK already uses `rstrip`/`urlparse` (linear); no Python change.

## Risks / Trade-offs

- [The release workflow cannot be fully exercised without a release] →
  a `workflow_dispatch` dry run of `rayd-v<current>` from this branch
  exercises `rayd-build` and `rayd-sign` end to end (artefact digest,
  signature, exact-identity self-check); `rayd-upload` and the
  tag-on-`main` check only run on the next real release.
- [A stale twine pin breaks the release] → `ci.yml` installs the same file
  on every pull request.
- [Timing-based ReDoS tests flake] → 50 000-character inputs and a
  150 ms budget: the quadratic path takes over 1 s, the linear one under
  1 ms.
