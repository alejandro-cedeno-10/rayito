## Context

The builder compiles each DSL call into `WireStep`s; `UserStep` sets the user
of the following `RunStep`s, and the renderer returns to `USER root` before
the final `CMD`.

## Decisions

- **D1 — root wrap.** E2B runs `apt_install` and `npm_install(g=True)` as
  root. A plain `RunStep` would run as whatever `set_user()` left in place and
  fail (dnf needs root; `npm -g` writes to `/usr/lib/node_modules`). The
  builder looks at the latest `UserStep`; if it is not `root` or `0`, it emits
  `UserStep("root")`, the `RunStep` and `UserStep(<previous>)`. A local
  `npm_install` keeps the current user: it writes in the WORKDIR.
- **D2 — E2B signature.** `packages` is optional (a plain `npm install` reads
  the `package.json` copied earlier) and `g` is accepted positionally in
  Python. In TypeScript `g` goes in an options object, like the rest of the
  SDK.
- **D3 — Node.js packages.** `nodejs20` + `nodejs20-npm`. Measured
  2026-10-14 with `docker run` on the pinned al2023-minimal image: Node
  20.20.2 and npm 10.8.2 with `node`/`npm` in the PATH through alternatives;
  plain `nodejs` is Node 18 (EOL). The set is one named constant per SDK.
- **D4 — dnf flag.** `--setopt=install_weak_deps=0`: this dnf rejects
  `install_weak_deps=False` with `Invalid boolean value`. Same value as
  `image/Dockerfile`.
- **D5 — missing npm.** `command -v npm` guard that exits 127 with a message,
  instead of `npm: not found`.
- **D6 — escaping.** Python `shlex.quote`; TypeScript a port with the same
  output, so both SDKs emit the same step.
