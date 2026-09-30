## Why

Two loose ends from the research read for M11–M14
(`docs/research/2026-10-e2b-out-of-scope.md` §4, §5, §7) stay open after
0.4.0:

- **Size (§4, option C).** E2B fixes a sandbox's CPU/RAM at
  `Template.build(cpu_count=, memory_mb=)`, not at `Sandbox.create()`.
  Rayito already has the same shape — `create-microvm-image`'s
  `resources[0].minimumMemoryInMiB` (`AWS_API_NOTES.md` §4) — and the CLI
  already has `rayito image publish --memory-mib` to set it, but nothing
  says so anywhere a reader would look: `limits.md` has no size section,
  `e2b-parity.md` row 82 (`cpu_count`/`memory_mb` por sandbox) reads "fuera
  por SPEC" as if E2B had no equivalent at all, and `e2b-compat.md` folds
  the cpu/memory footnote into an unrelated "CLI de templates" row. Nobody
  has proposed a size catalog or a `resources=` resolver (that stays out of
  scope, M15 at the earliest); this change only writes down the mechanism
  that already exists.
- **Local port access for development (§5, §7, option D).** E2B's "expose a
  port on a domain" recipe has no Rayito equivalent yet, and `e2b-parity.md`
  row 110 says so with a stale rationale (a hosted reverse proxy would need
  to hold IAM credentials and mint JWEs per request — true, and still out of
  scope — but it does not mention the one piece that is both useful and
  already inside the existing security model: minting the same
  `create-microvm-auth-token` JWE the SDK's own transport uses, scoped to a
  single port, and piping it through a local TCP listener). There is no CLI
  command to reach a guest port from a developer's machine without opening
  a full PTY session.

## What Changes

- **Docs: size is a property of the image.** `limits.md` gains a "## Tamaño
  (CPU/RAM)" section (before "## Compatibilidad SDK ↔ rayd ↔ imagen"): the
  `AWS_API_NOTES.md` §4 table (512/1024/2048/4096/8192 →
  baseline/pico/disco/ancho de banda), attributed to AWS's own
  documentation rather than a measurement (only the 2048 bandwidth, §7, and
  the `Q68` guest view carry a "medido" label), plus a `$/h` baseline
  column derived from §12/`cost.md`; the E2B analogy
  (`Template.build(cpu_count=, memory_mb=)`); how to choose a size today
  (`rayito image publish --memory-mib N --image-name <sufijo>` + create
  against that image); the cost angle (AWS bills the baseline row per
  second while `RUNNING`, and separately bills anything consumed above
  baseline at the vCPU/GB actually used, not the next size's price, plus
  per-version image storage); and the guest-view caveat
  (`SandboxInfo.cpu_count`/`memory_mb` report what the guest sees — the peak
  of the range, measured `Q68` — not the contracted baseline, so `nproc`-
  driven parallelism can cross it and pay for the excess, not just run
  faster). `images.md`'s "Publicar las tres" section gets a short
  cross-reference, without calling the table "measured". No catalog, no
  resolver, no `--memory-mib` validation: those stay out of scope.
- **Docs: e2b-parity.md rows 82 and 110, and the status-count table.** Row
  82 flips from "fuera por SPEC" to "divergente" with the per-image
  alternative and a `limits.md#tamano-cpuram` link; row 110 stays "fuera por
  SPEC" (ADR-014, owned by the sibling `m11-optin-adr` change, keeps hosted
  ingress out of scope) but its note now points at the CLI proxy for local
  development instead of repeating the stale IAM-credentials rationale. The
  status-count table (18 divergente, 12 fuera por SPEC, unchanged 72
  implementado / 11 imposible) and `e2b-compat.md`'s cpu/memory footnote
  move in lockstep, so `scripts/tests/test_m9_docs.py` stays green (113
  rows, valid statuses).
- **CLI: `rayito sandbox proxy <id> --port N`.** A new command,
  `clients/python/src/rayito/cli/_proxy.py` (pure helpers + an asyncio
  server) wired into `rayito/cli/sandbox.py`. `GetMicrovm` resolves the
  endpoint (a terminated sandbox is an error); the JWE is minted with the
  existing `TokenRefresher`/`TokenStore` of `rayito._transport` (the same
  45-minute refresh the SDK's own gRPC channel uses), always
  `PortSpec.single(N)` — port 9000 (the hooks port, ADR-006) and anything
  outside 1–65535 are rejected before any AWS call. The listener speaks
  HTTP/1.1: per connection it reads only the request header (64 KiB cap,
  `asyncio.StreamReader`'s own limit), strips any client-supplied
  `x-aws-proxy-*`, sets `Host: <endpoint>`, adds `X-aws-proxy-auth` and
  `X-aws-proxy-port`, forces `Connection: close` except on an `Upgrade`
  request (left untouched for WebSocket), opens TLS to `<endpoint>:443`
  (SNI = endpoint) and pipes both directions until either side closes. No
  new dependency: stdlib plus the SDK's own transport primitives, inside
  the existing `rayito[cli]` extra. `--bind` outside loopback requires
  `--allow-remote` (a usage error otherwise) and prints a warning. The
  command never logs the JWE, headers, bodies or paths.
- **Docs: SECURITY.md / security.md.** T2 (hook-port integrity) and T3
  (proxy JWE) each gain an M12 appendix: the proxy always scopes to one
  port, never mints for 9000, binds to loopback by default, strips
  client-sent `x-aws-proxy-*`, and never logs the JWE; residual risk (no new
  mitigation, no new threat number): any local process reaching the
  forwarded port uses the sandbox with the operator's access while the
  proxy runs. `docs/site/docs/security.md` gets the matching prose section.

## Migración

No behavior changes for existing SDK code paths: `Sandbox.create()`,
`connect()`, and every other API keep their exact signatures and defaults.
`rayito image publish --memory-mib` already existed in 0.4.0 unchanged; this
change only documents it. The new `rayito sandbox proxy` command is opt-in
(nothing runs unless invoked) and additive to the `sandbox` command group;
no existing command's argv or output changes.

## Coste

- **Docs:** none. No new AWS resource, no new SDK call.
- **CLI proxy:** $0 of AWS beyond `GetMicrovm` and `CreateMicrovmAuthToken`
  (both free; the account/region 50 TPS quota applies, and a long-running
  proxy re-mints roughly once every 45 minutes). Waking a `SUSPENDED`
  sandbox with `auto_resume` on the first proxied request bills its compute
  like any other resume — this is stated in the CLI and security docs, not
  a new cost this change introduces.
- **IAM:** `lambda:GetMicrovm` and `lambda:CreateMicrovmAuthToken`, already
  granted by the caller policy in `infra/iam.yaml`. No new permission.

## Capabilities

### New Capabilities

- `sizing-docs`: image size is documented as a property of the image
  (`resources[0].minimumMemoryInMiB`), not a `Sandbox.create()` parameter,
  with the verified size table, the E2B analogy, the cost angle and the
  guest-view caveat; the e2b-parity ledger reflects it without overstating
  scope.

### Modified Capabilities

- `cli`: ADDED `rayito sandbox proxy` — a local, no-AWS-cost TCP proxy onto
  a single guest port, reusing the SDK's own JWE minting and refresh.
- `security-docs`: ADDED the M12 appendix to `SECURITY.md` T2/T3 and to
  `docs/site/docs/security.md`, describing the proxy's JWE scope and its
  residual risk.

## Impact

- **Docs:** `docs/site/docs/limits.md`, `docs/site/docs/images.md`,
  `docs/site/docs/e2b-parity.md`, `docs/site/docs/e2b-compat.md`,
  `docs/site/docs/cli.md`, `docs/site/docs/security.md`, `SECURITY.md`.
- **Python CLI:** `clients/python/src/rayito/cli/_proxy.py` (new),
  `clients/python/src/rayito/cli/sandbox.py`.
- **Tests:** `clients/python/tests/unit/cli/test_proxy.py` (new),
  `clients/python/tests/e2e/test_sandbox_proxy_e2e.py` (new, `@pytest.mark.e2e`,
  skipped without `RAYITO_E2E=1`), `scripts/tests/test_m12_docs.py` (new).
- **No changes** to `crates/`, proto, `limits.json`, `optional-features.md`
  (owned by the sibling `m11-optin-adr` change), volumes, templates, the
  size catalog/`resources=` resolver, CloudFront/gateway, the rayd OTLP
  exporter, the secret loopback gateway, or `ConfigureSandbox`.
