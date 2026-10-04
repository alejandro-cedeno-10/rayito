## ADDED Requirements

### Requirement: A /run from a sandbox uid never claims the boot's run
`rayd` SHALL read the uid owning the caller's TCP socket of every `/run`
through the hooks' peer check (the `/proc/net/tcp` or `/proc/net/tcp6` row
whose local end is the caller's address) and, when that uid is in
the sandbox range 1000-65535, SHALL answer 200 with `outcome:
"sandbox_origin"`, count one `hook_anomalies` (the one call counted before
the accepted `/run`: a sandbox-owned `/run` is an anomaly whether or not the
boot accepted one) and SHALL NOT claim the boot's
once-only `/run`, install a token digest or apply any part of the payload. A
`/run` whose socket owner is root, a platform uid or cannot be found SHALL
behave exactly as before. `SECURITY.md` T2 and `ARCHITECTURE.md` ADR-022
SHALL describe the pre-`/run` template residual this closes and what it
leaves open (a root `start_cmd`, the other hooks).

#### Scenario: a template process posts /run before the platform
- **WHEN** a process owned by uid 1000 posts a well-formed `/run` before the
  platform's `/run`, over a real connection to the hooks listener
- **THEN** it gets `sandbox_origin`, its token never authenticates, and the
  platform's `/run` that follows answers `installed` with its own token

#### Scenario: an unknown caller keeps the previous behaviour
- **WHEN** no socket row matches the caller (or no connection info exists)
- **THEN** `/run` is accepted once per boot as before
