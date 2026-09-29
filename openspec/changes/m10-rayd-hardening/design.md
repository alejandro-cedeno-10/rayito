## Context

Three items from `MILESTONES.md`'s M9 "diferido con razón escrita" list,
none of them new surface: all three are `rayd`/`rayd-core` internals that
fell short of the posture the milestone that shipped them (M2, M9) was
supposed to give them. Full context lives in `ARCHITECTURE.md` ADR-011,
ADR-012 (and its addendum) and `SECURITY.md` T17.

## Decision 1 — Egress option A: DNS block for uid ≥ 1000 under deny-all

**Why priority 0 has to move.** A kernel FIB rule's priority is an
unsigned field; 0 is already the floor. The platform's resolvers are
reached at loopback/link-local addresses, resolved through the kernel's
`local` table, whose rule sits at priority 0 — ahead of the uid-scoped
policy rules at 150/151 (ADR-012). The only way to intercept a uid-1000
query to one of those addresses *before* `local` answers it is to run
something at a lower priority than 0, which does not exist, or to vacate
priority 0 for the new rule, which means moving `local` first.

**Why the new rule can't just share priority 0 with the old one.** Linux's
`fib_insert_rule` inserts a new rule immediately after the last existing
rule whose priority is `<=` the new one's; two rules at the same priority
are evaluated in insertion order. The kernel's own `local` rule exists
before `rayd` ever runs. A block rule added at priority 0 *after* it would
always lose the tie to it for any query the destination-based `local` rule
already answers (exactly the loopback/link-local resolvers this is meant to
stop) and would never actually intercept them.

**The fix (`rayd_core::network::dns_guard`, pure planning; `rayd`'s
`egress_routes.rs`, the `ip` adapter):**

1. Add a second `local` rule at priority 1 (`ip rule add priority 1 lookup
   local`) — a harmless duplicate; nothing depends on it yet.
2. Delete the kernel's original rule at priority 0. Local routing now
   depends solely on the duplicate from step 1, continuously — there is no
   point where neither exists.
3. Add `ip rule add priority 0 uidrange 1000-65535 ipproto <udp|tcp> dport
   53 prohibit`, one per transport per managed family. Priority 0 is now
   free, so the new rule is alone there: no tie to lose.

Removing reverses the order (block rules first — always safe to drop —
then the default `local` rule restored, then the duplicate dropped last),
so local routing is continuous in both directions.

**When it applies.** Exactly when the effective route plan denies all
direct traffic in every family the guest manages
(`RoutePlan::denies_all`), which is what "deny-all" means throughout
ADR-012 and T17: `/run`'s deny-all install, `UpdateNetwork` when the new
policy is a full deny-all (`ProxyOnly`, or `Routes` with nothing left
allowed), and the emergency recovery, which always lands on deny-all.
Anything less than full deny-all is untouched: those policies may still
need the platform's resolver for a hostname the proxy is not asked to
carry.

**Failure handling.** Best effort, matching the rest of the in-guest
enforcement's own framing (T17: "no son una frontera dura"). A failure
partway through *installing* the guard rolls all the way back to the
pre-install state (`plan_dns_guard_rollback`, undoing exactly the steps
that already succeeded, in reverse) rather than leaving a half-moved
`local` rule; it never fails the surrounding route/proxy install, which
stays the hard boundary. A failure while *removing* it is left as-is
(always a safe, if over-blocking, intermediate state) and retried on the
next transition.

**Alternatives considered.** Same two the ADR-012 addendum already
weighed: (B) a per-process `resolv.conf` under a mount namespace was
rejected there for not actually closing the channel (the resolvers stay
reachable by address) and for its complexity across every process, PTY and
kernel the sidecar launches; nothing here changes that call.

## Decision 2 — Closing the kernel rotation window synchronously

**Where the window was.** `/run`'s handler calls `session.run(...)`
(installs `sandbox_id`), then (previously) `enforce_egress_at_run(&state)
.await` — up to 1.5 s of `ip` work — before ever touching the kernel
supervisor. `CodeManager::spawn_run_rotation` → `SidecarSupervisor
::request_rotation` only *enqueued* `Control::Rotate` on an unbounded
`mpsc` channel; the visible `SidecarState` (what `Health.kernel_ready`
reads) only flipped to `Rotating` once the supervisor's own async loop
task was scheduled and processed that message — a real gap, not just the
"~100 µs" residual the milestone entry anticipated, since it spanned the
entire egress-enforcement `await`.

**The fix.** `SidecarSupervisor::mark_rotation_pending` does the state flip
synchronously (`watch::Sender::send_if_modified`, a plain mutex-guarded
write, no `await`) and is called from the `/run` handler the instant
`RunOutcome::Installed` is confirmed — before `spawn_defaults()`, before
the egress `await`, before anything else. `request_rotation` (called later,
after egress settles, to actually trigger the restart) calls it too, so
every path that used to flip the state now does so eagerly.

**Why `begin_rotation`'s own gate had to move.** Before this change,
`begin_rotation` used "was the state `Ready`?" both to decide whether a
restart was already running (its single-flight guard) *and* to perform the
transition to `Rotating`. Once `mark_rotation_pending` can flip that state
ahead of `begin_rotation` ever running, that combined gate would see its
own eager mark and skip the real restart — a correctness regression far
worse than the window it closes (a stale kernel silently kept for a new
sandbox). The single-flight guard is now a dedicated `AtomicBool`
(`rotation_inflight`), independent of the visible state; `begin_rotation`
still performs the state transition (a no-op if already `Rotating`) but no
longer uses it to decide whether to run.

**What stays.** The SDK-side check (`sandbox_id` in readiness, Q78) stays
as defense in depth — cheap, and it also covers the platform's own
independent Q78 finding (a `Health` call arriving *before* `/run` even
starts, during restoration, with an empty `sandbox_id`), which this change
does not touch.

## Decision 3 — Signal dispositions reset before exec

**Why `exec` alone does not fix this.** POSIX `execve` resets a *caught*
signal's disposition to `SIG_DFL` (its handler function would be invalid
in the new image), but leaves `SIG_IGN` exactly as it was — deliberately,
so a parent that ignores `SIGHUP` (`nohup`'s whole purpose) does not have
that undone by every program it execs. `fork` preserves both the
disposition table and the blocked-signal mask unconditionally. So a
process tree spawned under `nohup` — `rayd` itself in production, or the
test binary in CI/dev — has `SIGHUP` (and whatever else the shell or
runner left non-default) set to `SIG_IGN` all the way down, including every
user process, PTY shell and kernel sidecar `rayd` launches.

**The fix.** `PreExecPlan::apply` (`crates/rayd/src/adapters
/process_spawner.rs`, shared by `pty_backend.rs` and `sidecar_process.rs`
through the one `pre_exec` hook) walks `nix::sys::signal::Signal::iterator
()`, resetting each to `SIG_DFL` via `sigaction` (tolerating `EINVAL` on
`SIGKILL`/`SIGSTOP`, which refuse it — they are never anything else), then
clears the blocked set with `pthread_sigmask(SIG_SETMASK, &empty, _)`. Both
calls are on the async-signal-safe list and touch no memory beyond a fixed
`libc::sigaction`/`sigset_t`, consistent with everything else this
function already does between `fork` and `exec`. It runs last, after the
descriptor seal, since nothing about it depends on ordering relative to the
rest of the plan.

**Proof.** `crates/rayd/src/adapters/process_spawner.rs`'s own test module
forks a child that inherits `SIGHUP` ignored, calls
`reset_signal_dispositions`, then `raise(SIGHUP)`: the child must die from
the kernel's default action, not survive it. The integration-level proof,
`crates/rayd/tests/m2_process.rs`'s
`a_sighup_ignored_by_the_parent_does_not_reach_the_child`, sets `SIGHUP` to
`SIG_IGN` on the test process itself (playing `rayd`'s role under
`nohup`), spawns a shell that sends itself `SIGHUP`, and asserts it dies
before it can print anything.
