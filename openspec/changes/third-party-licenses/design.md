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
5. **Committed file + staleness gate, rather than generate-only.** A
   committed `THIRD_PARTY_LICENSES.md` puts license changes in front of the
   reviewer of the lock change, lets `make image-zip` and the local guest
   context stage notices without cargo-about, and lets the release prove
   that what it ships is what `main` reviewed (`make licenses-check` in
   `rayd-build`). Cost: a Dependabot cargo PR needs `make licenses` pushed
   to its branch; CI names the command when it fails.
6. **Coverage against the real binary.** A regeneration only proves the
   file matches cargo-about's view of the graph.
   `check_third_party_licenses.py --binary` cross-checks the `.dep-v0`
   section `cargo auditable` embeds (runtime, crates.io packages) so a
   graph-filtering mistake cannot ship an unlisted crate.
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

- Dependabot cargo PRs fail CI until `make licenses` is pushed; accepted for
  review visibility (decision 5).
- cargo-about bumps are manual (version, sha256, `Makefile`), like
  cargo-deny.
