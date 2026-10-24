# Secret gateway

An agent that calls an external API (Anthropic, OpenAI, your own backend)
normally needs its code to *have* the credential: `secrets=` delivers it
as an environment variable, readable by anything the agent runs.
`gateways=`/`gateways` offers the alternative: `rayd` opens, inside the
sandbox itself, a loopback HTTP listener for each route you declare.
The sandbox sends it requests without a credential; the gateway checks a
method/path allowlist and a per-minute request limit, injects the real
header (resolved from Secrets Manager, never stored in the sandbox) and
forwards to the fixed `upstream` you declared. The sandbox code can
**use** the secret; it cannot **read** it, unless the allowed `upstream`
reflects it in its response (see [What the gateway cannot
prevent](#lo-que-la-pasarela-no-puede-impedir)).

<small>Since 0.6.0 (M15, ADR-023).</small>

!!! info "Cost and activation"
    - **Default**: off. Without `gateways=`/`gateways`, `rayd` opens
      no loopback socket for this feature and the SDK builds
      no new `secretsmanager` client or sends any
      `ConfigureSandbox`. No environment variable turns it on
      (ADR-014 rule 4).
    - **Turns on**: `Sandbox.create(gateways={"name": SecretGateway(...)})`
      or `pool.take(gateways=...)` (Python) /
      `Sandbox.create({ gateways: { name: new SecretGateway({...}) } })` or
      `pool.take({ gateways })` (TypeScript). It requires a 0.6.0 or
      later image: with an earlier one, `create()`/`take()` terminates the
      MicroVM just launched or taken (except `keep_on_failure`/`keepOnFailure` in
      `create()`) and raises `UnimplementedError`.
    - **Resources and AWS calls**: `secretsmanager:GetSecretValue` once
      per header and TTL of the `SecretCache` that `secrets=`/`secrets` already uses
      (a hit does not call AWS), and one more per header on each
      `sbx.gateways.refresh()` (it always rereads); no new resources —
      it reuses `infra/secrets-access.yaml`.
    - **Approximate cost** (us-east-1, checked 2026-09-30,
      [pricing](https://aws.amazon.com/secrets-manager/pricing/)): that of
      `SecretCache` ($0.05 per 10,000 calls) plus that of the secret itself
      ($0.40/month) if it did not already exist.
    - **IAM** (credentials of whoever calls the SDK, not the execution role: the
      gateway runs inside `rayd`, it never needs the execution role):
      `RayitoSecretsReader` from `infra/secrets-access.yaml`.
    - **How to turn it off**: do not pass `gateways=`/`gateways` (its default
      value).

## What it does, exactly { #que-hace-exactamente }

For each route in `gateways=`/`gateways`:

1. `rayd` opens a `TcpListener` on `127.0.0.1:<OS-chosen port>`.
2. The request path goes through an allowed list before the allowlist is
   consulted, or it is rejected with 403: only RFC 3986 path characters
   except `;` (letters, digits, `-._~`, `:@`, `!$&'()*+,=` and
   well-formed `%XX`), no empty segment (`//`) except the last, and
   each segment, decoded once, must be valid UTF-8, with no control
   bytes, no `/`, `\`, `%` or `;`, and different from `.` and `..`.
   `rayd` never normalizes the path, so it never forwards one that the
   `upstream` (or the CDN in front of it) could take outside `allow`:
   neither `/v1/../admin` nor `/v1/%2e%2e/admin`, nor `/v1/..;/admin` (Tomcat,
   Spring or Jetty strip the `;` parameter and see `..`), nor
   `/v1/%252e%252e/admin` (a hop that decodes twice sees `..`)
   gets through a `/v1/*` rule. `/v1/files/a%20b` does pass. The `allow`
   paths follow the same rule: the SDK rejects one that no
   request could satisfy with `InvalidArgumentException`/
   `InvalidArgumentError` before any call, and `rayd` with
   `invalid_allow_path`.
3. An incoming request is compared against the `allow` allowlist
   (`(method, path)`, exact or with a `/*` suffix) and the
   `rate_per_minute`/`ratePerMinute` limit (a token bucket; `0` uses the default
   value, 600). If it fails either check, the
   gateway answers without opening any connection to the `upstream` (403 outside
   the allowlist, 429 above the limit).
4. If it passes, `rayd` removes from the request any header whose name
   matches one in `headers` (so the sandbox can never spoof its
   own credential) and injects the real value of each one.
5. It forwards to the `upstream` (always `https://host`, with no path, query,
   user or fragment: the path and query come from each request) through
   a shared HTTPS client that never resolves a host to loopback, link-local or the guest's own IMDS. The
   request and the response are streamed: a Server-Sent Events response
   or a chunked upload goes through the gateway
   unchanged. If the `upstream` does not accept the connection within 10 s or does not send the
   response header within 600 s, the gateway answers 504
   (`upstream_timeout`); any other failure after connecting (TLS, dropped
   connection, malformed response) is 502 (`upstream_error`). A streamed
   response body has no cap.
6. The `upstream`'s response reaches the sandbox with its status and its body
   **unchanged**. From its headers, `rayd` removes the transport ones, those
   carrying the name of one in `headers` and any whose value
   contains one of the injected values (of 8 characters or more).

## What the gateway cannot prevent { #lo-que-la-pasarela-no-puede-impedir }

!!! warning "An endpoint that reflects the request hands over the secret"
    The gateway does not inspect the response body. If an `allow` rule
    points at an endpoint that returns the request headers
    (debug or echo routes like `/headers` or `/anything`,
    verbose error pages that repeat `Authorization` or `x-api-key`),
    the sandbox code reads the secret in that body. The guarantee is "the
    sandbox can use the secret and cannot read it **unless the allowed
    `upstream` reflects it**": it is a limit the gateway cannot
    enforce for you. List in `allow` only the paths your agent
    needs and never a debug or echo one
    ([`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md), T24).

The `headers` names must be a valid HTTP *token* (RFC 9110), cannot
be repeated ignoring case (`X-Api-Key` and `x-api-key` are the
same) nor be a transport header the gateway sets itself
(`host`, `content-length`, `transfer-encoding`, `connection`, `keep-alive`,
`te`, `trailer`, `upgrade`, `proxy-authorization`, ...): the SDK rejects it
when building `SecretGateway`, and `rayd` checks it again.

Each header's value lives only in `rayd`'s memory, never in
`repr`/`Debug`, never in a log: it is resolved right before each
`ConfigureSandbox` (the RPC itself is never logged) and is exposed only
once, when building the outgoing request.

## Rotation { #rotacion }

`sbx.gateways.refresh()` (`await sbx.gateways.arefresh()` on
`AsyncSandbox`) drops the gateway's secrets from the `SecretCache`,
rereads them from Secrets Manager (even if their TTL has not expired) and sends
a new `ConfigureSandbox`. Each route **keeps its port**: a process
started with `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` keeps
working, and its next request, even over a keep-alive connection
already open, carries the new value. If `rayd` rejects the configuration
(for example a rotated value with a line break), `refresh()` raises the
same exception as `create()` and the gateway continues with the previous
configuration.

!!! note "Right after rotating, `refresh()` may push the old value"
    Secrets Manager is eventually consistent: for a few seconds
    after `SecretStore.update()` (or a rotation), `GetSecretValue`
    may still return the previous version, and `refresh()` sends what it
    reads. If you need the next request to already carry the new value,
    check that it does (for example, with a test request to an
    endpoint that costs nothing) and, if not, repeat `refresh()` after a
    short pause, a few times. If you rotate on a schedule (a key that
    expires), it is enough to do the `refresh()` with some margin before the
    old one expires.

A route that is no longer in the configuration is truly closed: it stops
accepting connections, immediately closes idle keep-alive ones and lets at most
the in-flight request finish.

## Renew the short-term Bedrock key { #renovar-la-clave-de-bedrock }

<small>Since 0.17.0. Off by default.</small>

The `bedrock` gateway injects `Bearer <Bedrock API key>`. A long-term key comes
from an IAM user; a **short-term** one is signed locally with the credentials of
the process that renews it, lasts at most 12 h (and never longer than that session) and only
in its region. With `renew_gateway_keys=True` (`renewGatewayKeys: true`,
`--renew-gateway-keys`), the fleet renews it by itself: before creating each VM
or opening each slot, for every gateway with the `bedrock` preset:

1. If this process renewed it less than 900 s ago, nothing.
2. Otherwise, it reads the secret's `LastChangedDate` (`DescribeSecret`) and, if
   it changed less than 900 s ago (another process renewed it), nothing.
3. Otherwise, it generates the key with the official AWS generator
   (`aws-bedrock-token-generator` / `@aws/bedrock-token-generator`, local
   signing, no AWS call), writes it as `Bearer <key>` (`PutSecretValue`) and
   refreshes the gateways of the VMs this process holds open.

`deploy_agent()` / `deployAgent()` (and `rayito agent deploy
--renew-gateway-keys`) also write a fresh key once, before the first VM,
without looking at the 900 s: a secret just created with a placeholder value
gets the key and the first invocation does not get a 403. The window only
limits the renewals of each new VM (since 0.17.1).

The key and the secret name never appear in logs, errors or the
[fleet recipe](../guias/flota-de-agentes.md#la-receta-de-la-flota). Without a
`bedrock` gateway, the option is `InvalidArgumentException`
(`renew_gateway_keys necesita una pasarela con preset bedrock`). VMs opened by
another process keep the previous key until they are refreshed; in a fleet
that can grow, VMs above `min_vms` end after `scale_in_after_seconds` without
use, and the others live `serve.timeout_seconds`.

- **Fixed-size fleet.** Renewal only runs when a VM is created or a slot is
  opened. A fleet that never grows (for example `--min-vms 1 --max-vms 1`)
  renews nothing after its first VM, and its key expires after 12 h at most
  (sooner if the signing session is shorter). Renew it yourself periodically
  (for example, a `scale` or a new `deploy`) or use a long-term key.
- **Never from the recipe.** The recipe records whether the deploy renewed,
  but `AgentFleet.connect(name)` only renews when the caller asks for it
  (`renew_gateway_keys=True` / `renewGatewayKeys: true`), because the key is
  signed with the caller's credentials. If the recipe asked for it and you do
  not pass it, the fleet does not renew and warns `recipe_renewal_ignored`
  (only the code, no path or values); an explicit `False` does not warn. From
  the CLI, `rayito agent invoke <fleet> --renew-gateway-keys` asks for it. With
  the option, an error (the missing `bedrock-keys` extra, `permission_denied`
  when writing the secret) propagates.
- **A Bedrock-only secret.** If the `bedrock` gateway's secret is also read by
  a gateway whose upstream is not Bedrock, the option is
  `InvalidArgumentException` and nothing is renewed: the new key would end up
  sent to that other upstream. Only an
  `https://bedrock-runtime.<region>.amazonaws.com` upstream with a valid AWS
  region (`us-east-1`) counts as Bedrock; a host like
  `bedrock-runtime.evil.s3.amazonaws.com` does not.

=== "Python"

    ```python
    from rayito import AgentDeployOptions, deploy_agent

    # pip install "rayito[bedrock-keys]"
    fleet = deploy_agent(
        "./mi-agente",
        options=AgentDeployOptions(bucket="amzn-s3-demo-bucket", renew_gateway_keys=True),
    )
    ```

=== "TypeScript"

    ```ts
    import { deployAgent } from "rayito";

    // pnpm add @aws/bedrock-token-generator @aws-sdk/client-secrets-manager
    const fleet = await deployAgent("./mi-agente", {
      options: { bucket: "amzn-s3-demo-bucket", renewGatewayKeys: true },
    });
    ```

!!! info "Cost and activation"
    - **Turns on:** `renew_gateway_keys=True` on `AgentFleet`,
      `AgentFleet.connect()`, `AsyncAgentFleet` or `AgentDeployOptions`
      (`renewGatewayKeys`, `--renew-gateway-keys`, MicroVMs only). Without it
      the generator is not imported and Secrets Manager is not called.
    - **AWS resources and calls:** nothing new; `DescribeSecret` before creating
      a VM or opening a slot (except inside the window) and, at most once every
      900 s per gateway, `PutSecretValue` on the secret the gateway already
      uses. Signing the key is local.
    - **Approximate cost:** $0.05 per 10,000 Secrets Manager calls (us-east-1,
      checked on 2026-10-22,
      [pricing](https://aws.amazon.com/secrets-manager/pricing/)); the key is
      free.
    - **IAM:** `secretsmanager:DescribeSecret` and
      `secretsmanager:PutSecretValue` on the gateway's secret, and
      `bedrock:CallWithBearerToken` on the signing principal (condition
      `bedrock:bearerTokenType` = `SHORT_TERM`), besides the model invocation
      permissions.
    - **How to turn it off:** do not pass the option (default `False`).
    - **Example:** `rayito agent deploy ./mi-agente --renew-gateway-keys`.

## Limits { #limites }

| | Value | Why |
|---|---|---|
| Routes per sandbox | 8 | each opens its own listener; a limit avoids exhausting the guest's ephemeral port range |
| Headers per route | 16 | — |
| `allow` rules per route | 32 | — |
| `rate_per_minute`/`ratePerMinute` | `0` (= 600) or 1..6000 | 600 = 10 requests/s, a conservative cap for an agent calling a single API interactively |
| Route name | 1-64 `[a-z0-9-]` | it is the public key of `sbx.gateways["name"]` |
| Wait for the response header | 600 s | enough for a long LLM request; a stuck `upstream` ends in 504 instead of hanging the command forever |
| Connection to the `upstream` | 10 s | — |

## Examples { #ejemplos }

=== "Python"

    ```python
    from rayito import (
        PoolConfig,
        Sandbox,
        SandboxPool,
        SecretCache,
        SecretGateway,
        SecretStore,
    )

    store = SecretStore(region="us-east-1")          # does not call AWS
    store.create("anthropic", "sk-ant-...")

    sbx = Sandbox.create(
        gateways={
            "anthropic": SecretGateway(
                upstream="https://api.anthropic.com",
                headers={"x-api-key": "anthropic"},   # secret name, not the value
                allow=[("POST", "/v1/messages")],
                rate_per_minute=600,
            )
        },
        secret_cache=SecretCache(),
    )
    url = sbx.gateways["anthropic"].url               # "http://127.0.0.1:<port>"
    sbx.commands.run("python agent.py", envs={"ANTHROPIC_BASE_URL": url})

    # rotate the credential without recreating the sandbox (same port, new value):
    store.update("anthropic", "sk-ant-new")
    sbx.gateways.refresh()

    sbx.kill()

    # from a pool: warm slots never carry the gateway
    with SandboxPool(PoolConfig(size=2, template="rayito-base")) as pool:
        pooled = pool.take(gateways={"anthropic": SecretGateway(
            upstream="https://api.anthropic.com",
            headers={"x-api-key": "anthropic"},
            allow=[("POST", "/v1/messages")],
        )})
        pooled.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SandboxPool, SecretCache, SecretGateway, SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" });   // does not call AWS
    await store.create("anthropic", "sk-ant-...");

    const sbx = await Sandbox.create({
      gateways: {
        anthropic: new SecretGateway({
          upstream: "https://api.anthropic.com",
          headers: { "x-api-key": "anthropic" },   // secret name, not the value
          allow: [["POST", "/v1/messages"]],
          ratePerMinute: 600,
        }),
      },
      secretCache: new SecretCache(),
    });
    const url = sbx.gateways.get("anthropic")!.url;   // "http://127.0.0.1:<port>"
    await sbx.commands.run("python agent.py", { envs: { ANTHROPIC_BASE_URL: url } });

    // rotate the credential without recreating the sandbox (same port, new value):
    await store.update("anthropic", "sk-ant-new");
    await sbx.gateways.refresh();

    await sbx.kill();

    // from a pool: warm slots never carry the gateway
    await using pool = await new SandboxPool({ size: 2, template: "rayito-base" }).start();
    await using pooled = await pool.take({
      gateways: {
        anthropic: new SecretGateway({
          upstream: "https://api.anthropic.com",
          headers: { "x-api-key": "anthropic" },
          allow: [["POST", "/v1/messages"]],
        }),
      },
    });
    ```

`sbx.gateways` is a read-only map `{name: GatewayStatus}`
(TypeScript: `sbx.gateways.get(name)`, `.entries()` and `.size`). Each
`GatewayStatus` carries `port`, `url` (`http://127.0.0.1:<port>`) and
`last_error_class`/`lastErrorClass` (`None`/`undefined` unless the route
has failed). Without `gateways=`, `sbx.gateways` is empty and `refresh()`
does nothing.

## Gateways for an agent's model { #pasarelas-para-el-modelo-de-un-agente }

For the model of an [agent in the sandbox](../guias/agente-en-el-sandbox.md),
three functions build the `SecretGateway` with the `upstream`, the header
and an `allow` already restricted to the provider (and, on Bedrock, to the models
you choose). Building them does not call AWS: the cost and activation are those of
`SecretGateway`.

| Python | TypeScript | Upstream | Header | `allow` |
|---|---|---|---|---|
| `bedrock_gateway(secret, region=, models=)` | `bedrockGateway(secret, { region, models })` | `https://bedrock-runtime.<region>.amazonaws.com` | `authorization` (the secret stores `Bearer <key>`) | `POST /model/<id>/converse-stream` and `/converse` of each model |
| `anthropic_gateway(secret)` | `anthropicGateway(secret)` | `https://api.anthropic.com` | `x-api-key` | `POST /v1/messages` |
| `openai_compatible_gateway(secret, upstream=, base_path=)` | `openaiCompatibleGateway(secret, { upstream, basePath })` | the `upstream` you pass | `authorization` (`Bearer <key>`) | `POST <base_path>/chat/completions` |

OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI and your
own LiteLLM proxy have their preset (`openai_gateway`,
`gemini_gateway`, `litellm_gateway`…): see
[Agent providers](../guias/agente-proveedores.md). In OpenAI-style APIs the
model goes in the body and the gateway cannot limit it: set the spending
cap at the provider.

They all also accept `rate_per_minute=`/`ratePerMinute`. `secret` is the
secret's name (or a `SecretRef`), never the value.

=== "Python"

    ```python
    from rayito import Sandbox, anthropic_gateway, bedrock_gateway

    with Sandbox.create(
        gateways={
            "bedrock": bedrock_gateway(
                "bedrock-key",
                region="us-east-1",
                models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
                rate_per_minute=60,
            ),
            "anthropic": anthropic_gateway("anthropic"),
        }
    ) as sbx:
        print(sbx.gateways["bedrock"].url)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, anthropicGateway, bedrockGateway } from "rayito";

    await using sbx = await Sandbox.create({
      gateways: {
        bedrock: bedrockGateway("bedrock-key", {
          region: "us-east-1",
          models: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
          ratePerMinute: 60,
        }),
        anthropic: anthropicGateway("anthropic"),
      },
    });
    console.log(sbx.gateways.get("bedrock")?.url);
    ```

`bedrock_gateway` does not accept ARNs in `models` (the gateway rejects an
encoded `/` in the path): use the model id or the inference profile id.
How to connect it to `sbx.agent`:
[Agent in the sandbox](../guias/agente-en-el-sandbox.md#1-el-secreto-y-la-pasarela).

## Errors { #errores }

- A malformed `SecretGateway` (forbidden or repeated header, impossible
  `allow` path, `upstream` that is not `https://host`, more routes or
  rules than the [limits](#limites)) is `InvalidArgumentException`/
  `InvalidArgumentError` when building it or passing it to `create()`/`take()`,
  before any call to AWS.
- A secret that does not exist is `SecretNotFoundException`/
  `SecretNotFoundError`: it is read right before configuring the already
  launched sandbox, which `create()` terminates (except `keep_on_failure`/
  `keepOnFailure`).
- If `rayd` rejects the configuration, `create()`/`take()`/`refresh()`
  raise `SandboxException`/`SandboxError` with the failure class in the
  message (`UnimplementedError` on an image older than 0.6.0).
- What the gateway rejects **per request** is never an SDK exception: the
  sandbox process receives the HTTP response (403 outside
  `allow`, 429 above the limit, 502/504 if the `upstream` fails).
  That is why the SDK has no exception of its own for the gateway.

## Differences from E2B { #divergencias-con-e2b }

E2B has no equivalent: it has no sandbox-internal proxy switch
that injects a credential from an external backend. `rayito.e2b` adds
no method for this feature; `Sandbox.create(gateways=)` is
native to Rayito.

Internal contract (no AWS; `AWS_API_NOTES.md` §28 documents the reuse
of Secrets Manager, already covered by §19).
