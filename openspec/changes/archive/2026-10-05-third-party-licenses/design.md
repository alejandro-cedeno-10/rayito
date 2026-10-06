## Context

The release ships `rayd` twice: as a bare binary asset and inside
`rayito-image.zip`, from which every user builds the `rayito-base` image in
their own account. Both are binary redistributions of the crates `rayd`
links. `deny.toml` already gates which licenses are allowed; nothing
produced the notices those licenses require.

## Decisions

1. **cargo-about 0.9.2, pinned by binary and sha256.** It is the
   EmbarkStudios companion of cargo-deny (same SPDX handling, same
   allowlist semantics). CI and release download the
   `x86_64-unknown-linux-musl` release asset through a composite action,
   verified against a sha256 matched in three sources (the release's
   `.sha256` file, the GitHub asset digest and a local hash), exactly like
   `.github/actions/cargo-deny`. No `cargo install` compile, no cache. The
   `Makefile` refuses any other version (`require-cargo-about`) because a
   different version can render differently.
2. **Graph = what the binary links.** `targets =
   ["aarch64-unknown-linux-musl"]`, `ignore-build-dependencies` and
   `ignore-dev-dependencies`, root manifest `crates/rayd/Cargo.toml`,
   private workspace crates ignored. Proc macros remain listed (normal
   dependencies in Cargo's graph); over-inclusion is harmless.
3. **Same allowlist as `deny.toml`.** `accepted` copies
   `[licenses].allow`; the order picks the side of an `OR` (MIT first,
   MPL-2.0 last), and `notify` alone accepts CC0-1.0, mirroring deny's named
   exception, so a new CC0 crate fails in both tools. `--fail` makes an
   unidentifiable license an error.
4. **Reproducible: `--frozen`.** Generation is offline and lock-bound;
   `cargo fetch --locked` runs first. Online and offline generation were
   compared during the change and are byte-identical today, so offline
   loses nothing and removes the network from the output.
5. **Generated at packaging time, never committed.** The first version
   committed `THIRD_PARTY_LICENSES.md` behind a staleness gate
   (`make licenses-check`), so every Dependabot cargo PR failed CI until a
   person pushed `make licenses` to its branch. The three ways out were
   weighed for a single maintainer:
   - *A bot that regenerates and commits to the Dependabot branch.* It needs
     a job with `contents: write` reacting to pull requests, the classic
     "pwn request" surface: its safety rests on every guard holding (author
     `dependabot[bot]`, head repo is this one, diff limited to manifests,
     tooling and `.cargo/config.toml`/`rust-toolchain.toml` taken from the
     base branch because `cargo metadata` runs `rustc -vV` through any
     configured `rustc`/`rustc-wrapper` and a toolchain file can point at a
     local toolchain). A commit made with `GITHUB_TOKEN`, even through
     `createCommitOnBranch` (signed by GitHub), triggers no workflow, so the
     new head has no required checks until an extra `workflow_dispatch`
     (`actions: write`) re-runs CI; and Dependabot stops rebasing a PR once
     someone else commits to it, so every grouped PR would need
     `@dependabot recreate` on the next conflict.
   - *Regenerate in CI and fail with a clear message.* No write token, but
     it is what already happened: the PR still needs a manual commit.
   - *Generate when packaging.* No write token anywhere, no extra commit,
     no re-trigger, Dependabot keeps rebasing. Chosen.

   The output is a pure function of inputs `main` reviews (`Cargo.lock`,
   the crate sources it pins by checksum, `about.toml`, `about.hbs` and the
   cargo-about binary pinned by sha256), so the release still ships what
   `main` reviewed without a committed copy; `rayd-build` generates it with
   `contents: read`, and `SHA256SUMS`, which `rayd-sign` signs, covers it.
   Neither cargo-about nor `cargo tree` compiles anything or runs a build
   script: both read `cargo metadata` and the sources `cargo fetch`
   downloads. What is lost is the license diff inside the PR: the policy
   itself is still enforced twice (`cargo-deny` with `deny.toml`,
   `cargo about --fail` with the same list), and CI's `rayd-aarch64-musl`
   artifact carries each PR's generated file. `make image-licenses` depends
   on `make licenses`, so a local `make image-zip*` needs cargo-about and
   never stages a stale copy. The local guest (`make local-guest-context`)
   is never redistributed: it copies the generated file when present and a
   placeholder otherwise, so `make local-up` needs no cargo-about.
6. **Coverage against cargo's own resolver.** A regeneration only proves
   the file matches cargo-about's view of the graph (its own feature
   resolution through `krates`). `make licenses` also requires the
   listed crates to equal, in both directions, the third-party crates of
   `cargo tree --frozen -p rayd --target aarch64-unknown-linux-musl -e
   normal`, i.e. what cargo compiles for the release build. The binary's
   `.dep-v0` was the first choice but is a superset: `cargo auditable`
   takes it from `cargo metadata`, which unifies features with the
   workspace's dev-dependencies, and the first CI run showed it naming
   `ring`, `untrusted 0.7`, `getrandom 0.2`, `zlib-rs`, `hyper-timeout` and
   `aho-corasick`, none of which the release binary links (no `ring_core_*`
   symbol, no source path of theirs). So `--binary` checks only the
   direction that holds: every listed crate is recorded in `.dep-v0`, which
   ties the notices to the binary being shipped.
7. **Upstream NOTICE files.** cargo-about collects license texts only.
   None of today's 219 crates ships a `NOTICE`; `--metadata` fails if a
   listed crate starts shipping one that the root `NOTICE` does not name,
   so the Apache-2.0 §4(d) duty is handled by a person, once, in `NOTICE`.
8. **Markdown output.** Readable in a terminal, on GitHub and in the
   image; each crate is one line "- `<name> <version>` <repo>", which the
   coverage script parses. HTML adds nothing a user of a CLI image needs.
9. **Placement.** Release assets `LICENSE`, `NOTICE`,
   `THIRD_PARTY_LICENSES.md` (per-tag releases, so the names do not clash);
   in the zip under `licenses/`; in the image under `/usr/share/doc/rayd/`
   (FHS location for package documentation and licenses), root-owned 0644.
10. **The zip enforces it.** `rayito.cli._artifact.image_files` refuses a
    tree that ships `rayd` without the three files: the Dockerfile's `COPY`
    would otherwise fail only in AWS, after the upload. A tree without
    `rayd` (no binary distributed) is left alone.
11. **Signing `SHA256SUMS`.** Instead of three more per-file bundles,
    `rayd-sign` signs `SHA256SUMS` with the same keyless identity and
    self-check. It already lists every asset but itself and its bundle, so
    one signature covers the notices and, for the first time, the SBOM.
    `rayd-upload` verifies the bundle's digest from `rayd-sign`'s outputs,
    like `SHA256SUMS`. The per-file bundles of `rayd` and the zip stay, so
    existing verification recipes keep working.
12. **Python sidecar dependencies.** Not redistributed: the zip ships only
    the pins; pip installs wheels in the user's account at image build, and
    each wheel keeps its `dist-info/licenses/` (checked for pyzmq, which
    bundles libzmq under MPL-2.0, file-level copyleft, unmodified). The
    vendored `e2b_charts` already ships its MIT `LICENSE` inside
    `kernel-sidecar/`. No pip-licenses step is added.

## Risks / Trade-offs

- License changes no longer show up as a diff in the PR that changes the
  lock (decision 5); the allowlist gates of `cargo-deny` and cargo-about
  still fail any license outside the policy.
- `make image-zip*` and `make image-licenses` need cargo-about 0.9.2 on the
  maintainer's machine (`make local-up` does not).
- cargo-about bumps are manual (version, sha256, `Makefile`), like
  cargo-deny.
