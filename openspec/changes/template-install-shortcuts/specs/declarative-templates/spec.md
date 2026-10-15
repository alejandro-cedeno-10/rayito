## ADDED Requirements

### Requirement: dnf_install and npm_install add quoted, validated install steps
`dnf_install`/`dnfInstall` and `npm_install`/`npmInstall` SHALL each append one `RUN` step whose package names are quoted for the shell. `dnf_install` SHALL run `dnf install -y --setopt=install_weak_deps=0 <names> && dnf clean all`. `npm_install(packages=None, g=False)` / `npmInstall(packages?, { g })` SHALL run `npm install --no-audit --no-fund` followed by `-g` when `g` is true and by the names when `packages` is given; without `packages` it SHALL run a plain `npm install` in the current WORKDIR. In Python `g` SHALL also be accepted positionally. An empty list, an empty name or a name starting with `-` SHALL raise `InvalidArgumentException`/`InvalidArgumentError` before any AWS call. The async builder and the `rayito.e2b` shim SHALL expose both methods.

#### Scenario: names are quoted
- **WHEN** `dnf_install("tree; rm -rf /")` is called
- **THEN** the step contains `'tree; rm -rf /'` as one quoted word

#### Scenario: option-like or empty names are rejected
- **WHEN** `npm_install(["--help"])` or `dnf_install([])` is called
- **THEN** `InvalidArgumentException` is raised and no step is added

#### Scenario: npm_install without packages
- **WHEN** `npm_install()` is called
- **THEN** the step ends with `npm install --no-audit --no-fund` and names no package

#### Scenario: async builder and E2B shim inherit the methods
- **WHEN** `AsyncTemplate().dnf_install("tree")` or `rayito.e2b.Template().npm_install("x")` is called
- **THEN** the returned builder keeps its class and holds the step

### Requirement: dnf_install and global npm_install run as root
`dnf_install` and `npm_install(g=True)` SHALL run as root, like `apt_install` and `npm_install(g=True)` in E2B. When the latest `set_user()` is neither `root` nor `0`, the builder SHALL emit `USER root`, the `RUN` step and `USER <previous user>`; otherwise it SHALL emit the `RUN` step alone. A local `npm_install` SHALL keep the current user.

#### Scenario: a non-root user is restored
- **WHEN** `set_user("user").dnf_install("tree")` is called
- **THEN** the steps are `USER user`, `USER root`, the `RUN`, `USER user`

#### Scenario: already root adds no user step
- **WHEN** `dnf_install("tree")` is called with no `set_user()` or after `set_user("root")`
- **THEN** only the `RUN` step is added

### Requirement: npm_install fails clearly when the image has no npm
`npm_install`/`npmInstall` steps SHALL start with a `command -v npm` guard. Without npm the step SHALL exit with code 127 and a Spanish message that names `dnf_install(["nodejs20", "nodejs20-npm"])`. The Node.js packages are `nodejs20` and `nodejs20-npm`, measured on the pinned base image.

#### Scenario: build on an image without Node.js
- **WHEN** a template with `npm_install("x")` and no Node.js builds on `rayito-base`
- **THEN** `BuildException`/`BuildError` carries `exit_code`/`exitCode` 127

## MODIFIED Requirements

### Requirement: only fromBaseImage() is a supported base source in 0.6

`from_image`/`fromImage`, `from_template`/`fromTemplate`,
`from_dockerfile`/`fromDockerfile` and `from_gcp_registry`/
`fromGcpRegistry` SHALL raise `UnimplementedError` immediately, before any
I/O, naming the method and the reason (injecting `rayd` into an external
base has no supported path in 0.6). `apt_install`/`aptInstall` SHALL raise
`UnimplementedError` naming `dnf_install`/`dnfInstall` as the Amazon Linux
2023 equivalent.

#### Scenario: fromImage raises before any I/O
- **WHEN** `Template().fromImage(...)` is called
- **THEN** `UnimplementedError` is raised naming `Template.fromImage`,
  and no file is read and no AWS call is made
