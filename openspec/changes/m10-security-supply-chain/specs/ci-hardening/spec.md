## ADDED Requirements

### Requirement: Dockerfile pip installs are hash-pinned, dependency-free and wheels-only
`scripts/check_pins.py` SHALL run a fifth gate. Every `pip install`/`python3[.x] -m pip install` instruction of `image/Dockerfile` (and any file named `Dockerfile` it is given) that names a requirements file with `-r`/`--requirement` SHALL carry `--require-hashes`, `--no-deps` and `--only-binary=:all:` (`--only-binary all` and `--only-binary=:all:` both count); any such instruction missing one of the three SHALL be reported as `KO <path>:<line>` with the reason naming the missing flags, and the script SHALL exit 1. Separately, every pin (`name==version`, its `--hash=` continuation lines joined the same way a Dockerfile's `\`-continued instruction is) of a file whose name starts with `requirements` and ends in `.txt` SHALL carry at least one `--hash=sha256:` of 64 lowercase hex characters; a pin without one SHALL be reported the same way. `kernel-sidecar/requirements.txt` and `kernel-sidecar/requirements-poly.txt` SHALL be added to the gate's default paths so a bare `python3 scripts/check_pins.py` covers both without arguments.

#### Scenario: an unflagged pip install is a finding
- **WHEN** the gate runs over a Dockerfile instruction `RUN pip install --no-cache-dir -r requirements.txt`
- **THEN** it is reported with the missing-flags reason and the gate exits 1

#### Scenario: the three flags pass
- **WHEN** the gate runs over `RUN pip install --require-hashes --no-deps --only-binary=:all: -r requirements.txt`
- **THEN** nothing is reported

#### Scenario: an unhashed pin is a finding
- **WHEN** the gate runs over a requirements file containing `unhashed-package==1.2.3` with no `--hash=` line
- **THEN** it is reported with the unhashed-pin reason

#### Scenario: the real sidecar pins are clean
- **WHEN** `python3 scripts/check_pins.py` runs over `image/Dockerfile`, `kernel-sidecar/requirements.txt` and `kernel-sidecar/requirements-poly.txt`
- **THEN** it exits 0, naming all three files
