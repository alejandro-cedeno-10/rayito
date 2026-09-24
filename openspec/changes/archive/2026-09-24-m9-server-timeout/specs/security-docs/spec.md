## ADDED Requirements

### Requirement: SECURITY.md T2 states the timeout exit and bounds forged hooks around the deadline
The mitigation cell of `SECURITY.md` T2 SHALL keep every existing sentence and SHALL append an `M9 (m9-server-timeout, ADR-011)` paragraph stating:

- `rayd` exits on its own at a kill-mode deadline and the VM is `TERMINATED` about 15 s later (Q58).
- A forged `/terminate` from uid 1000 has the same effect on the sandbox itself: self-DoS, no access gain, no other sandbox reached.
- `SetTimeout` requires `x-access-token`, so the sandbox's own code cannot extend its deadline.
- A forged `/suspend` at the deadline holds it at most 20 s, once per deadline.
- A forged `/suspend` + `/resume` pair neither opens the 30 s grace nor applies the 5-minute auto-resume rule, because both require a `CLOCK_MONOTONIC` jump of at least 2 s seen by the deadline watcher.
- Peer-uid authentication of `/terminate` and `/validate` stays pending (C-01), and M9 does not depend on it.

#### Scenario: T2 names the timeout exit
- **WHEN** `scripts/tests/test_lifecycle_docs.py::test_t2_names_the_timeout_exit` reads the T2 row
- **THEN** it contains `m9-server-timeout`, `SetTimeout` and `auto-DoS`, and still contains `0.0.0.0:9000`
