## MODIFIED Requirements

### Requirement: SECURITY.md T2 states the timeout exit and bounds forged hooks around the deadline
The mitigation cell of `SECURITY.md` T2 SHALL keep every existing sentence and SHALL append an `M9 (m9-server-timeout, ADR-011)` paragraph stating:

- `rayd` exits on its own at a kill-mode deadline and the VM is `TERMINATED` about 15 s later (Q58).
- A forged `/terminate` from uid 1000 has the same effect on the sandbox itself: self-DoS, no access gain, no other sandbox reached.
- `SetTimeout` requires `x-access-token`, so the sandbox's own code cannot extend its deadline.
- A forged `/suspend` at the deadline holds it at most 20 s, once per deadline.
- A forged `/suspend` + `/resume` pair neither opens the 30 s grace nor applies the 5-minute auto-resume rule, because both require a `CLOCK_MONOTONIC` jump of at least 2 s seen by the deadline watcher.
- The peer-uid check of `/terminate` and `/validate` (C-01) came later (`sec-sandbox-isolation`) and M9 does not depend on it: a forged `/terminate` that dodges it still has the effect of a deadline.

#### Scenario: T2 names the timeout exit
- **WHEN** `scripts/tests/test_lifecycle_docs.py::test_t2_names_the_timeout_exit` reads the T2 row
- **THEN** it contains `m9-server-timeout`, `SetTimeout` and `auto-DoS`, and still contains `0.0.0.0:9000`

## ADDED Requirements

### Requirement: SECURITY.md documents the secret gateway as T24
`SECURITY.md` SHALL carry a T24 row for the secret gateway (`gateways=`, ADR-023) stating the fixed `https://host` upstream and its resolver guard, the allowlist and rate limit decided before any connection, the in-memory-only value, the inbound header strip, that the upstream's status and body reach the sandbox **unchanged** while response headers carrying an injected name or a vaulted value are dropped, and the qualified guarantee: the sandbox can use the secret and cannot read it **unless the allowed upstream reflects it**, so `allow` must never list an endpoint that echoes request headers. `docs/site/docs/funciones-opcionales/pasarela-de-secretos.md` SHALL state the same guarantee in its introduction and carry a "Lo que la pasarela no puede impedir" section with a warning, and `docs/site/docs/security.md` SHALL carry one-line T2 and T24 summaries. `docs/SECURITY_AUDIT.md` §9 SHALL carry code rows for C-01, C-02 and C-03, and "Lo que sigue abierto" SHALL no longer name them.

#### Scenario: T24 qualifies the guarantee
- **WHEN** `scripts/tests/test_security_docs.py::test_t24_qualifies_the_gateway_guarantee` runs
- **THEN** the T24 row, the gateway page and the site security page contain the qualified guarantee and the warning, and `pasarela-de-secretos.md` no longer says the sandbox can never read its credential back

#### Scenario: C-01, C-02 and C-03 are closed in the audit
- **WHEN** `scripts/tests/test_security_docs.py::test_c01_c02_c03_are_closed_in_the_security_audit` runs
- **THEN** §9 has the three code rows and "Lo que sigue abierto" names only the real-AWS measurement those fixes still depend on
