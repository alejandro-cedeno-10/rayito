## ADDED Requirements

### Requirement: The image artifact never ships rayd without its notices
`rayito.cli._artifact.image_files`, used by `write_zip` (`rayito image zip`, `scripts/image_zip.py`), SHALL raise `SystemExit` naming the missing path and `make image-licenses` when the image directory holds a `rayd` file but not all of `licenses/LICENSE`, `licenses/NOTICE` and `licenses/THIRD_PARTY_LICENSES.md`, before the archive is created. An image directory without `rayd` SHALL zip as before.

#### Scenario: notices not staged
- **WHEN** `rayito image zip image out.zip` runs on a tree with `Dockerfile` and `rayd` but no `licenses/THIRD_PARTY_LICENSES.md`
- **THEN** the command fails naming `licenses/THIRD_PARTY_LICENSES.md` and `make image-licenses`, and `out.zip` is not written

#### Scenario: notices staged
- **WHEN** the same tree also holds the three files under `licenses/`
- **THEN** the zip carries `licenses/LICENSE`, `licenses/NOTICE` and `licenses/THIRD_PARTY_LICENSES.md` next to `rayd`
