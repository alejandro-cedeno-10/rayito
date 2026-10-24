# Custom domain (experimental)

<small>Since 0.7.0, experimental ([What's new](../novedades/0.7.0.md#dominio-propio-experimental)).</small>

## What it does { #que-hace }

Without this feature, a sandbox port is only reachable by sending the
`x-aws-proxy-auth`/`x-aws-proxy-port` headers that `get_host()` returns:
a browser, a webhook or an `<iframe>` cannot. With a custom domain, each
port has a normal HTTPS URL under **your** domain:

```text
https://<port>-<sandbox_id>.<your domain>     e.g. https://8000-3f2a….sbx.example.com
```

which opens from a browser without the proxy header. Underneath there is a
CloudFront distribution **in your account** with a CloudFront Function that,
on each request, looks up the hostname's route in a `KeyValueStore`, checks the
traffic token and forwards to the sandbox, setting the AWS proxy headers
itself.

!!! warning "Experimental: not verified end to end on real AWS"
    `CustomDomain` (deploying and deleting the stack, and route
    `register`/`unregister`/`refresh`) is implemented in Python and TypeScript,
    with unit tests, `cfn-lint` on the template and tests of the routing
    Function. It is published as experimental so you can try it: the API
    may change in a minor.

    **Verified on real AWS** (`AWS_API_NOTES.md`):

    - The CloudFront Function compiles and routes in the real
      `cloudfront-js-2.0` runtime (`TestFunction`, Q121): correct token → continues;
      no token or wrong token → 403; expired or nonexistent route → 404.
    - `CustomDomain.deploy()` creates the template's `KeyValueStore` and Function
      as is (Q140, after adjusting the `Comment` fields to CloudFront's 128-character
      limit), and the rollback and `destroy()` leave nothing behind.

    **Not verified on real AWS**: creating the distribution and serving traffic
    through it (HTTP/1.1, WebSocket, `KeyValueStore` propagation
    latency, JWE renewal). The distribution was not created in the
    acceptance run (Q141), so nobody has yet opened a custom domain URL
    deployed with this template. The repository's e2e
    (`tests/e2e/test_m15_custom_domain.py`, `custom-domain.e2e.test.ts`)
    automates that test for an account where the distribution can be created.

    `Sandbox.create(domain=...)`/`get_host()` also do not return that URL
    on their own yet: `domain=` still raises `UnimplementedError`
    (ADR-024 of `ARCHITECTURE.md`). Today you register the route yourself and
    `route.host` is exactly the hostname above (see the example).

    **If you try it**, tell us how it went in a
    [GitHub issue](https://github.com/alejandro-cedeno-10/rayito/issues/new/choose)
    with `[custom-domain]` in the title: SDK and version, region, which step failed
    (`deploy`, `register`, the HTTP request), the status code and the
    reason from the stack events if there was one. Never paste your
    certificate ARN, your real domain, tokens or JWE: use `example.com` and
    `123456789012`.

!!! info "Cost and activation"
    - **Status**: experimental. The cost figures are list prices and have not
      been measured with a real distribution (see the warning above).
    - **Default**: off. Without instantiating `CustomDomain` the SDK does not
      build any `cloudformation` or `cloudfront-keyvaluestore` client.
    - **Turns on**: `CustomDomain(public_domain="sbx.example.com").deploy(certificate_arn=...)`
      or `rayito domain deploy`.
    - **Resources and AWS calls**: one CloudFront distribution, its routing
      CloudFront Function and a KeyValueStore
      (`infra/custom-domain.yaml`). In use,
      `register()`/`unregister()`/`refresh()` call
      `DescribeKeyValueStore`/`PutKey`/`DeleteKey`.
    - **Approximate cost** (us-east-1,
      [CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/);
      list figures for CloudFront Functions/KeyValueStore since their
      launch, to be reconfirmed in AWS acceptance): $0 at rest;
      ~$0.085/GB + $0.0075/10,000 outbound HTTPS requests; the CloudFront
      Function, ~$0.10 per 1,000,000 invocations (one per request); the
      KeyValueStore, ~$0.50 per 1,000,000 reads (the Function's) and
      ~$5 per 1,000,000 management calls (`PutKey`/`DeleteKey`).
    - **IAM** (credentials of whoever calls the SDK): `cloudformation:*Stack*`
      for `deploy`/`status`/`destroy`, and the CloudFront permissions to
      create and delete the distribution, the Function and the KeyValueStore (the
      stack runs with those same credentials);
      `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey`
      on the stack's KVS.
    - **How to turn it off**: `destroy()` or `rayito domain destroy` deletes the
      distribution (it takes ~15 min to disable first), the Function and
      the KeyValueStore — no route survives, they are ephemeral. Also delete
      the `CNAME` you created in your DNS.

## When to use it { #cuando-usarlo }

- You need a sandbox port to answer on a normal HTTPS URL, without
  the client having to add headers: a browser, a webhook, an
  `<iframe>`.
- **When not to**: a programmatic client that already knows how to send
  `x-aws-proxy-auth`/`x-aws-proxy-port` — `get_host()` already solves that without
  deploying anything; for local development, `rayito sandbox proxy`.

## Installation { #instalacion }

Registering routes signs with SigV4A (required by the data plane of
`cloudfront-keyvaluestore`), which does not come in the default install:

=== "Python"

    ```bash
    pip install "rayito[custom-domain]"   # brings awscrt
    ```

    Without the extra, `register()`/`refresh()`/`unregister()` raise
    `CustomDomainException` with this same command. `deploy()`/`status()`/
    `destroy()` do not need it.

=== "TypeScript"

    ```bash
    npm i @aws-sdk/client-cloudformation @aws-sdk/client-cloudfront-keyvaluestore @aws-sdk/signature-v4a
    ```

    All three are optional peers: they are loaded on the first call that
    uses them.

## Turn it on { #activarlo }

You need three things:

1. **A domain** under which the URLs will live, e.g. `sbx.example.com`.
2. **An ACM certificate in `us-east-1`** (a CloudFront requirement, regardless
   of which region your sandboxes run in) covering
   `*.sbx.example.com`.
3. **A DNS record**: after deploying, a `CNAME` (or alias, in Route 53)
   from `*.sbx.example.com` to the `DistributionDomainName` the
   deployment prints (`dxxxxxxxxxxxx.cloudfront.net`).

=== "CLI"

    ```bash
    rayito domain deploy --public-domain sbx.example.com \
      --certificate-arn arn:aws:acm:us-east-1:<account>:certificate/<id>
    # ... DNS: point a CNAME/alias of *.sbx.example.com to dxxxxxxxxxxxx.cloudfront.net
    rayito domain status
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    domain = CustomDomain(public_domain="sbx.example.com")  # does not call AWS
    status = domain.deploy(certificate_arn="arn:aws:acm:us-east-1:<account>:certificate/<id>")
    print(status.outputs["DistributionDomainName"])  # CNAME target
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.example.com" }); // does not call AWS
    const status = await domain.deploy({
      certificateArn: "arn:aws:acm:us-east-1:<account>:certificate/<id>",
    });
    console.log(status.outputs.DistributionDomainName); // CNAME target
    ```

Without the SDK, the same template with CloudFormation:

```bash
curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/custom-domain.yaml
aws cloudformation deploy --stack-name rayito-custom-domain \
  --template-file custom-domain.yaml \
  --parameter-overrides PublicDomain=sbx.example.com CertificateArn=arn:aws:acm:us-east-1:<account>:certificate/<id>
```

## Expose a port { #exponer-un-puerto }

=== "Python"

    ```python
    import secrets

    from rayito import CustomDomain, Sandbox

    domain = CustomDomain(public_domain="sbx.example.com")
    domain.status()  # reads the KvsArn of the already deployed stack

    with Sandbox.create("rayito-base", allowed_ports=[8000]) as sbx:
        jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]  # (1)!
        traffic_token = secrets.token_urlsafe(32)  # (2)!
        route = domain.register(
            sbx.sandbox_id, 8000, endpoint=sbx.endpoint, jwe=jwe,
            traffic_token=traffic_token, ttl_seconds=2400,
        )
        print(f"https://{route.host}")  # (3)!
        # ... before it expires, with a freshly minted JWE (4):
        fresh_jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]
        domain.refresh(route, jwe=fresh_jwe, ttl_seconds=2400)
        domain.unregister(sbx.sandbox_id, 8000)
    ```

    1. The JWE that `get_host()` already issues; it is the same one the AWS
       Lambda MicroVMs proxy validates. The Function adds it for you on every request.
    2. Whoever opens the URL sends it in the `e2b-traffic-access-token`
       header or in the `rayito_tt` cookie (the practical one in a browser:
       your application sets it for `.sbx.example.com`). Without a token,
       `register()` rejects the call — a route is only public with an
       explicit `public=True`, never by default.
    3. `https://8000-<sandbox_id>.sbx.example.com`.
    4. `refresh()` needs a new JWE: `get_host()` mints it again
       on demand. Passing the original again only extends the route's
       TTL, not the token the AWS proxy checks.

=== "Python (async)"

    ```python
    import asyncio
    import secrets

    from rayito import AsyncCustomDomain, AsyncSandbox


    async def main() -> None:
        domain = AsyncCustomDomain(public_domain="sbx.example.com")
        await domain.status()

        async with await AsyncSandbox.create("rayito-base", allowed_ports=[8000]) as sbx:
            jwe = (await sbx.get_host(8000)).headers["x-aws-proxy-auth"]
            route = await domain.register(
                sbx.sandbox_id, 8000, endpoint=sbx.endpoint, jwe=jwe,
                traffic_token=secrets.token_urlsafe(32), ttl_seconds=2400,
            )
            print(f"https://{route.host}")
            await domain.unregister(sbx.sandbox_id, 8000)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```typescript
    import { randomBytes } from "node:crypto";
    import { CustomDomain, Sandbox } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.example.com" });
    await domain.status(); // reads the KvsArn of the already deployed stack

    const sbx = await Sandbox.create({ template: "rayito-base", allowedPorts: [8000] });
    try {
      const jwe = (await sbx.getHost(8000)).headers["x-aws-proxy-auth"] ?? "";
      const trafficToken = randomBytes(32).toString("base64url");
      const route = await domain.register(sbx.sandboxId, 8000, {
        endpoint: sbx.endpoint,
        jwe,
        trafficToken,
        ttlSeconds: 2400,
      });
      console.log(`https://${route.host}`);
      await domain.unregister(sbx.sandboxId, 8000);
    } finally {
      await sbx.kill();
    }
    ```

## Remove it { #quitarlo }

=== "CLI"

    ```bash
    rayito domain destroy
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    CustomDomain(public_domain="sbx.example.com").destroy()  # waits up to 30 min
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    await new CustomDomain({ publicDomain: "sbx.example.com" }).destroy(); // waits up to 30 min
    ```

Then delete the `CNAME` from your DNS. The ACM certificate is yours and is not touched.

## Explicit names instead of the wildcard { #nombres-explicitos-en-vez-del-comodin }

By default the distribution carries the alias `*.<your domain>`. CloudFront does not
allow the same alias on two distributions, so if another distribution
already has that wildcard, the deployment fails. You can pass a list of
exact hostnames (each `<label>.<your domain>`, normally
`host_for(alias, port)`): on an overlap CloudFront picks the most
specific name, so they coexist with the other party's wildcard. Only routes whose
hostnames are on the list reach this distribution; the cost does not change.
One exception: if your DNS already has a wildcard record that points to the other
distribution, CloudFront rejects the more specific name until you create
a record of its own for it that points to this one.

=== "CLI"

    ```bash
    rayito domain deploy --public-domain sbx.example.com \
      --certificate-arn arn:aws:acm:us-east-1:<account>:certificate/<id> \
      --alternate-domain-name 8000-demo.sbx.example.com
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    domain = CustomDomain(public_domain="sbx.example.com")
    domain.deploy(
        certificate_arn="arn:aws:acm:us-east-1:<account>:certificate/<id>",
        alternate_domain_names=[domain.host_for("demo", 8000)],  # 8000-demo.sbx.example.com
    )
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.example.com" });
    await domain.deploy({
      certificateArn: "arn:aws:acm:us-east-1:<account>:certificate/<id>",
      alternateDomainNames: [domain.hostFor("demo", 8000)], // 8000-demo.sbx.example.com
    });
    ```

## Test without touching DNS { #probar-sin-tocar-el-dns }

Before creating the `CNAME` you can check the distribution by connecting
directly to its `*.cloudfront.net` and sending your hostname as SNI and as
`Host` (CloudFront picks the distribution by the `Host`):

```bash
curl --connect-to 8000-<sandbox_id>.sbx.example.com:443:dxxxxxxxxxxxx.cloudfront.net:443 \
  -H "e2b-traffic-access-token: <token>" https://8000-<sandbox_id>.sbx.example.com/
```

That is also how the repository's e2e works
(`tests/e2e/test_m15_custom_domain.py`, `custom-domain.e2e.test.ts`): it takes
the domain and the certificate only from `RAYITO_E2E_DOMAIN` (a domain whose
wildcard the certificate covers) and `RAYITO_E2E_CERT_ARN`, deploys its own
stack per run (`rayito-cd-e2e-<id>`) with random explicit names
so as not to clash with any existing alias, creates no DNS record and
deletes everything when it finishes. Without those variables it is skipped.

## How it works { #como-funciona }

1. The distribution has a single "placeholder" origin in the template —
   it is never actually contacted.
2. Each request goes first through the CloudFront Function
   (`infra/functions/custom_domain_router.js`), which deletes any
   `x-aws-proxy-*` header the viewer sent, reads the route from the
   `KeyValueStore` by the hostname's first label
   (`{port}-{alias}`), checks the traffic token if the route is not
   public, and calls `cf.updateRequestOrigin()` with the sandbox's real
   `endpoint` and the proxy headers that are needed.
3. A route with no entry in the `KeyValueStore`, or whose `ttl_seconds` has
   already passed, gets a 404 straight from the Function: it never reaches the
   placeholder origin, and from outside it is indistinguishable from a route that never
   existed.

## Known limitations { #limitaciones-conocidas }

- Without `Sandbox.create(domain=)` or `get_host()` wired up, you have to
  register the route by hand (as in the example).
- The traffic token is only stored as its sha256 hash: if you lose it,
  generate a new one and register the route again.
- A route is not deleted by itself when the sandbox dies: call
  `unregister()` yourself. Without that, the route is bounded by two
  independent sides: `ttl_seconds` of `register()`/`refresh()` (which the Function
  itself checks — 404) and the JWE's own expiry, on the AWS
  proxy side (401/403), whichever comes first.
- There is no automatic refresher (DOM-14): call `refresh()` before the JWE
  expires if the route must live longer.
- Ports `8080` and `9000` (hooks port and reserved) cannot be
  exposed, as in the rest of the SDK.
- If your account is in an AWS organization, an SCP can deny
  `cloudfront:CreateDistribution` even if your role is an administrator. Then
  `deploy()` fails with `StackException` (the stack ends up in
  `ROLLBACK_COMPLETE`; the reason, `AccessDenied ... explicit deny in a
  service control policy`, shows up in the stack events,
  `aws cloudformation describe-stack-events`). Nothing is left created: the
  rollback deletes the Function and the KeyValueStore, and `destroy()` removes the failed
  stack. Ask the organization's administrator to allow that action
  (Q141 of `AWS_API_NOTES.md`).
