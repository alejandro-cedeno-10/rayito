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

### Requirement: SECURITY.md and security.md document the M12 local proxy's JWE scope
`SECURITY.md` threats T2 (hook-port integrity) and T3 (proxy JWE) SHALL each gain an M12 appendix stating that `rayito sandbox proxy`:

- always mints its JWE scoped to a single port (`PortSpec.single(N)`, never `allPorts`) and never for port 9000 (the lifecycle-hooks port), rejecting that port before any AWS call;
- binds to loopback (`127.0.0.1`/`::1`/`localhost`) by default, and requires `--allow-remote` for any other bind address;
- strips any `x-aws-proxy-*` header a client sends before forwarding the request;
- never logs, prints or otherwise records the JWE, headers, bodies or paths of what passes through it.

The appendix SHALL name the residual risk with no new mitigation and no new threat number: while the proxy runs, any local process that reaches the forwarded port has the same access to the sandbox as the operator who started it (and, if `--allow-remote` was used, so does any host that can reach the bound address).

`docs/site/docs/security.md` SHALL carry the matching prose, cross-linking `cli.md`'s `proxy` command documentation.

#### Scenario: SECURITY.md names the port-9000 exclusion and the residual risk
- **WHEN** a reviewer reads the T2 and T3 rows of `SECURITY.md`
- **THEN** T2 states that `rayito sandbox proxy` never mints a token for port 9000, and T3 states the single-port JWE scope, the loopback default, the `x-aws-proxy-*` stripping, and the residual local-process risk

#### Scenario: the docs site names the same facts
- **WHEN** a reader opens `docs/site/docs/security.md`
- **THEN** it names the single-port JWE scope, the loopback default requiring `--allow-remote` otherwise, and that the proxy never logs the JWE

### Requirement: SECURITY.md documents user secret custody as T18
`SECURITY.md` SHALL carry a T18 row ("custodia de secretos del usuario") covering the channel (per-call `envs` only; never `runHookPayload`, `metadata`, logs or errors), data at rest (KMS in Secrets Manager, SDK process memory only for the cache, the VM environment and the suspend snapshot, SEC-5 open), who reads what (the caller's IAM, never the execution role; in phase 1 any uid 1000 code can read an injected secret), and the recommendation for untrusted code (short-lived, least-privilege tokens; no long-lived credentials). `docs/site/docs/security.md` SHALL have a matching section.

#### Scenario: T18 is present and complete
- **WHEN** `scripts/tests/test_security_docs.py` runs
- **THEN** the T18 row exists and names the payload exclusion, the in-memory cache, the phase-1 readability, the snapshot and the short-lived-token recommendation, and the site page has the section

### Requirement: The threat model covers the metadata copy at rest (T19)
`SECURITY.md` SHALL have a row T19 for the optional metadata index stating what is copied (only the non-secret metadata and immutable launch facts; never the access token, envs or secrets), that a forged row never creates a phantom sandbox (the join starts from `list-microvms` and checks image and `startedAt`), that a writer can relabel how a real sandbox appears (metadata is not an access control), that the writer (`RayitoIndexWriter`, `PutItem`) and reader (`RayitoIndexReader`, `BatchGetItem`) roles are separate, and that rows expire by TTL and are filtered on read. `docs/site/docs/security.md` SHALL summarise it.

#### Scenario: the T19 row exists
- **WHEN** `scripts/tests/test_metadata_index_template.py::test_security_threat_model_has_t19` runs
- **THEN** exactly one `| T19 |` row exists and it mentions "nunca", "access token", "fantasma", `RayitoIndexWriter` and `RayitoIndexReader`

### Requirement: SECURITY.md documents the client-side hardening of the SDK-clients sweep
`SECURITY.md` SHALL state in T3 the proxy's Host/Origin allowlist (`421`/`403`), `--allowed-host`/`--allow-origin`, the wildcard-bind rule, the connection cap, the single-message-per-connection rule (`400` on ambiguous framing, a refused upgrade is closed) and the cookie-sharing risk with the `<id>.localhost` mitigation; in a T28 row the build-context threats (symlinks out of the context, `.dockerignore` semantics, the likely-secrets warning) and their residual risk; in T9 that credentialed git URLs live in `.git/config` during the operation, the always-attempted restore and its warning, the hook/helper isolation and URL-rewrite refusal, that a token passed to the sandbox must be treated as revealed to its code (short-lived, single-repository tokens), that an execution role can write to any `/rayito/*` log stream and that the CLI neutralises terminal control characters; in T4 the access-token minimum; in T7 the client-side output cap and that `run_code` output is not yet bounded; in T18 that the git credential rule matches the secrets rule; and under "Higiene de logging" the sanitizer coverage and the JWE repr redaction. `docs/site/docs/security.md`, `git.md`, `funciones-opcionales/proxy-local.md`, `cli.md`, `funciones-opcionales/templates.md`, `guias/comandos.md` and `referencia/variables-de-entorno.md` SHALL carry the matching guidance in Spanish.

#### Scenario: a reader checks the git guidance
- **WHEN** a reader opens `docs/site/docs/git.md`
- **THEN** the credentials section warns that the token is reachable by code already running in the sandbox and recommends short-lived, single-repository tokens, and no longer says the URL only exists in `/proc` while the command runs

### Requirement: The docs state what a release signature proves and verify before publishing
`docs/site/docs/verify.md` SHALL contain a section "Qué prueba la firma" stating that a valid signature proves `release.yml` of this repository signed the file in a run started from the `rayd-v<version>` tag and, for releases after 0.6.1, that the run passed the `release` environment's approval before the OIDC token existed, that a dry run signs nothing, and what the signature does not prove (that the tagged code was reviewed on `main`; signing needs both creating a `rayd-v*` tag and approving the `release` environment). It SHALL NOT claim that uploading requires approval as the only gate. The recommended "Desde la release" recipe of `docs/site/docs/primeros-pasos/configurar-aws.md` SHALL download `SHA256SUMS`, run `cosign verify-blob` with the exact identity `release.yml@refs/tags/rayd-v${RAYD_VERSION}` and a `sha256sum -c` before `rayito image publish`, and SHALL NOT present the verification as optional. `scripts/tests/test_security_docs.py` SHALL assert both.

#### Scenario: verification is optional again
- **WHEN** the recipe moves `cosign verify-blob` after `rayito image publish` or reintroduces "Opcional pero recomendable"
- **THEN** `test_the_release_recipe_verifies_before_publishing` fails

#### Scenario: the overclaim comes back
- **WHEN** `verify.md` again says only that uploading requires the maintainer's approval
- **THEN** `test_verify_states_what_a_signature_proves` fails
