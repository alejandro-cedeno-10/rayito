## Why

Templates ported from E2B call `apt_install`, which `rayito-base` (Amazon
Linux 2023) cannot honour, and `npm_install`, which does not exist in the
DSL. Users had to write `run_cmd("dnf install -y ...")` and a hand-made npm
line, repeating the escaping, the weak-dependency flag and the cache cleanup
that the base image layers already use.

## What Changes

- `Template.dnf_install(packages)` / `dnfInstall(packages)`: one `RUN` that
  runs `dnf install -y --setopt=install_weak_deps=0 <names> && dnf clean all`.
- `Template.npm_install(packages=None, g=False)` / `npmInstall(packages?,
  { g })`: the E2B signature. Without packages it runs a plain `npm install`
  in the current WORKDIR; with `g` it installs globally.
- Both quote every package name for the shell and reject, before any AWS
  call, an empty list, an empty name or a name starting with `-`.
- Both run as root, like E2B's `apt_install` and `npm_install(g=True)`: when
  a previous `set_user()` left another user, the step is wrapped in
  `USER root` and `USER <previous>`.
- `npm_install` guards on `npm`: without it the step fails with exit code 127
  and a Spanish message that names the Node.js packages (`nodejs20`,
  `nodejs20-npm`, measured on the pinned base image).
- `apt_install` keeps raising `UnimplementedError`, now naming `dnf_install`.
- The `rayito.e2b` shim inherits all of it (it subclasses the native builder).

## Impact

- Python and TypeScript SDKs (`_templates/_dsl.py`, `templates/dsl.ts`), docs
  (`funciones-opcionales/templates.md`) and CHANGELOGs.
- No `rayd` change, no new AWS call, nothing costs by default: the steps only
  run when `Template.build()` is called.
