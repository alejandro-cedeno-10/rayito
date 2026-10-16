# Lifecycle events and webhooks

`rayd` emits a signed line for each lifecycle transition
(`created`, `paused`, `resumed`, `killed`); in your own account, a Lambda
forwarder/deliverer/reconciler verifies, stores and delivers them to
your webhooks with E2B's signature.

!!! info "Cost and activation"
    - **Default**: off. Without `events=` (TypeScript: `events`) the SDK
      builds no DynamoDB, Secrets Manager or
      CloudFormation client, and `rayd` emits no event line.
    - **Turns on**: an explicit call to `LifecycleEvents().deploy(...)` (or
      `rayito events deploy`); afterwards, `events=LifecycleEvents(...)` in
      `Sandbox.create()` together with a `logging` that reaches CloudWatch
      (`"cloudwatch"` or `{"cloudWatch": {...}}`: the forwarder reads from there).
    - **Resources and AWS calls**: `Sandbox.create(events=...)` makes one
      `secretsmanager:GetSecretValue` of the stack key per `LifecycleEvents`
      instance (plus a `cloudformation:DescribeStacks` if that
      instance was not the one that deployed the stack) and sends that
      sandbox's key to `rayd` in the same `ConfigureSandbox` as the rest of
      the 0.6 options. `deploy()` creates in your account
      (`infra/events-webhooks.yaml`) an HMAC secret, an on-demand
      DynamoDB table with streams, three Lambdas with a log group each, a
      CloudWatch Logs subscription, two SQS queues (the forwarder's
      invocations and the deliveries that exhaust their retries), an
      EventBridge Scheduler rule and four managed IAM policies. `register_webhook`/`list_webhooks`/
      `delete_webhook`/`get_events` call DynamoDB directly; the
      forwarder, the deliverer and the reconciler do the rest inside your
      account.
    - **Approximate cost** (us-east-1, checked 2026-09-30): ~$0.40/month for the
      secret; DynamoDB, Lambda and SQS are on-demand/pay-per-use ($0 at rest);
      ~$1.25 per million events written (WRUs) plus the reads of
      `get_events`; the reconciler bills one invocation every
      `reconciler_interval_minutes` (5 by default, minimum 2,
      ~$0.0000002 each).
    - **IAM** (credentials of whoever calls the SDK, not the sandbox role):
      one policy per task, all outputs of the stack: `EventsLauncherPolicy`
      for `events=`, `EventsReaderPolicy` for `get_events` and
      `EventsWebhookAdminPolicy` to manage webhooks (see
      [Which policy each call needs](#que-politica-necesita-cada-llamada)).
      `EventsOperatorPolicy`, the union of the three, is deprecated.
    - **How to turn it off**: stop passing `events=`; `destroy()` deletes the
      secret (force-delete: any registered webhook can no longer be
      verified), the table, the three Lambdas with their log groups (and their
      logs), the subscription, the queues and the scheduler. First detach the
      stack's policies from the users and roles you attached them to:
      otherwise CloudFormation cannot delete them and the stack ends up in
      `DELETE_FAILED`. Each webhook's secrets (`rayito/webhooks/...`)
      and the image's log group are kept: delete them separately if you no longer
      want them. A stack deployed with an SDK 0.6.x may have left
      `/aws/lambda/rayito-events-webhooks-*` log groups that the Lambdas
      themselves created: they are not deleted on their own either.

## When to use it { #cuando-usarlo }

- You need to react to a sandbox being created, paused, resumed or
  dying, without polling `Health`/`list-microvms` yourself.
- You bill per sandbox-hour, clean up external resources when a sandbox is killed,
  or audit the lifecycle for compliance.
- **When not to**: if on-demand `get_metrics`/`Health` is enough for you, or
  CloudTrail data events, you do not need this stack.

## Quick example { #ejemplo-rapido }

Deploy the stack once (you need an S3 bucket for the Lambdas' code):

=== "Python"

    ```python
    from rayito import LifecycleEvents

    ev = LifecycleEvents()  # does not call AWS
    ev.deploy(artifact_bucket="my-bucket", log_group_name="/rayito/rayito-base")
    wh = ev.register_webhook(
        "https://hooks.example.com/rayito",
        secret_name="my-webhook",  # already created with SecretStore(prefix="rayito/webhooks/")
        types=["sandbox.lifecycle.killed", "sandbox.lifecycle.paused"],
    )
    for event in ev.get_events(limit=10):
        print(event.sandbox_id, event.type, event.occurred_at_ms)
    ```

=== "TypeScript"

    ```ts
    import { LifecycleEvents } from "rayito"; // + npm i @aws-sdk/client-dynamodb @aws-sdk/client-secrets-manager

    const ev = new LifecycleEvents();
    await ev.deploy({ artifactBucket: "my-bucket", logGroupName: "/rayito/rayito-base" });
    const webhook = await ev.registerWebhook("https://hooks.example.com/rayito", {
      secretName: "my-webhook",
      types: ["sandbox.lifecycle.killed", "sandbox.lifecycle.paused"],
    });
    for (const event of await ev.getEvents({ limit: 10 })) {
      console.log(event.sandboxId, event.kind, event.occurredAtMs);
    }
    ```

=== "CLI"

    ```bash
    rayito events deploy --artifact-bucket my-bucket --log-group-name /rayito/rayito-base
    # if your organization requires tags when creating resources: --tag Owner=my-team --tag Environment=dev
    rayito events webhook add https://hooks.example.com/rayito --secret-name my-webhook --type sandbox.lifecycle.killed
    rayito events list --limit 10
    rayito events webhook list
    # rayito events webhook remove <webhook_id>; rayito events status; rayito events destroy
    ```

Then, create sandboxes that emit events. `events=` needs a `logging`
that reaches CloudWatch (the forwarder reads from there, so also an
`execution_role_arn` with write permission on the image's log group)
and an image with `rayd` 0.6.0 or later:

=== "Python"

    ```python
    from rayito import LifecycleEvents, Sandbox

    role_arn = "arn:aws:iam::123456789012:role/rayito-sandbox"  # writes to the log group
    events = LifecycleEvents()  # the same stack as before
    sbx = Sandbox.create(
        execution_role_arn=role_arn,
        logging="cloudwatch",
        events=events,
    )
    sbx.pause()
    sbx.resume()
    sbx.kill()
    # created, paused, resumed, killed (a few seconds later)
    for event in events.get_events(sandbox_id=sbx.sandbox_id, order="asc"):
        print(event.type)
    ```

    With `AsyncSandbox.create(events=AsyncLifecycleEvents(), ...)` it is the same.

=== "TypeScript"

    ```ts
    import { LifecycleEvents, Sandbox } from "rayito";

    const roleArn = "arn:aws:iam::123456789012:role/rayito-sandbox"; // writes to the log group
    const events = new LifecycleEvents(); // the same stack as before
    const sbx = await Sandbox.create({ executionRoleArn: roleArn, logging: "cloudwatch", events });
    await sbx.pause();
    await sbx.resume();
    await sbx.kill();
    // created, paused, resumed, killed (a few seconds later)
    for (const event of await events.getEvents({ sandboxId: sbx.sandboxId, order: "asc" })) {
      console.log(event.kind);
    }
    ```

`create()` reads the stack key, derives this sandbox's (`k_sbx`) and
sends it to `rayd` right after the agent is ready, in the same
`ConfigureSandbox` as `mounts=`/`gateways=`/`telemetry=`. If the stack is not
deployed, the key cannot be read, the image is older than 0.6.0 or `rayd`
rejects the section, `create()` terminates the sandbox (unless `keep_on_failure`)
and re-raises the error: it never returns a sandbox that will not emit events.

## How it works { #como-funciona }

1. `rayd` emits `rayito.event.v1 <b64url(json)> <b64url(hmac)>` on stdout,
   only after a `ConfigureSandbox` with the per-sandbox key (`k_sbx`), which
   the SDK derives: `HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)`.
   `rayd` never sees `stack_key`.
2. A CloudWatch Logs subscription forwards each line to a forwarder
   Lambda, which takes the `sandbox_id` from the source log stream
   (`YYYY/MM/DD[<version>]<microvmId>`), re-derives `k_sbx` with it and verifies
   the MAC (constant time) **before** reading the content. Then it requires
   that the event be from that same sandbox, that it have the exact shape
   `rayd` emits and that it be no more than 24 h old (nor more than 5 min in the future), and
   admits it only if it advances that sandbox's lifecycle: `created`
   first, `paused` after `created`/`resumed` of the same generation,
   `resumed` with a higher generation and nothing after `killed`. `paused` and
   `resumed` also spend from a per-sandbox bucket (20 in a row, then one
   every 30 s). What is admitted is written idempotently to DynamoDB (TTL
   7 days). A rejected line never fails the batch: it is counted and
   discarded. Each invocation leaves a JSON line in its log with how many
   lines it accepted and rejected and why (`mac_invalid`, `sandbox_mismatch`,
   `malformed_line`, `stale_event`, `invalid_transition`, `rate_limited`,
   `internal_error`) and how many writes failed
   (`failed_writes`); if any fails, the invocation fails after writing the
   rest and, once Lambda's retries are exhausted, the batch ends up in the stack's
   `ForwarderFailuresQueueUrl` queue. Measured: the `paused`
   line reaches CloudWatch before the VM freezes, and an event
   takes 0.3 to 4 s to reach the table (10–14 s the first, with the
   forwarder cold).
3. A deliverer (triggered by the DynamoDB stream only when a new
   event appears: an event source mapping filter discards the other
   rows before invoking it) delivers the event to
   each webhook subscribed to that type, signed in E2B style, with an SSRF
   guard and up to 3 attempts (only on 5xx or a network error; a 4xx is the
   receiver's answer). Each delivery is marked `attempting` before
   attempting it and `delivered`/`failed` when done: only `delivered` is
   skipped if the stream redelivers it, so a failure does not lose the delivery. Each
   webhook is delivered in isolation: one with a broken URL (`invalid_url`) or that
   fails unexpectedly (`internal_error`) does not keep the
   others from being delivered, and each attempt's deadline (10 s) covers DNS resolution and the
   full response, so a receiver that answers byte by byte does not
   stretch it. If the receiver answers `401` or `403`, the deliverer rereads
   the webhook's secret bypassing its cache and, if it changed, signs again
   with the new one. What exhausts the stream's retries ends up in the stack's
   `DelivererFailuresQueueUrl` queue.
4. A reconciler (every `reconciler_interval_minutes`) compares
   `ListMicrovms` with the sandboxes the table still considers open
   (a sparse index that holds only those, never a scan of the whole
   table) and
   synthesizes `killed{reason: "unknown"}`, with the generation and image of
   their last event, for those that no longer appear. It is the only `killed` of
   a sandbox killed while **paused**: the platform does not call
   `/terminate` on a suspended MicroVM, so that event arrives up to
   one interval late. A sandbox that exhausts its `timeout` while running does
   go through `/terminate`, and its event arrives with `kill_reason: "request"`
   (the hook does not say why the VM dies). When updating a stack deployed
   with an SDK 0.6.x, an already open sandbox enters that index with its next
   event: if it dies without emitting any, it does not receive `killed{unknown}`.

!!! warning "`paused` and `resumed` are indicative"
    `rayd` emits `paused` and `resumed` when its `/suspend` and
    `/resume` hooks run, and the sandbox's own code (uid 1000) can call those
    hooks over loopback (`SECURITY.md`, T2). A validly signed `paused`
    proves `rayd` ran the hook, not that the platform suspended the
    VM. The forwarder limits the damage (strict ordering and a per-sandbox bucket),
    but does not prevent it: do not bill or clean up resources just because of a `paused`.
    `created` and `killed` do not have that problem. For the real state, use
    `get_info()`/`list()` or `killed`.

!!! warning "Right after the first deployment"
    The deliverer reads the table's stream from the most recent
    (`StartingPosition: LATEST`) and its *event source mapping* takes a few
    minutes to start reading after the stack is created. Events from that period are
    stored (`get_events` returns them) but may not reach your webhooks.
    If you are going to test delivery right after deploying, wait for the mapping
    to be `Enabled` (`aws lambda list-event-source-mappings --function-name
    <deliverer>`) and a minute more (`AWS_API_NOTES.md` Q157).

## Verify the signature in your receiver { #verificar-la-firma-en-tu-receptor }

Each delivery is a `POST` with a JSON body and five headers: E2B's four
and `rayito-signature`. A receiver that already verifies E2B webhooks
accepts Rayito's unchanged; a new one should verify
`rayito-signature` (below).

| Header | Value |
|---|---|
| `e2b-webhook-id` | the `webhook_id` that `register_webhook` returned |
| `e2b-delivery-id` | a random id **per attempt**: it changes on each retry |
| `e2b-signature-version` | `v1` |
| `e2b-signature` | base64 without trailing `=` of `sha256(secret + body)` |
| `rayito-signature` | `t=<Unix seconds>,v1=<hex of HMAC-SHA256(secret, "<t>.<webhook_id>." + body)>` |

The secret is the value of the Secrets Manager secret you named in
`secret_name` (`rayito/webhooks/<name>`), as is, in UTF-8. The signature is
E2B's, **not an HMAC**: a SHA-256 of the secret concatenated with the exact
bytes of the body. The body:

```json
{
  "event_id": "0123456789abcdef0123456789abcdef",
  "sandbox_id": "microvm-00000000-0000-0000-0000-000000000001",
  "type": "sandbox.lifecycle.killed",
  "kill_reason": "request",
  "generation": 0,
  "occurred_at_ms": 1790000000000,
  "sandbox_template_id": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
  "sandbox_execution_id": "microvm-00000000-0000-0000-0000-000000000001#0"
}
```

`kill_reason` is `null` except in `killed` (`request`, `timeout` or
`unknown`, the latter only when the reconciler synthesizes it).

=== "Python"

    ```python
    import base64
    import hashlib
    import hmac
    import json
    import os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    SECRET = os.environ["WEBHOOK_SECRET"].encode()  # the value of rayito/webhooks/my-webhook


    def signature_is_valid(secret: bytes, body: bytes, signature: str) -> bool:
        expected = base64.b64encode(hashlib.sha256(secret + body).digest()).decode().rstrip("=")
        return hmac.compare_digest(expected, signature)  # (1)!


    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))  # (2)!
            if not signature_is_valid(SECRET, body, self.headers.get("e2b-signature", "")):
                self.send_response(401)
                self.end_headers()
                return
            event = json.loads(body)
            print(event["type"], event["sandbox_id"], event["event_id"])  # (3)!
            self.send_response(204)
            self.end_headers()


    HTTPServer(("127.0.0.1", 8000), Receiver).serve_forever()
    ```

    1. Constant-time comparison: never `==`.
    2. Verify the bytes as they arrive, before parsing the JSON:
       re-serializing it changes the signature.
    3. Deduplicate by `event_id`, not by `e2b-delivery-id`: each retry
       brings a new `e2b-delivery-id`.

=== "TypeScript"

    With Node.js, no dependencies:

    ```ts
    import { createHash, timingSafeEqual } from "node:crypto";
    import { createServer } from "node:http";

    const secret = process.env.WEBHOOK_SECRET ?? ""; // the value of rayito/webhooks/my-webhook

    function signatureIsValid(secret: string, body: Buffer, signature: string): boolean {
      const expected = createHash("sha256").update(secret).update(body).digest("base64").replace(/=+$/, "");
      const a = Buffer.from(expected);
      const b = Buffer.from(signature);
      return a.length === b.length && timingSafeEqual(a, b);
    }

    createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => {
        const body = Buffer.concat(chunks); // the exact bytes, before JSON.parse
        const signature = req.headers["e2b-signature"];
        if (typeof signature !== "string" || !signatureIsValid(secret, body, signature)) {
          res.writeHead(401).end();
          return;
        }
        const event = JSON.parse(body.toString("utf8")) as { type: string; event_id: string };
        console.log(event.type, event.event_id); // deduplicate by event_id
        res.writeHead(204).end();
      });
    }).listen(8000, "127.0.0.1");
    ```

### Verify `rayito-signature` (recommended) { #verificar-rayito-signature-recomendado }

`e2b-signature` carries no timestamp and does not cover the `webhook_id`: a
captured delivery (in a log, a proxy or a webhook inspector) stays
valid forever and against any receiver with the same secret.
`rayito-signature` is an HMAC-SHA256 over `"<t>.<webhook_id>."` followed by
the body's bytes, with the attempt's time: reject whatever is more than
5 minutes old and whatever does not come addressed to your `webhook_id`.

=== "Python"

    ```python
    import hashlib
    import hmac
    import time

    TOLERANCE_SECONDS = 300


    def rayito_signature_is_valid(
        secret: bytes, webhook_id: str, body: bytes, header: str, now: float | None = None
    ) -> bool:
        fields = {k: v for k, _, v in (part.partition("=") for part in header.split(","))}
        if not fields.get("t", "").isdigit() or "v1" not in fields:
            return False
        t = int(fields["t"])
        if abs((now or time.time()) - t) > TOLERANCE_SECONDS:
            return False  # (1)!
        signed = f"{t}.{webhook_id}.".encode() + body
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, fields["v1"])
    ```

    1. An old delivery, even if the signature is good. Still deduplicate
       by `(sandbox_id, event_id)`: within the window it can also
       arrive twice.

=== "TypeScript"

    ```ts
    import { createHmac, timingSafeEqual } from "node:crypto";

    const TOLERANCE_SECONDS = 300;

    function rayitoSignatureIsValid(secret: string, webhookId: string, body: Buffer, header: string): boolean {
      const fields = new Map(
        header.split(",").map((part) => {
          const i = part.indexOf("=");
          return [part.slice(0, i), part.slice(i + 1)] as [string, string];
        }),
      );
      const t = Number(fields.get("t"));
      const v1 = fields.get("v1");
      if (!Number.isInteger(t) || v1 === undefined) return false;
      if (Math.abs(Date.now() / 1000 - t) > TOLERANCE_SECONDS) return false; // old delivery
      const expected = createHmac("sha256", secret)
        .update(`${t}.${webhookId}.`)
        .update(body)
        .digest("hex");
      const a = Buffer.from(expected);
      const b = Buffer.from(v1);
      return a.length === b.length && timingSafeEqual(a, b);
    }
    ```

Pass as `webhook_id` the one `register_webhook` returned to you (not the one in the
`e2b-webhook-id` header: that one is set by the sender).

### Responses and retries { #respuestas-y-reintentos }

The deliverer only delivers to `https://` URLs and never follows redirects:
put the receiver behind your TLS termination (a load balancer, API Gateway or
a Lambda function URL). It also does not deliver to a non-public
address: its SSRF guard resolves the host and discards loopback, private
networks, link-local (including the metadata one, `169.254.169.254`),
multicast, reserved and CGNAT (`100.64.0.0/10`), also written as IPv6
(`::ffff:127.0.0.1`). `register_webhook`/`registerWebhook` applies the same
rule before storing anything when the URL carries a literal IP or the host is
`localhost` (or ends in `.localhost`), with `InvalidArgumentException`/
`InvalidArgumentError`. A DNS name is not resolved when registering it: if it
points (or starts to point, DNS rebinding) to one of those addresses, the
deliverer still stops it on delivery. How it treats your response:

| Response | What the deliverer does |
|---|---|
| `2xx` | delivered; it does not try again |
| `5xx`, network error or more than 10 s without a response | retries, up to 3 attempts with waits of 0.5 s and 1 s |
| `401` or `403` | rereads the secret (without cache); if it changed, retries immediately with the new one; if not, final |
| any other `3xx` or `4xx` | final: it does not retry |

Respond quickly and process the event afterwards: the retry only protects against
your transient failures. A delivery that exhausts its three attempts ends up
marked `failed` and is not resent on its own, but the event stays in
`get_events()` for 7 days: a receiver that was down can catch
up by reading them.

### Replays, secrets and URLs { #repeticiones-secretos-y-urls }

- **`e2b-signature` carries no timestamp**: a captured delivery can
  be resent later with the same valid signature. Verify
  `rayito-signature` (above) and deduplicate by the pair
  `(sandbox_id, event_id)`, not just by `event_id`: an `event_id` is only
  unique within its sandbox. Keep the processed ones for at least a week.
- **Webhooks are stack-wide**: each webhook receives the events of
  all the stack's sandboxes of the types it subscribes to, with their
  ids, times and image ARN (which includes the account id). There are no
  per-team or per-customer webhooks as in E2B: if you give third parties access to `register_webhook`,
  each will see the others' sandboxes. Treat
  `EventsWebhookAdminPolicy` as an operator policy.
- **Rotating a webhook's secret takes up to 5 minutes** to reach the
  deliverer, which caches each secret for that time. A `401`/`403`
  from your receiver forces an immediate reread, so you can change the
  secret in Secrets Manager and in the receiver at the same time: at most one
  delivery is retried with the new one.
- **Use a long, random secret** (32 bytes or more, for example
  `secrets.token_urlsafe(32)` or `randomBytes(32).toString("base64url")`):
  a single observed delivery allows testing weak secrets offline.
- **Signing secrets live under `rayito/webhooks/`** and never reach
  a sandbox: `RayitoSecretsReader` denies them and the SDK refuses to pass them
  through `secrets=`. A sandbox with that secret could forge deliveries.
- **The webhook URL is stored in the clear** in the table (with DynamoDB's default
  encryption) and `list_webhooks()` returns it: if your receiver
  carries a credential in the path (Slack, Discord), treat it as data
  that anyone with `EventsWebhookAdminPolicy` (or the deprecated
  `EventsOperatorPolicy`) can read. `EventsReaderPolicy` cannot read it.

## Which policy each call needs { #que-politica-necesita-cada-llamada }

The stack emits one managed IAM policy per task. Attach to each
identity only its own: a dashboard that only reads events must not be able to
register a webhook that exfiltrates all sandboxes' events, nor
read the stack key (with it, an event of any sandbox can be signed).

| Python | TypeScript | Policy (stack output) | What it gives |
|---|---|---|---|
| `Sandbox.create(events=...)` | `Sandbox.create({ events })` | `EventsLauncherPolicy` (`LauncherPolicyArn`) | read the stack key and `DescribeStacks` |
| `get_events(...)` | `getEvents(...)` | `EventsReaderPolicy` (`ReaderPolicyArn`) | `Query` on the event rows and the `gsi1` index, and `DescribeStacks` |
| `register_webhook`, `list_webhooks`, `delete_webhook` | `registerWebhook`, `listWebhooks`, `deleteWebhook` | `EventsWebhookAdminPolicy` (`WebhookAdminPolicyArn`) | `PutItem`/`DeleteItem`/`Query` only on `WEBHOOK` rows, and `DescribeStacks` |
| `deploy`, `destroy` | `deploy`, `destroy` | those of [Optional stacks](pilas-opcionales.md) | create and delete the stack |

`EventsOperatorPolicy` (`OperatorPolicyArn`) is the union of the three and is
kept for one more version so as not to break anyone who already has it attached.

## `LifecycleEvents` options { #opciones-de-lifecycleevents }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `stack_name` | `stackName` | `"rayito-events-webhooks"` | stack name |
| `region` | `region` | the session's | stack region |
| `session` | `credentials` | the default session | AWS credentials |
| `deploy(artifact_bucket=, log_group_name=, reconciler_interval_minutes=, tags=)` | `deploy({ artifactBucket, logGroupName, reconcilerIntervalMinutes, tags })` | 5 minutes (minimum 2); no tags | deploys the stack; `tags` propagate to its resources. The Lambdas' code is uploaded to the bucket's `rayito/stacks/events-webhooks/<sha256>.zip`, which must be in your account (`ExpectedBucketOwner`); if an object already exists at that key, the SDK compares its content and overwrites it if it is not its own |
| `status()` | `status()` | — | the stack's state and outputs (`None`/`undefined` if not deployed); only `DescribeStacks` |
| `destroy(wait=)` | `destroy({ wait })` | waits for deletion | deletes the stack (what is deleted and what is kept: "How to turn it off" in the box above) |
| `register_webhook(url, secret_name=, types=)` | `registerWebhook(url, { secretName, types })` | — | `url` is an `https://` URL the deliverer can reach (a valid DNS host that is not `localhost`, or a public IP; port 1–65535); `types` are `sandbox.lifecycle.{created,paused,resumed,killed}` |
| `list_webhooks()` | `listWebhooks()` | — | the registered webhooks (`WebhookInfo`, with their `webhook_id`) |
| `delete_webhook(webhook_id)` | `deleteWebhook(webhookId)` | — | unregisters a webhook; does not delete its secret |
| `get_events(sandbox_id=, types=, limit=, order=)` | `getEvents({ sandboxId, types, limit, order })` | `limit=100` (1–100), `order="desc"` | reads directly from your DynamoDB table; filters `types` in DynamoDB and paginates until it gathers `limit` |

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `WebhookException` | `WebhookError` | a DynamoDB or Secrets Manager call failed (`aws_code`/`awsCode` carries only AWS's code, never ARNs or the account), or the stack is not deployed | check that the caller has that call's policy ([table](#que-politica-necesita-cada-llamada)); call `deploy()` first |
| `InvalidArgumentException` | `InvalidArgumentError` | `events=` that is not a `LifecycleEvents`, or without `logging` with CloudWatch; an unknown `type`; `limit` outside 1–100; a URL that is not `https://` or that the deliverer could not reach (port out of range, invalid host, `localhost` or a loopback, private, link-local or metadata IP) | fix the argument before retrying |
| `UnimplementedError` | `UnimplementedError` | `Sandbox.create(events=...)` on an image older than 0.6.0 (the sandbox is terminated) | use an image published with `rayd` 0.6.0 or later |
| `WebhookException` (from `create()`) | `WebhookError` | `Sandbox.create(events=...)` without the stack deployed, or without permission to read its key (the sandbox is terminated) | deploy the stack or attach `EventsLauncherPolicy` to the caller |
| `StackException` | `StackError` | the stack's `deploy`/`destroy` failed (code `blocked`/`not_found`/`failed`) | see [Optional stacks](pilas-opcionales.md) |
| `StackException` (`failed`, the stack in `ROLLBACK_COMPLETE`) | `StackError` | an SCP or tagging policy of your organization denied `sqs:CreateQueue`/`lambda:CreateFunction` without certain tags | delete the failed stack (`destroy()`) and repeat `deploy(tags={...})` (CLI: `--tag K=V`) with the keys and values your organization requires |
| `StackException` (`failed`, the stack in `DELETE_FAILED`) | `StackError` | `destroy()` with some stack policy still attached to a user or role | detach the policy and repeat `destroy()` |
| `InvalidArgumentException` from `run-microvm` ("Logging cannot be enabled without providing executionRoleArn") | `InvalidArgumentError` | `logging="cloudwatch"` without `execution_role_arn` | pass an execution role with write permission on the image's log group |
| no event in the table | — | the `deploy(log_group_name=)` log group is not the image's, or the forwarder rejects the lines | look at the forwarder's JSON line (`rejected_by_reason`) in its log |
| `paused`/`resumed` are missing | — | the sandbox paused and resumed more than 20 times in a row or more than once every 30 s (`rate_limited`), or a line older than the last admitted one arrived (`invalid_transition`) | that is expected: use `get_info()` for the real state |

## Differences from E2B { #diferencias-con-e2b }

E2B exposes lifecycle events and webhooks through its own REST API, with
a service hosted by E2B. Rayito has no server: the stack (secret,
table, Lambdas) lives in your account, and the shim adds no method (E2B
only exposes it through REST, never through the native SDK). The delivery signature is
compatible: a webhook already written for E2B can verify Rayito's
deliveries unchanged (same `e2b-*` headers and the same signature scheme).

## See also { #ver-tambien }

- [Optional features](../optional-features.md)
- [Optional stacks](pilas-opcionales.md)
- [IAM](../operacion/iam.md)
- Template: [`infra/events-webhooks.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/events-webhooks.yaml)

??? info "Sources and measurements"
    - CloudWatch Logs/DynamoDB/Scheduler contract:
      `AWS_API_NOTES.md` §25, on
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Deployment and deletion: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Design decision (ConfigureSandbox, the derivation of `k_sbx`, the
      SSRF guard): ADR-020 in
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
