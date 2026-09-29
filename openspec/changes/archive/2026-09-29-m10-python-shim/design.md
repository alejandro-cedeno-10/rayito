## Context

M10 group `python-shim` (`docs/research/2026-09-m9-architecture-review.md`,
finding "Python E2B shim: pure native-call resolution, integration
captured at the IO edge") gave four plan items. Item 1 is implemented in
this change. Items 2-4 are reported only, per the plan's explicit
instruction: each of them can only be "fixed" by changing an observable,
documented contract (an exception type callers catch, a new public
native API, or new warnings on calls that are silent today), which is out
of scope for a refactor and needs a user decision.

## Decision 1 — split "decide" from "build" at the `NativeCall` boundary

`native_call_kwargs` (`_compat.py`) used to both decide whether a new
shared control plane was needed **and** build it, by calling
`connection_overrides`, which called `shared_control_plane` — a boto3
session/client constructor. That made a module whose own docstring says
"Sin I/O" perform I/O, and made it read `ConnectionConfig.current_integration()`,
a mutable process-global, on every call.

The fix keeps the exact decision logic (same inputs, same
`CONTROL_PLANE_CONFLICT_MESSAGE`, same "`None` when nothing to build")
but returns it as data — `plane_settings: ClientSettings | None` on
`NativeCall` — instead of acting on it. A new function,
`bind_control_plane`, takes a `NativeCall` and, only if `plane_settings`
is not `None`, calls `shared_control_plane` and folds the result into
`kwargs["control_plane"]`. This is the only function in `_connection.py`
that does I/O; the module docstring says so.

`integration` moves from being read inside `native_call_kwargs` to being
a required parameter. The caller (`Sandbox._native_call` /
`AsyncSandbox._native_call`, both already IO code — they call the native
`Sandbox`) resolves it once via `ConnectionConfig.current_integration()`
and passes it in, then pipes the result through `bind_control_plane`
before `emit_warnings`. This is the "IO edge" the finding's title names:
the global read and the AWS-backed construction both now happen at the
same, single point, right before the native call, not inside a module
that claims to have none.

`ConnectionConfig`'s `_integration_snapshot` was being written from
outside the class by `snapshot_config` (`config._integration_snapshot =
integration`), reaching past the class's own encapsulation. The new
`ConnectionConfig._snapshot(...)` classmethod builds the config through
`cls(...)` (the normal, validated constructor) and sets the snapshot
attribute from inside the class body; `snapshot_config` just calls it.
The public `__init__` still has no `integration` parameter — passing one
still hits `**ignored` and raises `TypeError` via
`reject_unknown_params`, exactly as before this change. `set_integration`
/ `current_integration` remain the only mutation/read points of the
class variable itself (`ConnectionConfig._integration`); `_snapshot` only
reads the value the caller hands it, it does not touch the class
variable.

Net effect: `native_call_kwargs` and `plane_settings` are pure functions
of their arguments (proven in tests by monkeypatching
`shared_control_plane` to raise and confirming the pure paths never call
it, and by proving the result does not change when the process-global
`ConnectionConfig._integration` is set behind the caller's back).
`bind_control_plane` is the one IO function, proven to call
`shared_control_plane` exactly once with the same `(session, region,
settings)` triple as before, and to reuse the shared plane the same way
(`again is built`, same de-duplication contract as the pre-change
`connection_overrides`).

No behaviour changes: same kwargs dict is built, same order, same
warnings, same conflict message, same shared-plane reuse.

## Decision 2 — no spec delta

`openspec/specs/e2b-compat/spec.md` already states the requirement this
change preserves: "`retries`, `proxy` or an integration combined with an
explicit `control_plane`: this SHALL raise `InvalidArgumentException`."
The refactor does not change this text, the exception type, the message,
or any case that hits it — it changes which function inside the shim
performs the check and where the AWS-backed construction happens. Per
the plan's OpenSpec rule ("spec deltas only if a requirement text
changes"), no spec file is touched.

## Deferred findings (plan items 2-4) — report only, not implemented

### `py-exceptions-47` — three exception types for "the image/agent lacks this feature"

`UnimplementedError`, `LifecycleUnsupportedException(InvalidArgumentException)`
and a plain `InvalidArgumentException` (for a missing kernel) all
currently surface the same underlying condition. `is_history_unavailable`
(`_metrics_base.py:103`) already moved off string-matching the reason to
`isinstance(MetricsHistoryUnavailable)`, but the exception hierarchy
itself was left alone in that fix.

The clean fix considered — an `OutdatedAgentError(UnimplementedError)`
that both the missing-kernel case and lifecycle would raise — changes two
public contracts at once:

1. The missing-kernel case currently raises plain
   `InvalidArgumentException`; user code catching that today would stop
   catching an `OutdatedAgentError`.
2. `LifecycleUnsupportedException`'s base class would change from
   `InvalidArgumentException` to `UnimplementedError`; code catching
   `InvalidArgumentException` around a lifecycle call would stop
   catching it too.

Both are documented public contracts (`sandbox_sync/code.py:124` and the
exception hierarchy in `exceptions.py`), out of this change's file
ownership and out of scope for a "no public API/behaviour change"
refactor. **Not implemented.** Needs a user decision: accept the breaking
exception-type change (with a `CHANGELOG` entry and a migration note), or
leave the three types as they are.

### `py-async-396` — async `traffic_access_token` reaches into a private native method

`_async.py`'s `traffic_access_token` calls `self._native._current_jwe(DEFAULT_PORT)`
directly (a private method), while the sync twin (`_sync.py:626-631`)
goes through the public `get_host(8080).headers[PROXY_AUTH_HEADER]` path.
The two also differ in semantics: sync mints a token when none is
cached, async raises `SandboxException` instead.

Every clean fix considered changes observable behaviour:

- Adding a public accessor to the native `Sandbox`/`AsyncSandbox` trees
  is a new public API, and the sync/async semantics would still need to
  be reconciled (mint vs raise) — a decision, not a mechanical rename.
- Pre-warming the token via `await get_host(8080)` during launch/connect
  removes the private-method reach but adds a token mint on every
  create/connect, even for callers who never read
  `traffic_access_token` — extra AWS work by default.

Both are out of scope for this refactor (no public API addition, no new
IO on existing call paths). **Not implemented.** Needs a user decision on
which public surface to add and which semantics (mint vs raise) the
async side should match.

### `py-sync-118` — instance variants silently drop `ApiParams` that class variants apply

`request_timeout_of` (`_sync.py:119-124`) is used by the *instance*
`kill`/`pause`/`connect` (`_sync.py:515`, `:660`, `:833`, and the async
mirrors `_async.py:275`, `:421`, `:580`). Those instance variants validate
`headers`/`proxy`/`retries`/`request_timeout` (an invalid value still
raises) but then silently drop the valid ones, while the *class* variants
apply them. The module's own contract is "one `RayitoCompatWarning` per
non-applied param" — the instance variants violate it silently.

The clean fix — emit a `RayitoCompatWarning` for each dropped param on
the instance variants (or start applying `request_timeout`, the one that
would be free to honour) — is new, observable behaviour: a caller running
under `pytest -W error` (or any strict-warnings setup) that calls
`sbx.kill(retries=3)` today, silently, would start raising. This is a
behaviour change plan item 4 explicitly asked to report, not implement.

**Not implemented.** Needs a user decision: add the warnings (breaking
for `-W error` callers, but honest about the module's own contract), or
leave the instance variants silent and document the asymmetry instead.
