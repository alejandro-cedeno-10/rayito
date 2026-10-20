# Verify a release

Each release publishes three components from their tags (`python-v<v>`,
`typescript-v<v>`, `rayd-v<v>`) with `.github/workflows/release.yml`. What you
can check from a clean machine, trusting nothing but Sigstore and the
registries:

## `rayd` binary and `rayito-image.zip` (GitHub Release `rayd-v<v>`) { #binario-rayd-y-rayito-imagezip-github-release-rayd-vv }

The release assets are `rayd`, `rayito-image.zip`, their bundles
`rayd.sigstore.json` and `rayito-image.zip.sigstore.json` (cosign keyless
signature: the Sigstore certificate carries the identity of the workflow and
the tag), `rayd.cdx.json` (CycloneDX 1.5 SBOM of the crate graph for
`aarch64-unknown-linux-musl`), the license notices `LICENSE`, `NOTICE` and
`THIRD_PARTY_LICENSES.md`, `SHA256SUMS` and its bundle
`SHA256SUMS.sigstore.json`. Since 0.7.1, `SHA256SUMS` is also signed: it
lists every asset except itself and its bundle, so its signature covers the
SBOM and the notices, which have no bundle of their own.

```bash
RAYD_VERSION=0.14.0   # the version you are going to install, without the "v"

cosign verify-blob --bundle rayito-image.zip.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayito-image.zip

cosign verify-blob --bundle rayd.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  rayd

# Since 0.7.1: the SHA256SUMS signature
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "https://github.com/alejandro-cedeno-10/rayito/.github/workflows/release.yml@refs/tags/rayd-v${RAYD_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS

sha256sum -c SHA256SUMS
```

Verification fails if the file or the bundle change, or if the signature was
made by another workflow, another repository, another branch or **another
tag** than `rayd-v${RAYD_VERSION}`. The identity is given in full and not as a
regular expression on purpose: with a regexp ending in `rayd-v`, a bundle
signed by any `rayd` release verified any other, so an old asset (a version
with known bugs) or one signed under another tag passed for the one you were
about to install. `cosign` ≥ 2.x (the release uses
`sigstore/cosign-installer`, cosign 3.x).

### What the signature proves { #que-prueba-la-firma }

A valid signature proves that this repository's `release.yml` signed that
file in a run launched from the tag `rayd-v${RAYD_VERSION}`. Since 0.7.0 it
also proves that the run went through the maintainer's approval: the job that
signs runs in the `release` environment, with a required reviewer, so GitHub
does not give it the OIDC token (nor does Sigstore issue the certificate)
until someone approves the run. The approval comes before the OIDC token
exists, and a rehearsal (`dry_run`) signs nothing: it only builds. The job
that signs does not check out or compile, signs only what `sha256sum -c`
confirms came out of the build, and the one that uploads the assets never
replaces one that is already published.

What the signature does **not** prove on its own is that the tag's code was
reviewed on `main`: the identity is set by the workflow file and the tag, not
by who created the tag. Publishing requires the tag's commit to be on `main`,
but the `release.yml` that runs is the one from the tagged commit. To sign
with this identity you need, at the same time, to be able to create a
`rayd-v*` tag and to approve the `release` environment. With a single
maintainer and reviewer, that is their account: a leaked token that only
writes content and PRs (release-please's) can create the tag, but cannot
approve the environment.

The binary is compiled with `cargo auditable`, so it carries the exact crate
graph in the ELF section `.dep-v0`; any tool that reads that format can audit
it against RustSec without the source code:

```bash
cargo audit bin rayd          # cargo-audit >= 0.17 reads .dep-v0
python3 scripts/check_auditable.py rayd   # from the repository: root, version and key crates
```

The `rayd.cdx.json` SBOM is the same graph in CycloneDX 1.5 for tools that
consume SBOMs (the `metadata.component` is `rayd` with the tag's version).

### What `rayito setup` checks and what it does not { #que-comprueba-rayito-setup }

Without `--artifact`, [`rayito setup`](cli.md#rayito-setup) downloads
`SHA256SUMS` and `rayito-image.zip` over HTTPS from the `rayd-v<your SDK
version>` release and compares the zip's sha256 with the `SHA256SUMS` line. If
the line is missing or does not match, it publishes nothing
(`checksum_missing`, `checksum_mismatch`).

That proves **integrity, not authenticity**: it catches a truncated, corrupted
or wrong zip, but `SHA256SUMS` comes from the same release as the zip, so
whoever could replace one could replace both. `rayito setup` **does not check
the cosign signature**. If you want it, verify the zip with the commands above
and pass it to `rayito setup`, which publishes it instead of downloading the
release one:

```bash
rayito setup --artifact rayito-image.zip
```

### `rayd` license notices { #avisos-de-licencia-de-rayd }

`rayd` is Apache-2.0 and statically links third-party crates (MIT,
Apache-2.0, BSD, ISC, Unicode-3.0, Zlib and one CC0). Since 0.7.1, their
notices travel with every copy of the binary: as release assets (`LICENSE`,
`NOTICE` and `THIRD_PARTY_LICENSES.md`), in `licenses/` inside
`rayito-image.zip` and, in the image built from that zip, in
`/usr/share/doc/rayd/`. `THIRD_PARTY_LICENSES.md` is generated by
cargo-about from the tag's `Cargo.lock`, and the release checks that it lists
exactly the crates that `cargo tree` compiles into `rayd` for
`aarch64-unknown-linux-musl` and that all of them are in the binary's
`.dep-v0`. You can repeat the second check with the downloaded binary:

```bash
python3 scripts/check_third_party_licenses.py THIRD_PARTY_LICENSES.md --binary rayd   # from the repository
```

Inside a sandbox:

```bash
ls /usr/share/doc/rayd/   # LICENSE  NOTICE  THIRD_PARTY_LICENSES.md
```

## Python package `rayito` (PyPI) { #paquete-python-rayito-pypi }

It is published with Trusted Publishing (OIDC, no token) and
`pypa/gh-action-pypi-publish` attaches PEP 740 attestations automatically. On
the file's page on PyPI the provenance badge appears; from the command line:

```bash
uvx pypi-attestations verify pypi \
  --repository https://github.com/alejandro-cedeno-10/rayito \
  pypi:rayito-<v>-py3-none-any.whl
```

(`pypi:<file>` downloads the distribution and its attestations from PyPI and
checks that this repository's `release.yml` signed them.)

## npm package `rayito` { #paquete-npm-rayito }

It is published with npm trusted publishing (OIDC from `release.yml`,
`npm` environment), which attaches SLSA provenance with no extra
configuration:

```bash
npm view rayito@<v> dist.attestations
npm audit signatures        # in a project that installs it
```

## Which image runs { #que-imagen-corre }

The `rayito-base` image in your account is built with `make image-publish`
from `image/Dockerfile`, whose `FROM` is pinned by digest, and with an
explicit `--base-image-version` (`AWS_API_NOTES.md` §4 and Q52).
`get_health().agent_version` returns the version of `rayd` inside, the same
as the `rayd-v<v>` tag whose `rayito-image.zip` you verified above.
