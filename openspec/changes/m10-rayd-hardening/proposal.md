## Why

M9 closed with three items written down as "diferido con razón escrita" in
`MILESTONES.md` (M9, deferred list), each a known gap in `rayd`'s own
hardening rather than a new feature:

- **Egress, option A** (ADR-012 addendum, `SECURITY.md` T17): QE1 measured
  (Q66) that under `allow_internet_access=False` on `rayito-base-caps`, a
  uid ≥ 1000 process can still resolve a name through the platform's own
  resolvers (one loopback, one link-local) even though every actual
  connection out of the VM fails. The resolvers are reached through the
  kernel's `local` table at rule priority 0, ahead of the uid-scoped policy
  rules at 150/151. M9 accepted this as "resolves or not, but nothing
  resolved connects" (opção C); the addendum named the fix ("opción A": an
  `ip rule` for port 53 before `local`) and deferred it, since it needs its
  own atomic-change-and-rollback design and its own e2e.
- **The kernel rotation window after `/run`**: `rayd` requests the default
  kernel's rotation (fresh HMAC key, fresh PRNG seed, the new sandbox's
  envs) through an async control channel; a readiness probe landing in the
  gap between `/run` being accepted and the control loop actually flipping
  the visible state to `Rotating` could observe the *previous* sandbox's
  kernel as ready. The SDKs close this today by requiring `sandbox_id` in
  readiness (Q78), which is defense in depth, not a fix in the agent
  itself.
- **Signal dispositions inherited by rayd's children**: when `rayd` (or the
  test binary) runs under `nohup`, `SIGHUP` is `SIG_IGN` in the parent.
  `exec()` only resets a *caught* signal's disposition to default; one
  already `SIG_IGN` survives both `fork` and `exec` unless something resets
  it, so every process, PTY shell and kernel sidecar `rayd` launches
  inherits it too. `crates/rayd/tests/m5_pty.rs`'s
  `kill_takes_the_foreground_job_down_with_the_shell` fails exactly this
  way: the foreground job ignores the hangup its own parent's `SIGHUP`
  disposition never should have reached it with.

None of the three change the wire contract (`.proto`, `EgressEnforcement`,
`Health` fields, the policy semantics) or add a feature; all three hold
`rayd`/`rayd-core` to the security posture the milestone that shipped the
surface was supposed to give it (`openspec/project.md` hard rule 6).

## What Changes

- **`rayd_core::network::dns_guard`** (new, pure): the ordered step lists
  for installing and removing the DNS block, and their rollback. Install
  order is fixed so the guest is never without local routing and the block
  rules never have to share priority 0 with the kernel's own rule (same-
  priority ties break in insertion order, which would always lose to the
  rule that was already there): add a second `local` rule at priority 1,
  delete the kernel's original at priority 0, then add
  `uidrange 1000-65535 ipproto udp|tcp dport 53 prohibit` at the now-free
  priority 0. `NetworkManager` installs this exactly when the active route
  plan denies all direct traffic in every managed family (`/run`'s
  deny-all, `UpdateNetwork`, and the emergency recovery, which always lands
  on deny-all) and removes it when the policy stops denying everything;
  best effort, like the rest of the in-guest enforcement (`SECURITY.md`
  T17), with a full rollback to the pre-install state on any failure during
  install.
- **`SidecarSupervisor::mark_rotation_pending`** (new): the synchronous half
  of `/run`'s kernel rotation, called the instant `/run` is confirmed
  installed, before egress enforcement or anything else `await`s. It flips
  the visible `SidecarState` to `Rotating` immediately if it currently
  reads `Ready`, closing the window down to the unavoidable few
  instructions between marking the sandbox installed and this call — no
  scheduler hop involved. The actual restart request
  (`spawn_run_rotation`) is unchanged and still runs after egress settles,
  so the rotated kernel still picks up the right proxy env.
  `begin_rotation`'s own single-flight guard moves off the visible state
  (which the eager mark can now set ahead of it) onto a dedicated
  `AtomicBool`, so the real restart still happens.
- **`PreExecPlan::apply`**: resets every signal disposition to `SIG_DFL`
  (skipping `SIGKILL`/`SIGSTOP`, which refuse it) and clears the blocked
  set, right before `exec`, shared by the three launchers of user code
  (processes, PTY shells, the kernel sidecar).

## Non-goals

- No change to `NetworkService`'s contract, `EgressEnforcement`'s
  semantics, or the E2B-facing egress policy grammar: the DNS guard is
  purely an in-guest kernel-routing detail invisible to the SDK.
- No change to `LifecycleService`, `SetTimeout`, or any client-observable
  timing beyond closing the window (still logged as `restart_ms`).
- Real-AWS acceptance (image republish, `test_m9_egress.py` /
  `m9-egress.e2e.test.ts` against the republished `rayito-base-caps`) is
  out of scope for this change; it is the acceptance agent's job once this
  merges, per `openspec/project.md`'s acceptance process.
