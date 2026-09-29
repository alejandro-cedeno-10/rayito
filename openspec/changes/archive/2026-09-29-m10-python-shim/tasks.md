## 1. Pure `native_call_kwargs`, IO moved to `bind_control_plane`

- [x] 1.1 Read `_compat.py:364-400` and `_connection.py:253,294,347-365` to confirm the exact IO/global-state paths (`current_integration()`, `shared_control_plane`, the private `_integration_snapshot` write from outside the class)
- [x] 1.2 `_connection.py`: split `connection_overrides` into `transport_override(settings, transport)` (the `touches_transport` branch, unchanged) and `plane_settings(settings, *, control_plane, integration)` (same `None`/conflict/`ClientSettings` decision, no construction)
- [x] 1.3 Grep for other importers of `connection_overrides` before deleting it (none outside this module and its tests) and delete it
- [x] 1.4 `_compat.py`: add `plane_settings: ClientSettings | None` to the frozen `NativeCall` dataclass; update its docstring
- [x] 1.5 `_compat.py`: `native_call_kwargs` takes `integration` as a required keyword, drops the `ConnectionConfig`/`shared_control_plane` imports, keeps the exact original order (`reject_unknown_params`, bound split, `merge_bound_params`, `split_api_params`, transport override, then the control-plane decision), and leaves `control_plane` out of `kwargs` when a new plane is needed (still set through when the caller passed one and none must be built)
- [x] 1.6 `_connection.py`: new `bind_control_plane(call: NativeCall) -> NativeCall`, the module's only caller of `shared_control_plane`
- [x] 1.7 `_connection.py`: `ConnectionConfig._snapshot(...)` classmethod builds through `cls(...)` and sets `_integration_snapshot` inside the class; `snapshot_config` calls it instead of writing the attribute from outside; `ConnectionConfig.__init__` keeps rejecting an `integration` kwarg with `TypeError` (no new public parameter)
- [x] 1.8 `_sync.py` / `_async.py`: `Sandbox._native_call` / `AsyncSandbox._native_call` call `bind_control_plane(native_call_kwargs(..., integration=ConnectionConfig.current_integration(), ...))`, then `emit_warnings` as before
- [x] 1.9 `_compat.py` module docstring: keep "Sin I/O" (still true: no import of `ConnectionConfig` or `shared_control_plane` remains)
- [x] 1.10 Unit tests in `test_e2b_v2_base.py`:
  - [x] 1.10.a `native_call_kwargs` with `retries`/`proxy`/`integration` set returns the expected `plane_settings` while `rayito.e2b._connection.shared_control_plane` is monkeypatched to raise (purity)
  - [x] 1.10.b the result is independent of `ConnectionConfig.set_integration(...)` global state (set a global, pass `integration=None`, expect `plane_settings is None`)
  - [x] 1.10.c `bind_control_plane` calls `shared_control_plane` exactly once with `session`/`region`/`settings` and passes the call through unchanged when `plane_settings is None`
  - [x] 1.10.d conflict: `control_plane` given + `retries` raises the same `InvalidArgumentException` message, before any plane is built (`shared_control_plane` monkeypatched to raise)
  - [x] 1.10.e `snapshot_config(...).integration` equals the passed value; `ConnectionConfig(integration="x")` still raises `TypeError`
- [x] 1.11 Update/rewrite the existing `connection_overrides`-based tests to call `transport_override` / `plane_settings` / `bind_control_plane` directly, keeping every original assertion (transport metadata/proxy, shared-plane reuse `again is plane`, the three-way conflict table)
- [x] 1.12 Full gate: `uv run pytest tests/unit -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`
- [x] 1.13 `CHANGELOG.md` (`clients/python`): `[Unreleased]` entry

## 2. Report-only findings (plan items 2-4, no code change)

- [x] 2.1 `py-exceptions-47`: write up the `OutdatedAgentError(UnimplementedError)` option in `design.md` — needs a user decision (changes the public exception type for a missing kernel and `LifecycleUnsupportedException`'s base class)
- [x] 2.2 `py-async-396`: write up the `traffic_access_token` sync/async asymmetry in `design.md` — needs a user decision (new public native API, or extra token minting at create/connect)
- [x] 2.3 `py-sync-118`: write up the silent-drop vs class-variant asymmetry in `design.md` — needs a user decision (new `RayitoCompatWarning`s that are silent today)
- [x] 2.4 Confirm none of the three is implemented and none touches files outside this change's ownership list

## 3. OpenSpec and docs

- [x] 3.1 No spec deltas: confirm `openspec/specs/e2b-compat/spec.md`'s `control_plane` conflict requirement is unchanged in text (grep, read the surrounding scenario)
- [x] 3.2 `openspec validate m10-python-shim --strict` passes
- [x] 3.3 `clients/python/CHANGELOG.md`: `[Unreleased]` entry for the refactor (no entry for the report-only items, since nothing shipped)
- [x] 3.4 `docs/research/2026-09-m9-architecture-review.md`: mark the `python-shim` group's item 1 finding as done with the PR link; leave items 2-4 as open/reported
