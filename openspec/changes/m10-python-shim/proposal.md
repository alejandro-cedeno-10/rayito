## Why

`docs/research/2026-09-m9-architecture-review.md` flagged the E2B compat
shim's connection-resolution path (finding "Python E2B shim: pure
native-call resolution, integration captured at the IO edge", M10 group
`python-shim`): `clients/python/src/rayito/e2b/_compat.py` documents
itself as "Sin I/O" ("no I/O", so its tables are testable without AWS or
`rayd`), but `native_call_kwargs` (`_compat.py:364-400`, pre-change line
numbers) read the process-global `ConnectionConfig.current_integration()`
at `:383` and called `connection_overrides`, which in turn called
`shared_control_plane` (`_connection.py:253`) — a boto3-backed control
plane constructor. A module documented as pure was doing I/O and reading
mutable global state, both when a caller (or a future test) expected pure
computation from it. Separately, `_connection.py`'s `snapshot_config`
(pre-change `:347-365`) reached into `ConnectionConfig` from the outside
and wrote its private `_integration_snapshot` attribute directly, instead
of going through the class's own construction path.

This proposal (plan item 1, `py-compat-366+py-connection-296`) makes the
IO/pure boundary real: `native_call_kwargs` now only decides what a call
needs (including whether a new control plane must be built, captured as
data in `plane_settings`), and a new, explicitly-IO function,
`bind_control_plane`, is the only place that calls `shared_control_plane`.
`integration` becomes a required parameter the caller passes in, resolved
once at the call site (`Sandbox._native_call` / `AsyncSandbox._native_call`)
via `ConnectionConfig.current_integration()` — the global read now happens
at the IO edge, not inside the "pure" module. `ConnectionConfig`'s private
attribute is now written only from inside the class, through a new
`_snapshot` classmethod that `snapshot_config` calls.

No public signature of `ConnectionConfig`, `Sandbox`, `AsyncSandbox` or
`E2B` changes, and no observable behaviour changes: same kwargs are
built, in the same order, with the same `CONTROL_PLANE_CONFLICT_MESSAGE`
raised in the same situations, and the same shared-plane de-duplication
by `(session, region, settings)`. This is a refactor with tests proving
the boundary, not a behaviour change.

Three further findings from the same review group (plan items 2-4:
`py-exceptions-47`, `py-async-396`, `py-sync-118`) are **report-only** in
this change — each one needs a user decision because fixing it changes a
documented public contract (which exception type callers catch, a new
public native accessor, or new warnings on calls that are silent today).
They are written up in `design.md` under "Deferred findings" with the
options considered, not implemented, per the plan's explicit instruction
to report rather than improvise when a fix would require a behaviour
change.

## What Changes

- **`_compat.py`**: `native_call_kwargs` takes `integration` as a required
  keyword, no longer reads `ConnectionConfig.current_integration()` nor
  calls into AWS; it returns a `plane_settings: ClientSettings | None` on
  the (still frozen) `NativeCall` dataclass instead of a resolved
  `control_plane`, when a new plane would be needed.
- **`_connection.py`**: `connection_overrides` is split into two pure
  helpers, `transport_override` and `plane_settings`, plus a new IO
  function `bind_control_plane` that is the module's only caller of
  `shared_control_plane`. `ConnectionConfig` gains a `_snapshot`
  classmethod that builds the config and sets `_integration_snapshot`
  from inside the class; `snapshot_config` now calls it instead of
  writing the private attribute from outside. The public
  `ConnectionConfig.__init__` keeps rejecting an `integration` kwarg with
  `TypeError`, unchanged.
- **`_sync.py` / `_async.py`**: `Sandbox._native_call` and
  `AsyncSandbox._native_call` now read `ConnectionConfig.current_integration()`
  themselves and pipe the pure result through `bind_control_plane` before
  emitting warnings — the IO edge moves from inside `_compat.py` to these
  two call sites, which already do IO (they call the native `Sandbox`).
- No spec deltas: `openspec/specs/e2b-compat/spec.md`'s existing
  requirement ("`retries`, `proxy` or an integration combined with an
  explicit `control_plane` … SHALL raise `InvalidArgumentException`") is
  unchanged in text and in behaviour; this is an internal refactor of how
  that requirement is implemented.
- Deferred findings (plan items 2-4) reported in `design.md`, not
  implemented; `MILESTONES.md` is not touched.

## Impact

- Affected code: `clients/python/src/rayito/e2b/_compat.py`,
  `_connection.py`, `_sync.py`, `_async.py`.
- Affected specs: none (no requirement text changes).
- Affected tests: `clients/python/tests/unit/test_e2b_v2_base.py` (new
  purity/conflict/reuse tests for `native_call_kwargs`, `plane_settings`,
  `bind_control_plane`, `snapshot_config`); existing pins in
  `test_e2b_v2_sync.py` / `test_e2b_v2_async.py` (class and instance
  variants, `CONTROL_PLANE_CONFLICT_MESSAGE`, `set_integration`) continue
  to pass unmodified, proving the refactor is behaviour-preserving.
