## ADDED Requirements

### Requirement: rayd's own listeners cannot exhaust its descriptors
The gRPC listener (`:8080`) and the hooks listener (`:9000`) SHALL each
serve at most a fixed number of connections at once
(`GRPC_MAX_CONNECTIONS` = 256, `HOOKS_MAX_CONNECTIONS` = 32, in
`rayd_core::listeners`), calling `accept` only while a slot is free and
holding that slot until the connection closes, so a connection over the cap
waits in the kernel's accept backlog without taking a descriptor. Together
with the local egress proxy the listeners SHALL never hold more than three
quarters of `rayd`'s 1024-descriptor limit. An `accept` that fails for any
reason other than the connection itself going away SHALL wait
`ACCEPT_BACKOFF` (100 ms) before the next attempt and SHALL be logged once
per streak (`accept_exhausted`), so a persistent `EMFILE` never spins a
CPU. The hooks listener SHALL drop a connection whose request head does not
arrive within `HOOKS_HEADER_READ_TIMEOUT` (10 s) and SHALL close every
connection after its one response. `SECURITY.md` T7 SHALL list the
listeners and their residual (a sandbox process can fill its own VM's
slots).

#### Scenario: idle connections beyond the cap wait
- **WHEN** a sandbox process holds as many idle connections to the hooks
  port as it has slots and the platform posts `/terminate`
- **THEN** the `/terminate` connection is not served while the idle ones
  hold every slot, and is answered 200 as soon as one of them closes

#### Scenario: a request head that never ends loses its slot
- **WHEN** a connection to the hooks port sends half a request head and
  stops
- **THEN** it is closed at the head deadline and its slot serves the next
  connection

#### Scenario: running out of descriptors does not spin
- **WHEN** `accept` fails with `EMFILE` several times in a row
- **THEN** each retry waits `ACCEPT_BACKOFF` and the listener resumes
  accepting once a descriptor is free
