# security-docs Specification

## Purpose
TBD - created by archiving change m8-security-docs. Update Purpose after archive.

## Requirements

### Requirement: The access token environment variable is documented as a per-process shared secret
`docs/site/docs/concepts.md` ("Dos tokens") and `SECURITY.md` T4 SHALL state that `RAYITO_ACCESS_TOKEN` is read by `Sandbox.create()` as well as by `Sandbox.connect()` in both SDKs, so exporting it makes every sandbox that process creates share one secret — the fleet-wide secret T14 rejects ("nunca uno por pool: una fuga abre un VM, no la flota") — instead of the fresh 32-byte secret `create()` mints per sandbox. Both texts SHALL say what to do instead: keep the variable for `connect()` from another process, store `sbx.access_token` next to the `sandbox_id`, and pass `access_token=` / `accessToken` explicitly when a sandbox needs a fixed secret. `SECURITY.md` T4 SHALL additionally state that the MCP server creates sandboxes without passing a token, so an operator with the variable exported shares one secret across every sandbox of that server and has no opt-out today. Neither text SHALL describe the variable as read only by `connect()`.

#### Scenario: both files name create() next to the variable
- **WHEN** `scripts/tests/test_security_docs.py::test_access_token_env_var_is_shared_by_create` reads the "Dos tokens" section of `docs/site/docs/concepts.md` and the T4 row of `SECURITY.md`
- **THEN** both name `create()` as a reader of `RAYITO_ACCESS_TOKEN` and state that exporting it shares one secret across the sandboxes of the process, and `concepts.md` no longer offers the variable as a bare alternative to storing `sbx.access_token`

#### Scenario: the paragraph says what to do instead
- **WHEN** the same test asserts the content of the corrected `concepts.md` paragraph
- **THEN** the paragraph names `access_token=` / `accessToken` as the way to fix one sandbox's secret and keeps `RAYITO_ACCESS_TOKEN` as the way to `connect()` from another process, and the T4 row names the MCP server as having no opt-out

### Requirement: SECURITY.md names CallerPolicy as the publisher policy
The IAM section of `SECURITY.md` SHALL introduce `CallerPolicy` as the policy of the **publisher** — the machine that publishes images with `rayito image publish` / `prune` and launches sandboxes from the SDK — instead of "la máquina que ejecuta el SDK", SHALL say that it therefore carries the image verbs on `arn:aws:lambda:<region>:<acct>:microvm-image:*` and SHALL NOT be attached to an application server that only launches sandboxes, and SHALL point at `infra/ci-oidc-role.yaml` as the runtime-only shape that already exists (the six MicroVM verbs on named image ARNs, `ListMicrovms`, and no image, S3, quota or tagging permission). The section SHALL NOT enumerate individual image verbs, so that removing `lambda:DeleteMicrovmImage` from `infra/iam.yaml` needs no further documentation change.

#### Scenario: the IAM bullet says publisher and offers the runtime-only policy
- **WHEN** `scripts/tests/test_security_docs.py::test_caller_policy_is_the_publisher_policy` reads the `CallerPolicy` bullet of the IAM section of `SECURITY.md`
- **THEN** the bullet names the publisher, warns against attaching it to an application server, and cites `infra/ci-oidc-role.yaml` as the runtime-only policy

### Requirement: A gate test pins every documentation correction of the audit
`scripts/tests/test_security_docs.py` SHALL assert every sentence this change corrects, resolving the repository root as `Path(__file__).resolve().parents[2]` (the pattern already used by `clients/python/tests/unit/cli/test_compat.py` and `scripts/tests/test_check_license.py`), with one test per audit row (C-01, C-02, C-03, C-04, C-07, C-08, C-09, H-06) plus one for the published persistence recipe that C-07's page shares with H-01. Each test SHALL assert both the presence of the corrected wording and the absence of the retired wording, so reverting a sentence fails the gate. The module SHALL run in the existing `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider` gate (`Makefile` target `test-scripts` and the CI `check` job) and SHALL be `ruff`-clean under `uvx ruff check scripts`, requiring no change to `Makefile`, `.github/workflows/*` or any runtime code. `mkdocs build --strict` SHALL stay green and `docs/site/mkdocs.yml` SHALL NOT change.

#### Scenario: the gate runs where the other script tests run
- **WHEN** `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider` runs after the change
- **THEN** the nine tests of `test_security_docs.py` pass together with the pre-existing script tests, and `git diff --name-only` shows no change to `Makefile` or `.github/workflows/`

#### Scenario: a reverted sentence fails the gate
- **WHEN** any one corrected sentence is restored to its pre-change wording (for example "el kernel no se reinicia" in T2 or "otra rama, otro environment u otro repositorio no pueden asumirlo" in `infra/README.md`) and the gate runs
- **THEN** exactly the test of that audit row fails, naming the file and the retired wording

#### Scenario: the docs site still builds strictly
- **WHEN** `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict` runs
- **THEN** the build succeeds with the edited `concepts.md`, `security.md` and `persistence.md`, and the navigation is unchanged

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

### Requirement: SECURITY.md documents the in-guest egress policy (T8 and T17)
`SECURITY.md` T8 SHALL state:
- the in-guest egress policy on `rayito-base-caps` (routes for uid 1000–65535 and the local proxy);
- that the default image fails closed (the SDK terminates the MicroVM and raises `UnimplementedError`);
- that the customer VPC connector of `infra/egress-connector.yaml` remains the only out-of-guest control, and that security groups do not filter Amazon DNS.

A new threat row T17 "Política de egress en el guest y proxy local de rayd" SHALL state:
- that the local proxy runs as root and is an SSRF surface, closed by the guard: loopback, link-local including `169.254.169.254` and `fd00:ec2::254`, own interface addresses, unspecified, multicast and broadcast, checked after resolution and dialled at the checked address only;
- that in-guest layers fall to a guest-kernel exploit, to root in the guest (`RAYITO_ALLOW_ROOT`) and to the exempt platform agent uids 991–994;
- that DNS resolution for uid ≥ 1000 under deny-all on `rayito-base-caps` is a known residual risk (DNS exfiltration through the in-guest platform resolvers) while every connection outside the VM fails, that in-guest enforcement is best-effort with the VPC connector as the hard control, and that the proxy never resolves a denied name under a deny-by-default policy;
- that proxy-unaware clients fail closed;
- that upstream proxy credentials travel only in the token-authenticated `UpdateNetwork`, are zeroized, never logged, never echoed and never placed in the `runHookPayload`;
- that any process can use the proxy but only within the policy;
- that the IMDS rule of C-05's family and the deferred C-01 are not regressed.

`docs/site/docs/security.md` SHALL carry a one-line T17 summary.

#### Scenario: T17 names the guard and the residual risks
- **WHEN** a reviewer reads the T8 and T17 rows of `SECURITY.md`
- **THEN** T8 names `rayito-base-caps`, the fail-closed default image and the VPC connector with its DNS caveat, and T17 names `169.254.169.254`, the own-address rule, the guest-kernel and guest-root residuals, the DNS behaviour, the fail-closed proxy-unaware clients and the credential handling
