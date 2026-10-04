## ADDED Requirements

### Requirement: The image artifact builder ships regular files only
`rayito.cli._artifact.shipped_files` SHALL skip the excluded directories (also when the excluded directory itself is a symlink, such as a relocated `.venv`) and SHALL raise `SystemExit` naming the path for any other symlink, to a file or to a directory, under the walked root, so that `write_zip` (`rayito image zip`, `scripts/image_zip.py`) and `copy_tree`/`copy_sidecar` (`scripts/copy_sidecar.py`, `make image-publish`) never read through a link. A symlink would otherwise ship the bytes of an arbitrary file of the builder's machine into every sandbox image, readable by uid 1000 after the Dockerfile's `chmod -R a+rX`.

#### Scenario: a link to a file outside the tree
- **WHEN** `kernel-sidecar/leak.txt` is a symlink to a file in the builder's home and the zip is built
- **THEN** the build stops with an error that names the link, and no archive entry carries the target's bytes

#### Scenario: a relocated virtualenv
- **WHEN** `kernel-sidecar/.venv` is a symlink to a directory elsewhere
- **THEN** the copy skips it like any other `.venv` and ships the same files as without it
