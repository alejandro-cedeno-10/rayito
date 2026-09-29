## 1. Egress option A: DNS block for uid ≥ 1000 under deny-all

- [x] 1.1 `crates/rayd-core/src/network/dns_guard.rs`: `DnsGuardStep`,
  `DnsProto`, `plan_dns_guard_install`, `plan_dns_guard_remove`,
  `plan_dns_guard_rollback`, with a kernel-simulation unit test proving
  local routing is never dropped at any step of install, remove, or a
  rollback after a partial install, and that a DNS lookup resolves through
  `local` before install and through `prohibit` after.
- [x] 1.2 Wire the module into `crates/rayd-core/src/network/mod.rs`.
- [x] 1.3 `crates/rayd/src/adapters/egress_routes.rs`: `EgressRoutes
  ::execute_dns_guard`, idempotent `ip rule add/del` for the moved/default
  `local` rule and the block rules, with unit tests on the argument lists
  and the `ip rule show` line-matching helpers.
- [x] 1.4 `crates/rayd/src/network/fake_kernel.rs`: `FakeKernel
  ::execute_dns_guard` and `dns_guard_active`, seeded with the kernel's own
  default `local` rule at boot.
- [x] 1.5 `crates/rayd/src/network/manager.rs`: `Installed.dns_guard`,
  `wants_dns_guard` (denies-all in every managed family),
  `NetworkManager::ensure_dns_guard` wired into `apply` (after commit),
  `recover` (always wants it — recovery always lands on deny-all) and
  `reverify_locked`'s reinstall branch; unit tests for install-on-deny-all,
  remove-on-relaxation, recovery-installs-it-too, and rollback-on-injected-
  failure-without-failing-the-update.
- [x] 1.6 `crates/rayd/tests/m9_egress.rs`: real `ip` proof, as root in
  `unshare --net` — under deny-all a uid-1000 DNS query (`ip route get
  ... ipproto udp|tcp dport 53`) no longer resolves while ordinary loopback
  routing and other ports are untouched and the IMDS rule is never
  touched; once the policy leaves deny-all, the guard's rules are gone and
  DNS resolves again.
- [x] 1.7 Docs: `SECURITY.md` T17, `ARCHITECTURE.md` ADR-012 addendum (M10
  follow-up noting option A is implemented), `docs/site/docs/network.md`.

## 2. Kernel rotation window after `/run`

- [x] 2.1 `crates/rayd/src/code/supervisor.rs`:
  `SidecarSupervisor::mark_rotation_pending` (synchronous state flip),
  `rotation_inflight: AtomicBool` decoupling `begin_rotation`'s
  single-flight guard from the visible state, `request_rotation` calling
  the eager mark. Unit tests: the flip is visible with nothing awaited in
  between; the eager mark does not skip the real restart; a rotation
  requested before the sidecar is ready is still applied once it is.
- [x] 2.2 `crates/rayd/src/code/manager.rs`: `CodeManager
  ::mark_rotation_pending`, a no-op without a sidecar.
- [x] 2.3 `crates/rayd/src/code/fake_sidecar.rs`: `starting_supervisor`, a
  fixture whose loop has launched the fake but not yet seen `ready`, for
  the "requested before ready" test.
- [x] 2.4 `crates/rayd/src/hooks/mod.rs`: call `mark_rotation_pending`
  before the first `await` inside `/run`'s `Installed` branch; doc comment
  on `run` explaining why and that `spawn_run_rotation` stays after egress
  enforcement.
- [x] 2.5 `openspec/specs/code-execution/spec.md`: MODIFIED "Health
  .kernel_ready reflects the default kernel" with the synchronous
  guarantee and a scenario for it.

## 3. Signal dispositions reset before exec

- [x] 3.1 `crates/rayd/src/adapters/process_spawner.rs`:
  `reset_signal_dispositions` (reset every signal to `SIG_DFL`, tolerating
  `EINVAL` on `SIGKILL`/`SIGSTOP`; clear the blocked set), called last in
  `PreExecPlan::apply`. Unit test: a forked child that inherited `SIGHUP`
  ignored dies from its own `raise(SIGHUP)` after the reset.
- [x] 3.2 `crates/rayd/tests/m2_process.rs`:
  `a_sighup_ignored_by_the_parent_does_not_reach_the_child`, an
  integration-level proof through the real spawn path.
- [x] 3.3 `openspec/specs/process-lifecycle/spec.md`: ADDED "Signal
  dispositions are reset before exec".

## 4. Gates (Lima VM `rayito`, `CARGO_TARGET_DIR=/var/tmp/rayito-target-m10-rayd`)

- [x] 4.1 `cargo fmt --all --check`.
- [x] 4.2 `cargo clippy --workspace --all-targets --locked -- -D warnings`.
- [x] 4.3 `cargo test --workspace --locked` as uid 1500, full suite
  including `m2_process`, `m5_pty`; `m9_egress` as root in `unshare --net`.
  `m5_pty` and `m2_process` repeated ≥ 20 times each for flakes.
- [x] 4.4 `cargo deny check`.
- [x] 4.5 `openspec validate m10-rayd-hardening --strict`.

## 5. PR

- [ ] 5.1 Push, open a PR to `main`, wait for CI, fix failures until green.
  Do not merge; do not archive this change (real-AWS acceptance — a
  `rayito-base-caps` republish and the M9 egress e2e re-run against it —
  is the acceptance agent's job).
