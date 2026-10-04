## ADDED Requirements

### Requirement: internal launchers never pass rayd's environment or descriptors to a child
The mount readiness probe SHALL run an absolute `stat` binary as the guest
user with an environment holding only `PATH`, and SHALL, like `mount-s3`,
mark every descriptor above the ones the child must inherit close-on-exec
and reset every signal disposition between `fork` and `exec`, through the
same posture the user-code launchers use.

#### Scenario: the probe's environment is only PATH
- **WHEN** the agent builds the readiness probe for a mount path
- **THEN** its program is `/usr/bin/stat`, its environment is cleared and
  `PATH` is the only variable set

#### Scenario: an inheritable descriptor never reaches a guest helper
- **WHEN** `rayd` holds a descriptor opened without `O_CLOEXEC` while it
  launches a guest helper
- **THEN** the helper's environment lists only `PATH` and the descriptor is
  not open in the helper
