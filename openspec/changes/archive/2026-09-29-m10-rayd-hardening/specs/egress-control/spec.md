## ADDED Requirements

### Requirement: DNS for uid ≥ 1000 is blocked under deny-all (egress option A)
On an image whose `CapEff` includes `CAP_NET_ADMIN`, whenever the effective route plan denies all direct traffic in every family the guest manages (`RoutePlan::denies_all` true for each of them — the deny-all `rayd` installs at `/run`, an `UpdateNetwork` policy that is a full deny-all, or the emergency recovery, which always lands on deny-all), `rayd` SHALL additionally block DNS (port 53, UDP and TCP) for `uidrange 1000-65535`, closing the residual channel `SECURITY.md` T17 and the ADR-012 addendum recorded (QE1, `AWS_API_NOTES.md` Q66: the platform's own resolvers, reached at loopback and link-local addresses, answer uid ≥ 1000 through the kernel's `local` table ahead of the uid-scoped policy rules).

Mechanism, atomic with rollback:
- The block SHALL be a kernel policy rule (`ip rule ... uidrange 1000-65535 ipproto udp|tcp dport 53 prohibit`) placed at rule priority 0, one per transport per managed family.
- Because rule priority is an unsigned kernel field with 0 as its floor, and same-priority rules are evaluated in insertion order (a new one always loses ties to the kernel's own pre-existing `local` rule), installing SHALL first add a second `local` rule at priority 1 (a harmless duplicate), then delete the kernel's original rule at priority 0, and only then add the block rules at the now-free priority 0. At every point in this sequence local routing SHALL remain reachable through at least one `local` rule.
- Removing (when the policy stops denying everything) SHALL reverse the order: the block rules first, then the default `local` rule restored, then the duplicate dropped last.
- A failure partway through installing SHALL be rolled back to the exact pre-install state (undoing exactly the steps that already succeeded, in reverse) rather than leaving a half-moved `local` rule; it SHALL NOT fail the surrounding route/proxy install.
- Outside this exact deny-all condition (a partial policy that still allows something directly) the block SHALL NOT be installed, and an installed one SHALL be removed when the policy relaxes to that.
- The IMDS rule (table 100, priority 100) SHALL NOT be touched.

#### Scenario: a DNS query stops resolving under deny-all, real kernel
- **WHEN** `crates/rayd/tests/m9_egress.rs` runs as root in a fresh network namespace and `rayd` installs `/run`'s deny-all
- **THEN** `ip route get 127.0.0.53 uid 1000 ipproto udp dport 53` (and `tcp`) no longer resolves a route, `ip route get 127.0.0.53 uid 1000` (no port selector) still resolves through `local`, a query on a different port still resolves, and the IMDS block is unaffected

#### Scenario: install never opens a window and rolls back on failure
- **WHEN** the domain test simulates the rule set after every step of installing the guard, of removing it, and of a rollback after an injected failure partway through install
- **THEN** at every step at least one `local` rule remains, and a rollback ends in exactly the pre-install rule set

#### Scenario: relaxing the policy removes the guard
- **WHEN** an egress policy denying everything is replaced by one that still allows something directly (`Routes` mode with a non-empty allow beyond the deny)
- **THEN** the DNS block rules and the moved `local` rule are gone and a uid-1000 DNS query resolves again
