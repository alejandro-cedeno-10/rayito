## Why

The 0.6.1 security sweep confirmed three sandbox-isolation findings, all
reachable from code running inside the sandbox (uid 1000) and none crossing
to another sandbox:

- **RAYITO-SEC-03** (low; audit rows C-02 and C-03): `/validate` is a build
  hook that runs on a throwaway VM, so every launched sandbox starts with
  its validation state `Idle`. A `/validate` posted to the hooks listener
  after `/run` therefore restarted the operator's `default` kernel context
  once per boot and ran the validation cell past the stream gate, with no
  `hook_audit` line and no `hook_anomalies` increment. `/ready` after `/run`
  was not audited either.
- **RAYITO-SEC-05** (info; audit row C-01): the hooks listener has no
  authentication against an in-VM origin, so a sandbox process can post
  `/terminate` (the VM goes with `rayd`) or `/validate`. The source address
  is useless (everything arrives from `127.0.0.1`), but the kernel's socket
  table says which uid owns the client end of each connection.
- **RAYITO-SEC-04** (low): the secret gateway strips the injected header
  from the inbound request, but streams the upstream's whole response back.
  An allowed endpoint that reflects request headers (an echo or debug
  route, a verbose error page) hands the vaulted credential to the sandbox,
  and the documented guarantee ("puede usarlo; no puede leerlo") did not
  say so.

## What Changes

- **Build hooks after `/run`** (`rayd`): `validate_hook_decision` takes
  `run_claimed` and answers `ValidateDecision::AfterRun` once `/run` is
  accepted, whatever the validation state; the handler answers 200
  `validate_skipped` without touching the kernel and audits the call as an
  anomaly. A `/ready` after `/run` answers 200 (`illegal`, the phase
  machine already refuses it) and is audited as an anomaly too. Before
  `/run` nothing is counted, so the build's genuine calls never leave a
  count in the memory snapshot.
- **Peer-uid check on the hooks listener** (`rayd_core::hook_peer`,
  `rayd::hooks::guard_peers`, `adapters::proc_net_peers`): the listener is
  served with connection info; a middleware looks the peer address up in
  `/proc/net/tcp` and `/proc/net/tcp6` (IPv4-mapped forms included) and
  classifies the caller as `platform` (an established socket owned by a uid
  outside `SANDBOX_UID_RANGE`), `sandbox` (owned by a uid inside it) or
  `unverified`. `/terminate` and `/validate` from a sandbox uid answer 200
  `peer_refused` without running and are audited as anomalies; an
  unverified `/terminate` is honoured but counted; a `/suspend` or `/resume`
  from a sandbox uid is honoured but counted (refusing it stays unsafe
  while the uid of the platform's hook caller is unmeasured). No hook ever
  answers non-2xx because of the check.
- **Gateway response headers** (`header_template::must_drop_from_response`,
  `listener::redact_response_headers`): hop-by-hop headers, the route's
  injected names and any header whose value contains a vaulted value (8
  characters or more) are dropped from the upstream response. The body is
  still streamed unchanged.
- **Docs**: `SECURITY.md` T2 (peer-uid check, build hooks audited, the
  residual of a caller that dodges the lookup), T17 (C-01 no longer
  deferred), a new T24 row for the secret gateway with the qualified
  guarantee, `ARCHITECTURE.md` (hook table, "Origen de los hooks", ADR-006,
  ADR-023), `docs/SECURITY_AUDIT.md` §9 (C-01/C-02/C-03 code rows, removed
  from "Lo que sigue abierto"), `docs/site/docs/security.md` and
  `funciones-opcionales/pasarela-de-secretos.md` (warning: never allow an
  endpoint that reflects request headers), `crates/rayd/CHANGELOG.md`.

## Capabilities

### Modified Capabilities

- `hook-defense`: the origin requirement now allows a peer-uid check; the
  audit covers build hooks after `/run`; a new requirement for refusing
  `/terminate` and `/validate` from a sandbox uid.
- `security-docs`: T2 no longer calls the peer-uid check pending; new T24
  requirement.
- `secret-gateway`: response headers never carry a vaulted value back.

## Impact

`rayd` only (`rayd-core` and `rayd`): no proto, SDK, CLI or infra change,
no new dependency and no cost. A new `rayd` ships in the next image; a
sandbox on an older image keeps the old behaviour. Not verified on real AWS
in this change: the uid that owns the platform's hook connections (inferred
from the agent sockets measured in Q48), and the forged-hook e2e
(`tests/e2e/test_m6_hardening.py`) against a rebuilt image.
