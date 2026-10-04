## Context

`rayd` serves the six lifecycle hooks on `0.0.0.0:9000` in the network
namespace it shares with every sandbox process (`SECURITY.md` T2). The
source address cannot tell the platform from the sandbox (both arrive from
`127.0.0.1`), and AWS shares no per-boot secret. `docs/SECURITY_AUDIT.md`
C-01/C-02/C-03 deferred three code fixes; the 0.6.1 sweep confirmed them
(RAYITO-SEC-03, RAYITO-SEC-05) and added a gateway finding (RAYITO-SEC-04).

## Decisions

### D1. The `run_claimed` guard lives in the domain decision

`validate_hook_decision(state, run_claimed)` returns `AfterRun` before it
looks at the validation state, so no state (`Idle`, `Running`, `Done`) can
ever start the cell after `/run`. The adapter maps `AfterRun` to 200
`validate_skipped` plus `audit(.., Anomalous)`. `execute_unchecked` stays:
after the guard it is reachable only before `/run`, when no client stream
exists. `/ready` needs no new variant: `LifecycleState::ready` already
refuses every phase after `Running`, so the handler short-circuits on
`run_claimed()` and audits.

### D2. Peer identity comes from the kernel's socket table

The client end of a loopback connection is listed in `/proc/net/tcp` (or
`tcp6` for a dual-stack socket, as `::ffff:127.0.0.1`) with its owner uid.
`find_in_proc_net` is pure (the adapter passes the text); addresses are
decoded word by word with native endianness, which is how the kernel
prints them on the host that reads them.

Classification (`classify_peer`):

| Socket | Origin |
|---|---|
| owned by a uid in `SANDBOX_UID_MIN..=SANDBOX_UID_MAX` (and not `rayd`'s own euid), any state | `sandbox` |
| owned by another uid, `ESTABLISHED` | `platform` |
| owned by another uid, any other state (closing, orphaned: the uid may read as 0) | `unverified` |
| not listed, no peer address, lookup failed or exceeded 1 s | `unverified` |

`rayd`'s own euid is excluded so an unprivileged developer run (where the
sandbox runs as `rayd`'s uid and there is no boundary) never refuses its
own hooks. The numeric bounds sit next to `SANDBOX_UID_RANGE` with a test
that pins both spellings.

### D3. What each origin does (`peer_action`)

| Hook | `sandbox` | `unverified` | `platform` |
|---|---|---|---|
| `/terminate` | refuse (200 `peer_refused`, audited) | honour, count | honour |
| `/validate` | refuse (200 `peer_refused`, audited) | honour | honour |
| `/suspend`, `/resume` | honour, count | honour | honour |
| `/ready`, `/run` | honour | honour | honour |

- Refusing never means a non-2xx: a non-2xx runtime hook terminates the VM,
  which is exactly what a forged `/terminate` wants.
- An unverified `/terminate` is honoured because a lookup can miss a genuine
  call; counting it makes a caller that dodges the lookup (closing or
  resetting its socket before `rayd` looks) visible in `hook_anomalies`.
- `/suspend` and `/resume` are never refused (hook-defense: a refused
  genuine `/suspend` leaves a real checkpoint unprepared); refusing them
  needs the platform caller's uid measured on real AWS first.
- `/ready`, `/run` and a non-refused `/validate` are not counted by the
  middleware: their handlers already count every call after `/run`, and
  before it nothing counts (`HookAudit::note_anomaly_after_run`).

### D4. The check is a layer, not a field of `HookServices`

`guard_peers(router, session, PeerGuard)` wraps the router with an axum
middleware; `main.rs` applies it and serves with
`into_make_service_with_connect_info::<SocketAddr>()`. In-process tests and
the integration harness keep using the bare router (no peer address), so
none of their constructors change.

### D5. Gateway responses: drop headers that can carry the credential

`must_drop_from_response(route, name, value)`: hop-by-hop, a name the route
injects, or a value containing a vaulted value of at least
`MIN_REFLECTED_VALUE_LEN` (8) bytes. Shorter values are matched by name only
so a short secret can never strip `content-type` or `content-encoding`. The
body is not inspected (it may be compressed, chunked or encoded, so
scanning it would give a false guarantee); the documented guarantee becomes
"cannot read it unless the allowlisted upstream reflects it", and the docs
tell operators never to allow echo or debug endpoints.

## Risks / Trade-offs

- If the platform's hook caller turns out to own its socket as a uid in
  1000-65535, a genuine `/terminate` would be ignored (the platform
  terminates the VM anyway; `on_terminate` participants would not run) and
  every genuine `/suspend`/`/resume` would count as an anomaly. Q48 measured
  the agent's sockets as uids 991-994; the e2e on a rebuilt image is listed
  as follow-up.
- A sandbox process that resets its connection right after sending
  `/terminate` can make the lookup miss: the call is honoured but counted.
