## ADDED Requirements

### Requirement: Every download in a Dockerfile is verified against a pinned sha256
`scripts/check_pins.py` SHALL run a third gate over `image/Dockerfile` (added to its default paths) and over any file named `Dockerfile` it is given. The gate joins backslash-continued lines into instructions and skips comment lines. Every instruction that contains `curl` SHALL:
- assign a `<NAME>_SHA256=` value of exactly 64 lowercase hex characters;
- contain `sha256sum -c`;
- name no floating release (`/releases/latest` or `/latest/download/`).

Any other `curl` instruction SHALL be reported as `KO <path>:<line>` with the reason `la descarga no está verificada contra un sha256 fijado`, and the script SHALL exit 1. The gate SHALL use only the standard library and no network, like the other two gates, and CI and `make lint` SHALL keep running the script.

#### Scenario: the real tree passes
- **WHEN** `python scripts/check_pins.py` runs from the repository root after the Deno layer landed
- **THEN** it prints `OK` naming the checked files, `image/Dockerfile` among them, and exits 0

#### Scenario: unpinned downloads are findings
- **WHEN** the unit test runs `unpinned_downloads` over instructions with a `curl` and no `_SHA256=`, with a sha256 but no `sha256sum -c`, with a 63-hex sha256, and with a `releases/latest` URL
- **THEN** each yields one finding with the download reason, while the Deno instruction of the Dockerfile and a commented-out `curl` line yield none
