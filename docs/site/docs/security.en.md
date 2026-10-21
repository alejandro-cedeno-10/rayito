# Security

Summary of [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(the full threat model; the T*n* identifiers on this page are its rows).
Three principals: the SDK operator (full trust), the code that runs inside
the sandbox (**none**) and AWS (proxy, hooks, snapshot).

## Reporting a vulnerability { #reportar-una-vulnerabilidad }

Privately, with [GitHub Private Vulnerability Reporting](https://github.com/alejandro-cedeno-10/rayito/security/advisories/new);
never in a public issue, and never pasting a JWE (`x-aws-proxy-auth`), an
access token (`x-access-token`) or a `runHookPayload`. Include the SDK
version, the `agent_version` from `get_health()`, the image and its
version, the region and a minimal reproduction without customer data.
Acknowledgement within 7 days and a fix or mitigation within 90; there is no
bounty program.

**Supported versions**: only the latest `MAJOR.MINOR` line, today
**0.15.x** (Python and TypeScript SDKs, and an image with `rayd` 0.15.x);
while the project is 0.x there are no maintenance branches for earlier lines
([Versioning and support](limits.md#versionado-y-soporte),
[SDK ↔ rayd ↔ image compatibility](limits.md#compatibilidad-sdk-rayd-imagen)).

## What the SDK protects by default { #lo-que-protege-el-sdk-por-defecto }

| Threat | Mitigation |
|---|---|
| Sandbox code reads the execution role's credentials through IMDS | `create(execution_role_arn=None)` **by default**: with no role there are no credentials inside (nor runtime logs). With a role, least privilege |
| Theft of the proxy JWE | Every RPC except `Health` also requires `x-access-token`; 60 min TTL; the SDK never logs headers |
| The secret's sha256 travels in `runHookPayload` (CloudTrail) | Only the hash leaves the client; `rayd` compares in constant time and never writes the `/run` body |
| Escalation to root inside the guest | Processes, PTYs and kernels as uid 1000; another account only if its uid and gid are between 1000 and 65535 (the range covered by the IMDS block and the network rules) and it is not in group 0; root only with `user="root"` **and** `RAYITO_ALLOW_ROOT=1` in the image |
| Resource exhaustion from the sandbox | rlimits, process groups, server timeouts, bounded output channels, max. 256 processes/PTYs, max. 8 kernels; on the client, the stored output of a command or a PTY is bounded to 64 MiB per descriptor (`max_output_bytes`/`maxOutputBytes`, `truncated` flags it); `rayd`'s ports (8080 and 9000) serve a maximum number of connections at a time and the hooks cut a header that never finishes arriving, so idle connections do not exhaust their descriptors (T7) |
| Fake pids in the sidecar protocol (T12) | `rayd` only registers and signals the kernel that `/proc` confirms as a child of the sidecar, leader of its group and of the same uid, pinned by its start time; it never signals group 0, group 1 or its own |
| Git credentials in the sandbox | `clone`/`push`/`pull` with `username`/`password` run without hooks or credential helpers, refuse if the git configuration rewrites URLs and always try to remove the token from `.git/config` when done; **residual risk**: sandbox code running as the same user can still capture it: treat it as disclosed and use short-lived, single-repository tokens ([Git](git.md#credenciales)) |
| Third-party content or secrets in a template build | symbolic links inside a copied directory are never followed (like Docker), so a link to a file on your machine does not end up in the artifact or the image; `.dockerignore` follows Docker semantics (`**/.env` also excludes the root `.env`) and the SDK warns if it is about to package `.env`, `.git/`, `.aws/`, `.ssh/` or `*.pem`/`*.key` keys, which any sandbox code could read ([Templates](funciones-opcionales/templates.md#contexto-de-build)) |
| CloudWatch or build logs with escape sequences | the CLI shows control characters as visible `\xNN` when writing to a terminal; with an execution role, a sandbox's code can write to any `/rayito/*` stream, so runtime logs do not prove integrity |
| `rayd` (root) as a *confused deputy* on the filesystem | deny list over canonical paths, the user's `setfsuid` on every operation, no `..`; each path is opened component by component with `O_NOFOLLOW` and acted on through the descriptor, so a component that sandbox code swaps for a link between the check and the use is rejected instead of followed, and a directory in `proc`, `sysfs` or `devpts` is rejected however it is reached (T11) |
| Secret gateway as a *confused deputy* | each request's path passes an allowed-characters list (RFC 3986 without `;`, decoded only once, no `.`/`..` segments) before the allowlist, so an upstream that normalizes `..;` or decodes twice cannot take it out of `allow` ([Secret gateway](funciones-opcionales/pasarela-de-secretos.md)) |
| Cloned snapshot state shared between sandboxes | nothing unique before `/ready`; `/run` restarts the kernel by default; `/resume` reseeds `random`/`numpy.random` |
| Exfiltration over the outbound network | explicit `egress=` in `create()`; allowlist through a VPC connector (outside the guest); since 0.3.0, `network=` / `allow_internet_access=False` applied inside the guest in `rayito-base-caps` (per-uid routes + local proxy, [Outbound network](network.md)); on any other image they fail closed |
| Presigned URLs and `rayd` SSRF (T16) | the SDK signs them with your credentials, `rayd` stores none; they are never logged; each URL covers one sandbox-bound key, one method and one expiry; `rayd` only accepts `https` to the bucket's exact regional host and its resolver drops loopback, link-local and IMDS ([Files](files.md)) |
| `rayd`'s egress proxy as SSRF (T17) | guard after resolving (loopback, IMDS, the guest's own addresses), the proxy does not resolve denied names, the operator's proxy credentials only over RPC and never in logs; the `Host` header is rewritten only in the absolute `http://` form: a `CONNECT`/SOCKS5 tunnel to an allowed name (80 or 443) shares the risk of shared IPs; under deny-all in `rayito-base-caps`, since 0.3.2 DNS for uid ≥ 1000 is also blocked (a port-53 `ip rule` ahead of the platform resolvers, which listen inside the guest), so neither the connections nor the DNS queries of the sandbox's processes leave the VM; **residual risk**: the guest layers are best-effort and do not resist root in the guest or a kernel exploit: the VPC connector remains the hard platform control |
| Injected secrets (T18) | off by default; with `secrets=` the value travels only in the per-call `envs` (never in `runHookPayload`, `metadata`, logs or errors) and is cached only in the SDK's memory; **residual risk**: the sandbox code can read it (phase 1) and it ends up in the snapshot if suspended ([Secrets](secrets.md)) |
| Metadata index (T19) | off by default; with `index=DynamoDbIndex(...)` only the (non-secret) `metadata` plus the image, `startedAt` and the TTL are copied to your DynamoDB table, never tokens, `envs` or secrets; a fake row never creates a phantom sandbox (the listing starts from `list-microvms` and requires the same image and `startedAt`); writer role (`PutItem`) separate from the reader (`BatchGetItem`) ([Metadata index](funciones-opcionales/indice-de-metadatos.md)) |
| Lifecycle hooks forged from inside the VM (T2) | port 9000 never goes in a token; `rayd` checks which uid owns each connection to the hooks: a `/terminate` or `/validate` from a sandbox process answers 200 and does nothing, a `/run` from it does not consume the boot `/run` (so a template's `start_cmd`, which arrives before the platform, does not install its token), and after the first `/run` a `/ready` or `/validate` restarts no kernel; everything is recorded in `hook_audit` and in `get_health().hook_anomalies`; **residual risk**: forged `/suspend` and `/resume` are accepted (they count as an anomaly), and a `/terminate` that dodges the lookup is accepted but also counts |
| Secret gateway (T24) | off by default; with `gateways=` the value lives only in `rayd`, the `upstream` is fixed (`https://host`) and the allowlist decides before connecting; headers carrying the value are stripped from the response; **residual risk**: the body comes back uninspected, so an `allow` rule to an endpoint that reflects the request headers hands over the secret ([Secret gateway](funciones-opcionales/pasarela-de-secretos.md#lo-que-la-pasarela-no-puede-impedir)) |
| Custom domain (T25, experimental) | off by default; with `CustomDomain` the CloudFront Function deletes every `x-aws-proxy-*` header from the viewer before setting its own, a route requires a traffic token unless an explicit `public=True` (only its sha256 is stored) and an expired route answers 404; **residual risk**: the route is not deleted by itself when the sandbox is killed (call `unregister()`) and each route's JWE lives in the KeyValueStore of your account ([Custom domain](funciones-opcionales/dominio-propio.md)) |
| Agent on AgentCore with no secrets gateway (T36) | `--target agentcore` is opt-in; the agent reads the runtime role's credentials, so that role is least privilege (Bedrock only on the models of `--bedrock-model`, Secrets Manager only on those of `--readable-secret`), with MMDSv2 always on and no secrets copied into the runtime ([details](#agentcore)) |

## What not to put in `envs` or `metadata` { #que-no-poner-en-envs-ni-en-metadata }

`envs` and `metadata` travel together in `runHookPayload`. AWS may record the
payload in CloudTrail data events and any principal with
`lambda:CreateMicrovmAuthToken` on the image can read the metadata through
`Health`; and so can the code running inside the sandbox itself, without any
credential: `Health` is the only anonymous RPC and `rayd` serves it on
`0.0.0.0:8080`, so a uid 1000 process in the sandbox reads its `sandbox_id`
and the full `metadata` map. They are labels and configuration, **not
secrets**: credentials go through `sbx.files.write`, through the `envs=` of
a specific command or, since 0.5.0, through `secrets=` (from Secrets
Manager, with cache; see [Secrets](secrets.md)), never in the creation
payload. Neither `rayd` nor the SDK write `metadata` keys or values to logs
(only the number of keys).

## The E2B shim and the network { #el-shim-de-e2b-y-la-red }

`rayito.e2b.Sandbox` reproduces E2B's defaults: public endpoint
(`ingress=["ALL_INGRESS"]`) and internet egress
(`egress=["INTERNET_EGRESS"]`). A MicroVM without an egress connector in
`run-microvm` inherits the image version's and still reaches the internet
(measured, `AWS_API_NOTES.md` Q44 and Q60), so since 0.3.0
`allow_internet_access=False` and `network=` are applied **inside the
guest** in `rayito-base-caps` and, on any other image, the SDK terminates
the VM and raises `UnimplementedError`: it never hands you back a sandbox
with the network open ([Outbound network](network.md), `SECURITY.md` T17).
To close ingress: `ingress=["NO_INGRESS"]`; outside the guest, the allowlist
through a VPC connector with `rayito.Sandbox(egress=[...])`.

## Supply chain { #cadena-de-suministro }

`rayd` runs as root inside each MicroVM, so what is built and published is
controlled end to end (details in `SECURITY.md` T10 and in the "Supply
chain" table):

- The base image is pinned by digest in `image/Dockerfile`
  (`FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38…`) and
  each publish passes `--base-image-version` explicitly. When AWS moves the
  tag, refreshing the digest is a Dependabot PR (`docker`) or
  `docker buildx imagetools inspect public.ecr.aws/lambda/microvms:al2023-minimal`
  (without Docker: the `Dockerfile`'s own recipe with the `public.ecr.aws`
  token and `Docker-Content-Digest`); a new digest is a new image version,
  published and accepted with the e2e like any other.
- Dependencies audited in CI and every week: `cargo deny` (licenses,
  advisories, sources), `pip-audit` over the image and SDK pins,
  `pnpm audit --prod`.
- `rayd` is built with `cargo auditable` (embedded crate graph) and carries
  a CycloneDX SBOM; each release's assets are signed keyless with cosign,
  and PyPI and npm publish through OIDC with attestations. How to check it:
  [Verify a release](verify.md). The recipe binds the signature to the exact
  version you install (`release.yml@refs/tags/rayd-v<version>`), so an old
  asset or one signed under another tag does not pass it.
- The job that builds has no credentials: signing (OIDC token) and uploading
  to the release (`contents: write`) are separate jobs that do not check out
  or run build tools, and verify with `sha256sum -c` what the build
  produced. Both wait for the maintainer's approval (`release`
  environment): without it there is no OIDC token or Sigstore certificate,
  and a dry run signs nothing. No job of the release, of the documentation
  site or of the e2e restores an Actions cache; zig, `cargo-deny` and uv are
  downloaded against a pinned sha256; the workflow never replaces a
  published asset, and publishing requires the tag's commit to be on `main`.
- A repository token with `contents: write` could replace a release asset
  (immutable releases are still off): that is why the
  [Set up AWS](primeros-pasos/configurar-aws.md) recipe checks the signature
  and `SHA256SUMS` **before** `rayito image publish`, which verifies nothing
  by itself.
- CI tools with dependencies (twine, pip-audit, cfn-lint) are installed from
  requirements with `--hash`, never with `uvx`, and `uv` never re-locks an
  out-of-date `uv.lock` (`UV_LOCKED=1`).
- Dependabot waits 7 days (14 for a major) before proposing a freshly
  published version; security updates do not wait.
- The image artifact builder (`rayito image zip`, `make image-publish`)
  rejects any symbolic link in `image/` and in `kernel-sidecar/`: a link
  would put the contents of a file from the building machine into the image.

## Persistence to S3 (T15) { #persistencia-en-s3-t15 }

With `Sandbox.create(persist=S3Prefix(...))` the user's `HOME` travels to
your bucket. `rayd` does it as root with the execution role (which
`persist=` explicitly requires), not the sandbox code: in
`rayito-base-caps` uid 1000 still cannot reach IMDS. The role can only write
and read under `<bucket>/<prefix>/*` (`infra/iam.yaml`), never delete.
`persist=` also binds the sandbox to its bucket and its `prefix`: `rayd`
rejects with `permission_denied` any checkpoint or restore outside them, so a
`prefix` per tenant separates tenants even if they share a role. A sandbox
created without `persist=` binds nothing and its access token reaches any
destination its role reaches: do not give it a role that reaches the
persistence prefix. The restore runs with the user's identity and only
extracts regular files, directories and symlinks (no devices or hard links),
rejects `..`, absolute paths and parents that leave `HOME`, drops the
setuid/setgid/sticky bits and verifies the archive's sha256. Whoever can
write under your prefix can plant files in the next incarnation's `HOME`:
treat the prefix as part of the sandbox. Add an
`AbortIncompleteMultipartUpload` rule at 1 day to the bucket. Details in
[Persistence](persistence.md) and in `SECURITY.md` T15.

## Custody of user secrets (T18) { #custodia-de-secretos-del-usuario-t18 }

`secrets=` ([Secrets](secrets.md)) is **off by default** and only the option
itself turns it on. When it is used:

- **Channel**: the value travels only in the per-call `envs` of commands,
  PTYs, Python cells and code contexts, over the same authenticated channel
  as files (TLS to the AWS proxy, `x-access-token`); never in
  `runHookPayload`, `metadata`, labels, the image's environment, the pool's
  records, logs, `repr` or errors.
- **At rest**: in Secrets Manager (KMS); on the client, only in the SDK
  process's memory (`SecretCache`, TTL 300 s by default: never one read per
  command); in the VM, in the environment of the processes that received it
  and in the memory snapshot if the sandbox is suspended with them alive
  (open question SEC-5).
- **Who reads what**: reader IAM on the **caller's** credentials
  (`infra/secrets-access.yaml`), never the execution role. **Phase 1: the
  sandbox code can read an injected secret.** With untrusted code, inject
  only short-lived, least-privilege tokens, never long-lived credentials.
- **Logs**: Rayito never writes the value or the name to its logs. The DEBUG
  log of the AWS SDK it uses underneath (botocore/urllib3 in Python, a
  `SecretsManagerClient` `logger` in TypeScript) **does** print the Secrets
  Manager bodies with the value in clear: do not enable it in processes that
  handle secrets ([Secrets](secrets.md#que-activa-y-que-cuesta)).
- The E2B shim's `Secret.fill()` returns the placeholder, but Rayito does
  not resolve it anywhere.

Details in `SECURITY.md` T18.

## Metadata copy at rest (T19) { #copia-de-metadatos-en-reposo-t19 }

With the optional metadata index (`index=DynamoDbIndex(...)`), each sandbox's
`metadata` is kept **at rest** in a DynamoDB table of your account
(`infra/metadata-index.yaml`), encrypted by default and with a TTL. It is the
same `metadata` that is already not secret (T4: `Health` returns it to any
principal that can mint a JWE and to the sandbox code itself), so do not put
in it anything you would not put in a label.

- **What is stored**: `pk` (sandbox id), ARN and version of the image,
  `startedAt`, `metadata`, the SDK version and `expires_at`. Never the access
  token or its hash, `envs`, secrets, the `runHookPayload` or the JWE.
- **Integrity**: a fake row does not create a phantom sandbox; the listing
  starts from `list-microvms` (the state comes from there) and requires the
  same image and `startedAt` (±1 s). Whoever can write to the table can
  change what metadata a real sandbox shows up with or hide it from an
  indexed listing: do not use `metadata` as access control.
- **IAM**: `RayitoIndexWriter` (`dynamodb:PutItem`) for whoever creates
  sandboxes or runs a pool, `RayitoIndexReader` (`dynamodb:BatchGetItem`) for
  whoever lists; both over the table's ARN.
- **Turning it off**: do not pass `index=`; delete the stack to delete the
  table.

Details in `SECURITY.md` T19.

## Events and webhooks (T22) { #eventos-y-webhooks-t22 }

With `events=`/`LifecycleEvents`, `rayd` emits signed events on stdout and
three Lambdas in your account verify, store and deliver them to your
webhooks.

- **Only the MAC authenticates.** The forwarder derives the key from the
  sandbox named by the *log stream* and verifies the MAC before reading the
  content. The stream name does not prove identity: whoever has the
  execution role (readable from the default image, T1) or the build role can
  write to another sandbox's stream, but without its key the MAC does not
  pass.
- **Only what `rayd` emits, and recent.** Strict types, the same
  `sandbox_id` as the stream, fields shaped like `rayd`'s and an
  `occurred_at_ms` less than 24 h old: a line repeated days later is not
  delivered again.
- **`paused` and `resumed` are indicative.** The sandbox code (uid 1000, no
  root) can call `rayd`'s `/suspend` and `/resume` hooks over loopback (T2),
  and `rayd` then emits a validly signed `paused`/`resumed`. The forwarder
  only admits events that advance the sandbox's lifecycle and limits
  `paused`/`resumed` to 20 in a row and one every 30 s per sandbox; the rest
  is counted and dropped. Do not bill or clean up resources just because of a
  `paused`.
- **One sandbox does not degrade the others.** The reconciler queries an
  index with only the open sandboxes (never the whole table) and the
  deliverer is only invoked for new events.
- **A bad line or webhook does not block the others.** The bad line is
  counted and dropped; a webhook with a broken or slow URL fails alone.
  What could not be written or delivered after the retries stays in the
  stack's SQS queues.
- **Two signatures.** `e2b-signature` (E2B's, byte for byte) carries no
  timestamp; `rayito-signature` is an HMAC with the attempt's time and the
  `webhook_id`. At the receiver, verify `rayito-signature` with a 5-minute
  window, deduplicate by `(sandbox_id, event_id)` and use a random secret of
  32 bytes or more.
- **Stack-wide webhooks.** Each webhook receives the events of all the
  sandboxes of its types: do not give `register_webhook` to your customers.
- **Secrets**: the webhook signing ones (`rayito/webhooks/`) never reach a
  sandbox (`RayitoSecretsReader` denies them and the SDK rejects them in
  `secrets=`). Webhook URLs are stored in clear in your table. A rotation of
  a webhook's secret reaches the deliverer within 5 minutes at most (sooner
  if your receiver answers `401`/`403`).
- **Least privilege**: each Lambda writes only its own rows and only in its
  own log group. The caller's policies are per task: `EventsLauncherPolicy`
  for `events=`, `EventsReaderPolicy` for `get_events` (it sees no webhooks
  or the stack key) and `EventsWebhookAdminPolicy` for the webhooks.

Details in `SECURITY.md` T22.

## Who publishes images { #quien-publica-imagenes }

`infra/iam.yaml` separates `SandboxLauncherPolicy` (launch and manage
sandboxes) from `ImagePublisherPolicy` (publish images); `CallerPolicy` is
the union of the two. Attach only the launcher one to a production
service's role: if it is compromised, it cannot publish a backdoored version
of the image that all your sandboxes will use. See [IAM](operacion/iam.md).

`RayitoTemplateBuilder` (`Template.build()`) is also a separate policy and
only reads `rayito/*` of the artifacts bucket, never the `HOME` checkpoints
or the transfers that share the bucket (T26). The optional size guardrail
(`sizes-guard`, T27) denies launching images outside its list **and**
publishing images, so nobody with that policy rebuilds an allowed image at a
larger size; an older, larger version that still exists under an allowed
name can be launched, so keep each name at a single size. See
[Sizes](funciones-opcionales/tamanos.md).

## S3 mounts and telemetry (T20, T23) { #montajes-s3-y-telemetria-t20-t23 }

- `mounts=` (T20) only exists in `rayito-base-caps`: `mount-s3` runs as a
  user of its own (uid 990), never receives a credential by argument or by
  environment, and only mounts buckets from `RAYITO_ALLOWED_MOUNT_BUCKETS` at
  paths under `/mnt/` or `/home/user/`. The bucket name and prefix are not
  secrets: the sandbox code can see them.
- `telemetry=` with `OtlpAuth.execution_role()` (T23) gives the guest the
  execution role's credentials for `PutMetricData`, which cannot be scoped by
  namespace: a compromised sandbox can write arbitrary metrics. If you do
  not trust the sandbox code, use `OtlpAuth.bearer(...)`.

## What is never logged { #que-nunca-se-loguea }

File contents, executed code, PTY bytes, tokens, proxy headers, `envs`,
`metadata`, the hooks' body, nor the raw message of an AWS error (a
signature error carries the canonical string with the session token: the
SDK, the stacks, `Template.build` and the CLI only show its sanitized
summary); nor presigned URLs, buckets, keys or paths of a transfer, file
metadata, egress policy entries, proxy destinations or git credentials, nor
the JWE, the headers, the bodies or the paths of what goes through
`rayito sandbox proxy`, nor secret values or names, nor the prompt,
instructions or text of an `sbx.agent` agent (its errors carry a closed
`reason` and a fixed-table message, and the optional `rayito.agent.run` span
only `gen_ai.*` and token attributes). Only ids, status codes, counts and
durations.

<a id="rayito-sandbox-proxy-m12"></a>

## `rayito sandbox proxy` { #rayito-sandbox-proxy }

The local proxy (`rayito sandbox proxy <id> --port N`, [CLI](cli.md#proxy))
mints the same JWE the SDK's gRPC channel uses, but scoped to **a single
port** (`PortSpec.single(N)`, never `allPorts`) and **never to 9000**, the
lifecycle hooks' (ADR-006): `validate_proxy_port` rejects it before touching
AWS. The listener binds to `127.0.0.1` by default; `--bind` outside loopback
requires `--allow-remote` and warns on stderr. It strips any `x-aws-proxy-*`
header the client brings before forwarding the request, so a local client
cannot impersonate the AWS proxy's authentication.

**Web pages open in the operator's browser**: since the proxy adds the JWE
to everything it forwards and the guest only sees `Host: <endpoint>`, any
web page could use the sandbox's service through DNS rebinding, with a
cross-site POST or with a cross-origin `WebSocket` (with Jupyter,
code-server or a web terminal, that is running code in the sandbox). Since
0.7.0 the proxy only forwards requests whose `Host` is loopback with the
local port, the `--bind` address, `<id>.localhost` or an `--allowed-host`
(otherwise `421`) and whose `Origin`, if present, is one of those origins or
an `--allow-origin` (otherwise `403`); at most `--max-connections` (8)
connections at a time (`503` afterwards). For each connection it forwards
only **one message**: the first rewritten request, with its body delimited
by `Content-Length` or `chunked` (an ambiguous delimitation is `400`);
nothing the client sends afterwards reaches the upstream, and an upgrade the
guest does not accept with `101` is answered with `Connection: close` and
closed, instead of being left as an unfiltered tunnel. **Cookies**: they are
not separated by port, so what the sandbox serves at
`http://127.0.0.1:<port>` receives the cookies of your other local apps and
shares a "site" with them; open it at `http://<id>.localhost:<port>` (its own
jar, the CLI announces it) or in a dedicated browser profile
([Local proxy](funciones-opcionales/proxy-local.md#navegador-y-cookies)).

**Both CLIs, the same guarantee**: the TypeScript CLI's proxy
(`npx rayito sandbox proxy`) runs the same vectors as the Python one
(`testdata/cli/proxy-vectors.json`: headers with a bare CR or LF, `obs-fold`,
names outside the `tchar` set, body delimiting, `Host`/`Origin`, ports) and
they share the caps and deadlines of `limits.json`. The whitespace trimmed
around a header value is only `OWS` (space and tab), not everything a
language calls whitespace: a `Content-Length: 12` followed by a `\x85` is
invalid in both. A `Content-Length` or chunk size that does not fit in a
JavaScript safe integer is `400` in the TypeScript CLI.

Residual risk (`SECURITY.md` T2/T3): while the proxy is running, any process
that reaches the forwarded local port —on the operator's machine, or on
another if `--allow-remote` was used— has the same access to the sandbox as
the operator. A `SUSPENDED` sandbox with auto-resume wakes up on the first
request that reaches it (it bills compute, like any resume).

## Local management dashboard (`rayito dashboard`) { #panel-de-gestion-local }

`rayito dashboard` ([guide](funciones-opcionales/panel-de-gestion.md#panel-web-local))
serves a page and a JSON API on `127.0.0.1` with **your** AWS credentials.
Any web page you open in the same browser could try to use them, so the
server bounds the body (64 KiB at most, `413` if exceeded) and then applies,
in this order and before interpreting it:

- **`Host` and `Origin`**: the same list as the local proxy (`127.0.0.1`,
  `localhost` and `[::1]` with the dashboard's port). A foreign `Host` is
  `421` (DNS rebinding); a present and foreign `Origin`, including `null`,
  is `403`.
- **Session**: the startup URL carries a single-use token (with an expiry)
  that the server exchanges for an `HttpOnly`, `SameSite=Strict`, `Path=/`
  cookie and redirects to the clean URL with `Referrer-Policy: no-referrer`.
  The cookie's name includes the port, because cookies are not isolated by
  port. Without the cookie nothing answers, neither the page nor the static
  files.
- **`Sec-Fetch-Site`**: on `/api/v1`, if the browser sends the header and it
  is neither `same-origin` nor `none`, the request is `403`. It covers what
  `Origin` does not see: an `<img>` or a navigation from the web that a
  sandbox serves with `rayito sandbox proxy` at `127.0.0.1:<another port>` is
  `same-site`, shares the cookie jar and sends no `Origin`. In addition, the
  proxy does not forward the `rayito_dashboard_*` cookies to the sandbox.
- **CSRF**: every `POST` requires `Content-Type: application/json` (a foreign
  web page cannot send it without a preflight) and the `X-Rayito-CSRF` header
  with the session's token, which is only handed to whoever already has the
  cookie. The billed-cost query (`GET /api/v1/cost?billed=true`) also
  requires the token, because even though it is a `GET` it costs money each
  time.
- **Read-only by default**: without `--allow-actions` the server rejects any
  action with `403` before touching AWS. With the option, each action
  requires `"confirm": true`, is validated (ids, per-request cap) and goes
  into a queue with a single worker that respects the TPS.
- **Browser**: CSP without `unsafe-inline` or `unsafe-eval` (the page is
  modules and style sheets of the same origin), `nosniff`, `no-store`,
  `X-Frame-Options: DENY` and `Cross-Origin-Resource-Policy: same-origin`.
  The page writes everything with `textContent`: no AWS data or log line is
  interpreted as HTML.
- **User theme and logo**: validated before starting (closed keys, color
  values, length and font with a strict grammar, SVG without code or external
  references), the CSS is generated by the dashboard from the validated
  values and served from the same origin under the same CSP; the logo, in
  addition, with a CSP of its own (`default-src 'none'`, `sandbox`).
- **What does not leave**: no tokens, JWE, `envs` or `runHookPayload` (they
  do not exist in the model), no AWS message in errors (only its code), no
  URL with the token in any log. The server does not log requests.
- **Cost**: the billed cost (Cost Explorer, paid) is only queried with
  `--cost-explorer`, when pressing the button and with the CSRF token.

Residual risk: while the dashboard runs, any process **on your machine** that
reads the printed URL (for example from the terminal history before it is
used) or that obtains the cookie has the same access as you; the token is
spent on first use. With `--allow-actions` that includes killing sandboxes.
A sandbox's logs are what that sandbox wrote and are shown as is (as in
`rayito sandbox logs`): if your code prints secrets, the dashboard will show
them.

## Management dashboard, phase 3: multiple accounts and quota alerts { #panel-fase-3 }

Summary of **T33** of
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md);
the [guide](funciones-opcionales/panel-extras.md) covers usage.

- **Read roles, not action roles.** `rayito dashboard --account` assumes the
  role you created with `dashboard-read-role` in the target account: only
  five reads (MicroVMs, images, versions and quotas), nothing that writes, no
  Cost Explorer and no logs. It trusts only the role or user you name (never
  an account root) and, with `ExternalId`, only if it is presented.
- **Always read-only.** Additional accounts have no actions even if you
  start with `--allow-actions`; an unknown `?account=` is a `400` with no AWS
  call.
- **No ARN on the page.** The API and the messages only know the label you
  chose; an STS rejection is shown with its code and without its message.
- **Short and visible session.** The assumed session is not requested until
  it is used, lasts {{dashboardAssumeRoleSessionSeconds}} s and leaves
  `rayito-dashboard` as the session name in the target account's CloudTrail.
- **Quota alerts**: the function only reads and writes in its topic, its
  state row and its metrics namespace; the message carries no ids or names,
  and an SNS failure is not counted as sent.
- **CloudWatch dashboard**: the prefix and the groups are validated against a
  closed list of characters before entering a query.

## Management dashboard in your account (the `dashboard` stack) { #panel-de-gestion-en-tu-cuenta }

The `dashboard` stack ([guide](funciones-opcionales/panel-en-tu-cuenta.md))
publishes a Function URL without AWS authentication (`AuthType NONE`,
because a browser does not sign with SigV4), so **anyone on the Internet can
call it**: the protection is that the function validates identity before
doing anything. This is the summary of **T32** of
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).

- **Identity before anything.** Each request to `/api/v1` carries a Cognito
  access token in `Authorization`. Before routing or calling AWS, the
  function checks the format and size, that the algorithm is `RS256` (`none`
  and the others are rejected), the signature against the group's JWKS (with
  the standard library, comparing the whole encoded message), the issuer,
  `token_use=access`, the `client_id`, the expiry, `nbf` and `sub`. Without a
  token, `401`. If Cognito cannot be queried, `503`, never "pass". Only the
  static page and `/config.json` (the Hosted UI domain and the client id,
  both public) do not ask for a token.
- **Two roles.** The function runs with a **read-only** role. Killing,
  suspending and resuming are done by an **actions role** that only trusts
  the function's role, which assumes it **after** checking the user's group
  and with their `sub` in the session name. Without `AllowActions=true` that
  role does not exist.
- **Audit that cannot be rewritten from the application.** One row per
  sandbox and action, written **before** executing (if it fails, the action
  does not run), with who, what, when, request id and IP. Conditional writes
  and no `UpdateItem` or `DeleteItem` (the template denies them).
- **No ambient session.** The token goes in the header, in memory, not in a
  cookie: a foreign web page has nothing to reuse. Each `POST` checks `Origin`
  and `application/json`, and the URL enables no CORS.
- **Theme and logo**: the same validators as the local dashboard; an invalid
  one is discarded and the factory one is served.
- **Cost**: the billed cost only with `EnableCostExplorer=true`. The 24 h
  cache in DynamoDB is not enough (its key carries the window and the region,
  which are chosen by whoever asks), so the function only accepts windows of
  whole months within the last {{dashboardStackCostLookbackMonths}} months and
  known regions, and spends a budget of {{dashboardStackCostMissesPerDay}}
  paid queries per UTC day for the whole stack (not per user) that, once
  exhausted or if it cannot be checked, stops billing. `ReservedConcurrency`
  bounds an avalanche of unauthenticated requests.
- **Actions with live identity**: before assuming the actions role, the
  function checks in Cognito that the user is still enabled and in the group;
  if Cognito does not answer, no action is taken.
- **Nothing secret in logs**: not the token, not `Authorization`, not the
  body.

Residual risk: an access token already issued cannot be revoked (the function
validates it offline): it is valid until it expires
({{dashboardStackAccessTokenHours}} hour) even if you disable the user,
remove them from the group or close their sessions
(`admin-user-global-sign-out` only prevents renewing it). Until then it can
read, but not act, because each action is checked in Cognito; whoever
administers the account can alter the audit table; and the address is public,
so what protects it is the token validation, not the network.

## Coding agent inside the sandbox { #agente-de-codigo-dentro-del-sandbox }

Summary of **T29** and **T30** of
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`ai-agent-core`), the threat model of
[`sbx.agent`](guias/agente-en-el-sandbox.md).

**The agent itself (T29).** `opencode run --auto` answers "yes" to any
permission the model asks for, and deepagents' default backend
(`LocalShellBackend`) runs whatever the model asks without prompting:
**the `AgentPermissions`/`AgentSpec` permissions are not a security
boundary**, they are a preference that a hallucinating model, or a *prompt
injection* from a file in the workdir itself (an `AGENTS.md` or a cloned
repository with hidden instructions), can bypass by asking for another tool
or rephrasing the request. The real boundary is the one that already
protects any other code running inside the sandbox: the MicroVM and closed
egress, which requires passing `allow_internet_access=False` explicitly
(`Sandbox.create` leaves it open by default; the guide's recipes pass it).
Exfiltration can only leave through the `upstream` that the secret gateway
allows, never directly. **Residual risk**: code inside the sandbox can call
the gateway itself on its own, outside the SDK's token budget (there is no
way to tell, from the gateway, a call made by the agent's runtime from one
made by the model asking it to run `curl`). Mitigations: restrict `allow` to
the `(method, path)` of the exact models you use (`bedrock_gateway` never
opens `/model/*`; `gemini_gateway` only opens the given `models`) and
`rate_per_minute`. In OpenAI-style APIs (OpenAI, Azure OpenAI, xAI,
OpenRouter, Groq, Mistral, DeepSeek, LiteLLM) the model goes in the body and
the gateway **cannot** limit it: set the spending cap at the provider (see
[Agent providers](guias/agente-proveedores.md#limites-del-lado-del-proveedor)).
Consumer subscriptions (ChatGPT plan, Claude Pro/Max, Copilot, SuperGrok) are
not supported: see
[Unsupported providers](guias/agente-proveedores.md#proveedores-no-admitidos).

**The runtime's supply chain (T30).** OpenCode and ripgrep are installed by
version and sha256 pinned in `limits.json`, which the template build checks
with `sha256sum -c` before installing; the deepagents venv is installed with
`--require-hashes`; the deepagents runner's sha256 (`runner_sha256`) is
recorded in the template manifest (`/opt/agents/rayito-agent.json`);
`AgentSpec.runtime_version` is today only validated as text and is **not**
compared against that manifest. Autoupdate, model downloads, LSP downloads,
the default plugins and reading a workdir's `.claude/` are off
(`autoupdate: false`, `share: "disabled"`, `OPENCODE_DISABLE_CLAUDE_CODE=1`);
none of that goes out looking for something on the Internet inside a sandbox
with closed egress, and if it tried, it would fail like any other disallowed
outbound connection.

## AgentCore: no secrets gateway (T36) { #agentcore }

Summary of **T36** of
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`agentcore-deploy`, since 0.15.0): the threat model of
[Deploy to AgentCore](guias/desplegar-en-agentcore.md).

**The difference.** In a Rayito MicroVM the agent never sees the model
credential: the secrets gateway adds it when forwarding the request. On
AgentCore there is no Rayito secrets gateway: the agent gets credentials from
the runtime role through the microVM metadata service, and **any code running in
the session can read them**: yours, the framework's and a tool the model decides
to run. The «the agent cannot read the credential» guarantee does not exist
here; what replaces it is least privilege.

**What the runtime role can do** (the one `infra/agentcore-runtime.yaml` creates,
one per agent, which only AgentCore can assume in your account and region):

- pull **its** image from ECR and write to **its** log group
  (`/aws/bedrock-agentcore/runtimes/<runtime>-*`);
- send traces to X-Ray and metrics to the `bedrock-agentcore` namespace;
- get workload identity tokens (`GetWorkloadAccessToken` and
  `GetWorkloadAccessTokenForJWT`) on its runtime's directory and identities,
  **without** `GetWorkloadAccessTokenForUserId`;
- invoke in Bedrock (`InvokeModel` and `InvokeModelWithResponseStream`) **only**
  the ARNs of `--bedrock-model` (by default, the project's model; with no model,
  no Bedrock permission);
- read (`secretsmanager:GetSecretValue`) **only** the secrets of
  `--readable-secret` (by default, none).

It can do nothing else. What the agent tries with those credentials, such as
calling another model or reading another secret, IAM denies.

**What the role does not change.**

- **`PUBLIC` network.** The runtime has unfiltered Internet egress: Rayito's
  egress policy (`egress`, `network`) does not apply, which is why a project that
  declares it is not deployed (`network_policy_unsupported`). Code that reads the
  role's credentials can send them anywhere; if you do not want that risk, give
  the role nothing you cannot afford to lose.
- **MMDSv2 always on.** AgentCore requires version 2 of the metadata service
  (`requireMMDSV2`) since 2026-06-30 and refuses to invoke a runtime without it.
  Rayito enables it right after creating the runtime and on every update.
- **No secrets in the runtime.** The runtime's environment variables can be read
  with `GetAgentRuntime`: Rayito never copies secret values there, nor the key
  of a provider other than Bedrock (`provider_unsupported`). The agent reads what
  it needs with the role, at run time.
- **Sessions and users.** Each session runs in its own microVM, isolated from the
  others, but AgentCore does not tie sessions to users: who may continue which
  session (which `session_id` each person may use) is up to your backend.
- **Authentication.** Invoking a runtime requires
  `bedrock-agentcore:InvokeAgentRuntime` and a SigV4 signature: there is no access
  token or `RAYITO_AGENT_SERVICE_TOKEN` (do not set it on this target).

**The build.** The context zip is uploaded to `rayito/images/agentcore/<name>/` in
the artifact bucket, deleted as soon as the build ends (successfully or not) and,
if any is left, the `rayito setup` bucket expires it after 7 days. The CodeBuild
role only reads that prefix, only writes to its log group and only pushes images
to its repository, which has immutable tags and scan on push. The base image is
pinned by digest.

**What `rayito agent delete` deletes.** Only what Rayito created for that name: a
runtime without the `rayito.agentcore/1` marker in its description is never
touched (`name_conflict`).

**Residual risk.** Whatever the role allows, any code in the session can use; an
organization with SCPs that require tags needs `--tag`, and the tags of an
already created runtime are not updated.

## CDK constructs: custom-resource and image-build roles (T37) { #cdk }

Summary of **T37** in
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`agentcore-cdk`, ADR-032, since 0.15.0): what the
constructs in [Deploy with CDK](guias/desplegar-con-cdk.md) add to what the
CLI already creates.

**The agent's role does not change.** `RayitoAgent` deploys the same
`infra/agentcore-runtime.yaml` template as `rayito agent deploy`, so the runtime
role is the same, with the same least privilege as [AgentCore](#agentcore).
What is new are two pieces that the CLI did with the deployer's credentials
and that CloudFormation does here:

- **The custom-resource functions** (`Custom::RayitoImageBuild` and
  `Custom::RayitoRuntimeMmds`, code in `infra/agentcore-cdk-handler.py`). Their
  role can only call `codebuild:StartBuild` and `BatchGetBuilds` on **that**
  agent's CodeBuild project, `ecr:DescribeImages` on **its** repository,
  `bedrock-agentcore:GetAgentRuntime` and `UpdateAgentRuntime` on **its**
  runtime, and `iam:PassRole` **only** on the runtime role (required by
  `UpdateAgentRuntime`). It cannot create or delete runtimes, pass any other
  role, or read images, secrets or models.
- **The MicroVMs image build role** (`RayitoMicrovmImage`). Only Lambda in your
  account can assume it (`aws:SourceAccount`), it reads **only** the zip asset
  object and writes **only** to its images' `/rayito/<name>` log groups. It does
  not use the shared `rayito-m0-iam` role.

**The code asset.** The agent's CodeBuild build role can read, besides its
usual prefix, the exact object of the code asset in the CDK bootstrap bucket;
nothing else in that bucket.

**Who can change what.** The runtime carries the marker
`rayito.agentcore/1+cdk` and the image the tag `rayito:managed-by=rayito-cdk`:
`rayito agent deploy`, `rayito agent delete` and `rayito image publish` refuse
to modify them before writing anything (not even the artifact in S3), so the
CLI cannot silently undo what the stack declares.

**Residual risk.** Deployment runs with the CDK bootstrap execution role,
which has `AdministratorAccess` by default: whoever can run `cdk deploy` in the
account can change the runtime role. If that is not acceptable, restrict the
bootstrap ([Restricted bootstrap](guias/desplegar-con-cdk.md#bootstrap-restringido)).
The CloudFormation resource has no MMDSv2 property, but AgentCore already
creates every version with MMDSv2 on; `Mmds` checks it on every new version
and would only turn it on outside CloudFormation (where drift detection could
flag it) if one ever came without it. Assets (the agent code, the image zip) stay in the bootstrap
bucket until `cdk gc`.
